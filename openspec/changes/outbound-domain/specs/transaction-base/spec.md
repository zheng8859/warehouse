## ADDED Requirements

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

## MODIFIED Requirements

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
