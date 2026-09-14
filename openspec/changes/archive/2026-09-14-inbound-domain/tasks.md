# Tasks: 入库域剩余工作（队列查询接口 + 理由读接口 + p3 数据层）

> **变更边界**：本变更只补「队列查询接口」+「入库作业页 p3 数据层接线」两点。评分引擎、批量分配、确认链、台账、后验已在阶段三（`recommendation-engine`）与阶段四（`transaction-base`）落地并通过 1166 条测试，本变更**不改其编排**。
>
> **「不适用」登记**（tasks 规则是 F1~F10 全链口径，单阶段 change 显式登记而非静默删）：
> - 「引擎单测（评分正确性 / 作业闭环一致性 / KPI 计量正确性）」「后验与 KpiSnapshot 聚合」—— 已在阶段三/四落地，本变更无新增评分 / 后验 / KPI 逻辑，**不适用**。
> - 「导入校验失败阻断 / cap 不足降级 / 快照过期阻断 / 乐观锁并发冲突」—— 已由既有 `data-import` / `transaction-base` / `recommendation-engine` 测试覆盖，本变更的写路径复用既有端点、不新造写语义，**不适用**。
> - 本变更唯一新增的行为是「只读队列查询 + 只读理由查询 + 前端接线」，对应的可断言契约见 specs「入库作业队列查询」「入库作业页数据层接入」「推荐理由读」三条需求及其场景。

## 1. 后端：队列查询 + 理由读（DTO + 端点 + 测试）

- [x] 1.1 在 `backend/app/schemas/job.py` 新增 `JobQueueItem`（`from_attributes=True`），投影 `order_no` / `line_no` / `material_code` / `material_name` / `qty` / `abc_class` / `batch_no` / `status` / `bulk_batch_no`，并额外带 `job_order_id`（`str(id)` 形态）与 `lock_version`（D2）。验证：`cd backend && python -m pytest tests/api -q` 全绿，无既有 DTO 回归。

- [x] 1.2 在 `backend/app/api/routes/job.py` 的 `reads_router` 新增 `GET /api/jobs`：查询参数 `warehouse_id`（必填）、`type`（必填，`JobType`）、可选 `status` / `material_code` / `abc_class` / `order_no`；`WHERE warehouse_id == :wid AND job_type == :type`（可选 AND 收窄），按 `id` 升序返回 `list[JobQueueItem]`，纯 `SELECT` 不改状态（D1 / D3 / D4）。验证：`cd backend && python -m pytest tests/api -q` 全绿。

- [x] 1.3 新增 `backend/tests/api/test_jobs.py`（先 RED 后 GREEN）：覆盖 specs「入库作业队列查询」五个场景——① 按类型只返回 `INBOUND`（混入 `OUTBOUND` 单不返回）② 按 `status` 筛选 ③ 跨仓隔离（他仓单不返回）④ 查询不改变状态（`PENDING` 保持 `PENDING`、`lock_version` 不变）⑤ 响应含 `job_order_id` 与 `lock_version`。验证：`cd backend && python -m pytest tests/api/test_jobs.py -q` 通过。

- [x] 1.4 在 `backend/app/api/routes/job.py` 的 `reads_router` 新增 `GET /api/plan/{plan_id}`（D8）：查询参数 `warehouse_id`（必填），`plan_id` 走 `_ID_PATTERN` 校验，`WHERE id == :pid AND warehouse_id == :wid` 取 `RecommendationPlan`，返回其 `payload_json`；未知或跨仓 `plan_id` 对齐 `_load_order` 抛 `ValidationBlocked`（422）。新增 `backend/tests/api/test_plan.py`（先 RED 后 GREEN）覆盖 specs「推荐理由读」两场景——按方案取理由、未知/跨仓拒绝。验证：`cd backend && python -m pytest tests/api/test_plan.py -q` 通过。

## 2. 前端：入库作业页 p3 数据层接线

- [x] 2.1 `backend/frontend/inbound.html` 增加内联 `<script>`（同 `data-import.html` 的 `(function(){...})()`）：`loadPageData` 调 `window.api('/jobs?type=INBOUND&warehouse_id=' + WID, {method:'GET'})` 渲染 `.ord` 队列卡片（单号 / 物料编码 / 物料描述 / 箱数·ABC / 状态），`job_order_id` 与 `lock_version` 存 `data-*`；空队列走 `setState(el, 'empty', {text:'请先导入 PO'})`，异常走 `setState(el, 'error', {retry})`。验证：`cd backend && python -m pytest tests/frontend -q` 全绿（见 3.1），且无后端时页面渲染空/异常态不白屏。

- [x] 2.2 批量分配接线：勾选 `.ord` 收集 `job_order_id` → 「批量分配」按钮直接调 `window.api('/allocate/batch', {method:'POST', body:{warehouse_id, job_order_ids, snapshot_version:null}})`（不弹二次确认卡，D7），用响应 `plans[]` 渲染 `.btab`（PO / 物料 / ABC / 箱数 / 推荐巷道集 `aisles` / 预测跨巷道 / 操作）；「微调」下钻调 `window.api('/plan/' + plan_id + '?warehouse_id=' + WID)` 渲染 `.reason` 6 因子理由卡（D8）。验证：手动/集成走查——选中若干单点批量分配，方案表数据来自接口而非硬编码；「微调」显示来自 `GET /api/plan/{plan_id}` 的 6 因子理由。

- [x] 2.3 逐单处置 + 批量落位 + 后验条接线：`.btab` 的接受 / 微调 / 驳回映射到确认链与 `POST /api/job/{id}/reject`；「批量执行落位」弹二次确认卡后 `window.api('/job/batch/confirm', {method:'POST', body:{warehouse_id, orders:[{job_order_id, target_location_code, lock_version}]}})`；`.postbar` 后验条调 `window.api('/verification/' + job_order_id + '?warehouse_id=' + WID)` 显示 `verify_result` 与三口径（D5 / D7）。验证：集成走查「确认未通过不产生台账」，后验条展示来自 `GET /api/verification/{job_id}` 的 `verify_result`。

## 3. 前端静态断言 + 全量回归

- [x] 3.1 新增 `backend/tests/frontend/test_frontend_inbound.py`（对齐 `test_frontend_foundation.py` 静态断言范式）：断言 p3 无硬编码演示 `PO-3573743144K55G` 常量、含内联脚本且引用 `window.api('/jobs?...')`、`window.api('/plan/...')` 与 `setState`、二次确认逻辑只绑定「批量执行落位」按钮、`.ord` / `.btab` / `.reason` / `.postbar` 四区仍在。验证：`cd backend && python -m pytest tests/frontend/test_frontend_inbound.py -q` 通过。

- [x] 3.2 全量回归：`openspec validate inbound-domain`（退出 0，仅 3 条既有 RFC2119 中文假警报）已绿；`python -m pytest -q` 排除在途 `tests/logic/test_outbound.py`（`outbound-domain` 的 RED 测试，导入尚未实现的 `derive_pick_sequence`）后 **1179 passed**（= 基线 1166 + 本变更 13 条）。全量绿待 `outbound-domain` / `relocate-domain` 落地后达成。

## 4. 设计文档路径漂移清扫（apply 阶段收尾）

> **延后（用户 2026-09-14 定）**：出库域（`outbound-domain`）与移库域（`relocate-domain`）
> 当前仍在执行，此刻清扫 `/api/v1 → /api` 会在正被改动的文档里留下半截改动。待两域落地后
> **统一**清扫（`/api/v1` → `/api` + 已落地端点对齐代码真实路径），改前逐份建 `.<时间戳>.bak`。
> 本变更不阻塞于此项。

- [x] 4.1 （**延后**至 `outbound-domain` / `relocate-domain` 落地后统一处理）仓库外设计文档残留 `/api/v1` 清扫登记：`15-03` / `15-04` / `15-05` / `16` / `17` / `18` / `19` / `13` / `28-03` / `28-04` / `28-05` 中尚未改的 `/api/v1/jobs...` → `/api/jobs?type=...`、`/api/v1/jobs/batch/allocate` → `/api/allocate/batch`；改前一律建 `.<时间戳>.bak`（设计文档改动惯例）。验证：`grep -rn "/api/v1" "D:/成品库位智能推荐/产品设计/"` 只剩本变更明确保留的出库/移库 TBD 路径（`15-03` / `15-04` 的 `/api/v1/jobs?type=outbound|relocate` 属 28-03 / 28-04，未落地前保留原文）。
