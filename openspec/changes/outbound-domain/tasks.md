# Tasks: 出库域（顺路取只读派生 + 出库作业接口 + 拣货路径入账）

> **变更边界**：本变更只补「顺路取只读派生」「批量顺路取接口」「出库确认记录最终拣货路径」「出库后验读拣货路径」四块。出库确认链 / 台账 / 后验底座已在阶段四（`transaction-base`）落地，队列查询端点 `GET /api/jobs?type=outbound` 由兄弟变更 `inbound-domain` 提供，本变更**只消费不 spec 不实现**。
>
> **「不适用」登记**（tasks 规则是 F1~F10 全链口径，单阶段 change 显式登记而非静默删）：
> - 「引擎单测：评分正确性 / 作业闭环一致性 / KPI 计量正确性」—— 本变更**无评分/引擎逻辑**（顺路取是只读派生，不调 `engine.invoke`），基线层评测落地属阶段六（`evals/`），**不适用**；本变更的对应可断言契约见 specs「顺路取只读派生」「批量顺路取接口」「三类作业确认与落位执行」「同步后验与三口径判定」四条需求及其场景。
> - 「导入校验失败阻断 / cap 不足降级」—— 属入库/导入域（`data-import` / `recommendation-engine` 已覆盖），**不适用**。
> - 「后验与 KpiSnapshot 聚合」—— KpiSnapshot 聚合属阶段六（`kpi`），本变更只改**出库后验的数据来源口径**（读 `pick_path_json`），不新增 KPI 聚合，**不适用**。
> - 「快照过期阻断 / 乐观锁并发冲突」—— 本变更复用既有 `_current_snapshot` 阻断（无快照 → 409）与 confirm 乐观锁，不新造语义，相关场景已入 specs。

## 1. 顺路取只读派生（逻辑层）

- [x] 1.1 **RED**：在 `backend/tests/logic/test_outbound.py` 写 `derive_pick_sequence` 的用例——按巷道聚合现状分布（每巷 qty + batches）、巷道升序、加权集中度（复用 `concentration_aisle_count` 80% 降序累加）、`exceeded`（>N 为 true，仅高亮）、空 profile（无该物料 → 空结果/标记未入库）、确定性（同样 profile 两次同输出）。验证：`cd backend && python -m pytest tests/logic/test_outbound.py -q` 全红（函数未定义）。

- [x] 1.2 **GREEN**：在 `backend/app/services/outbound.py` 实现 `derive_pick_sequence(*, material_code, profile, n=DEFAULT_CONCENTRATION_N) -> dict`（17 §10.2 形状），纯函数、不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`（D1）。验证：`cd backend && python -m pytest tests/logic/test_outbound.py -q` 全绿。

## 2. 批量顺路取接口（schema + 端点 + API 层）

- [x] 2.1 在 `backend/app/schemas/reason.py` 新增 `PickPathItem` / `PickSequence` / `BatchPickSequenceRequest`（`warehouse_id` + `job_order_ids` + 可选 `snapshot_version`）/ `BatchPickSequenceResponse`（分列 `plans[]` + `not_in_stock[]`），形状对齐 17 §10.2（D6）。验证：`cd backend && python -m pytest tests/api -q` 全绿（无既有 DTO 回归）。

- [x] 2.2 在 `backend/app/api/routes/job.py` 新增 `POST /api/job/batch/pick-sequence`：`require_permission(Permission.OUTBOUND_OPERATE)`；报文级校验（`job_order_ids` 非空/`^[1-9]\d*$`/全为 `OUTBOUND`，否则整批 422）；`_current_snapshot`（无快照 409）+ `load_snapshot_index`；逐单派生（`index.profile` 空 → `not_in_stock`，否则写 `RecommendationPlan(plan_kind=PICK, payload_json 含 snapshot_version)` + `assert_transition(PENDING, PLANNED)`）；幂等键 = `bulk_batch_no` × 视图版本（`PENDING` 派生迁 `PLANNED`；`PLANNED` 同版本返既有、异版本追加新方案行）（D2 / D3）。验证：`cd backend && python -m pytest tests/api -q` 全绿。

- [x] 2.3 **RED→GREEN**：在 `backend/tests/api/test_pick_sequence.py` 覆盖 specs「批量顺路取接口」五场景——① 派生迁 `PLANNED` ② 货未入库分列 `not_in_stock` 不阻断 ③ 幂等命中返既有不重复写 ④ 视图推进重新派生 ⑤ 无快照 409。验证：`cd backend && python -m pytest tests/api/test_pick_sequence.py -q` 通过。

## 3. 出库确认记录最终拣货路径（confirm pick_path + 源可空连锁）

- [x] 3.1 新增迁移：放宽 `ck_ledgers_location_columns_by_type` 出库析取项为 `target IS NULL`（source 可空），同步改 `app/models/job.py::_LEDGER_LOCATION_CHECK`；删改 `tests/logic/test_ledger_write.py::test_outbound_without_source_is_rejected_by_check`（出库 source 可空转合法）（D7）。验证：`cd backend && python -m pytest tests/logic/test_ledger_write.py -q` 全绿 + `alembic upgrade head` 通过。

- [x] 3.2 改 `app/cap/increment.py` OUTBOUND 分支：优先按 `pick_path_json` 逐巷扣减（`location_code LIKE '<aisle>%'`，按 `location_code` 升序递减），回退 `source_location_code` 单巷，两者皆无 → `ValidationBlocked`（D7）。验证：`cd backend && python -m pytest tests/logic/test_cap_increment.py -q` 全绿。

- [x] 3.3 在 `backend/app/schemas/job.py` 的 `ConfirmItem` 增 `pick_path: list[PickPathItem] | None`；`app/api/routes/job.py::_require_locations` 出库分支放宽为「目标必空、源可空」（D4）。验证：`cd backend && python -m pytest tests/api -q` 全绿（既有出库确认用例的库位填法断言同步调整）。

- [x] 3.4 改 `_confirm_one` 出库分支（`source_location_code=None`、`pick_path_json=item.pick_path`）与 `confirm_outbound` 签名（`source_location_code: str | None = None`）；实现「`pick_path` 省略 → 读该单当前方案 `pick_sequence` 回退，取不到 → `ValidationBlocked`」（D4）。验证：`cd backend && python -m pytest tests/api -q` 全绿。

- [x] 3.5 **RED→GREEN**：在 `backend/tests/api/test_pick_sequence.py`（或 `test_job.py`）覆盖 specs「三类作业确认与落位执行·出库确认记录最终拣货路径」——① 确认带 `pick_path` → 台账 `pick_path_json` 写确认值、`source_location_code` 为 NULL ② 省略 `pick_path` → 回退取当前方案 ③ 无方案可回退 → 422。验证：`cd backend && python -m pytest tests/api/test_pick_sequence.py -q` 通过。

## 4. 出库后验读拣货路径

- [x] 4.1 改 `backend/app/services/verify.py::_pick_qty_from_ledger`：优先解析 `ledger.pick_path_json`（`pick_sequence[]`）按 `aisle` 聚合 `qty` → `{aisle: qty}`；`pick_path_json` 缺失/空回退 `source_location_code`（单巷）；两者皆无 → `ValidationBlocked`（D5）。验证：`cd backend && python -m pytest tests/logic -m logic -q` 全绿。

- [x] 4.2 **RED→GREEN**：在 `backend/tests/logic/test_outbound.py` 覆盖 specs「同步后验与三口径判定·出库后验读台账拣货路径」——① 多巷 `pick_path_json` 聚合算集中度（≤N 为 PASS / >N 为 DEVIATION）② 回退单源 ③ 缺失报错迁 `VERIFY_FAILED`。验证：`cd backend && python -m pytest tests/logic/test_outbound.py -q` 通过。

## 5. 集成冒烟 + 全量回归

- [x] 5.1 在 `backend/tests/api/test_outbound_pipeline.py` 写出库 7 步管线集成冒烟：DO 导入 → `GET /api/jobs?type=outbound` → 批量顺路取 → 逐单确认（带 `pick_path`）→ 台账 `pick_path_json` + 后验 `Verification`（`verify_result` 与加权集中度口径）。验证：`cd backend && python -m pytest tests/api/test_outbound_pipeline.py -q` 通过。

- [x] 5.2 全量回归：`cd backend && python -m pytest tests/ --tb=short -q` 全绿（既有 1166 条无回归 + 本变更新增用例）。
