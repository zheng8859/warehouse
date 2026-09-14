## Context

出库确认链已落地：`confirm_outbound`（`app/services/outbound.py`）走 `_confirm_and_execute`（`app/services/confirm.py`）完成 `PLANNED → CONFIRMED → EXECUTED`，台账/后验底座复用 `transaction-base`。缺的是顺路取派生、批量派生接口、以及「出库确认记录最终拣货路径」的入账口径。本变更补这三样，动机见 proposal.md。

可复用的既有件：

- `SnapshotIndex.profile(material_code)` → `InventoryProfile`（`plates_by_aisle` = 每巷库存量、`batches_by_aisle` = 每巷批号集），`aisle_of(loc) = loc[:2]`、`load_snapshot_index(session, snapshot_id)`（`app/engine/factors.py`）。顺路取派生**就是**对这一 profile 的按巷道聚合，无需新取数。
- `concentration_aisle_count(*, pick_qty_by_aisle) -> int`、`verify_outbound(*, pick_qty_by_aisle, n)`、`DEFAULT_CONCENTRATION_N = 5`（`app/services/verify.py`）。
- `next_bulk_batch_no` / `load_bulk_batch_nos`（`app/engine/reasons.py`）、`_current_snapshot` / `_render_snapshot_version`（`app/api/routes/allocate.py`）。
- `RecommendationPlan`：`job_order_id`（**不唯一**，当前方案 = 该单 `id` 最大）、`plan_kind`、`payload_json`、`degraded`/`degrade_reason`（`app/models/job.py`）。无 `snapshot_id` 列。
- `_require_locations`（`app/api/routes/job.py:182`）：出库当前要求「源必填、目标空」——本变更要放宽。
- 状态机 `assert_transition`（`app/core/state_machine.py`）：`PENDING → PLANNED` 已合法。
- `GET /api/jobs?type=outbound` 由兄弟变更 `inbound-domain` 以类型无关端点提供，本变更只消费。

## Goals / Non-Goals

**Goals:**
- 顺路取派生为**纯函数**（确定性、无会话、无写），落在出库服务域。
- `POST /api/job/batch/pick-sequence` 批量派生 + 幂等 + 货未入库分列。
- 出库确认把操作员确认/微调后的最终拣货路径写进台账 `pick_path_json`；后验改读它算加权集中度。

**Non-Goals:**
- 不新增实体字段、不做迁移（幂等所需版本号塞进 `payload_json`，见 D3）。
- 不重做出库确认链/台账/后验底座，只改出库侧口径。
- 不碰入库/移库确认语义。

## Decisions

### D1 — 顺路取派生 = 纯函数 `derive_pick_sequence`

新增 `derive_pick_sequence(*, material_code, profile: InventoryProfile, n: int = DEFAULT_CONCENTRATION_N) -> dict`（`app/services/outbound.py`），输入既有的 `SnapshotIndex.profile(material_code)`，输出 `17` §10.2 形状：

```json
{
  "do_no": "…",
  "pick_sequence": [ {"aisle": "01", "qty": 3, "batches": ["GJP…"]}, … ],
  "weighted_concentration": 3,
  "threshold_n": 5,
  "exceeded": false
}
```

- `pick_sequence` 取自 `profile.plates_by_aisle`（每巷库存量 = 拣货量，**现状分布**，不跨巷分配）+ `profile.batches_by_aisle`（每巷批号），按 `aisle` **升序**（顺路 = 库位号序走仓，`17` §10.2 示例即升序）。
- `weighted_concentration = concentration_aisle_count(pick_qty_by_aisle=…)`（复用，不重写 80% 降序累加）。
- `exceeded = weighted_concentration > n`；**仅高亮不阻断**（`15-03` §6.2）。
- 纯函数：不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`。**确定性**：同样 profile 必得同样输出（无随机、无大模型）。

备选：把聚合逻辑内联进端点——否决，纯函数才可进逻辑层单测（三层 TDD），且「同样输入必得同样输出」在函数边界最可断言。

### D2 — 端点 `POST /api/job/batch/pick-sequence`

落在 `app/api/routes/job.py`（与 `/batch/confirm` 同文件、同前缀 `/api`）。请求 `{warehouse_id, job_order_ids: [str], snapshot_version?: str | None}`；守卫 `require_permission(Permission.OUTBOUND_OPERATE)`（对齐 `allocate` 的 `inbound.operate` 端点级鉴权，`13` §2.2 仓管员/管理员可 operate）。

流程（复用 `allocate` 的批量编排骨架）：

1. 报文级校验：`job_order_ids` 非空、全为 `^[1-9]\d*$`、去重；加载后全为 `OUTBOUND`，否则整批 422。
2. `_current_snapshot`（无快照 → `BlockedMissingPrerequisite` 409，提示重新导入，不猜测）。
3. `load_snapshot_index(session, snapshot_id=snapshot.id)`。
4. 逐单：`profile = index.profile(material_code)`。`profile` 无该物料（`plates_by_aisle` 空）→ 记入 `not_in_stock`（提示「该品项尚未入库，暂无法生成顺路取」，不阻断，单停留 `PENDING`）；否则 `derive_pick_sequence` → 写 `RecommendationPlan(plan_kind=PICK, payload_json=…)` → `assert_transition(PENDING, PLANNED)`。
5. 响应分列 `plans[]`（含 `do_no` / `pick_sequence` / `weighted_concentration` / `threshold_n` / `exceeded`）与 `not_in_stock[]`。

批量上限复用 `MAX_JOB_ORDERS_PER_BATCH = 50`（`app/schemas/reason.py`）。

### D3 — 幂等键 = `bulk_batch_no` × 库存视图版本

幂等键由 `bulk_batch_no`（`next_bulk_batch_no` 生成并回写每单，与 `allocate` 同源）与**库存视图版本**合成；版本号落进 `RecommendationPlan.payload_json` 的簿记字段 `snapshot_version`（= `_render_snapshot_version(snapshot)`，与 `allocate` 响应契约同口径；这是对 `17` §10.2 的**加字段**，不破坏消费方）。

逐单判定：

- `PENDING` 单 → 派生、写方案（含 `snapshot_version`）、迁 `PLANNED`、回写 `bulk_batch_no`。
- 已是 `PLANNED` 的单 → 读其「当前方案」（该单 `id` 最大的 `plan_kind=PICK` 行）的 `snapshot_version`：
  - 与当前视图版本**相同** → 返回既有方案（幂等命中，不重复写、不重迁状态）。
  - **不同**（视图已推进）→ 重新派生，**追加**一行新方案（`id` 更大，天然成为「当前方案」），不重迁状态（已 `PLANNED`）。

备选：新增 `RecommendationPlan.snapshot_id` 列——否决，违反「无迁移」非目标；`payload_json` 簿记字段零迁移且可随方案版本化。

### D4 — 出库确认记录最终拣货路径

- `ConfirmItem`（`app/schemas/job.py`）增 `pick_path: list[PickPathItem] | None`，`PickPathItem` = `{aisle, qty, batches}`（`17` §10.2 的 `pick_sequence` 元素形）。
- `_require_locations` 出库分支放宽：出库改为「目标必空、**源可空**」（多巷无单一源库位）；`pick_path` 的缺失在编排层处理，不在报文层判。
- `_confirm_one` 出库分支：`source_location_code=None`，`pick_path_json=item.pick_path`。
- `confirm_outbound` 的 `source_location_code` 形参改 `str | None = None`。
- **微调/省略回退**：`pick_path` 为 `None` 时，编排读该单「当前方案」的 `pick_sequence` 落 `pick_path_json`（未微调 = 接受推荐）；仍取不到 → `ValidationBlocked`（不猜测拣货路径）。
- `write_ledger` 把 `pick_path_json`（确认值，非推荐值）写入台账行；出库 `source_location_code` 落 `NULL`（列本可空，6 位校验由 `source_location_code_len6` 兜底）。**注意**：既有 `_LEDGER_LOCATION_CHECK`（约束名 `ck_ledgers_location_columns_by_type`）把出库钉成 `source IS NOT NULL` —— 与「源可空」冲突，本变更加一处迁移放宽出库析取项（见 D7 / Migration Plan）。

### D5 — 出库后验改读台账拣货路径

`verify.py::_pick_qty_from_ledger` 改为：优先解析 `ledger.pick_path_json`（`pick_sequence[]`）按 `aisle` 聚合 `qty` → `{aisle: qty}`；`pick_path_json` 缺失/空时回退到 `source_location_code`（单巷，兼容无拣货路径的历史出库单）。`verify_outbound` 口径不变（`concentration_aisle_count` 80% ≤ N）。台账缺失拣货路径且无源库位 → `ValidationBlocked` → `VERIFY_FAILED`，不静默 PASS。

### D6 — schema 落点

新增 `PickSequence` / `PickPathItem` / `BatchPickSequenceRequest` / `BatchPickSequenceResponse`（`app/schemas/reason.py`，与 `BatchAllocateRequest` 同文件），响应 DTO 形状对齐 `17` §10.2。

### D7 — 出库扣减按 `pick_path_json` 逐巷 + 迁移放宽 CHECK

D4 的「源可空」在**实现时**撞到两处既有硬要求（设计原稿漏记），本变更一并补上：

1. **迁移**：`ck_ledgers_location_columns_by_type` 的出库析取项由 `ledger_type = 'OUTBOUND' AND source_location_code IS NOT NULL AND target_location_code IS NULL` 放宽为 `ledger_type = 'OUTBOUND' AND target_location_code IS NULL`（source 可空；`source_location_code_len6` 仍兜「给了就 6 位」）。
2. **`apply_increment` OUTBOUND 分支**：优先按 `ledger.pick_path_json`（`{aisle, qty, batches}[]`）逐巷扣减 —— 每巷按 `location_code LIKE '<aisle>%'` 找该料号（可选按该巷 `batches` 集过滤）的库存行，按 `location_code` 升序逐行递减至该巷拣货量扣完；`pick_path_json` 缺失/空时回退到 `source_location_code` 单巷扣减（兼容历史单源出库行）；两者皆无 → `ValidationBlocked`（不静默）。

**粒度说明**：`pick_path_json` 是**巷道级**（`17` §10.2 的 `aisle`），`InventoryItem` 是**库位级**（6 位）。所有消费方（`factors` 的 `plates_by_aisle`、顺路取派生、cap 聚合）都按 `[:2]` 聚合，故扣减只要在**巷道总量**上正确即可；巷道内按库位号升序确定性分配，不引入新的库位级业务语义。`to_occupied_cells` 本阶段恒等（板 == 格），扣减量与 `pick_path_json[].qty`（板）直接对应。

## Risks / Trade-offs

- **[R] `payload_json` 加 `snapshot_version` 簿记字段，轻微偏离 `17` §10.2** → 只加字段不删改，消费方忽略未知键；簿记字段与「当前方案版本化」一致（每份方案自带其视图版本）。
- **[R] 出库 `source_location_code` 落 `NULL`，丢失「单一源库位」审计粒度** → 完整多巷路径在 `pick_path_json`（决策权在人、确认值入账），后验读它；`NULL` 仅表示「多巷、无单一源」。
- **[R] 幂等再派生会在已 `PLANNED` 单上追加方案行** → `RecommendationPlan`「当前方案 = `id` 最大」已天然收敛，不产生歧义。
- **[R] 货未入库单若始终不入库则永久停留 `PENDING`** → `15-03` §6.1 已定义：等生产入库后重新生成顺路取；属运维口径，非本变更缺陷。
- **[R] 批量派生失败面** → 纯派生 + 写方案，无台账/库存写；任一单失败只记该单（沿用 `allocate` 整批校验 / 逐单处理的骨架），不写半成品。

## Migration Plan

一处 DB 迁移：放宽 `ck_ledgers_location_columns_by_type` 的出库析取项（`source IS NOT NULL` → 允许 `NULL`，目标仍空），不新增列 / 表 / 约束。变更面 = `services/outbound.py`（加 `derive_pick_sequence` + 放宽 `confirm_outbound` 源库位）、`api/routes/job.py`（加 `/batch/pick-sequence` + 放宽出库确认报文）、`schemas/job.py` + `schemas/reason.py`（DTO）、`services/verify.py`（`_pick_qty_from_ledger`）、`cap/increment.py`（出库按 `pick_path_json` 逐巷扣减）。回滚 = 还原这几个文件 + 迁移 downgrade；功能开关式回滚，不删历史台账（本变更新增的 `pick_path_json` 只是台账行内一列数据，回滚不影响既有台账）。
