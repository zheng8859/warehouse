## Why

阶段一交付的是**能装配、能启动的空骨架**：`app/models/` 下 10 个模块只有 docstring，23 个实体一个都没落地；
`app/api/permissions.py` 里 `ROLE_PERMISSIONS` 已是 `13` §2.2 的逐条搬运，但**零测试**；
`/api/auth/login` 在认证白名单内却尚无实现，任何角色都拿不到凭据。

阶段二把这两块补成**后续阶段可直接依赖的地基**。`26` 号把它排在最前，因为
`F1` 文件级数据导入的落库、`F2` 数据衔接契约配置的字段映射、`F7` 落位执行与台账对 `ledger.write` 的拦截，
全部长在这两个能力上 —— 它们分别是关键路径 `F1 → F2 → F9 → F3 → F4 → F5 → F6 → F7 → F8 → F10`
的第一、二环与第七环。地基不先定死字段口径与权限边界，后续四个阶段会各自臆造一套。

## What Changes

- 落地 `17` §2~§8 的 **23 个实体**为 SQLAlchemy 2.0 模型（`DeclarativeBase` / `Mapped`），按依赖链分 8 组 TDD 实现；
  表名 snake_case 复数、字段名 snake_case、全部实体携带 `warehouse_id` 过滤、**不引入 `is_deleted`**
- 建立 **Alembic 迁移**并独占真实库 schema；`scripts/init_db.py` 退化为「`upgrade head` + 开发种子」的便捷入口；
  测试用内存库 + `create_all`。规则：改模型必须带 migration，禁止 `create_all` 建真实库
- 统一 `AccountStatus` 口径：`models/identity.py` 的 docstring（照抄 `13` §5.1，含 `rejected`）与
  `core/enums.py` 的取值（照抄 `13` §5.2 / `17` §9，不含）此前互相矛盾。已按 `13` §5.1 的状态图订正
  `17` §9 与 `13` §5.2 —— `AccountStatus` 增加 `rejected` 取值（**枚举个数仍为 11**，改的是取值不是数量）
- 实现**无状态会话凭据**（JWT·HMAC-SHA256 自实现，8 小时）与 `/api/auth/login`；密码用 `bcrypt` 直接哈希
  （不用 passlib）；紧急吊销 = 置 `disabled`，下次请求校验即失败，不引入服务端黑名单
- 为 `ROLE_PERMISSIONS` 矩阵补**全组合测试**（4 角色 × 10 资源 × 操作），并把
  `ledger.write` / `engine.invoke` 恒为 `False` 这条红线固化为断言
- 版本化**命名三分**，避免三类语义被同一个 `version` 承载：`lock_version`（乐观锁，对用户不可见）/
  `version_no`（配置型业务版本，配 `生效时间`）/ `snapshot_id`（基线型 FK）
- `17` §9 表外的 **4 个实体局部值域**（`RecommendationPlan` 方案类型、`CapAlert` 告警类型与是否已处理、
  `Deviation` 状态与成因分类）定义在各自 model 模块，**不进 `core/enums.py`**
- 同步 `openspec/config.yaml` 中 `account_status` 的取值列表 —— 这是上述 `17` §9 订正的直接后果，
  该文件的 `context` 是产物写作约束，不得与事实来源漂移

## Capabilities

### New Capabilities

- `data-model`: 23 个实体的表结构、约束、索引、枚举归属、留存生命周期与 Alembic 迁移
- `auth`: 本地基础账号与无状态会话凭据 —— 登录、凭据签发与校验、失效与吊销路径
- `permission`: 角色权限矩阵、权限检查函数、认证中间件访问边界与免认证白名单

### Modified Capabilities

无。本项目尚无已归档规格（`openspec list --specs` 为空），本阶段是首批能力。

## 非目标

- **端点级资源鉴权（403）不实现**。`13` §6.2 明列 `PermissionChecker` 为路线图，v1 以「角色菜单可见性 +
  写操作二次确认」近似，并明确「后端端点的强制角色校验为路线图，避免 v1 过度工程」。本阶段交付矩阵数据与
  纯检查函数，**不给业务端点挂鉴权依赖**。
- **Evals / golden 数据集不实现**（属阶段六，文档 `30`）。`26` 完成标准 #9 的 `run_evals --tier l1` 在本阶段
  **显式豁免**：`evals/run_evals.py` 刻意以退出码 3 失败以防静默通过，提前实现会越界到阶段六的地盘。
- **前端页面与角色菜单可见性不做**（属阶段七，文档 `31`）。因此 `13` §3.1 的移库作业行勘误与合计数字错误
  **不阻塞本阶段**，但须在阶段七前修正 —— 否则会出现「矩阵给了权限、菜单却藏了页面」的死路。
- 6 因子评分引擎、批量分配、预留池与降级链（阶段三）；三类作业管线、文件导入管线、cap 全量重算与增量（阶段四）；
  冷路径 LLM 与对话台（阶段五/七）。
- 精细 RBAC（组织/角色/项目空间隔离，M5）、企业 SSO（M4）、`AuditLog` 审计日志（M6）—— 均为路线图；
  `13` §4.4 的审计字段契约仅作后续实现契约备查，本阶段不建表。
- 软删除、多 worker 并发写、Redis/RabbitMQ 等中间件 —— 触碰红线，不得加回。

## Impact

**实体**（23 个，按 `17` 四条数据链）：

- 主数据链（6）：`Warehouse` / `Aisle` / `Location` / `AisleStation` / `Material` / `Batch`
- 衔接链（5）：`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert`
- 作业链（5）：`JobOrder` / `RecommendationPlan` / `Ledger` / `Verification` / `Deviation`
- 度量（1）：`KpiSnapshot` · 身份链（1）：`Account` · 配置（5）：`WeightConfig` / `CapacityConfig` /
  `FieldMappingConfig` / `PromptTemplate` / `ConversationContext`

> 分组以 `17` 号数据链为准。`26` 附录A 把 `AisleCap` 归入「主数据」、`Material`/`Batch` 单列一组，
> 与 `17` §3「衔接链：`ImportSession` → `Snapshot` → `InventoryItem` / `AisleCap`」不符，属该附录的笔误，
> 不作为分组依据。

**API 路由**：

- 新增 `/api/auth/login`（免认证白名单内）；凭据失效返回 401
- 既有 `/health`、`/docs`、`/openapi.json` 保持免认证；`/api/health` 保持需认证
  （`13` §6.1 白名单未含它，探活请用 `/health` —— 这个已标记的文档不一致继续按 `13` 字面执行）
- 本阶段**不新增**受权保护的业务端点，故不产生 403 路径

**KPI 指标**：本阶段**不产出任何 KPI 口径**（集中度、同物料/同批跨巷道数等属阶段三以后）。
`KpiSnapshot` 仅落地表结构，不含计算逻辑。

**依赖与配置**：

- `alembic` 已在 `backend/requirements.txt` 中，本阶段首次实际使用；需新增 `alembic.ini` 与 `migrations/`
- `openspec/config.yaml` 的 `account_status` 取值列表需同步补 `rejected`（见 What Changes 末条）
- 事实来源文档已订正两处（`17` §9、`13` §5.2），备份留在 `产品设计/*.md.20260911-143242.bak`

**受影响的既有代码**：`app/core/enums.py`（补 `AccountStatus.REJECTED`）、`app/models/*.py`（10 个占位模块转实现）、
`app/api/permissions.py`（补测试，矩阵本身不改）、`app/main.py`（若需挂载 auth 路由）。
