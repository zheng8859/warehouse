# auth Specification

## Purpose

定义本地基础账号与无状态会话凭据的契约：账号开通与状态迁移、凭据签发与校验、失效与紧急吊销路径。

## Requirements

### Requirement: 账号由管理员开通

系统必须仅允许 `admin` 角色开通、激活、停用与重新启用账号。`warehouse_keeper` / `planner` / `supervisor` 触发账号创建或停用端点时必须返回 403。

#### Scenario: 管理员开通账号
- **GIVEN** 当前会话角色为 `admin`
- **WHEN** 提交创建账号请求并指定角色与用户名
- **THEN** 账号创建成功，初始状态为 `pending`

#### Scenario: 非管理员被拒
- **GIVEN** 当前会话角色为 `warehouse_keeper`
- **WHEN** 提交创建账号请求
- **THEN** 返回 403，且不产生账号记录

### Requirement: 账号状态迁移

账号状态必须限于 `pending` / `active` / `disabled` / `rejected` 四值，迁移必须限于：`pending → active`、`pending → rejected`、`active → disabled`、`disabled → active`。系统不得新增未定义状态，也不得放行未列出的迁移。

#### Scenario: 驳回待激活账号
- **GIVEN** 账号状态为 `pending`
- **WHEN** 管理员驳回该申请
- **THEN** 状态迁移为 `rejected`

#### Scenario: 停用后可重新启用
- **GIVEN** 账号状态为 `disabled`
- **WHEN** 管理员重新启用
- **THEN** 状态迁移为 `active`

#### Scenario: 已驳回账号不得直接激活
- **GIVEN** 账号状态为 `rejected`
- **WHEN** 尝试将其迁移为 `active`
- **THEN** 迁移被拒绝，状态保持 `rejected`

### Requirement: 初始密码与首次强制改密

账号激活时必须生成初始凭据并由管理员线下发放，用户首次登录必须强制修改密码。密码必须以不可逆哈希存储，系统不得以明文或可逆方式存储密码，也不得在响应中回显密码或哈希。

#### Scenario: 首次登录强制改密
- **GIVEN** 账号已激活且初始密码尚未修改
- **WHEN** 用户以初始密码登录
- **THEN** 登录被接受，但改密前不得访问其他业务端点

#### Scenario: 响应不回显密码
- **GIVEN** 任意账号相关接口
- **WHEN** 读取其响应体
- **THEN** 不含密码明文，也不含密码哈希

### Requirement: 无状态会话凭据

系统必须签发无状态会话凭据，有效期 8 小时。凭据必须携带 `user_id` / `role` / `status` / `iat` / `exp`，并携带 `warehouse_id`（首期固定 `GTJ10036`）。系统不得依赖服务端会话存储或凭据黑名单。

#### Scenario: 凭据在有效期内可用
- **GIVEN** 用户在 08:00 登录取得凭据
- **WHEN** 在 15:00 携带该凭据请求受保护端点
- **THEN** 请求被接受

#### Scenario: 超过 8 小时后失效
- **GIVEN** 用户在 08:00 登录取得凭据
- **WHEN** 在 16:01 携带该凭据请求受保护端点
- **THEN** 返回 401

#### Scenario: 签名被篡改的凭据被拒绝
- **GIVEN** 一份有效凭据
- **WHEN** 篡改其载荷后提交
- **THEN** 返回 401

### Requirement: 紧急吊销

系统必须支持通过将账号状态置为 `disabled` 紧急吊销其访问：该账号凭据在下一次请求校验时即失效，且无需重启服务。登出必须仅清理客户端凭据，不要求服务端记录。

#### Scenario: 停用后旧凭据立即失效
- **GIVEN** 某账号持有一份尚未过期的凭据
- **WHEN** 管理员将该账号置为 `disabled` 后该账号再次请求
- **THEN** 返回 401，无需重启服务

#### Scenario: 登出不产生服务端记录
- **GIVEN** 用户已登录
- **WHEN** 用户执行登出
- **THEN** 客户端清除凭据并跳转登录页，服务端不保留吊销记录

### Requirement: 登录端点与失败语义

`/api/auth/login` 必须免认证可访问。凭据无效、缺失、过期或签名错误时，系统必须一律返回 401，且不得区分失败原因。

#### Scenario: 登录端点免认证
- **GIVEN** 未携带任何凭据
- **WHEN** 以合法用户名与密码请求 `/api/auth/login`
- **THEN** 返回 200 与凭据

#### Scenario: 失败原因不区分
- **GIVEN** 一个不存在的用户名，与一个存在但密码错误的账号
- **WHEN** 分别提交登录
- **THEN** 两次响应均为 401 且响应体一致，不泄露账号是否存在
