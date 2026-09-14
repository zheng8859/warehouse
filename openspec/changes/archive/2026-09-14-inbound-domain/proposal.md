## Why

入库域后端底座（`engine.invoke` 集成、批量分配、逐单确认、台账、后验）已在阶段三（`recommendation-engine`，v0.3.0）与阶段四（`transaction-base` + `data-import`，v0.4.0）落地并通过 1166 条测试。但入库作业页 p3 仍是硬编码演示值，且后端缺一个只读的作业队列查询端点——操作员在界面上拿不到「待分配 PO 队列」，也就无法走完 `F4 软推荐 → F5 人工确认 → F7 落位台账 → F8 后验` 这条界面化闭环。本变更补齐这两个缺口，让入库域全链路在界面上可操作、可验证。

## What Changes

- 新增只读端点 `GET /api/jobs?type=INBOUND`：按 `job_type` 过滤并支持 `status` / `material_code` / `abc_class` / `order_no` 等筛选，返回入库作业单队列（供 p3 多选队列与批量分配入口消费）。
- 新增只读端点 `GET /api/plan/{plan_id}`：按 `plan_id` 返回该方案（`RecommendationPlan`）的推荐理由体，供 p3 推荐理由卡下钻展示 6 因子理由。
- 入库作业页 p3 数据层改造：`backend/frontend/inbound.html` 与 `assets/app.js` 由硬编码演示值接入后端真实 API——队列接 `GET /api/jobs?type=INBOUND`、批量分配接既有 `POST /api/allocate/batch`、批量落位接既有 `POST /api/job/batch/confirm`、后验条接既有 `GET /api/verification/{job_id}`、推荐理由卡接新增 `GET /api/plan/{plan_id}`，统一用空 / 加载 / 成功 / 异常四态渲染。
- 设计文档路径漂移已在仓库外修正（`15-01` / `15-02` 的 `/api/v1` → `/api`、批量分配路径 → `/api/allocate/batch`、幂等口径 → 状态守卫）；其余文档残留的 `/api/v1` 在 apply 阶段登记清扫。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `transaction-base`: 新增两条需求——① `GET /api/jobs?type=INBOUND` 作业队列只读查询端点；② 入库作业页 p3 数据层接入后端 API（队列 / 批量分配 / 批量落位 / 后验条）。
- `recommendation-engine`: 新增一条需求——③ `GET /api/plan/{plan_id}` 推荐理由只读查询（透传 `RecommendationPlan.payload_json`，供 p3 推荐理由卡下钻）。

## Impact

- **实体**：`JobOrder`（只读查询，无新字段、无迁移）。
- **API 路由**：新增 `GET /api/jobs?type=INBOUND` 与 `GET /api/plan/{plan_id}`；复用既有 `POST /api/allocate/batch`、`POST /api/job/batch/confirm`、`GET /api/verification/{job_id}`（均已在 `/api/*` 约定内）。
- **前端**：`backend/frontend/inbound.html`、`backend/frontend/assets/app.js`。
- **权限**：沿用既有资源标识 `inbound.view`（队列查询）与 `inbound.operate`（批量分配，已落地端点级鉴权），不新增资源。
- **KPI**：不改变指标口径；支撑 `推荐采纳率 ≥60%`、`后验达标率`（同物料跨巷道 ≤5 且 同批跨巷道 ≤3）在界面上的可操作度量。

## 非目标

- 不重做评分引擎 / 批量分配 / 确认链 / 台账 / 后验——阶段三四已落地并通过测试，本变更只补只读查询端点与前端接线。
- 不改 6 因子算法、降级链、预留池口径（`14` 号文档）。
- 不做出库域（p4）与移库域（p5）——属 28-03 / 28-04。
- 不新增写端点、不新增实体字段、不做迁移。
- 不引入前端框架 / 构建工具（保持零构建 Vanilla）。
- 不做细粒度 RBAC 扩展（沿用既有端点级鉴权，其余属 M5 路线图）。
