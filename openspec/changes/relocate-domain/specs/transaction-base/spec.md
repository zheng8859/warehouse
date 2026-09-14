## ADDED Requirements

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
