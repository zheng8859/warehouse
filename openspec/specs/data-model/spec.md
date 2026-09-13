# data-model Specification

## Purpose

定义 23 个实体的持久化契约：字段口径、代理键与业务键、状态迁移、版本化语义与留存生命周期，作为后续所有能力取数与落库的单一事实来源。

## Requirements

### Requirement: 实体清单与数据链分组

系统必须持久化 `17` 号定义的 23 个实体，并按四条数据链归属：主数据链 6 个（`Warehouse` / `Aisle` / `Location` / `AisleStation` / `Material` / `Batch`）、衔接链 5 个（`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert`）、作业链 5 个（`JobOrder` / `RecommendationPlan` / `Ledger` / `Verification` / `Deviation`）、度量 1 个（`KpiSnapshot`）、身份链 1 个（`Account`）、配置 5 个（`WeightConfig` / `CapacityConfig` / `FieldMappingConfig` / `PromptTemplate` / `ConversationContext`）。系统不得新增平行台账类实体。

#### Scenario: 实体总数与分组计数
- **GIVEN** 数据库完成建表
- **WHEN** 统计已建表数量并按数据链归组
- **THEN** 共 23 张表，分组计数依次为 6 / 5 / 5 / 1 / 1 / 5

#### Scenario: 拒绝新增平行台账
- **GIVEN** 某后续变更尝试新增第二张台账类表
- **WHEN** 审查该变更的实体清单
- **THEN** 该变更被拒绝 —— 台账只有一套（`Ledger`）

### Requirement: 代理键、业务键与无软删除

系统必须为每个实体使用整型自增 `id` 作代理键，并对其业务键建立唯一约束：库存行唯一键 = 库位号 + 批号 + 料号；PO/DO 行唯一键 = 单据号码 + 行号。系统不得引入 `is_deleted` 之类软删除列，也不得提供物理删除接口。

#### Scenario: 业务键冲突被拒绝
- **GIVEN** 已存在库位号 `010104` + 批号 `B001` + 料号 `M001` 的库存行
- **WHEN** 写入同一组合的第二条记录
- **THEN** 写入被唯一约束拒绝，不产生第二条记录

#### Scenario: 表结构不含软删除列
- **GIVEN** 23 张表已建
- **WHEN** 检查每张表的列名
- **THEN** 不存在 `is_deleted` 或 `deleted_at` 列

### Requirement: 全实体 warehouse_id 隔离

系统必须让全部 23 个实体携带 `warehouse_id` 并在查询时按其过滤；首期取值固定 `GTJ10036`。

#### Scenario: 查询默认过滤到试点厂
- **GIVEN** 库中存在 `warehouse_id` 为 `GTJ10036` 的库位记录
- **WHEN** 通过应用层查询库位
- **THEN** 结果仅含 `GTJ10036` 的记录

### Requirement: 库位号按 6 位文本处理

系统必须将库位号按 6 位文本读取与存储，巷道取前 2 位（`010104` → 巷道 `01`），所有巷道级聚合必须对该文本切片 `[:2]` 得到。系统不得将库位号数值化，也不得按列序号硬取字段。

#### Scenario: 前导 0 不丢失
- **GIVEN** 源文件中某库位号写作 `010104`
- **WHEN** 导入并落库
- **THEN** 存储值为字符串 `010104`，长度为 6，首字符为 `0`

#### Scenario: 巷道聚合按前 2 位切片
- **GIVEN** 库位号为 `010104`
- **WHEN** 计算其所属巷道
- **THEN** 结果为 `01`

### Requirement: 枚举登记范围与取值

系统必须以 11 个跨模块共享枚举为唯一登记范围：`job_type` / `job_status` / `import_status` / `file_type` / `ledger_type` / `disposition` / `abc_class` / `role` / `account_status` / `verify_result` / `item_status`。其中 `account_status` 取值必须为 `pending` / `active` / `disabled` / `rejected`。实体局部值域（`RecommendationPlan` 的方案类型、`CapAlert` 的告警类型与是否已处理、`Deviation` 的状态与成因分类）必须定义在各自实体所在模块，不得进入该共享登记表。

#### Scenario: 共享枚举个数为 11
- **GIVEN** 枚举登记表已实现
- **WHEN** 统计其登记的枚举类型数
- **THEN** 恰好 11 个

#### Scenario: account_status 接受 rejected
- **GIVEN** 一个状态为 `pending` 的账号
- **WHEN** 管理员驳回该账号申请
- **THEN** 账号状态置为 `rejected`，该取值是合法枚举值而非自由字符串

#### Scenario: 局部值域不进入共享登记表
- **GIVEN** 枚举登记表已实现
- **WHEN** 查询其中是否登记方案类型、告警类型、`Deviation` 状态
- **THEN** 三者均不在共享登记表中

### Requirement: 版本语义三分

系统必须用三个互不混用的列承载三类版本语义：`lock_version`（整数，乐观锁，对用户不可见）、`version_no`（整数，配置型业务版本，与生效时间同用）、`snapshot_id`（外键，指向不可变基线）。系统不得用同一个 `version` 列承载两种以上语义。

#### Scenario: 配置版本按生效时间取当前值
- **GIVEN** `WeightConfig` 存在版本 1（生效 08:00）与版本 2（生效 18:00）
- **WHEN** 在 20:00 读取当前 6 因子权重
- **THEN** 返回版本 2，且版本 1 仍然保留可回滚

#### Scenario: 乐观锁列不与业务版本共用
- **GIVEN** `ImportSession` 同时具有并发控制与业务版本需求
- **WHEN** 检查其表结构
- **THEN** 并发控制与业务版本分别落在独立列上

### Requirement: 乐观锁并发守卫

`JobOrder` 与 `ImportSession` 必须以 `lock_version` 实现乐观锁。多端同时确认同一作业单时，系统必须拒绝后到的写入，不得静默覆盖先到的写入。服务端必须单进程运行，不得以多 worker 并发写。

#### Scenario: 并发确认仅一方成功
- **GIVEN** 同一 `JobOrder` 被两端同时读到 `lock_version = 3`
- **WHEN** 两端先后提交确认
- **THEN** 先提交者成功并推进版本，后提交者被拒绝并提示重新读取

#### Scenario: EXECUTED 后不重复写台账
- **GIVEN** 某作业单状态已为 `EXECUTED`
- **WHEN** 再次提交该单的落位确认
- **THEN** 被状态机守卫拒绝，不产生第二条台账记录

### Requirement: 不可变快照基线与 cap 引用

`Snapshot` 必须以追加方式版本化：每次导入建立新基线，旧版本归档保留，系统不得覆盖或删除历史基线。`AisleCap` 必须通过 `snapshot_id` 引用其来源基线。`CapAlert` 必须引用快照或台账事务**恰一个** —— 两个引用列中恰好一个非空。

#### Scenario: 新快照不覆盖旧基线
- **GIVEN** 已存在 `snapshot_id = 1` 的基线快照
- **WHEN** 导入新快照生成 `snapshot_id = 2`
- **THEN** 基线 1 仍然完整可读，`snapshot_id = 2` 成为当前基线

#### Scenario: CapAlert 引用恰好一个来源
- **GIVEN** 一条容量告警
- **WHEN** 检查其 `snapshot_id` 与台账事务号
- **THEN** 恰好一个非空；两个都空或都非空的写入被拒绝

### Requirement: JobOrder 状态机

`JobOrder` 状态必须限于 `PENDING` / `PLANNED` / `CONFIRMED` / `REJECTED` / `CANCELLED` / `EXECUTED` / `VERIFYING` / `VERIFIED` / `VERIFY_FAILED` / `VOID` 十值，迁移必须限于：`PENDING → PLANNED`、`PENDING → PENDING`（批量分配失败可重试）、`PENDING → CANCELLED`、`PLANNED → CONFIRMED`、`PLANNED → REJECTED`、`PLANNED → CANCELLED`、`REJECTED → PENDING`、`CONFIRMED → EXECUTED`（写台账成功）、`CONFIRMED → PLANNED`（写台账失败或回滚）、`EXECUTED → VERIFYING`（台账写入成功后自动触发后验）、`VERIFYING → VERIFIED`（后验完成，达标/偏离由 `Verification` 标记）、`VERIFYING → VERIFY_FAILED`（后验失败或超时）、`VERIFY_FAILED → VERIFYING`（重试后验）、`EXECUTED → VOID`（冲正）、`VERIFIED → VOID`（冲正）。系统不得新增未定义状态，也不得放行未列出的迁移；`VERIFY_FAILED` 仅提供重试与告警，不提供「放弃后验」终态。

#### Scenario: 合法迁移被接受
- **GIVEN** 作业单状态为 `PLANNED`
- **WHEN** 操作员确认该单
- **THEN** 状态迁移为 `CONFIRMED`

#### Scenario: 未定义迁移被拒绝
- **GIVEN** 作业单状态为 `PENDING`
- **WHEN** 尝试直接迁移到 `EXECUTED`
- **THEN** 迁移被拒绝，状态保持 `PENDING`

#### Scenario: 后验拆为两段
- **GIVEN** 作业单状态为 `EXECUTED` 且台账已写入
- **WHEN** 触发后验
- **THEN** 状态先迁移为 `VERIFYING`，后验完成后迁移为 `VERIFIED`（达标记 `PASS`、偏离记 `DEVIATION`）

#### Scenario: 冲正置 VOID
- **GIVEN** 作业单状态为 `EXECUTED` 或 `VERIFIED`
- **WHEN** 发起冲正
- **THEN** 状态迁移为 `VOID`，成为终态

### Requirement: 留存生命周期与归档不删除

系统必须按以下生命周期保留数据：`Ledger` / `Verification` / `Deviation` 永久保留；`Snapshot` 至少 90 天；导入原文件至少 30 天。系统不得提供删除历史台账或历史基线的接口 —— 归档不删除，回滚以功能开关实现而非删除数据。

#### Scenario: 超期快照仍无删除入口
- **GIVEN** 存在建立时间超过 90 天的快照
- **WHEN** 检查系统是否暴露删除该快照的接口
- **THEN** 不存在此类接口

#### Scenario: 台账无删除入口
- **GIVEN** 已写入的入库台账记录
- **WHEN** 检查系统是否暴露删除台账的接口
- **THEN** 不存在此类接口
