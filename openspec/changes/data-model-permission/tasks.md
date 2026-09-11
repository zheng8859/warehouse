# 实现任务 —— 阶段二：数据模型与权限体系

## 0. 适用性说明（先读）

`openspec/config.yaml` 的 tasks 规则共 6 条，其中 3 条为**全项目**口径，本阶段作为纯地基阶段不适用。
逐条声明如下，避免静默跳过：

| 规则 | 本阶段处置 |
|---|---|
| 1 按关键路径 `F1 → … → F10` 排序 | 本阶段**不在该路径上**，而是它的前置 —— `26` 号明确「26 必须最先」。故按**依赖链**排序（主数据 → 衔接 → 作业 → 度量/身份 → 配置），最终通向 `F1`（第 1 环）与 `F7`（第 7 环） |
| 2 每个任务标注文件/模块 + 验证方式（对应评测场景编号） | 文件/模块与验证方式**逐条标注**。**评测场景编号不标**：`SC-`/`CL-`/`AC-`/`TR-` 系列属 `20` 号，其落地平台 `evals/` 归阶段六；本阶段验证以 spec 的需求名指代，阶段六再建编号映射 |
| 3 必须包含基线层评测场景落地任务（评分正确性等） | **不适用** —— 评分正确性属阶段三，KPI 计量属阶段四。本阶段无评分代码可测 |
| 4 必须包含异常/边界任务 | **部分适用**：`乐观锁并发冲突` 在范围内（第 1.6、3.1 任务）；`导入校验失败阻断`、`cap 不足降级`、`快照过期阻断` 属阶段三/四，不在本阶段 |
| 5 必须包含后验与 `KpiSnapshot` 聚合实现 | **不适用** —— 后验属阶段四。本阶段只为 `KpiSnapshot` 建表（4.1），不实现聚合口径 |
| 6 单个任务 ≤2 天 | 适用，全部任务按此拆分 |

验证方式统一约定：`pytest` 命令 + 期望结果。测试落在三层 TDD 的对应层 —— 模型 CRUD/约束进 `tests/models/`，
端点行为进 `tests/api/`，状态机/乐观锁等纯逻辑进 `tests/logic/`。

## 1. 前置与基础设施

- [x] 1.1 同步 `openspec/config.yaml` 的 `account_status` 取值列表补 `rejected`；验证：该文件命中 `pending` / `active` / `disabled` / `rejected` 四值，且「11 个枚举」表述未被改动
- [x] 1.2 建立 `app/models/base.py`：`Base(DeclarativeBase)` + 公共列约定（`id` 整型自增代理键、`warehouse_id`、`created_at`）；验证：`tests/models/test_base.py` 断言 `Base` 为 `DeclarativeBase` 子类且公共列齐备
- [x] 1.3 建立 `tests/conftest.py`：内存库引擎（**显式 `PRAGMA foreign_keys=ON`**）+ `create_all` + `yield` session，并提供按依赖链顺序建父表记录的夹具工厂；验证：最小用例能取得 session，且 `PRAGMA foreign_keys` 读回为 `1`
- [x] 1.4 初始化 Alembic：`backend/alembic.ini` + `migrations/env.py`（`target_metadata = Base.metadata`）+ 空基线版本；验证：对空库执行 `alembic upgrade head` 退出码为 0
- [x] 1.5 改造 `backend/scripts/init_db.py` 为「`upgrade head` + 开发种子」入口，移除任何建表逻辑；验证：对空库运行后表已建，且脚本源码内不出现 `create_all`
      （入口与守卫已落地：空库跑 `python scripts/init_db.py` 退出码 0、`alembic_version` 写入基线、`tests/models/test_migrations.py` 以 AST 钉住「不出现 create_all **调用**」。
      注意两点：① `init_db.py` 的 docstring 正当提及 `create_all` 这个词以解释它为何被禁，故守卫必须查调用而非子串；
      ② **种子实体**依赖 §2/§6 尚未建的模型，`scripts/seed_dev.py` 现为空实现 —— 填充约定已写在该文件 docstring，随 2.7 与 §6 补齐；
      「表已建」的实质断言在 2.7 迁移落地后才成立）
- [x] 1.6 实现乐观锁校验辅助（接收实例与期望 `lock_version`，不匹配即抛 `DomainError` 而非静默覆盖）；验证：`tests/logic/test_optimistic_lock.py` 覆盖「匹配通过」「不匹配拒绝」两例

## 2. 主数据链（`26` 附录A 的 A + B 组，6 实体）

- [x] 2.1 `Warehouse`；验证：`tests/models/test_master_data.py` 断言唯一键与 `warehouse_id` 必填
      （仓库号即 `warehouse_id`，与 `16` A.1 的「仓库号 → warehouse_id」一致；另加 `name` 必填、
      `plant_code` 可空 —— 文档未给「工厂编码」与仓库号的区分口径，不编第二套编码体系）
- [x] 2.2 `Aisle`（含 `total_cells` 总格数、`is_near_station` 是否近站台）；验证：同文件断言两条记录可区分近站台/非近站台
      （**两列均可空**：同源于待补充导出的巷道主数据（`16` §6.1）。`is_near_station = NULL`
      表示「未导出」，不得当 `False` 用 —— 否则近站台巷道静默退出预留池）
- [x] 2.3 `Location`（`location_code` 6 位文本、层/列/格、状态；唯一约束 `(warehouse_id, location_code)`）；验证：写入 `010104` 后读回为长度 6 的字符串且首字符为 `0`（spec `data-model`「库位号按 6 位文本处理」）
      （三处文档一致：6 位 = 前 2 位巷道 + **中 2 位「层列」**（一个字段，不再拆层与列）+ 后 2 位格。
      三条 CHECK 把编码规则钉进库；**不建到 `aisles` 的外键** —— 巷道由库位号切片派生（`16` A.4），
      文档未要求库位先有巷道记录。`status` 可空、不建 CHECK：取值域文档未定义）
- [x] 2.4 `AisleStation`；验证：断言与 `Aisle` 的外键关系可建立
      （复合外键 → `aisles(warehouse_id, aisle_no)`，并按 17 ER 图 `Aisle ||--|| AisleStation`
      加唯一约束。`distance_weight` **不加 0~1 的 CHECK**：文档只给示例 0.9/0.3，未声明值域）
- [x] 2.5 `Material`（含 `abc_class`，取值限于 `A` / `B` / `C`）；验证：写入非法取值 `D` 被 CHECK 拒绝
      （D3 两层各一条用例：Python 侧 `StatementError`、绕过 ORM 的原生 SQL 撞 DB CHECK。
      `abc_class` 可空 —— 它是成品清单导入触发的派生字段（`16` A.4））
- [x] 2.6 `Batch`（外键 → `Material`）；验证：断言非法 `material_id` 因 `foreign_keys=ON` 被拒绝
      （`production_date` 用 DATE：Excel 序列号须先转日期）
- [x] 2.7 为本组生成迁移并落库；验证：`alembic upgrade head` 后 6 张表存在，且 `autogenerate` 产生空 diff
      （迁移 `56fbc88f62e8`，`autogenerate` 生成、未手改 DDL。空 diff 走 `command.check`
      而非 `compare_metadata` —— 前者会完整跑 `env.py`，自带过滤器。
      **本组踩到并修掉一个工具侧假阳性**：`sa.Enum` 的 CHECK 挂在类型上，Alembic 只在 metadata 侧
      排除它（`_is_type_bound`，SQLAlchemy #3260），于是库里那条真实 CHECK 被报成「多出来」，
      每次 autogenerate 都想 DROP —— 全部枚举列都会踩。修在 `migrations/env.py` 的
      `include_object`（反射侧同口径排除），代价是**改枚举取值时 autogenerate 报不出差异，须手写迁移**，
      已写进该函数的 docstring。
      另：`scripts/seed_dev.py` 按自身「填充约定」补了主数据链种子（幂等、父先于子；
      `AisleStation` 刻意不种 —— 待补充导出的主数据不编造），`tests/models/test_seed_dev.py` 覆盖）

## 3. 衔接链（`26` 附录A 的 C 组 + `AisleCap`，5 实体）

- [x] 3.1 `ImportSession`（`lock_version` 与业务版本**分列**、`receipt_json`、`status` 限 8 值）；验证：`tests/models/test_linkage.py` 断言两列不共用，且非法 `import_status` 被拒
      （「业务版本」列取 16 §3.3「分流去向：快照 → cap 基线版本号」，落为 `snapshot_version_no`，
      **可空**：会话可在建基准前 `FAILED`/`DISCARDED`，权威值仍在 `Snapshot.version_no`。
      `lock_version` 直接进 §1 的 `app/core/concurrency.py` 守卫，用例同时证明推进乐观锁不碰业务版本列。
      `status` 按 D3 两层各一条用例（8 值全可写 + 非法值两层各拒一次）。
      另三处处置：① `import_batch_no` **不加唯一约束** —— 16 §3.1 允许 `FAILED → DRAFT` 重新校验再导入，
      重试即新批次，加唯一约束会在重试路径上误伤；② 时间戳按「解析起止 + 里程碑」分列
      （`validating_at`/`validated_at`/`imported_at`/`baselined_at`）—— design.md 性能目标表要求
      「记录解析起止时间以支撑解析 ≤5min/文件 的 SLA」，16 §3.3 另给四个里程碑；
      ③ 数据时点用 DATETIME 而非 DATE（16 A.1 的「库存记录时间」是日期时间，存 DATE 会静默截断时刻））
- [x] 3.2 `Snapshot`（`snapshot_time` 数据时点、`version_no`、`cap_snapshot_json`；追加式）；验证：断言两次写入产生两行，旧行可读（spec `data-model`「不可变快照基线」）
      （判据取「旧行仍带着它那一刻的值可读」而非「有两行」—— 追加式的意义在供追溯与回滚。
      另断言表上**没有** `updated_at`：这是「只 INSERT 不 UPDATE」在表形状上仅剩的可验事实，
      真正的写入约束在阶段四的 cap 计算层。`version_no` 不存 17 §10.5 的时间戳串（D1：那是展示形状））
- [x] 3.3 `InventoryItem`（唯一键 = 库位号 + 批号 + 料号）；验证：同组合第二条写入被唯一约束拒绝
      （唯一键 = `warehouse_id` + `snapshot_id` + 库位号 + 批号 + 料号：唯一是**一份快照内**唯一，
      换快照必须能再写一行，否则版本化追溯与趋势分析无从谈起。库位号 6 位文本 + `qty > 0`
      （16 A.1 的校验规则）两条 CHECK；`item_status` **不建 CHECK** —— 取值以导出为准、
      由 `FieldMappingConfig` 归一（design.md Open Questions / 17 §9⑪）。
      `zone` / `production_date` 保留可空：17 §3.3 列了、16 A.1 模版已移除，是**文档冲突**，登记在 9.4c①。
      **不建**指向 `locations`/`batches`/`materials` 的外键，理由见下条）
- [x] 3.4 `AisleCap`（`snapshot_id` 外键、`cap_total` / `cap_reserved` / `cap_usable`、`is_near_station`）；验证：断言 `snapshot_id` 指向不存在的快照时写入被拒
      （唯一约束 `(warehouse_id, snapshot_id, aisle_no)` **同时是** design.md 性能目标表要的 cap 查询索引
      （SLA ≤100ms），列序被用例钉住，不另建重复索引。三列 cap **不落算术 CHECK**（D5）：
      漂移校正会以快照重算值改写同一行（16 §6.4），写死的等式会让校正写不进去 —— 故另有 `updated_at`。
      与 `InventoryItem` 一并不建指向主数据的外键：快照必须能记录「源文件里有、主数据还没建」的行
      （16 A.5 #4 把料号匹配列为**校验规则**而非落库前置），而巷道/库位又由快照派生（16 A.4）——
      反向建键即形成循环依赖）
- [x] 3.5 `CapAlert`（`snapshot_id` 与 `ledger_txn_id` 两可空外键 + `CHECK` 恰好一个非空、`alert_kind` 局部值域、`handled`）；验证：`tests/logic/test_cap_alert.py` 断言「两个都空」「两个都非空」均被 CHECK 拒绝，「恰好一个」通过
      （四类取值按 16 §6.5 的中文原文存（`负cap`/`超总格`/`增量失败`/`漂移超阈值`）—— 它们会直接出现在
      回执与 `/api/cap/alerts` 的「告警项」里，翻成代号只多一层映射点；D2 已把该值域归为实体局部，
      故不进 `enums.py`，并有用例断言它不在共享登记表里。用例放 `tests/logic/` 而非 `tests/models/`：
      「恰好一个来源」是 16 §6.5 四类异常在本模型里的落点，属口径判定。
      **`ledger_txn_id` 本阶段无外键**：D5 要求它是外键，但目标表 `ledgers` 属作业链 §4，
      声明后 SQLAlchemy 编译 DDL 即抛 `NoReferencedTableError`，本组建表与测试全跑不起来 ——
      由 §4 用 `batch_alter_table` 补，登记在 9.4c②）
- [x] 3.6 为本组生成迁移；验证：`alembic upgrade head` 后 5 张表存在，`autogenerate` 空 diff
      （迁移 `2c10a7d2b5bf`，`autogenerate` 生成、未手改 DDL；`alembic check` 报
      「No new upgrade operations detected」。本组**先把空 diff 用例跑成红的再生成迁移** ——
      D8 那条「模型改动必带同 commit 迁移」的闸门就是这样起作用的。
      另按 D4 给两个引擎（生产 `app/core/db.py` 与测试 `tests/conftest.py`）配了
      `json_serializer` / `json_deserializer`：键排序 + `ensure_ascii=False`，使库里的 JSON 文本
      只有一种字节表示、中文不转义 —— 两条都用例钉住（`test_json_columns_are_stored_canonically`）；
      其中「原生 SQL 读 JSON 列拿到的是字符串、ORM 拿到的是 dict」也一并断言，
      避免今后把两种读法混为一谈）

## 4. 作业链（`26` 附录A 的 D 组，5 实体）

- [ ] 4.1 `JobOrder`（`job_type` 区分三类、`status` 限 7 值、`lock_version`、时间戳）；验证：`tests/models/test_job.py` 断言非法 `job_status` 被拒
- [ ] 4.2 状态机迁移表与守卫（放 `app/core/state_machine.py`，纯函数无 IO）；验证：`tests/logic/test_job_state.py` 覆盖全部 10 条合法迁移通过 + 未列出迁移（如 `PENDING → EXECUTED`）被拒（spec `data-model`「JobOrder 状态机」）
- [ ] 4.3 `RecommendationPlan`（`payload_json` 承载推荐理由 / 顺路取顺序 / 收拢方案，按 `job_type` 区分）；验证：断言 `degrade_reason` 可为空但 `degraded` 为真时必须非空
- [ ] 4.4 `Ledger`（`ledger_type` 复用 `job_type` 取值域、唯一约束防重复写）；验证：断言同类同单第二条台账写入被拒
- [ ] 4.5 `Verification`（`verify_result` 限 `PASS` / `DEVIATION`）；验证：断言非法取值被拒
- [ ] 4.6 `Deviation`（`status` 与 `cause_kind` 为实体局部值域，不进 `enums.py`）；验证：断言两者不在共享枚举登记表中（spec `data-model`「枚举登记范围与取值」）
- [ ] 4.7 为本组生成迁移；验证：`alembic upgrade head` 后 5 张表存在，`autogenerate` 空 diff

## 5. 度量与身份（`26` 附录A 的 E + F 组，2 实体）

- [ ] 5.1 `KpiSnapshot`（`card_json`）；验证：`tests/models/test_kpi_identity.py` 断言可写入并读回 JSON 结构（非字符串）
- [ ] 5.2 `Account`（`username` 全局唯一、`password_hash`、`role`、`status` **含 `rejected`**、`created_by`、`last_login_at`、初始密码是否已修改）；验证：断言 `rejected` 为合法值、`username` 重复被拒、响应模型中不含密码与哈希
- [ ] 5.3 为 5.1/5.2 生成迁移；验证：`alembic upgrade head` 后两表存在，`autogenerate` 空 diff
- [ ] 5.4 确认 `core/enums.py` 的 `AccountStatus` 补入 `REJECTED` 取值且**枚举个数仍为 11**；验证：`tests/models/test_kpi_identity.py` 断言 `len({...}) == 11` 计数不变

## 6. 配置与对话（`26` 附录A 的 G + H 组，5 实体）

- [ ] 6.1 `WeightConfig`（6 因子权重、`version_no` + `effective_at`、变更人）；验证：`tests/models/test_configuration.py` 断言两版本可共存
- [ ] 6.2 「当前配置版本」取法 = `effective_at <= now` 中 `version_no` 最大者，**不加指针列**；验证：`tests/logic/test_config_version.py` 覆盖「取到最新生效版本」「未到生效时间不取」两例（spec `data-model`「版本语义三分」）
- [ ] 6.3 `CapacityConfig`（近站台预留比例默认 40%、cap 口径）；验证：断言默认值与非法比例被拒
- [ ] 6.4 `FieldMappingConfig`；验证：断言字段映射可读写
- [ ] 6.5 `PromptTemplate`（映射冷路径 4 类能力）；验证：断言能力类型为局部值域、不在共享枚举表
- [ ] 6.6 `ConversationContext`（关联 `JobOrder`，承载待确认写操作）；验证：断言与 `JobOrder` 的外键可建立
- [ ] 6.7 为本组生成迁移；验证：`alembic upgrade head` 后 5 张表存在，`autogenerate` 空 diff

## 7. 认证

- [ ] 7.1 `app/core/security.py`：`bcrypt` 哈希与校验（不用 passlib），并处理超过 72 字节的输入（给出明确错误而非 500）；验证：`tests/logic/test_security.py` 覆盖「正确口令通过」「错误口令不通过」「超长口令给出明确错误」三例
- [ ] 7.2 凭据签发与校验（标准库 `hmac` + `hashlib` + `base64` + `json`，只认 HS256，不解析算法协商；比较用 `hmac.compare_digest`）；验证：`tests/logic/test_token.py` 覆盖「签发后校验通过」「篡改载荷被拒」「错误签名被拒」「过期被拒」四例（`26` 完成标准 #2）
- [ ] 7.3 账号状态机迁移表（`pending → active|rejected`、`active → disabled`、`disabled → active`）；验证：`tests/logic/test_account_state.py` 断言 `rejected → active` 被拒（spec `auth`「账号状态迁移」）
- [ ] 7.4 `/api/auth/login` 端点（免认证、返回凭据、首次登录标记须改密）；验证：`tests/api/test_auth.py` 断言未携带凭据时返回 200 与凭据
- [ ] 7.5 登录失败语义：用户名不存在与口令错误**返回一致**的 401，不泄露账号是否存在；验证：同文件断言两次响应状态与响应体一致
- [ ] 7.6 中间件校验凭据中的 `status`，使 `disabled` 账号的旧凭据在下一次请求即失效（无需重启、无服务端黑名单）；验证：同文件断言置 `disabled` 后旧凭据返回 401
- [ ] 7.7 覆盖 `26` 完成标准 #4 的四场景：白名单放行、有效凭据通过、过期凭据 401、权限拒绝路径；验证：同文件四例全绿

## 8. 权限矩阵补测（矩阵本身不改）

- [ ] 8.1 4 角色 × 10 资源全组合断言，逐条对照 `13` §2.2；验证：`tests/logic/test_permissions.py` 覆盖全组合且与矩阵逐条一致（`26` 完成标准 #3）
- [ ] 8.2 边界用例：`supervisor` 无 `inbound.view`；`supervisor` 有 `outbound.view` 无 `outbound.operate`（以 `13` §2.2 为准，**不是** `26` 附录B）；`planner` 三作业 `operate` 全无；验证：同文件四例
- [ ] 8.3 `AUTO_ONLY` 封闭性：`ledger.write` 与 `engine.invoke` 对四角色**恒为 False**；验证：同文件断言四角色两项均不可用（`26` 完成标准 #5）
- [ ] 8.4 配置子模块受限：`supervisor` 仅 `weight`，其余 4 个子模块拒绝；`warehouse_keeper` 与 `planner` 无 `config` 权限；验证：同文件覆盖 5 个子模块 × 4 角色
- [ ] 8.5 白名单恰好 4 条，且 `/api/*` 未认证返回 401 而非 404；验证：`tests/api/test_smoke.py` 的既有断言保持全绿，并新增未知 `/api/*` 路径的 401 断言

## 9. 验收与收尾

- [ ] 9.1 端到端建库验收：空库 → `alembic upgrade head` → `autogenerate` 产生**空 diff**；验证：两步均退出码 0 且 diff 为空
- [ ] 9.2 全量测试：`python -m pytest backend/tests --tb=short -q`；验证：全绿且 L1 单元 < 5 秒（pre-commit 门禁）
- [ ] 9.3 逐条核对 `26` 完成标准的 9 条，并对 **#9（`run_evals --tier l1`）出具书面豁免记录**：`evals/run_evals.py` 刻意以退出码 3 失败以防静默通过，其实现归阶段六（`30` 号）；验证：核对清单落进 `design.md` 或本文件，9 条各有「达成」或「豁免 + 理由」
- [ ] 9.4 勘误登记（全部为**记录**，不在本阶段修）：`13` §3.1 移库作业行与合计数字（阶段七前必修）；`13` §5.1 注解「激活时创建 Account 记录」与状态图矛盾；`26` 附录A 的 `AisleCap` 分组、附录B 的 `outbound.operate` 单元格；`26` 前置检查 #2 的参考项目残留路径；`产品设计/31` 文档缺失（阶段七的规划文档）；`config.yaml`「枚举值全大写」与 `Role`/`AccountStatus` lowercase 的既有冲突
- [ ] 9.4b **§2 暴露的字段口径待确认项**（`17` 只列字段名、未给取值域或区分口径，故本阶段按「可空 + 文本 / 不加 CHECK」处置，**不编造取值**）：
      ① `Warehouse.plant_code` 与仓库号的区分 —— `19` §3.4 的多厂举例里「单厂编码」与仓库号同值 `GTJ10036`，文档未给区分口径（`17` §2.1 却把「工厂编码」列为独立字段）；
      ② `Location.status` 与 `Batch.status` 的取值域 —— 全文唯一候选是 `item_status`（源数据驱动），但**没有任何原文把二者等同**，故按文本存、不建 CHECK，待与业务方确认；
      ③ `Material.units_per_carton` / `cartons_per_pallet` 的**单位** —— 文档只有样本串「PET500茉莉柚茶15入纸箱（广饮溯源版）102/板」，未说明「102/板」的计量单位是箱还是别的，「箱规用于数量→板数折算」的具体换算亦未给全（`17` §2.3 只说「按单厂换算规则内置」）；
      ④ `Aisle.total_cells` 在巷道主数据到位前的**近似值不标注来源** —— `16` §6.1 要求「在推荐理由中标注'容量基于快照近似'」，但 `Aisle` 表没有承载该标注的列，阶段四实现 cap 计算时需决定标注落在哪（该列？`AisleCap`？还是理由 JSON）
- [ ] 9.4c **§3 暴露的口径待确认项**（同样只记录、不擅自补设计）：
      ① `InventoryItem.zone` / `production_date` 的**文档冲突** —— `17` §3.3 列了这两列，而 `16` A.1 的 INV 模版已移除它们（原文「缺号为模版中已移除的字段（库区号、生产日期不再需要）」）。本阶段按 17 保留列、按 A.1 置可空。若确认不再需要应改 `17` 后删列；若仍需要 `zone`，须定其来源 —— `14` §3.4 把「库区」列为**可行巷道集**的匹配条件之一，长期为空会让该条件静默失效；
      ② `CapAlert.ledger_txn_id` 的**外键待 §4 补** —— 本阶段无外键（目标表 `ledgers` 属作业链）。§4 落地 `ledgers` 后须用 `op.batch_alter_table("cap_alerts")` 加外键（SQLite 改约束只能 batch 重建），并同步收紧 `tests/logic/test_cap_alert.py` 中「两个都非空」用例（届时先撞外键而非 CHECK，断言应从「抛 IntegrityError」改为「抛的是 CHECK 不是外键」）；
      ③ `ImportSession` 的「业务版本」口径 —— spec 要求它与乐观锁分列，而 `17` §3.1 没有给 `ImportSession` 的 `version_no`；本实现取 `16` §3.3「分流去向：快照 → cap 基线版本号」落为 `snapshot_version_no`。若评审认为该值只应活在 `receipt_json` 里（D4），删列即可 —— 但那样 spec 的这条场景要一并改；
      ④ `ImportSession.session_no` 与 `import_batch_no` 是否同值 —— `17` §3.1 与 `16` §3.3 都并列列了「导入会话 ID」与「批次号」，未说差异；本阶段按「会话可重试（`FAILED → DRAFT`）→ 一批次一会话、可多对一」处置（故批次号不唯一）。若确认一会话一批次，应加唯一约束；
      ⑤ `InventoryItem` 是否要为 16 A.4 的**解析时派生字段**（巷道、占用格数）落列 —— 该表只给了派生逻辑与时机，没给存储口径，`17` §3.3 的字段表也没有。本阶段不落列：占用格数的「板-格」换算规则属 `CapacityConfig`（任务 §6，实体尚未建模），口径未定前连列类型都只能猜。阶段四按实际查询计划定（cap 聚合、同物料跨巷道 是否需要列级索引）；
      ⑥ `ImportSession.files_json` 超出 D4 的 JSON 映射表 —— D4 只映射 `17` §10 的 6 类结构，而 `17` §3.1 / `16` §3.3 要求三类文件清单（含**校验和**，是 16 §11.5 判重的依据）在库。本阶段按实体章节落列，若评审要求严格对齐 D4，需补 D4 或改述
- [ ] 9.5 收尾：分支 `phase-2/data-model-permission` 以 `--no-ff` 合并 `main` 并打 `v0.2.0`；验证：`git tag | grep v0.2.0` 命中，且工作区干净
