# transaction-base 任务清单

> **关键路径落位**：本 change 落位在 `F5（操作员确认 L1 的服务端）→ F7（落位执行与台账）→ F8（落位后验）`，`F9` 的事务增量随 `F7` 同事务交付。
>
> **范围登记（跨阶段 rules 的「不适用」项，显式登记而非静默删）**：
> - `F1/F2` 数据导入与契约配置、**导入校验失败阻断**、**快照过期阻断** → `import-base` change。
> - `F3/F4/F6` 评分引擎 / 软推荐 / 库位族、**cap 不足降级**、基线层**评分正确性（SC-001~006）** → 阶段三已交付。
> - `F10` 集中度 KPI 看板、**KpiSnapshot 聚合** → 阶段六（本 change 只写 `Verification` / `Deviation` 作为其输入）。
>
> 每任务先写测试（RED）再实现（GREEN），三层 TDD 对应 `tests/models` / `tests/logic` / `tests/api`。

## 1. 状态机与枚举（前置）

- [x] 1.1 `app/core/enums.py` 的 `JobStatus` 增 `VERIFYING` / `VERIFY_FAILED` / `VOID`，并新增 Alembic 迁移用 `batch_alter_table` 将 `job_orders.job_status` 的 CHECK 由 7 值重建为 10 值。验证：`pytest tests/models -k enum` 枚举完整性 + `alembic upgrade head` / `downgrade -1` 往返通过。
- [x] 1.2 `app/core/state_machine.py` 扩展 `LEGAL_TRANSITIONS`（`EXECUTED→{VERIFYING,VOID}`、`VERIFIED→{VOID}`、新增 `VERIFYING→{VERIFIED,VERIFY_FAILED}`、`VERIFY_FAILED→{VERIFYING}`、`VOID→{}`），`TERMINAL_STATUSES` 随之变 `{CANCELLED, VOID}`。验证：`tests/logic/test_job_state.py` 新增边全通过、未定义边（如 `VERIFY_FAILED→VOID`、`VERIFYING→VOID`）被拒（场景：未定义迁移被拒绝 / 冲正置 VOID / 后验拆为两段）。

## 2. 台账反向行 schema（冲正前置）

- [x] 2.1 `app/models/job.py` 的 `Ledger` 增 `is_reversal: bool = False`，唯一约束 `uq_ledgers_job_order_id(job_order_id)` 放宽为 `uq_ledgers_job_order_id_reversal(job_order_id, is_reversal)`（迁移 + 模型同步）。验证：`tests/models/test_job.py` 约束断言 —— 一单至多一正常行 + 一反向行；第二条正常行被拒，一正常 + 一反向可共存。

## 3. 台账写入与 cap 增量（F7 + F9 增量）

- [ ] 3.1 `app/services/ledger.py` 落地台账唯一写入入口（含 `is_reversal` 反向行），不向角色开放手动入口，写入即固化。验证：`tests/logic/test_ledger_write.py` 正常行 / 反向行落账 + 字段口径（`source/target_location_code` 按 `_LEDGER_LOCATION_CHECK`）断言。
- [ ] 3.2 `app/cap/increment.py` 落地事务增量：复用 `app/engine/allocator.py::to_occupied_cells()` 恒等占位做板-格换算（入库↑已占 / 出库↓已占 / 移库源↓目标↑），与台账同事务。验证：`tests/logic/test_cap_increment.py` 三类增量口径 + 与台账同事务回滚断言（场景：台账与 cap 增量原子提交）。
- [ ] 3.3 确认编排（`app/services/inbound.py` / `outbound.py` / `relocate.py`）：`CONFIRMED→EXECUTED` 迁移 + 写台账 + cap 增量在同一真实 DB 事务，任一步失败整体回滚、状态回 `PLANNED`。验证：`tests/logic/test_confirm.py` 成功链路 + 写台账失败回滚（场景：写台账失败回退到 PLANNED / 未确认不产生台账）。

## 4. 同步后验与偏离（F8）

- [ ] 4.1 `app/services/verify.py` 三口径纯函数：入库 = 同物料跨巷道 ≤5 且 同批 ≤3；出库 = 拣货量加权集中度 80% ≤ N（N=5）；移库 = 移库后同物料跨巷道数低于移库前；据此判 `verify_result = PASS / DEVIATION`。验证：`tests/logic/test_verify.py` 三口径 PASS / DEVIATION 断言（场景：入库后验达标 / 入库后验偏离）。
- [ ] 4.2 后验编排：`EXECUTED→VERIFYING→VERIFIED/VERIFY_FAILED` 在确认请求内同步完成，`VERIFYING` 不对外停留；`VERIFY_FAILED→VERIFYING` 重试、无放弃后验终态。验证：`tests/logic/test_verify_flow.py` 请求结束必为 `VERIFIED`/`VERIFY_FAILED` + 失败重试边断言（场景：后验失败可重试）。
- [ ] 4.3 偏离标记：`verify_result = DEVIATION` 写 `Deviation`，`PASS` 不写。验证：`tests/logic/test_deviation.py` 偏离写 / 达标不写断言（场景：偏离写入 Deviation / 达标不写 Deviation）。

## 5. 冲正（F7 反向，有意偏离 15-01 §6.3）

- [ ] 5.1 冲正编排：原单 `EXECUTED/VERIFIED → VOID` + 写 `is_reversal` 反向台账行 + 同事务释放 cap / 减少库存；不创建反向 `JobOrder`、不改写历史台账。验证：`tests/logic/test_void.py` 冲正回冲断言（VOID 终态、反向行、cap/库存回补、无反向 JobOrder）。
- [ ] 5.2 冲正守卫：仅 `EXECUTED` / `VERIFIED` 可冲正，`VOID` 后不可再冲正，未通过二次确认不冲正。验证：`tests/logic/test_void_guard.py` 非法源状态（如 `PLANNED→VOID`、`VOID→VOID`）被拒断言。

## 6. API 端点（写路径 + 验证读）

- [ ] 6.1 `POST /api/job/batch/confirm`（`app/api/routes/job.py`）逐单独立事务，个别单失败回 `PLANNED` 不影响同批其余单。验证：`tests/api/test_job_confirm.py`（场景：批量确认逐单独立事务）。
- [ ] 6.2 `POST /api/job/{id}/reject`、`POST /api/job/{id}/retry`、`POST /api/job/{id}/void` 端点 + 写操作二次确认语义（未确认不产生台账）。验证：`tests/api/test_job_actions.py` 各端点状态迁移 + 未确认不产生台账断言。
- [ ] 6.3 `GET /api/ledger`、`GET /api/verification/{job_id}` 验证读。验证：`tests/api/test_job_reads.py` 台账含反向行 + 后验结果可查断言（场景：台账查询返回反向行 / 后验结果可查）。

## 7. 基线层评测场景与异常/边界落地（20 号基线层维度）

- [ ] 7.1 作业闭环一致性（基线层维度，`CL-001~006`）：确认→写台账→后验→冲正闭环断言落入 `tests/logic`。验证：`pytest tests/logic -m logic` 对应闭环场景全绿。
- [ ] 7.2 KPI 计量正确性（基线层维度）：后验三口径跨巷道 / 加权集中度计量与 `Deviation` 阈值计量断言。验证：`pytest tests/logic` 计量口径全绿（evals harness 接线属阶段六，本任务只落断言）。
- [ ] 7.3 乐观锁并发冲突（异常/边界）：多端同时确认同一作业单仅一端成功、后到写入被拒。验证：`tests/logic/test_confirm_concurrency.py` 并发确认断言（spec：乐观锁并发守卫 / 并发确认仅一方成功）。
