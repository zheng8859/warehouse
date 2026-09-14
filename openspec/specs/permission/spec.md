# permission Specification

## Purpose

定义 4 角色 × 10 资源的授权事实、自动写权限的封闭性、写操作二次确认要求，以及全站访问边界与免认证白名单。

## Requirements

### Requirement: 角色权限矩阵

系统必须按 `13` §2.2 逐条实现 4 角色（`warehouse_keeper` / `planner` / `supervisor` / `admin`）× 10 资源（`account` / `data_import` / `inbound` / `outbound` / `relocate` / `kpi` / `config` / `conversation` / `ledger` / `engine`）× 操作的授权事实，并以下列边界为准：`planner` 对 `data_import` 仅 `view`、对入/出/移库作业无任何权限；`supervisor` 对 `inbound` 无任何权限、对 `outbound` 仅 `view`、对 `relocate` 具 `view` 与 `operate`；`admin` 拥有全部非自动权限。

#### Scenario: 主管无入库权限
- **GIVEN** 当前角色为 `supervisor`
- **WHEN** 查询其是否具有 `inbound.view`
- **THEN** 结果为无权限

#### Scenario: 主管对出库仅查看不得执行
- **GIVEN** 当前角色为 `supervisor`
- **WHEN** 分别查询 `outbound.view` 与 `outbound.operate`
- **THEN** `view` 有权限，`operate` 无权限

#### Scenario: 计划员不执行作业
- **GIVEN** 当前角色为 `planner`
- **WHEN** 查询入/出/移库作业的 `operate` 权限
- **THEN** 三项均为无权限

#### Scenario: 全组合逐条断言
- **GIVEN** 4 角色 × 10 资源的全部权限组合
- **WHEN** 逐一求值
- **THEN** 每一组合的判定结果与 `13` §2.2 矩阵逐条一致

### Requirement: 自动写权限封闭

`ledger.write` 与 `engine.invoke` 必须对所有角色恒为不可用 —— 任何角色都不得持有这两项资源。系统不得以 `ledger` 或 `engine` 为**目标资源**暴露面向业务角色的手动接口，即不得存在「直接写台账」或「直接调用引擎」这样独立的手动入口。

引擎的调用只能作为**业务动作的内部后果**发生：其授权依据是该业务动作的资源标识（`POST /api/allocate/batch` 的依据为 `inbound.operate`），不因触发一次业务动作而构成一次 `engine.invoke` 的授予，也不得据此对该端点施加面向 `engine` 的资源级鉴权。台账写入同理，只能由作业流 / 引擎在业务动作内产生。

#### Scenario: 四角色均无台账写权限
- **GIVEN** 四个角色逐一接受检查
- **WHEN** 查询其 `ledger.write` 权限
- **THEN** 四者结果均为不可用

#### Scenario: 引擎调用无手动入口
- **GIVEN** 全部 4 角色
- **WHEN** 查询其 `engine.invoke` 权限
- **THEN** 四者结果均为不可用，且不存在以 `engine` 为目标资源、面向业务角色的可调用端点

#### Scenario: 业务动作间接触发引擎不算手动入口
- **GIVEN** 当前角色持有 `inbound.operate` 且不持有 `engine.invoke`
- **WHEN** 该角色调用 `POST /api/allocate/batch` 触发当日队列的批量分配
- **THEN** 请求被接受，其授权依据为 `inbound.operate`；`engine.invoke` 仍为不可用，且该次调用不产生任何 `engine.invoke` 的授予

### Requirement: 配置子模块受限

`config` 资源的变更权限必须按子模块受限：`admin` 可变更全部 5 个子模块（导入模板 / cap 口径 / 评分因子权重 / 近站台预留比例 / 对话指令词）；`supervisor` 仅可变更**权重**子模块，其余 4 个必须拒绝；`warehouse_keeper` 与 `planner` 必须对 `config` 无任何权限。

#### Scenario: 主管仅可改权重
- **GIVEN** 当前角色为 `supervisor`
- **WHEN** 分别以权重与 cap 口径为作用域请求变更配置
- **THEN** 权重被接受，cap 口径被拒绝

#### Scenario: 仓管员与计划员无配置权限
- **GIVEN** 角色分别为 `warehouse_keeper` 与 `planner`
- **WHEN** 查询其 `config.view` 与 `config.configure`
- **THEN** 两者均无权限

### Requirement: 写操作二次确认

所有写操作（落位确认、出库拣配确认、移库确认、配置变更、账号管理）必须经二次确认后执行，未确认的操作不得落库、不得写台账。系统不得静默接管落位决策，也不得在未确认的情况下产生台账记录。

#### Scenario: 未确认不产生台账
- **GIVEN** 一份已生成的落位方案
- **WHEN** 操作员未提交确认
- **THEN** 不产生台账记录，作业单状态不变

#### Scenario: 确认失败不触发引擎
- **GIVEN** 操作员提交确认但确认校验失败
- **WHEN** 处理该请求
- **THEN** 不写台账、不触发引擎，状态回滚至确认前

### Requirement: 访问边界与免认证白名单

认证必须覆盖全部 `/api/*` 请求。系统必须仅放行以下恰好 4 条免认证路径：`/api/auth/login`、`/health`、`/docs`、`/openapi.json`。未携带或携带无效凭据访问 `/api/*` 必须返回 401，不得返回 404（不泄露路径是否存在）。`/api/health` 必须保持需认证。

#### Scenario: 白名单恰好四条
- **GIVEN** 免认证白名单已实现
- **WHEN** 逐条比对
- **THEN** 恰好为 `/api/auth/login`、`/health`、`/docs`、`/openapi.json`

#### Scenario: 未知 API 路径返回 401 而非 404
- **GIVEN** 未携带凭据
- **WHEN** 请求一个不存在的 `/api/*` 路径
- **THEN** 返回 401

#### Scenario: /api/health 需认证
- **GIVEN** 未携带凭据
- **WHEN** 请求 `/api/health`
- **THEN** 返回 401

### Requirement: v1 端点级资源鉴权覆盖方案生成端点

v1 阶段对**方案生成端点**施加端点级资源鉴权（403）：`POST /api/allocate/batch` 依据 `inbound.operate`（仅仓管员/管理员可调用）、`POST /api/job/batch/pick-sequence` 依据 `outbound.operate`（仅仓管员/管理员可调用）、`POST /api/job/batch/relocate-plan` 依据 `relocate.operate`（仅仓管员/主管/管理员可调用），其余角色返回 403。确认类写端点（`POST /api/job/batch/confirm`、`POST /api/job/{id}/reject`、`POST /api/job/{id}/retry`、`POST /api/job/{id}/void`）不施加端点级资源鉴权 —— 二次确认卡即操作员确认入口，越权拦截由角色菜单可见性与二次确认承担。`13` §6.2 已将该检查器列为路线图，v1 交付方案生成端点这一批实例。

#### Scenario: 分配端点按 inbound.operate 鉴权
- **GIVEN** 四个角色逐一持凭据调用 `POST /api/allocate/batch`
- **WHEN** 检查返回码
- **THEN** 仓管员与管理员返回 200，计划员与主管返回 403

#### Scenario: 顺路取端点按 outbound.operate 鉴权
- **GIVEN** 四个角色逐一持凭据调用 `POST /api/job/batch/pick-sequence`
- **WHEN** 检查返回码
- **THEN** 仓管员与管理员返回 200，计划员与主管返回 403

#### Scenario: 收拢方案端点按 relocate.operate 鉴权
- **GIVEN** 四个角色逐一持凭据调用 `POST /api/job/batch/relocate-plan`
- **WHEN** 检查返回码
- **THEN** 仓管员、主管与管理员返回 200，计划员返回 403

#### Scenario: 其余业务端点不挂鉴权依赖
- **GIVEN** 本阶段交付的确认类写端点（`batch/confirm`、`reject`、`retry`、`void`）
- **WHEN** 检查其依赖链
- **THEN** 不含资源级鉴权依赖，越权拦截由角色菜单可见性与二次确认承担
