## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: v1 不做端点级资源鉴权

本阶段系统不得在业务端点施加资源级鉴权（403）以替代二次确认与角色约束。`13` §6.2 已将该检查器列为路线图，v1 仅交付矩阵数据与纯检查函数。

#### Scenario: 业务端点不挂鉴权依赖
- **GIVEN** 本阶段交付的全部业务端点
- **WHEN** 检查其依赖链
- **THEN** 不含资源级鉴权依赖，越权拦截由角色菜单可见性与二次确认承担

## ADDED Requirements

### Requirement: v1 端点级资源鉴权仅限分配端点

v1 阶段只在 `POST /api/allocate/batch` 施加端点级资源鉴权（403），依据为 `inbound.operate`：仅仓管员/管理员可调用，计划员/主管返回 403。其余业务端点不得施加资源级鉴权（403）以替代二次确认与角色约束 —— `13` §6.2 已将该检查器列为路线图，v1 交付第一个端点级实例，其余端点仍由角色菜单可见性与二次确认承担。

#### Scenario: 分配端点按 inbound.operate 鉴权
- **GIVEN** 四个角色逐一持凭据调用 `POST /api/allocate/batch`
- **WHEN** 检查返回码
- **THEN** 仓管员与管理员返回 200，计划员与主管返回 403

#### Scenario: 其余业务端点不挂鉴权依赖
- **GIVEN** 本阶段交付的除分配外的业务端点
- **WHEN** 检查其依赖链
- **THEN** 不含资源级鉴权依赖，越权拦截由角色菜单可见性与二次确认承担
