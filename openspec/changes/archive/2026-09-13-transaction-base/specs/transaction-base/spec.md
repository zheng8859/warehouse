## Purpose

定义三类作业（入库 / 出库 / 移库）交易底座的端到端行为契约：从 `PLANNED` 方案经人工确认 L1 落位执行、同事务写台账与 cap 增量、同步后验判定达标或偏离、以及冲正回冲，保证台账是 cap 与库存分布增量的唯一来源。

## ADDED Requirements

### Requirement: 三类作业确认与落位执行

系统必须在操作员对 `PLANNED` 作业单逐单确认（L1 软推荐，决策权在人）后，才将其迁移到 `CONFIRMED`，并在台账写入成功后迁移到 `EXECUTED`；台账写入失败或事务回滚时，作业单必须回退到 `PLANNED`（确认作废，须重新确认）。系统不得在未经操作员确认时自动落位或产生台账（未确认不产生台账）。

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

系统必须在台账写入成功后自动触发后验，作业单经 `VERIFYING` 迁移至 `VERIFIED` 或 `VERIFY_FAILED`。后验按作业类型判定：入库 = 同物料跨巷道数 ≤5 且 同批跨巷道数 ≤3；出库 = 拣货量加权集中度 80% 落在 ≤N 巷道（N=5，可配）；移库 = 移库后同物料跨巷道数低于移库前。后验计算完成（无论达标与否）迁移为 `VERIFIED`，由 `Verification` 以 `verify_result = PASS / DEVIATION` 标记；后验计算失败或超时迁移为 `VERIFY_FAILED`，仅提供重试与告警，不提供「放弃后验」终态。

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
