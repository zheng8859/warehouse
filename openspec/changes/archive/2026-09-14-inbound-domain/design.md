## Context

入库域后端底座已在阶段三四落地：`POST /api/allocate/batch`（批量分配，含 `require_permission(inbound.operate)` 端点级鉴权）、`POST /api/job/batch/confirm` / `reject` / `retry` / `void`、以及挂在 `reads_router`（`app/api/routes/job.py`，前缀 `/api`）的三个验证读 `GET /api/ledger` / `GET /api/verification/{job_id}` / `GET /api/deviation`。缺的是两个只读端点：**作业队列查询**，以及**按 `plan_id` 取理由体**（`allocate.py` D10 已注明「理由体不随 `plans[]` 返回、按 `plan_id` 另取」，但该取数端点尚未实现）。前端 `inbound.html`（p3）是静态 shell，队列 / 方案表 / 理由卡全为硬编码演示值，无内联脚本；`data-import.html`（p2）已建立 `window.api` + `setState` 四态接线范式，本变更照此接线。动机见 proposal.md。

## Goals / Non-Goals

**Goals:**
- 补两个只读端点：`GET /api/jobs?type=INBOUND`（作业队列）与 `GET /api/plan/{plan_id}`（理由体），让 p3 能拿到队列与 6 因子理由。
- 把 p3 的队列 / 批量方案 / 逐单处置 / 理由卡 / 后验条接到后端端点，四态渲染。

**Non-Goals:**
- 不改引擎 / 分配 / 确认 / 台账 / 后验的编排（`allocate.py` / `services/*` 一律不动）。
- 不新增实体字段、不做迁移；队列查询为纯 `SELECT`，不推进任何状态机。
- 不做出库 / 移库队列（`type=OUTBOUND` / `RELOCATE` 的查询归 28-03 / 28-04）。

## Decisions

### D1 — 端点落在 `reads_router`，路径 `/api/jobs`

新增 `GET /api/jobs`，挂在 `job.py` 的 `reads_router`（前缀 `/api`），与 `GET /api/ledger` 等同一层。查询参数：`warehouse_id`（必填，沿用读端点 `Query(min_length=1, max_length=32)` 惯例）、`type`（必填，`JobType` 枚举）、可选 `status` / `material_code` / `abc_class` / `order_no`。

- **为什么不用 `/api/job/jobs`**：三个验证读都是顶层资源（台账 / 后验 / 偏离是独立资源，不是某张作业单的子资源，见 `job.py` 顶部注释）。作业队列同理是「作业单集合」的读，`config.yaml` 与已修正的 `15-01` / `15-02` 都写 `/api/jobs`。挂在 `reads_router` 复用既有 `_ID_PATTERN` 与 `warehouse_id` 隔离约定。
- **为什么 `type` 用枚举而不是自由串**：非法取值在 FastAPI 参数校验层即 422，避免「拼错类型返回空队列」被误读成「无数据」。端点本身类型无关（`WHERE job_type = :type` 一个通式），`outbound` / `relocate` 由 28-03 / 28-04 复用同一端点，本变更只接线并测试 `inbound`。

### D2 — 响应 DTO 额外带 `job_order_id` 与 `lock_version`

新增 `JobQueueItem`（`app/schemas/job.py`，`from_attributes=True`），投影 spec「入库作业队列查询」点名的字段（`order_no` / `line_no` / `material_code` / `material_name` / `qty` / `abc_class` / `batch_no` / `status` / `bulk_batch_no`），**并额外投影 `job_order_id = str(id)` 与 `lock_version`**。

- **为什么额外带这两列**：队列是「分配 → 确认」写链的入口视图——`POST /api/allocate/batch` 要 `job_order_ids`（`str(id)` 形态），`POST /api/job/batch/confirm` 要 `job_order_id` + `lock_version`（乐观锁）。缺了它们，p3 拿到队列后仍无法把选中项喂给下游写端点。spec 的「至少含」措辞给这两列留了位置。

### D3 — 筛选字段只到 JobOrder 既有列，`order_date` 显式延后

筛选字段定为 `status` / `material_code` / `abc_class` / `order_no`。28-02 文档 Phase B 列的「订单日期」筛选**不实现**：`JobOrder` 没有 `order_date` 列（PO 的「生产日期」未持久化到作业单上），而本变更的非目标明确「不新增实体字段、不做迁移」。前端 `inbound.html` 的 `.filterbar` 映射为：状态→`status`、ABC→`abc_class`、物料→`material_code`、搜索物料/PO 号→`order_no`（前缀匹配）；「箱数」「排序」为**客户端**筛选/排序，不走后端参数。

### D4 — 查询只读 + 确定性

队列查询是纯 `SELECT`：`WHERE warehouse_id = :wid AND job_type = INBOUND`（`status` 等为可选 `AND`）。不取业务钟、不 `commit`、不改 `lock_version`、不迁移状态（spec「查询不改变状态」）。跨仓隔离靠 `warehouse_id` 进查询（`CLAUDE.md` §七）。

### D5 — 前端接线复用 `window.api` + `setState`

`inbound.html` 增加内联 `<script>`（同 `data-import.html` 的 `(function(){...})()`），把硬编码 `.ord` / `.btab` / `.reason` 换成动态渲染：

- 队列：`window.api('/jobs?type=INBOUND&warehouse_id=' + WID, {method:'GET'})` → 渲染 `.ord` 卡片（`job_order_id` / `lock_version` 存 `data-*` 供下游取用）。
- 批量分配：选中 `job_order_id` 集合 → `window.api('/allocate/batch', {method:'POST', body:{warehouse_id, job_order_ids, snapshot_version:null}})` → 渲染 `.btab`（推荐巷道集 / 预测跨巷道 / 操作）。理由卡 `.reason` 不随 `plans[]` 内联（`allocate.py` D10），「微调」下钻时按 `PlanItem.plan_id` 调 `window.api('/plan/' + plan_id + '?warehouse_id=' + WID)` 取 6 因子理由与降级标记（D8）。
- 逐单处置：接受 / 微调 / 驳回映射到确认链与 `POST /api/job/{id}/reject`；微调改巷道后的容量冲突提示与预测跨巷道重算属前端交互，不改引擎。
- 后验条：`window.api('/verification/' + job_order_id + '?warehouse_id=' + WID)` → 显示 `verify_result` 与三口径。

`window.api` 把 `path` 原样交给 `fetch`，故 GET 的查询串由页面拼接进 `path`；Bearer 令牌由 `window.api` 从 `sessionStorage.token` 注入。

### D6 — 鉴权沿用既有口径，不新增资源

队列查询挂 `reads_router`，与 `list_ledger` / `list_deviation` 一致：仅全局认证中间件覆盖（401），**不挂 `require_permission`**（v1 读端点无端点级 RBAC，属 M5 路线图）。写路径沿用既有守卫：`POST /api/allocate/batch` 已挂 `inbound.operate`，`POST /api/job/batch/confirm` 是操作员确认入口（`transaction-base` D6 明文不加端点级 RBAC）。本变更**不新增** `resource.action` 标识，沿用 `inbound.view` / `inbound.operate`。

### D7 — 写操作二次确认卡

二次确认卡只挂「批量执行落位」（写台账的 `POST /api/job/batch/confirm`）。「批量分配」（`POST /api/allocate/batch`）**不弹卡**——它只生成方案、迁 `PENDING → PLANNED`、不写台账，是 F4「给建议」而非 F7「落位」。台账防线在**后端**已经成立（状态机守卫 + 台账唯一来源 + `_LEDGER_LOCATION_CHECK`），前端卡是 L1 的 UX 兑现，不是唯一防线——前端被绕过也不破坏「未确认不产生台账」这条红线。

### D8 — 推荐理由读端点 `GET /api/plan/{plan_id}`

新增 `GET /api/plan/{plan_id}`，挂在 `job.py` 的 `reads_router`（`warehouse_id` 查询参数隔离），按 `plan_id` 返回 `RecommendationPlan.payload_json`（`build_reason_payload` 序列化的 6 因子理由 + `degraded` / `degrade_reason` + 因子级 `factor_degraded`）。未知或跨仓 `plan_id` 拒绝（对齐 `_load_order` 的 `ValidationBlocked` 422，见 `job.py`）。理由体只有一个来源（库里的方案行），不复制副本；`plans[]` 不带理由体，下钻才取，避免方案表加载时的 N+1。

## Risks / Trade-offs

- **[R] `order_date` 筛选不可实现** → 已在 D3 显式延后，登记为与 28-02 筛选清单的设计偏离；前端不渲染「订单日期」筛选。
- **[R] 队列无分页、单仓试点量小** → 现有读端点（ledger / deviation）也不分页，保持一致；单批上限 50 单约束了单次操作规模。若试点数据量超预期，分页属后续 change，不动本变更的 spec。
- **[R] 去掉硬编码演示值后，无后端/无数据时页面空转** → `loadPageData` 包 try/catch，异常走 `setState(el, 'error', {retry})`；空队列走 `setState(el, 'empty', {text:'请先导入 PO'})`，不白屏。
- **[R] 理由卡下钻需二次取数（一次「微调」两次请求）** → 已由 D8 的 `GET /api/plan/{plan_id}` 解决；理由在「微调」时才取，不在队列 / 方案表加载时预取，避免 N+1。

## Migration Plan

无 DB 迁移：不新增列 / 表 / 约束。变更面 = 后端（`schemas/job.py` 加 `JobQueueItem` + `job.py` 加一个 `reads_router` 端点）+ 前端（`inbound.html` 内联脚本 + 硬编码行替换）。回滚 = 还原这两个文件；无数据需迁移，功能开关式回滚不删历史台账（本变更本身不写台账）。

## Open Questions

（无——筛选字段集在 D3 定案、理由卡取数路径在 D8 定案，不改变 spec、方案或任务拆分。）
