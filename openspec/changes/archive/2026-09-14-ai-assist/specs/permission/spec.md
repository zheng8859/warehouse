# permission

## MODIFIED Requirements

### Requirement: 角色权限矩阵

系统必须按 `13` §2.2 逐条实现 4 角色（`warehouse_keeper` / `planner` / `supervisor` / `admin`）× 11 资源（`account` / `data_import` / `inbound` / `outbound` / `relocate` / `kpi` / `config` / `conversation` / `ledger` / `engine` / `ai`）× 操作的授权事实，并以下列边界为准：`planner` 对 `data_import` 仅 `view`、对入/出/移库作业无任何权限；`supervisor` 对 `inbound` 无任何权限、对 `outbound` 仅 `view`、对 `relocate` 具 `view` 与 `operate`；`admin` 拥有全部非自动权限；`ai` 资源含 4 个动作 `ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`，其中 `ai.assist` / `ai.weight.update` / `ai.relocate.propose` 仅授予仓管员 / 主管 / 管理员（计划员无），`ai.toggle` 仅授予管理员。

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

#### Scenario: 计划员无 ai 建议权限
- **GIVEN** 当前角色为 `planner`
- **WHEN** 查询 `ai.assist` / `ai.weight.update` / `ai.relocate.propose`
- **THEN** 三项均为无权限

#### Scenario: ai.toggle 仅管理员
- **GIVEN** 依次以仓管员 / 主管 / 管理员身份调用 `POST /api/llm/toggle`
- **WHEN** 检查返回码
- **THEN** 仓管员与主管返回 403，管理员返回 200

#### Scenario: 全组合逐条断言
- **GIVEN** 4 角色 × 11 资源的全部权限组合
- **WHEN** 逐一求值
- **THEN** 每一组合的判定结果与 `13` §2.2 矩阵逐条一致

## ADDED Requirements

### Requirement: 冷路径端点级资源鉴权

系统必须对冷路径端点施加端点级资源鉴权（403）：`POST /api/llm/kpi/interpret`、`POST /api/llm/deviation/attribute`、`POST /api/llm/weight/tune` 依据 `ai.assist`（仅仓管员/主管/管理员）；`POST /api/llm/weight/apply` 依据 `ai.weight.update`（仅仓管员/主管/管理员）；`POST /api/llm/relocate/propose` 依据 `ai.relocate.propose`（仅仓管员/主管/管理员）；`POST /api/llm/toggle` 依据 `ai.toggle`（仅管理员）。其余角色返回 403。

#### Scenario: 计划员调用 ai 建议端点被拒
- **GIVEN** 以 `planner` 角色调用 `POST /api/llm/kpi/interpret`
- **WHEN** 检查返回码
- **THEN** 返回 403（`permission_denied`）

#### Scenario: 非管理员切换开关被拒
- **GIVEN** 以 `warehouse_keeper` 角色调用 `POST /api/llm/toggle`
- **WHEN** 检查返回码
- **THEN** 返回 403（`permission_denied`）

#### Scenario: 授权角色调用成功
- **GIVEN** 以 `supervisor` 角色调用 `POST /api/llm/deviation/attribute`
- **WHEN** 冷路径已开启且护栏未熔断
- **THEN** 返回 200（`rule` 恒有，`ai` 视护栏而定）
