## RENAMED Requirements

FROM: ### Requirement: v1 端点级资源鉴权仅限分配端点
TO: ### Requirement: v1 端点级资源鉴权覆盖方案生成端点

## MODIFIED Requirements

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
