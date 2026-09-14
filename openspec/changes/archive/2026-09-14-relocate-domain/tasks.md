# Tasks: 移库域（收拢方案只读派生 + 批量收拢方案接口）

> **变更边界**：本变更只补「收拢方案只读派生」「批量收拢方案接口」两块。移库确认链 / 台账 / 后验底座已在阶段四（`transaction-base`）落地，队列查询端点 `GET /api/jobs?type=relocate` 由兄弟变更 `inbound-domain` 提供，本变更**只消费不 spec 不实现**。
>
> **「不适用」登记**（tasks 规则是 F1~F10 全链口径，单阶段 change 显式登记而非静默删）：
> - 「引擎单测：评分正确性 / 作业闭环一致性 / KPI 计量正确性」—— 本变更**无评分/引擎逻辑**（收拢方案是本地规则，不调 `engine.invoke`），基线层评测落地属阶段六（`evals/`），**不适用**；本变更的对应可断言契约见 specs「收拢方案只读派生」「批量收拢方案接口」两条需求及其场景。
> - 「导入校验失败阻断 / cap 不足降级（引擎降级链）」—— 导入域与引擎降级链属 `data-import` / `recommendation-engine` 已覆盖，**不适用**（本变更的 cap 校验是「目标巷道 cap 充足」的**方案级**判据，非引擎降级链）。
> - 「后验与 KpiSnapshot 聚合」—— KpiSnapshot 聚合属阶段六（`kpi`），本变更不改后验口径（`verify_relocate` 已落地），**不适用**。
> - 「快照过期阻断 / 乐观锁并发冲突」—— 本变更复用既有 `_current_snapshot` 阻断（无快照 → 409）与 confirm 乐观锁，不新造语义，相关场景已入 specs。

## 1. 收拢方案只读派生（逻辑层）

- [x] 1.1 **RED**：在 `backend/tests/logic/test_relocate.py` 写 `derive_consolidation_plan` 的用例——主巷道（板数最多、并列按巷道号升序）、`from_aisles`/`plates` 按批号聚合、`expected_cross_aisle` 前后（before = profile 跨巷道、after 减变空巷道数）、三重校验①cap 充足、②`batch_unchanged`、③`after < before` 拦截、降级链（主巷道 cap 不足→次选→仍不足移出批量 `moved_out_reason`）、确定性（同样输入两次同输出）。验证：`cd backend && python -m pytest tests/logic/test_relocate.py -q` 全红（函数未定义）。

- [x] 1.2 **GREEN**：在 `backend/app/services/relocate.py` 实现 `ConsolidationPlanResult` 与 `derive_consolidation_plan(*, material_code, batch_no, profile, batch_plates_by_aisle, available)`（17 §10.3 形状 + 三重校验 + 降级链），纯函数、不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`（D1/D2）。验证：`cd backend && python -m pytest tests/logic/test_relocate.py -q` 全绿。

## 2. 批量收拢方案接口（schema + 端点 + API 层）

- [x] 2.1 在 `backend/app/schemas/reason.py` 新增 `ConsolidationCrossAisle` / `ConsolidationPlan` / `BatchRelocatePlanRequest`（`warehouse_id` + `job_order_ids` + 可选 `snapshot_version`）/ `BatchRelocatePlanResponse`（分列 `plans[]` + `moved_out[]`），形状对齐 17 §10.3（D6）。验证：`cd backend && python -m pytest tests/api -q` 全绿（无既有 DTO 回归）。

- [x] 2.2 在 `backend/app/api/routes/job.py` 新增 `POST /api/job/batch/relocate-plan`：`require_permission(Permission.RELOCATE_OPERATE)`；报文级校验（`job_order_ids` 非空/`^[1-9]\d*$`/全为 `RELOCATE`，否则整批 422）；`_current_snapshot`（无快照 409）+ `load_snapshot_index`；批号级取数 `batch_plates_by_aisle`（`InventoryItem` 按 `(material_code, batch_no)` 聚合）；逐单 `derive_consolidation_plan`（有方案写 `RecommendationPlan(plan_kind=CONSOLIDATE, payload_json 含 snapshot_version)` + `assert_transition(PENDING, PLANNED)`，移出批量记 `moved_out[]`）；幂等键 = `bulk_batch_no` × 视图版本（D2/D3/D4/D5）。验证：`cd backend && python -m pytest tests/api -q` 全绿。

- [x] 2.3 **RED→GREEN**：在 `backend/tests/api/test_relocate_plan.py` 覆盖 specs「批量收拢方案接口」五场景——① 派生迁 `PLANNED` ② 主巷道 cap 不足降级次选 ③ 仍不足移出批量（`moved_out` + 降级原因）④ 幂等命中返既有不重复写 ⑤ 无快照 409。验证：`cd backend && python -m pytest tests/api/test_relocate_plan.py -q` 通过。

## 3. 集成冒烟 + 全量回归

- [x] 3.1 在 `backend/tests/api/test_relocate_pipeline.py` 写移库 7 步管线集成冒烟：偏离清单可查 → `GET /api/jobs?type=relocate` → 批量收拢方案 → 逐单确认（源+目标）→ 台账（`ledger_type=RELOCATE`，源/目标都写）+ 后验 `Verification`（`同物料跨巷道是否下降`）。验证：`cd backend && python -m pytest tests/api/test_relocate_pipeline.py -q` 通过。

- [x] 3.2 全量回归：`cd backend && python -m pytest tests/ --tb=short -q` 全绿（既有用例无回归 + 本变更新增用例）。
