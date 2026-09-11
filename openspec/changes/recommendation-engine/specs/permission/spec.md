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
