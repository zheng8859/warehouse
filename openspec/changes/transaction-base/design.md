# transaction-base 设计

## Context

阶段三已交付评分引擎（`app/engine/`），产出 `PLANNED` 软推荐方案；阶段七已交付前端二次确认卡与菜单过滤。但交易侧（`app/services/` 六模块）与 `app/cap/increment.py` 仍是桩，`POST /api/job/*` 未注册端点，`JobStatus` 仍为 7 值、状态机仍为 7 态。本设计补上「确认 → 写台账 → cap 增量 → 同步后验 → 冲正」的交易闭环，动机见 `proposal.md`。

**硬约束**（`19` / `17` / CLAUDE.md 红线，不重新论证）：
- 单进程 SQLite + WAL，写并发由 `JobOrder.lock_version` 乐观锁保证；cap 与台账必须真实同事务。
- 台账只有一套 `Ledger`，追加式（无 `updated_at`）、不删不改；台账是 cap 与库存分布增量的唯一来源。
- 决策权在人：写操作二次确认，未确认不产生台账。
- 核心链路确定性、不出域：后验 / cap / 台账全部本地计算，无外部 LLM / API。

## Goals / Non-Goals

**Goals:**
- 用最小 schema 变更落地 10 态状态机 + 冲正：`JobStatus` 枚举 +3 值、`Ledger` +1 列 +1 约束调整，`JobOrder` 不加冗余列。
- 后验与确认同请求同步完成（D3），不引入异步调度。
- cap 增量与台账严格同事务，复用引擎既有板-格换算（D5），避免两套口径漂移。

**Non-Goals（设计级边界，非 proposal 复述）:**
- 不做 cap 基线全量重算 / 漂移对账 / 告警（`app/cap/baseline.py` / `reconcile.py` / `alerts.py` 留待 4b）。
- 不引入任务队列 / 异步 worker（保持单进程无中间件）。
- 不做端点级 RBAC 落地（M5 路线图，见 D6）。

## Decisions

### D1：状态机 7→10 扩展落在纯函数表，枚举 CHECK 需迁移重建

`app/core/state_machine.py` 的 `LEGAL_TRANSITIONS` 是唯一判定基准，扩展如下：

| 状态 | 现出边 | 新出边 |
|---|---|---|
| `EXECUTED` | `{VERIFIED}` | `{VERIFYING, VOID}` |
| `VERIFIED` | `{}`（终态） | `{VOID}` |
| `VERIFYING` | —（新增） | `{VERIFIED, VERIFY_FAILED}` |
| `VERIFY_FAILED` | —（新增） | `{VERIFYING}` |
| `VOID` | —（新增） | `{}`（终态） |

`TERMINAL_STATUSES` 是「无出边」的推导值，随之从 `{CANCELLED, VERIFIED}` 变为 `{CANCELLED, VOID}` —— **`VERIFIED` 不再是终态**（可被冲正拉回 `VOID`），这是 10 态图的直接推论，需在 `tests/logic/test_job_state.py` 同步更新断言。

`JobStatus` 枚举（`app/core/enums.py`）增 `VERIFYING` / `VERIFY_FAILED` / `VOID`。**注意**：`enum_column` 渲染为 `VARCHAR + CHECK`（`create_constraint=True`），故 `job_orders.job_status` 的 CHECK 需 `batch_alter_table` 重建为 10 值，见「Migration Plan」。

**备选**：把状态机散落进各服务层的 if-else。否决 —— 模块 docstring 已论证（多处各抄一份正是「`EXECUTED` 后重复写台账」红线的失效方式），继续复用单表。

### D2：后验同步（D3），VERIFYING 为内部中间态

确认请求内一次完成 `CONFIRMED → EXECUTED（写台账）→ VERIFYING → VERIFIED / VERIFY_FAILED`：后验在台账写入成功后同请求同步计算，请求结束时单子必为 `VERIFIED` 或 `VERIFY_FAILED`，`VERIFYING` 不暴露为可停留状态。`VERIFY_FAILED`（后验计算失败 / 超时）仅提供重试（→ `VERIFYING`）+ 告警，无「放弃后验」终态。

**备选**：异步后验（`VERIFYING` 停留、后台任务推进）。否决 —— 无 Redis/RabbitMQ、任务队列用本地库状态表，异步需额外调度器与「谁在跑」的恢复逻辑；而 SLA「后验 ≤1s」在同步下可满足（`08` §14.1）。同步也把「后验在确认回执里即可读」落成真。

### D3：冲正 v1 简化 —— VOID + 反向台账行，不建反向作业单（有意偏离 15-01 §6.3）

冲正 = 原单 `VOID` + 写一条 `is_reversal=True` 的反向台账行 + 同事务释放 cap / 减少库存；**不创建反向 `JobOrder`**、不写 `reversal_ref` 指向新单。这是对 `15-01` §6.3「新反向交易（红冲）+ 原单 VOID + reversal_ref」的**有意偏离**（D4 定稿），理由：

1. 反向行仍落 `Ledger` —— 「台账是 cap 与库存分布增量的唯一来源」不变量不被破坏，也不新增平行台账。
2. 反向交易若作为新作业单走完整确认流程，会要求「反向单也要再写台账、再后验」，递归地放大 v1 复杂度，而 v1 场景是「台账写错 / 落位错 → 整单冲掉重来」，不需要保留一次完整反向审计。

**schema 变更**：`Ledger` 增 `is_reversal: bool = False`；唯一约束由 `uq_ledgers_job_order_id(job_order_id)` 放宽为 `uq_ledgers_job_order_id_reversal(job_order_id, is_reversal)`（一单至多一正常行 + 一反向行；`VOID` 为终态，冲正至多一次，由状态机守卫）。`JobOrder` 不加 `void` 布尔列 —— `status = VOID` 已唯一表达冲正态，`updated_at`（终态后不再变）即冲正时间；为审计清晰度不另立 `voided_at`，理由同模型 docstring 第 8 条「不为一时刻立两列」。重新入库走标准流程（导入新订单 → 推荐 → 确认），不提供「原单复活」。

**待办**：`tasks.md` 登记一条「回写 `15-01` §6.3 / `15-00` F13 为 v1 简化口径」，按设计文档改动惯例先 `.<时间戳>.bak`。

### D4：cap 增量复用 `to_occupied_cells()` 恒等占位（D5）

`app/cap/increment.py` 的板-格换算复用 `app/engine/allocator.py::to_occupied_cells(qty, cartons_per_pallet=None)`，与分配侧同一口径：入库 ↑已占、出库 ↓已占、移库源 ↓ / 目标 ↑，与台账同事务写入。**D14 板-格换算规则仍未确认**，恒等占位是明确选择 —— 引擎「占几格」与 cap「扣几格」必须一致，否则「台账是 cap 增量唯一来源」会被换算口径差异偷偷打破。

全量重算（快照导入 `IMPORTED → BASELINE`）与漂移对账 / 告警归 4b，本 change **不实现**；此处显式登记「不适用」而非静默删除（跨阶段 rules 冲突，见记忆条目）。

### D5：服务层编排 —— 六模块 + 单事务

`app/services/` 六模块职责划分（三类作业共用一条编排链，按 `job_type` 分派）：
- `inbound` / `outbound` / `relocate`：各作业类型的确认→执行编排，产出实际库位 / 数量。
- `ledger`：台账**唯一写入入口**（含 `is_reversal` 反向行），不向角色开放手动入口。
- `verify`：三口径后验纯函数 + `verify_result = PASS / DEVIATION` 判定 + 达标/偏离分派。
- `kpi`：本 change 只提供偏离清单的聚合（移库任务来源），看板归阶段六。

编排在一个 DB 事务内：`status 推进 → 写台账 → cap 增量 → 后验记录（→ 偏离记录）`，任何一步失败整体回滚（`CONFIRMED` 回 `PLANNED`，确认作废）。后验计算本身是纯函数（同样输入必得同样输出），无随机、无 LLM。

### D6：API 端点与二次确认、RBAC 现状

写路径：`POST /api/job/batch/confirm`、`POST /api/job/{id}/reject`、`POST /api/job/{id}/retry`（后验重试）、`POST /api/job/{id}/void`（冲正）。验证读：`GET /api/ledger`、`GET /api/verification/{job_id}`。路径按本仓 `/api/*` 约定（无 `/api/v1`），与 `15-01` §5.1 的 `/api/v1/jobs/*` 不一致处以 config.yaml 为准。

二次确认（G3）是前端守卫（阶段七已建）：后端写端点**即**操作员显式确认入口，无「自动落位 / 静默执行」路径，故「未确认不产生台账」由「不存在自动写台账的路径」成立。resource.action 映射登记为 `inbound.operate` / `outbound.operate` / `relocate.operate`（13 号矩阵目标模型）；**v1 事实**：端点级 RBAC 仅 `/api/allocate/batch` 已落地，本 change 不新增端点鉴权，仅保留映射供 M5 落地。

### D7：事务、幂等与恢复路径

- **事务**：`CONFIRMED → EXECUTED` 的迁移必须与台账、cap 增量在同一真实 DB 事务（`app/core/db.py`），用事务回滚而非应用补偿。
- **幂等**：`EXECUTED` 后重复确认 → 状态机守卫拦截，返回既有台账（15 §11.7），不写第二条。
- **批量独立事务**：`batch/confirm` 逐单独立提交，个别单失败回 `PLANNED` 不影响同批其余单（15-01-F11）。
- **功能开关回滚**：Go/No-Go 失败时一键关闭推荐、恢复均分，**不删历史台账**（`Ledger` / `Verification` / `Deviation` 永久，归档不删除）。

## Risks / Trade-offs

- **[冲正偏离 15-01 §6.3 的审计缺口]** 反向行 `is_reversal` 只落一条合计反向台账，不保留「反向交易作为独立作业单」的完整确认轨迹 → 缓解：`VOID` 态 + 反向行 + 原正常行三者可重建「冲正前 / 后」全貌；登记回写文档任务，未来若需完整反向审计再单列。
- **[VERIFIED 退出终态集]** `TERMINAL_STATUSES` 语义变化，任何依赖「VERIFIED 是终态」的既有查询（看板 / 队列过滤）需重新核对 → 缓解：`tests/logic/test_job_state.py` + 既有 API 测试覆盖，`TERMINAL_STATUSES` 是推导常量、非手写，改一处全仓生效。
- **[板-格恒等占位的口径风险]** D14 未确认，若将来确认的换算规则非恒等，cap 增量与引擎占格会不一致 → 缓解：换算收敛在 `to_occupied_cells()` 单一函数，规则确认后只改一处；`cap/increment.py` 不另写换算。
- **[同步后验阻塞确认请求]** 后验 ≤1s（SLA）但若数据量超预期会拖慢确认 → 缓解：三口径均为本地聚合（对库位号 `[:2]` 切片），单厂试点数据量下远低于 1s；`VERIFY_FAILED` 兜底超时，不拖死请求。

## Migration Plan

一个 Alembic 迁移（沿用中文描述命名），`batch_alter_table` 三处：

1. `job_orders.job_status` 的 CHECK 由 7 值重建为 10 值（`enum_column` 的 `create_constraint=True` 所致）。
2. `ledgers` 增 `is_reversal`（`Boolean`，`default=False`，`nullable=False`，服务端默认）。
3. `ledgers` 删 `uq_ledgers_job_order_id`，建 `uq_ledgers_job_order_id_reversal(job_order_id, is_reversal)`。

均为**超集 / 加列**变更：既有 7 态数据仍满足 10 值 CHECK，`is_reversal` 默认 False 不改变既有行语义，无数据回填。回滚 = 反向迁移（重建 7 值 CHECK、删列、复原唯一约束）；因「不删历史台账」红线，冲正产生的反向行在回滚时**保留**（功能开关式回滚不删数据）。

## Open Questions

- **D14 板-格换算规则**（优先级权重系数、板-格换算、分档阈值与溢出区形态三项仍未确认）—— 不影响本 change：恒等占位收敛在 `to_occupied_cells()`，确认后单点替换，不改规格、不改任务分解。
- **`void_reason` 是否要留**（冲正原因审计列）—— v1 未要求，`status=VOID` + 反向行已够审计；若评审要求可在 `apply` 阶段作为可选列补上，不改规格。
