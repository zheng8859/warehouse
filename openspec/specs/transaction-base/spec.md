# transaction-base Specification

## Purpose
定义三类作业（入库 / 出库 / 移库）交易底座的端到端行为契约：从 `PLANNED` 方案经人工确认 L1 落位执行、同事务写台账与 cap 增量、同步后验判定达标或偏离、以及冲正回冲，保证台账是 cap 与库存分布增量的唯一来源。

## Requirements

### Requirement: 三类作业确认与落位执行

系统必须在操作员对 `PLANNED` 作业单逐单确认（L1 软推荐，决策权在人）后，才将其迁移到 `CONFIRMED`，并在台账写入成功后迁移到 `EXECUTED`；台账写入失败或事务回滚时，作业单必须回退到 `PLANNED`（确认作废，须重新确认）。系统不得在未经操作员确认时自动落位或产生台账（未确认不产生台账）。出库确认时，台账必须记录操作员确认/微调后的最终拣货顺序（`pick_path_json`，非系统推荐的顺路取顺序），出库侧 `source_location_code` 可空（多巷无单一源库位）。

#### Scenario: 确认后落位并写台账
- **GIVEN** 一张状态为 `PLANNED` 的入库作业单
- **WHEN** 操作员对该单发起确认并通过二次确认
- **THEN** 该单先迁移为 `CONFIRMED`，台账写入成功后迁移为 `EXECUTED`

#### Scenario: 写台账失败回退到 PLANNED
- **GIVEN** 一张状态为 `PLANNED` 的作业单
- **WHEN** 操作员确认后台账写入失败或事务回滚
- **THEN** 该单回退为 `PLANNED`，不产生任何台账记录，须重新确认

#### Scenario: 未确认不产生台账
- **GIVEN** 一张状态为 `PLANNED` 的作业单，操作员尚未确认
- **WHEN** 检查该单的台账记录
- **THEN** 不存在该单对应的台账记录

#### Scenario: 快照缺失阻断确认
- **GIVEN** 一张状态为 `PLANNED` 的作业单，本仓无当前库存快照
- **WHEN** 操作员对该单发起确认
- **THEN** 确认被阻断（`BlockedMissingPrerequisite`，409），作业单停留在 `PLANNED`，不产生台账、不迁 `VERIFY_FAILED`，提示重新导入快照（不猜测落位）

#### Scenario: 出库确认记录最终拣货路径
- **GIVEN** 一张状态为 `PLANNED` 的 `OUTBOUND` 作业单，操作员微调拣货巷道后确认
- **WHEN** 该单落台账
- **THEN** 台账 `pick_path_json` 记录微调后的最终拣货顺序（非系统推荐值），`source_location_code` 为空

### Requirement: 台账与 cap 增量同事务

系统必须在同一数据库事务内写入台账与 cap 增量（入库 ↑已占格数、出库 ↓已占格数、移库源 ↓ 目标 ↑），并整体提交或整体回滚；台账必须作为 cap 与库存分布增量的唯一来源，任何绕过台账直接改 cap 或库存分布的写入必须被拒绝。v1 的增量载体是 `InventoryItem`（库存分布）：`AisleCap` 是快照时刻的冻结值，事务内不写它，已占格数随库存分布增减隐式变化、由 4b 全量重算 / 对账在快照层固化（与 `16` §6.3「增量落 `AisleCap`」的差异登记为设计偏离）。

#### Scenario: 台账与 cap 增量原子提交
- **GIVEN** 一张入库作业单确认落位
- **WHEN** 系统写入台账并更新库存分布（`InventoryItem` 增 / 减，已占格数随之增减）
- **THEN** 两者在同一事务内，要么都提交、要么都回滚

#### Scenario: 台账是增量的唯一来源
- **GIVEN** 一次落位使某巷道库存分布增加
- **WHEN** 检查该巷道库存分布（`InventoryItem`）的增量来源
- **THEN** 该增量对应一条台账记录，不存在无台账来源的 cap 变更

### Requirement: 同步后验与三口径判定

系统必须在台账写入成功后自动触发后验，作业单经 `VERIFYING` 迁移至 `VERIFIED` 或 `VERIFY_FAILED`。后验按作业类型判定：入库 = 同物料跨巷道数 ≤5 且 同批跨巷道数 ≤3；出库 = 拣货量加权集中度 80% 落在 ≤N 巷道（N=5，可配），取自台账记录的拣货路径（`pick_path_json`，操作员确认/微调后的最终顺序）按巷道聚合；移库 = 移库后同物料跨巷道数低于移库前。后验计算完成（无论达标与否）迁移为 `VERIFIED`，由 `Verification` 以 `verify_result = PASS / DEVIATION` 标记；后验计算失败或超时迁移为 `VERIFY_FAILED`，仅提供重试与告警，不提供「放弃后验」终态。

#### Scenario: 入库后验达标
- **GIVEN** 一张入库作业单落位后同物料跨巷道数 ≤5 且 同批 ≤3
- **WHEN** 触发后验
- **THEN** 该单迁移为 `VERIFIED`，`verify_result = PASS`

#### Scenario: 入库后验偏离
- **GIVEN** 一张入库作业单落位后同物料跨巷道数 >5
- **WHEN** 触发后验
- **THEN** 该单迁移为 `VERIFIED`，`verify_result = DEVIATION`

#### Scenario: 后验失败可重试
- **GIVEN** 一张作业单后验计算失败，状态为 `VERIFY_FAILED`
- **WHEN** 操作员发起重试
- **THEN** 该单迁移为 `VERIFYING`，重新执行后验；不存在放弃后验的终态

#### Scenario: 出库后验读台账拣货路径
- **GIVEN** 一张 `OUTBOUND` 作业单台账的 `pick_path_json` 记录多巷拣货路径
- **WHEN** 触发后验
- **THEN** 按台账拣货路径按巷道聚合计 80% 拣货量覆盖巷道数，落在 ≤N 为 `PASS`，否则为 `DEVIATION`

### Requirement: 偏离批次标记

系统必须在后验判定为偏离（`verify_result = DEVIATION`）时写入 `Deviation` 记录，登记不达标的作业单、物料与跨巷道数等成因，作为移库补救的任务来源。后验达标的作业单不得写入 `Deviation`。

#### Scenario: 偏离写入 Deviation
- **GIVEN** 一张作业单后验判定为偏离
- **WHEN** 后验完成
- **THEN** 系统写入一条 `Deviation` 记录

#### Scenario: 达标不写 Deviation
- **GIVEN** 一张作业单后验判定为达标（`verify_result = PASS`）
- **WHEN** 后验完成
- **THEN** 不产生 `Deviation` 记录

### Requirement: 冲正回冲

系统必须支持整单冲正：将原作业单状态置为 `VOID`，写入反向台账行（`is_reversal` 标记），并在同一事务内释放对应 cap 与减少库存。冲正不得创建反向作业单，也不得改写或删除历史台账；重新入库须按标准流程导入新订单、重新推荐、重新确认。冲正为写操作，须经二次确认，未确认不冲正。

#### Scenario: 冲正置 VOID 并回冲
- **GIVEN** 一张状态为 `EXECUTED` 的入库作业单，已写台账并占用了 cap
- **WHEN** 操作员发起冲正并通过二次确认
- **THEN** 原单置为 `VOID`，写入一条反向台账行（`is_reversal`），并同事务释放该单占用的 cap 与减少库存

#### Scenario: 冲正不创建反向作业单
- **GIVEN** 一张已 `EXECUTED` 的作业单
- **WHEN** 发起冲正
- **THEN** 系统中不新增反向 `JobOrder`，原单保持为唯一作业单并置 `VOID`

#### Scenario: 冲正不改写历史台账
- **GIVEN** 已写入的入库台账记录
- **WHEN** 该单被冲正
- **THEN** 原台账记录保持不变，仅追加一条反向台账行，不出现改写或删除

### Requirement: 作业单写操作端点

系统必须提供作业单写操作端点：`POST /api/job/batch/confirm`（批量确认）、`POST /api/job/{id}/reject`（驳回）、`POST /api/job/{id}/retry`（后验重试）、`POST /api/job/{id}/void`（冲正）。批量确认必须逐单独立提交，个别单失败回 `PLANNED` 且不影响同批其余单。

#### Scenario: 批量确认逐单独立事务
- **GIVEN** 一批多张 `PLANNED` 作业单，其中一张写台账失败
- **WHEN** 调用批量确认
- **THEN** 失败单回 `PLANNED`，其余单正常 `EXECUTED`，互不影响

#### Scenario: 驳回移出批量
- **GIVEN** 一张 `PLANNED` 作业单
- **WHEN** 操作员调用驳回端点
- **THEN** 该单迁移为 `REJECTED`，不产生台账

### Requirement: 台账与后验验证读

系统必须提供 `GET /api/ledger`（按作业单查询台账，含反向行 `is_reversal`）、`GET /api/verification/{job_id}`（查询某作业单的后验结果与 `verify_result`）与 `GET /api/deviation`（查询本仓偏离批次清单，作为移库补救的任务来源），供验证写路径是否正确落账、后验与偏离标记。

#### Scenario: 台账查询返回反向行
- **GIVEN** 一张作业单已冲正，存在一条正常台账行与一条反向行
- **WHEN** 查询该单台账
- **THEN** 返回两条记录，反向行带 `is_reversal` 标记

#### Scenario: 后验结果可查
- **GIVEN** 一张作业单后验完成
- **WHEN** 查询其后验
- **THEN** 返回 `verify_result` 与三口径指标值

#### Scenario: 偏离清单可查
- **GIVEN** 本仓存在若干后验判定为偏离的批次
- **WHEN** 查询偏离清单
- **THEN** 返回各偏离批次的物料 / 批号、实测 / 阈值跨巷道数与成因、状态

### Requirement: 顺路取只读派生

系统必须按巷道（库位号 `[:2]`）聚合系统自持库存视图（`version_no` 最大的快照 `InventoryItem` 分布，由台账增量维护）的既有库位/批次，为一张 `OUTBOUND` 作业单生成顺路取拣货顺序，输出拣货顺序 + 需遍历巷道数 + 加权集中度是否超标（80% 拣货量降序累加落在 ≤N 巷道，N=5 可配）。顺路取为只读派生，系统不得调用评分引擎（`engine.invoke`）、不得重新决定落位、不得写入 `InventoryItem` 或 `Ledger`。

#### Scenario: 按巷道聚合生成拣货顺序
- **GIVEN** 库存视图含某物料的既有库位/批次分布在若干巷道
- **WHEN** 为对应 `OUTBOUND` 作业单生成顺路取
- **THEN** 产出按巷道聚合的拣货顺序（每巷含拣货量与批号，`17` §10.2 形状），不触发评分引擎、不改变任何库存分布或台账

#### Scenario: 集中度超标高亮不阻断
- **GIVEN** 派生结果 80% 拣货量落在超过 N 个巷道（N=5）
- **WHEN** 生成顺路取
- **THEN** 方案标注 `exceeded = true` 并高亮，仍返回该方案，不拦截

#### Scenario: 顺路取不写库存分布与台账
- **GIVEN** 一次顺路取派生
- **WHEN** 检查 `InventoryItem` 与 `Ledger`
- **THEN** 不产生任何新增或变更记录（只读派生）

### Requirement: 批量顺路取接口

系统必须提供 `POST /api/job/batch/pick-sequence`，对一组 `OUTBOUND` 作业单批量生成顺路取方案（写入 `RecommendationPlan`，`plan_kind = PICK`，`payload_json` 为 `17` §10.2 形状），并把生成成功的作业单由 `PENDING` 迁移至 `PLANNED`。货未入库的品项（库存视图无对应分布）必须分列返回 `not_in_stock` 提示、不阻断同批其余单。幂等键 = `bulk_batch_no` × 库存视图版本：库存视图未变时重复生成返回既有方案，库存视图推进时重新派生。集中度超标（80% 落 >N 巷道）仅高亮、不阻断。

#### Scenario: 批量派生并迁 PLANNED
- **GIVEN** 一组状态为 `PENDING` 的 `OUTBOUND` 作业单
- **WHEN** 调用批量顺路取
- **THEN** 每张单生成顺路取方案（`plan_kind = PICK`）并迁移为 `PLANNED`

#### Scenario: 货未入库分列不阻断
- **GIVEN** 批量中部分品项在库存视图无对应分布
- **WHEN** 调用批量顺路取
- **THEN** 响应分列 `plans`（成功派生）与 `not_in_stock`（未入库提示），未入库单停留 `PENDING`，不阻断同批成功单

#### Scenario: 幂等命中返回既有方案
- **GIVEN** 同一 `bulk_batch_no` 已派生，且库存视图版本未变
- **WHEN** 重复调用批量顺路取
- **THEN** 返回既有方案，不重复写入 `RecommendationPlan`

#### Scenario: 库存视图推进重新派生
- **GIVEN** 同一 `bulk_batch_no` 已派生，且库存视图版本已推进（新快照导入）
- **WHEN** 再次调用批量顺路取
- **THEN** 基于新库存视图重新派生，而非返回旧方案

#### Scenario: 无快照阻断
- **GIVEN** 本仓无当前库存快照
- **WHEN** 调用批量顺路取
- **THEN** 阻断并提示重新导入快照，不猜测落位、不派生

### Requirement: 收拢方案只读派生

系统必须按**批号**聚合系统自持库存视图（`version_no` 最大的快照 `InventoryItem` 分布，由台账增量维护）的散落板，为一张 `RELOCATE` 作业单生成收拢方案：目标巷道 = 主巷道（该物料库存最集中的巷道，板数并列时按巷道号文本升序取最小），把该批号在非主巷道的散落板收拢回主巷道。收拢方案必须经三重校验：① 目标巷道 cap 充足 ② 批号不变（`batch_unchanged`）③ 收拢后同物料跨巷道数低于收拢前。收拢方案为本地规则，系统不得调用评分引擎（`engine.invoke`）、不得写入 `InventoryItem` 或 `Ledger`。

#### Scenario: 主巷道为最集中巷道且并列取升序
- **GIVEN** 库存视图含某物料分布在若干巷道，其中两个巷道板数并列最多
- **WHEN** 为该物料的某批号生成收拢方案
- **THEN** 目标巷道取板数最多的巷道，并列时按巷道号文本升序取最小

#### Scenario: 按批号聚合散落板
- **GIVEN** 某批号在非主巷道有散落板
- **WHEN** 生成收拢方案
- **THEN** `from_aisles` 为该批号在非主巷道的巷道集、`plates` 为其散落板总数、`batch_unchanged = true`

#### Scenario: 集中度改善才放行
- **GIVEN** 收拢后同物料跨巷道数不低于收拢前
- **WHEN** 生成收拢方案
- **THEN** 该单移出批量并记录降级原因，不产出方案

#### Scenario: 收拢方案不写库存分布与台账
- **GIVEN** 一次收拢方案派生
- **WHEN** 检查 `InventoryItem` 与 `Ledger`
- **THEN** 不产生任何新增或变更记录（本地规则，只读派生）

### Requirement: 批量收拢方案接口

系统必须提供 `POST /api/job/batch/relocate-plan`，对一组 `RELOCATE` 作业单批量生成收拢方案（写入 `RecommendationPlan`，`plan_kind = CONSOLIDATE`，`payload_json` 为 `17` §10.3 形状），并把生成成功的作业单由 `PENDING` 迁移至 `PLANNED`。目标巷道 cap 不足时必须降级到次选巷道（板数第二多）；次选仍不足则该单移出批量、记录降级原因（降级不静默）。幂等键 = `bulk_batch_no` × 库存视图版本：库存视图未变时重复生成返回既有方案，库存视图推进时重新派生。

#### Scenario: 批量派生并迁 PLANNED
- **GIVEN** 一组状态为 `PENDING` 的 `RELOCATE` 作业单
- **WHEN** 调用批量收拢方案
- **THEN** 每张单生成收拢方案（`plan_kind = CONSOLIDATE`）并迁移为 `PLANNED`

#### Scenario: 主巷道 cap 不足降级次选
- **GIVEN** 某单主巷道剩余容量不足以收拢该批号散落板
- **WHEN** 调用批量收拢方案
- **THEN** 目标巷道降级为次选巷道（板数第二多），方案记录降级原因

#### Scenario: 次选仍不足移出批量
- **GIVEN** 某单主巷道与次选巷道容量均不足
- **WHEN** 调用批量收拢方案
- **THEN** 该单移出批量，响应 `moved_out` 记录该单及降级原因，不产出方案、不迁状态

#### Scenario: 幂等命中返回既有方案
- **GIVEN** 同一 `bulk_batch_no` 已派生，且库存视图版本未变
- **WHEN** 重复调用批量收拢方案
- **THEN** 返回既有方案，不重复写入 `RecommendationPlan`

#### Scenario: 无快照阻断
- **GIVEN** 本仓无当前库存快照
- **WHEN** 调用批量收拢方案
- **THEN** 阻断并提示重新导入快照，不猜测落位、不派生

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
