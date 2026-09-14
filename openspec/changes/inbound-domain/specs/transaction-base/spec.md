## ADDED Requirements

### Requirement: 入库作业队列查询

系统必须提供只读端点 `GET /api/jobs`，`type` 为必填 `JobType` 枚举参数（`INBOUND` / `OUTBOUND` / `RELOCATE`），按 `type` 过滤返回本仓（按 `warehouse_id` 隔离）对应作业单队列，并支持按 `status` / `material_code` / `abc_class` / `order_no` 筛选。返回字段至少含 `order_no` / `line_no` / `material_code` / `material_name` / `qty` / `abc_class` / `batch_no` / `status` / `bulk_batch_no`，并额外含 `job_order_id`（`str(id)` 形态）与 `lock_version`，供下游批量分配 / 批量确认写端点消费。查询为只读，系统不得在查询时改变任何作业单状态。

#### Scenario: 按类型返回入库队列
- **GIVEN** 本仓存在 `INBOUND` 与 `OUTBOUND` 作业单各若干
- **WHEN** 调用 `GET /api/jobs?type=INBOUND`
- **THEN** 仅返回 `job_type = INBOUND` 的作业单，不含 `OUTBOUND` 单

#### Scenario: 按状态筛选
- **GIVEN** 本仓存在多张 `INBOUND` 作业单，状态分别含 `PENDING` 与 `PLANNED`
- **WHEN** 调用 `GET /api/jobs?type=INBOUND&status=PLANNED`
- **THEN** 仅返回状态为 `PLANNED` 的入库作业单

#### Scenario: 跨仓隔离
- **GIVEN** 另一个 `warehouse_id` 下存在 `INBOUND` 作业单
- **WHEN** 当前仓调用 `GET /api/jobs?type=INBOUND`
- **THEN** 不返回其他仓的作业单

#### Scenario: 查询不改变状态
- **GIVEN** 一张状态为 `PENDING` 的入库作业单
- **WHEN** 调用 `GET /api/jobs?type=INBOUND`
- **THEN** 该作业单状态保持 `PENDING`，不产生任何写操作或状态迁移

#### Scenario: 响应含下游写端点所需标识
- **GIVEN** 本仓存在一张 `INBOUND` 作业单
- **WHEN** 调用 `GET /api/jobs?type=INBOUND`
- **THEN** 返回项含 `job_order_id`（`str(JobOrder.id)` 形态）与 `lock_version`，可直接作为批量分配 / 批量确认的输入

### Requirement: 入库作业页数据层接入

系统必须在入库作业页 p3 用真实后端接口替换硬编码演示数据：多选队列接 `GET /api/jobs?type=INBOUND`、批量分配接 `POST /api/allocate/batch`、批量落位接 `POST /api/job/batch/confirm`、后验条接 `GET /api/verification/{job_id}`、推荐理由卡下钻接 `GET /api/plan/{plan_id}`。页面必须用空 / 加载 / 成功 / 异常四态渲染（`21` §7.11）。写台账的落位动作（批量执行落位）必须先弹二次确认卡，系统不得在未经操作员确认时调用批量确认端点或产生台账（未确认不产生台账）。

#### Scenario: 队列由真实接口渲染
- **GIVEN** 已导入 PO 并产生 `INBOUND` 作业单
- **WHEN** 打开入库作业页 p3
- **THEN** 多选队列渲染的数据来自 `GET /api/jobs?type=INBOUND`，而非硬编码演示值

#### Scenario: 空队列显示空态
- **GIVEN** 本仓无 `INBOUND` 作业单
- **WHEN** 打开入库作业页 p3
- **THEN** 队列区显示空态引导文字（提示先导入 PO）

#### Scenario: 落位须二次确认
- **GIVEN** 操作员选中若干张作业单并点击批量执行落位
- **WHEN** 操作员尚未在二次确认卡上确认
- **THEN** 系统不调用 `POST /api/job/batch/confirm`，不产生任何台账记录

#### Scenario: 推荐理由卡可下钻
- **GIVEN** 一张作业单已批量分配并产出方案
- **WHEN** 操作员在方案表点击「微调」下钻推荐理由
- **THEN** 理由卡展示来自 `GET /api/plan/{plan_id}` 的 6 因子理由与降级标记

#### Scenario: 后验条显示后验结果
- **GIVEN** 一张作业单已 `EXECUTED` 且后验完成
- **WHEN** 查看该单的后验条
- **THEN** 后验条展示来自 `GET /api/verification/{job_id}` 的 `verify_result` 与三口径指标值
