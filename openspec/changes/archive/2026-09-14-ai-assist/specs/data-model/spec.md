# data-model

## MODIFIED Requirements

### Requirement: 实体清单与数据链分组

系统必须持久化 26 个实体：其中 23 个由 `17` 号定义并按四条数据链归属 —— 主数据链 6 个（`Warehouse` / `Aisle` / `Location` / `AisleStation` / `Material` / `Batch`）、衔接链 5 个（`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert`）、作业链 5 个（`JobOrder` / `RecommendationPlan` / `Ledger` / `Verification` / `Deviation`）、度量 1 个（`KpiSnapshot`）、身份链 1 个（`Account`）、配置 5 个（`WeightConfig` / `CapacityConfig` / `FieldMappingConfig` / `PromptTemplate` / `ConversationContext`）；另 3 个为冷路径链（`AiSuggestion` / `ConversationLog` / `AiCostQuota`，由 `10` 号 / `29` 号定义）。系统不得新增平行台账类实体。

#### Scenario: 实体总数与分组计数
- **GIVEN** 数据库完成建表
- **WHEN** 统计已建表数量并按数据链归组
- **THEN** 共 26 张表，分组计数依次为 6 / 5 / 5 / 1 / 1 / 5 / 3

#### Scenario: 拒绝新增平行台账
- **GIVEN** 某后续变更尝试新增第二张台账类表
- **WHEN** 审查该变更的实体清单
- **THEN** 该变更被拒绝 —— 台账只有一套（`Ledger`）

#### Scenario: 冷路径实体非台账
- **GIVEN** 冷路径链 3 个实体已建表
- **WHEN** 核对 `AiSuggestion` / `ConversationLog` / `AiCostQuota` 的写入路径
- **THEN** 三者均不进入 `Ledger` 台账链，`AiSuggestion` 只读、`AiCostQuota` 仅配额记账
