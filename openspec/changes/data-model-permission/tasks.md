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

- [x] 4.1 `JobOrder`（`job_type` 区分三类、`status` 限 7 值、`lock_version`、时间戳）；验证：`tests/models/test_job.py` 断言非法 `job_status` 被拒
      （唯一约束 `(warehouse_id, job_type, order_no, line_no)` —— 行唯一键取 16 附录A 的「单据号码 + 行号」，
      `job_type` 入键是因为三类作业各自编号：同一单号行号在两种作业下可以同时存在（用例
      `test_same_order_line_allowed_across_job_types` 钉住，否则会误伤跨类型同号）。
      `lock_version` 直接进 §1 的 `app/core/concurrency.py` 守卫，**且本表无业务版本列** ——
      D1 的「版本语义三分」里 `JobOrder` 只有乐观锁，用例 `test_job_order_has_no_business_version_column` 反向看住。
      17 §4.1 的字段表把推荐侧 / 后验侧 / 降级标记列在本表，但那是**聚合级枚举**（说明「这一单有方案、有后验」），
      具体化在 §4.3 / §4.4 各自的表里 —— 只有执行侧（`actual_location_code` / `actual_qty` / `executed_at` /
      `disposition` / 确认卡三列）留在本表，使 15 §7.3 的追溯链只有一个来源。`confirmed_by_id` 无外键，见 9.4d①）
- [x] 4.2 状态机迁移表与守卫（放 `app/core/state_machine.py`，纯函数无 IO）；验证：`tests/logic/test_job_state.py` 覆盖全部 10 条合法迁移通过 + 未列出迁移（如 `PENDING → EXECUTED`）被拒（spec `data-model`「JobOrder 状态机」）
      （10 条合法迁移逐条抄 15 §3.1；用例用 `itertools.product` 穷举 7×7 = 49 对，断言「合法者恰 10 对、
      其余 39 对全拒」—— 用一个常数把「未列出的迁移一律拒绝」钉死，而不是只抽查几条。
      `PENDING → PENDING` 是唯一合法自环（重复下单 / 同单再提交），两个终态 `CANCELLED` / `VERIFIED` 无出边。
      `CONFIRMED → PLANNED` 是合法回边（退回重选），而 `EXECUTED → PLANNED` **不是** —— 台账已写，回退会破坏
      「台账是 cap 增量唯一来源」。守卫对非 `JobStatus` 入参抛 `TypeError` 而非 `StateConflict`：`str` 枚举
      `JobStatus.PENDING == "PENDING"` 为真但哈希按成员名走，放行裸字符串会让同一份代码在改过取值后就静默变脸。
      模块纯净性由 AST 白名单用例看住（只许 `enum` / `typing` / `collections.abc` 等 + 自有 `core/enums`、`core/errors`））
- [x] 4.3 `RecommendationPlan`（`payload_json` 承载推荐理由 / 顺路取顺序 / 收拢方案，按 `job_type` 区分）；验证：断言 `degrade_reason` 可为空但 `degraded` 为真时必须非空
      （`plan_kind` 是实体局部值域（`分配`/`顺路取`/`收拢`），取值即 15 §3 的三类方案名 —— 不进 `enums.py`（D2）。
      `job_order_id` **非唯一** + 显式 `ix_recommendation_plans_job_order_id`：一单可被重规划，
      用例 `test_a_job_order_may_be_replanned` 钉住（对比 `Ledger` 的唯一约束，两张表的差异是刻意的）。
      `degraded` / `degrade_reason` 的 CHECK 在三条链上同名同形（`ck_*_degrade_reason_required`），
      降级必须在理由里写明 `degrade_reason`（CLAUDE.md 红线「降级不静默」）））
- [x] 4.4 `Ledger`（`ledger_type` 复用 `job_type` 取值域、唯一约束防重复写）；验证：断言同类同单第二条台账写入被拒
      （唯一约束落在 `job_order_id` 上，**不是**（类型 + 单号）：16 附录A 的行唯一键含行号，而 15 附录A 三类台账的
      字段表都没有行号 —— 按「类型 + 单号」会退化成「一张多行单据只能写一行」。`ledger_type` 与 `job_type`
      取值同域但独立登记（两个枚举名，D2 的 11 个里各占一个）。库位三选一的 CHECK 按类型分派：
      入库源空目标非空、出库源非空目标空、移库两者都非空（与 `source/target` 各自 6 位文本的 CHECK 叠加）。
      `operator_id` 必填无外键（账号表属 §5，见 9.4d①）；本表**没有 `updated_at`** —— 台账是冻结记录，
      写入即固化。§4 同时补上了 §3 欠的账：`CapAlert.ledger_txn_id` 的真外键，并收紧了对应用例）
- [x] 4.5 `Verification`（`verify_result` 限 `PASS` / `DEVIATION`）；验证：断言非法取值被拒
      （唯一约束 `(job_order_id, metric_kind)` —— 一单每指标一行。`metric_kind` 是 `String(32)` **无枚举**：
      17 §4.4 只列字段名、未给取值域（指标名在 18 号），本阶段不编造，登记在 9.4d②。
      实际值 / 阈值用 `Float` 而非整型 —— 阈值可能是比例或小数（用例 `test_actual_value_is_a_float`））
- [x] 4.6 `Deviation`（`status` 与 `cause_kind` 为实体局部值域，不进 `enums.py`）；验证：断言两者不在共享枚举登记表中（spec `data-model`「枚举登记范围与取值」）
      （两条结构类 CHECK：`batch_no` 与 `material_code` **至少一个非空** —— 两个都空则偏离记录无从定位；
      `status = 已发起移库` 时 `relocate_job_order_id` 必填 —— 状态说已发起却没有单号，追不下去。
      `cause_kind` 取值照 17 §4.5 原文（`新入库收拢不达标` / `历史库存拖累`））
- [x] 4.7 为本组生成迁移；验证：`alembic upgrade head` 后 5 张表存在，`autogenerate` 空 diff
      （迁移 `f01b0406d12c`，`autogenerate` 生成；唯一手改是把 CHECK 文本里一处 f-string 造成的双空格归一
      —— CHECK 文本不在 autogenerate 的比对范围内，模型与迁移必须手工保持一致，已逐条比对过。
      补 `cap_alerts` 外键用的是 `batch_alter_table`（SQLite 只能 batch 重建），故**实测**了重建前后的
      `sqlite_master.sql` 指纹：`cap_alerts` 原有 3 条 CHECK 一条不少、新增 1 条外键。
      `alembic check` 报「No new upgrade operations detected」）

## 5. 度量与身份（`26` 附录A 的 E + F 组，2 实体）

- [x] 5.1 `KpiSnapshot`（`card_json`）；验证：`tests/models/test_kpi_identity.py` 断言可写入并读回 JSON 结构（非字符串）
      （字段照 17 §5.1：周期 / 两项跨巷道均值 / 加权集中度 / 采纳率 / 落位准确率 / 环比 / 生成时间；
      生成时间即继承的 `created_at`，不另立第二列 —— 与 `ImportSession` 同一处置。
      **一仓库一周期一行**：18 §3.1 的 `READY → STALE → PENDING`、`ERROR → PENDING` 都是同一行的状态变化
      （重算），不是新增行；唯一起见键 `(warehouse_id, period)` 列序同时兼作「看板查最新」的索引，
      与 `AisleCap` 同一处置。**指标列全可空**不是宽松：18 §3.2 写明 `PENDING` 时只有周期与仓库号，
      写 NOT NULL 状态机在存储层就落不了地。采纳率 / 准确率**无样本时为 NULL 而非 0**（0% 像全线崩盘，
      真实语义是「还没有可采纳的决策」；与 `Aisle.is_near_station` 用 NULL 表达「未导出」同一口径）。
      两处 17/18 未给口径的处置：`status` 落列来自 18 §3.1 的状态机（17 §5.1 的字段表没有它，
      落地理由见 design.md Goals「阶段三~七不必回头改模型」）、环比落 JSON 列（字段在 17 列了、
      两处文档都没给口径）—— 均登记在 9.4e）
- [x] 5.2 `Account`（`username` 全局唯一、`password_hash`、`role`、`status` **含 `rejected`**、`created_by`、`last_login_at`、初始密码是否已修改）；验证：断言 `rejected` 为合法值、`username` 重复被拒、响应模型中不含密码与哈希
      （`username` **全局唯一**（17 §6.1 原文）—— 这是 `warehouse_id` 过滤维度的一处刻意例外：
      账号是运维主体，按仓库分域会让「同用户名两个账号」看起来合法，而登录页只有一个用户名输入框；
      `warehouse_id` 仍保留（数据范围区分用，13 §3.2）。`created_by_id` 是**自引用且可空** ——
      首个管理员没有上级可指（13 §5.1 的状态图以「管理员创建账号」为起点），用 NULL 表达「基建写入」，
      不编造 `system` 账号来凑外键。`last_login_at` 空 = 从未登录，不拿 `created_at` 兜底。
      `initial_password_changed` 默认 `False` 且它就是**强制改密的开关**（首次登录必须改密）。
      表上**只有 `password_hash`**，并有一条用例逐个点名禁止 `password` / `initial_password` 等明文列
      —— 明文列一旦存在，迟早会被某条「临时」路径写进去。
      「响应模型中不含密码与哈希」的断言属 §7（端点尚不存在），此处守的是模型形状）
- [x] 5.3 为 5.1/5.2 生成迁移；验证：`alembic upgrade head` 后两表存在，`autogenerate` 空 diff
      （迁移 `0559bebb5207`，`autogenerate` 生成、未手改。它同时**销了 9.4d① 的账**：
      用 `batch_alter_table` 补上 `job_orders.confirmed_by_id` 与 `ledgers.operator_id` → `accounts.id`
      两条外键（SQLite 只能 batch 重建）。已实测重建前后 `sqlite_master.sql` 的 CHECK 数：
      `job_orders` 5→5、`ledgers` 5→5、其余 4 张作业链表也不变。`alembic check` 报空 diff）
- [x] 5.4 确认 `core/enums.py` 的 `AccountStatus` 补入 `REJECTED` 取值且**枚举个数仍为 11**；验证：`tests/models/test_kpi_identity.py` 断言 `len({...}) == 11` 计数不变
      （用例不只数个数，还**逐条点名** 11 个类名 —— 只数个数的话，「数量对了但换了一个」会假通过。
      `REJECTED` 在枚举里的注释写明它与另外三值的地位不同：它是**终态**，`rejected → active`
      不是合法迁移（spec `auth`「账号状态迁移」），迁移表属 §7.3。
      另：17 §6.1 的散文里账号状态只写了三值（缺 `rejected`），而 §九 的枚举表是四值 ——
      这两处是**文档内部冲突**（§九 为准，已由 13 §5.2 与 design.md D2 订正），登记在 9.4）

## 6. 配置与对话（`26` 附录A 的 G + H 组，5 实体）

- [x] 6.1 `WeightConfig`（6 因子权重、`version_no` + `effective_at`、变更人）；验证：`tests/models/test_configuration.py` 断言两版本可共存
      （6 因子落**六列**而非一个 JSON：14 §四 / §135 明说因子集合「不新增、不删除」—— 集合固定时六列能逐列加约束、
      能按因子查询，JSON 会把「因子少了一个」推迟到运行期才发现；D4 只把 17 §10 的 6 类结构落成 JSON。
      每项 CHECK 在 `[0,1]`，**不 CHECK 六项之和为 1** —— 文档全文没有「归一」口径（17 §10.1 给的是示例值），
      加这条等于替文档定口径，登记在 9.4f。`changed_by_id` **可空**：首版权重由种子写入，没有变更人
      （与 `Account.created_by_id` 同一处置，不自造 `system` 账号）。
      「两版本可共存」是**回滚的前提**：若新版覆盖旧行，史上用旧权重算出的分数就再也解释不了
      —— 而「改口径伪装改善」正是 18 号要防的反指标。版本号在仓库内唯一（否则「取 version_no 最大者」会取到两行，
      两行权重不同时评分不可复现））
- [x] 6.2 「当前配置版本」取法 = `effective_at <= now` 中 `version_no` 最大者，**不加指针列**；验证：`tests/logic/test_config_version.py` 覆盖「取到最新生效版本」「未到生效时间不取」两例（spec `data-model`「版本语义三分」）
      （落 `app/core/config_version.py` 的**纯函数** `is_effective` / `pick_current_version`，10 例：另覆盖
      「编号优先于生效时间」（D1 原文是取 `version_no` 最大者；补录一条生效更早的旧口径时，补的仍是编号大的那版）、
      「全部未生效 → `None`，**不回退到最早那版**」（无生效版本说明发布流程有问题，调用方该走降级链，
      而不是拿用户没启用的版本算分）、「与入参顺序无关」（查询返回顺序不是契约）、「不改动入参」、
      「表上没有 `is_current` 指针列」（指针列一旦存在，「到点翻牌」就成了写入方必须记住的责任，
      而本阶段没有定时任务 —— 那列只能靠读时顺手写，把读放大成单写库上的写冲突）。
      另挡掉朴素时间与带时区时间的混比：报错要指向**该改哪一边**（改 `now`），而不是让人去改模型））
- [x] 6.3 `CapacityConfig`（近站台预留比例默认 40%、cap 口径）；验证：断言默认值与非法比例被拒
      （默认值逐条取自 16 §353~356：40% / 当日 18:00 / N=5 / 同物料 5 / 同批 3 / 漂移 1%；
      比例 > 1 会让 `cap_reserved = cap_total × 比例`（14 §3.3）超出巷道容量 → `cap_usable` 为负，
      而负的可用容量会**静默通过**所有「cap 是否足够」的判断；三项计数阈值 ≥1（阈值 0 让任何一行都「超标」）；
      漂移阈值 >0（16 §6.4 用它决定是否以快照重算值校正基线）。
      `reserved_release_at` 用 `Time` 而非 `DateTime`：16 写的是「当日 18:00」，无日期分量，
      落 DateTime 就得补一个「用哪天的日期」的口径（文档没给）；也正因如此它是**本地墙上时间**，
      与其余时间戳列相反 —— 按 UTC 存比会让现场变成次日 02:00 释放。
      本表**没有** `changed_by_id`：17 §七 只把「变更人」列在权重配置上，不替文档补列））
- [x] 6.4 `FieldMappingConfig`；验证：断言字段映射可读写
      （`(warehouse_id, plant_code, file_type)` 唯一（16 附录 A.1/A.2/A.3 三类文件各有模版）；
      `mapping_json` NOT NULL 但允许空字典 —— 「有配置但没填」与「没有配置」是两件事：
      前者应报「映射不完整」、后者报「缺映射配置」，两个错指向不同的动作。
      没有版本号 / 生效时间 / 变更人：17 §七 把它列为**内置**，版本机制是为「可回滚的口径变更」准备的，
      改错了就再改一条，给内置映射加版本只会多出永远没人读的历史行。
      **内容不落种子**（见 9.4f），空表是正确状态））
- [x] 6.5 `PromptTemplate`（映射冷路径 4 类能力）；验证：断言能力类型为局部值域、不在共享枚举表
      （`Capability` 取值照 10 §152 / 15 §8.2 的**中文原文**（KPI 解读 / 偏离归因 / 权重调优 / 移库方案）：
      它随问句模板一起在管理页面上给人看（15 §8.5「新增一条问句即新增一条映射，无需改代码」），
      翻成代号只多一层要维护的映射。`(warehouse_id, template_id)` 唯一 —— 模板 ID 是前端调用参数的一部分，
      是接口契约。新增模板**默认启用**（新增即生效；默认关闭会让新问句悄悄点不到，而没人会记得回来开开关）。
      `params_json` 与文本里的 `{}` 都要：前者是选择器取值来源（15 §8.3 第 2 步弹物料选择器），
      后者是渲染源 —— 从文本反解占位符需要一套正则约定）
- [x] 6.6 `ConversationContext`（关联 `JobOrder`，承载待确认写操作）；验证：断言与 `JobOrder` 的外键可建立
      （`account_id` 指向 `accounts.id` 且 **NOT NULL**：它承载待确认的写操作上下文，
      一个没有主体的写操作意图既不能审计也不能授权。`expires_at` **必填** —— 本产品不做长期记忆
      （17 §十二：会话级 ≤8 小时 / 关闭即清除），没有过期时间的会话行会永久留下用户的问句原文；
      滑动续期由**重写该列**表达，故**不设 `updated_at`**（多那一列会让人以为「最后问句时间」存在那里，
      而现场真正要用的判断始终是「过期了没有」）。`pending_write_json` 只落**上下文**，不建确认卡实体：
      确认卡是前端交互态（15 §7.2），落库会与 `JobOrder` 上的 `disposition` / `confirm_card_digest`
      形成两套处置记录（红线「台账只有一套」同理）））
- [x] 6.7 为本组生成迁移；验证：`alembic upgrade head` 后 5 张表存在，`autogenerate` 空 diff
      （迁移 `62cdb8b54011`，`autogenerate` 生成、未手改。**本组同时销了 9.4e③ 的账**：
      `kpi_snapshots` 补 `capacity_config_id` 外键 → `capacity_configs.id`（可空：`PENDING`/`ERROR` 的行还没算）。
      指向**行**而非 `version_no`：行有唯一约束、可被外键看住，与 D1 拒绝把时间戳串当配置引用同一理由。
      用 `batch_alter_table` 补列 + 补外键（SQLite 改约束只能 batch 重建），已实测重建前后
      `sqlite_master.sql`：`kpi_snapshots` 的 `ck_kpi_snapshots_kpi_status` 仍在，5 张新表的 CHECK 全部落库。
      `alembic check` 报空 diff；`tests/models/test_migrations.py` 新增「**恰好** 23 张实体表」断言
      （迁移侧 + `Base.metadata` 侧各一条，取等于而非包含：多一张是有人绕开迁移建表，少一张是迁移没落地）；
      `scripts/seed_dev.py` 补 3 组种子（第 1 版权重 = 17 §10.1 示例值 / 第 1 版容量阈值 = 16 §353~356 /
      四条 L2 问句 = 15 §8.2，第二条与第四条用 §8.3 的参数化写法），幂等按 `(warehouse_id, version_no)`
      与 `template_id` 判，生效时间取固定值而非 `utcnow()`（种子要可复现，否则每次重建库的配置行都不同））

## 7. 认证

- [x] 7.1 `app/core/security.py`：`bcrypt` 哈希与校验（不用 passlib），并处理超过 72 字节的输入（给出明确错误而非 500）；验证：`tests/logic/test_security.py` 覆盖「正确口令通过」「错误口令不通过」「超长口令给出明确错误」三例
      （已完成。三例之外补 6 例，都对应**会静默失效**的失败模式：加盐（两次哈希必须不同，否则一张彩虹表解全系统）；
      **按字节而非字符**判长度（`汉`×24 = 72 字节可过、×25 = 75 字节必须报错 —— 按字符判会放行 75 字节，
      而 bcrypt 只取前 72 字节，于是「24 个汉字」与「24 个汉字 + 1」互相通过）；畸形哈希返回 `False` 而非抛
      （抛出去就是 500，而 500 与 401 的差别本身是探测面）；非 ASCII 口令按 UTF-8 往返。
      新增 `settings.bcrypt_cost`（默认 **12**，单次约 0.28s 本机实测）：测试经 autouse fixture 降到 4（约 0.001s），
      否则本文件与 `test_auth.py` 合计几十次哈希会把整包推出 pre-commit 的 L1 门禁（<5s）——
      `assert_production_safe()` 拒绝 prod 下低于 12 的取值，堵住「测试旋钮带到生产」这条路）
- [x] 7.2 凭据签发与校验（标准库 `hmac` + `hashlib` + `base64` + `json`，只认 HS256，不解析算法协商；比较用 `hmac.compare_digest`）；验证：`tests/logic/test_token.py` 覆盖「签发后校验通过」「篡改载荷被拒」「错误签名被拒」「过期被拒」四例（`26` 完成标准 #2）
      （已完成。会话半段由 PyJWT 改写为自实现 HS256（D7），`requirements.txt` 里的 `PyJWT==2.13.0` 钉子同步移除，
      并由 `tests/logic/test_token.py` 的 AST 断言守住「不再引入 `jwt`/`jose`/`authlib`」。
      解析顺序固定为**结构 → 算法 → 签名 → 载荷语义**：签名验的是前两段**原文**（验解析后的对象会因 JSON 键序重排而错误通过），
      头部的 `alg` 只被核对、不参与选择。四例之外补 12 例，其中两例专打自实现特有的失效模式：
      `alg=none`（含两段式与空签名段）与**算法混淆**（头写 `HS512`、签名按 SHA-512 算 —— 这一例需要真知道密钥，
      故它考的是「算法协商被排除」而非「不验签」）；另有段数/非法 base64/非 JSON 载荷一律 `SessionInvalid`（不得漏成 500）、
      缺 claim 报错须**指名**缺了哪个字段、`None`/`bytes`/`int` 入参不抛类型错。
      两处口径取严：`exp` 边界为**到期即失效**（`now >= exp`，08:00 签发 → 16:00 起 401）；
      `now` 必须带时区（朴素时间的 `timestamp()` 按**本机**时区解释，会让同一份代码在开发机与服务器上给出不同的 `exp`）。
      claims 集合封闭为 `SESSION_CLAIMS`（13 §7.2 逐字的五个 + `warehouse_id`），逐字比对而非「包含」）
- [x] 7.3 账号状态机迁移表（`pending → active|rejected`、`active → disabled`、`disabled → active`）；验证：`tests/logic/test_account_state.py` 断言 `rejected → active` 被拒（spec `auth`「账号状态迁移」）
      （已完成，落在新模块 `app/core/account_state.py` —— 与 `state_machine.py` **分成两个模块**：
      两张表服务两个主体（作业单由操作员按流程推进、账号由管理员按权限动作推进），取值域也不同（七值 vs 四值），
      合表会让「键是哪种状态」变成调用方要先判断的事；共用的是**形状**（只读迁移表 + 类型守卫 + 返回目标态），
      不是内容，形状的复用靠约定而非继承。4 状态 16 有序对逐对穷举，合法 4 / 非法 12；
      另断言三条**与作业单状态机刻意相反**的口径：这里**无合法自环**（作业单的 `PENDING → PENDING` 是「分配失败可重试」）、
      **终态只有 `rejected`**（`disabled → active` 存在，故停用是可达回的中转态，判据是出边而非「还能不能登录」这种语感）、
      `rejected` 四条出边全拒。类型守卫比 `JobStatus` 那处更深一层：`AccountStatus` 是 lowercase 的 str 枚举，
      `AccountStatus.ACTIVE == "active"` 为真而 `Enum.__hash__` 取成员名 ——「值比较通过、哈希查表失败」是同一份数据的两套语义，
      故裸字符串（含大写成员名）一律 `TypeError` 而非 `KeyError`/「非法迁移」。AST 断言无 IO 依赖。
      **本阶段没有消费者**：账号管理的写端点属路线图（与 D9 同批），本阶段交付的是数据 + 纯判定函数 + 全组合断言）
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
- [ ] 9.4 勘误登记（全部为**记录**，不在本阶段修）：`13` §3.1 移库作业行与合计数字（阶段七前必修）；`13` §5.1 注解「激活时创建 Account 记录」与状态图矛盾；`17` §6.1 散文只写三个账号状态、`17` §九 的枚举表是四个（缺 `rejected`；以 §九 为准，已由 `13` §5.2 与 design.md D2 订正）；`26` 附录A 的 `AisleCap` 分组、附录B 的 `outbound.operate` 单元格；`26` 前置检查 #2 的参考项目残留路径；`产品设计/31` 文档缺失（阶段七的规划文档）；`config.yaml`「枚举值全大写」与 `Role`/`AccountStatus` lowercase 的既有冲突
- [ ] 9.4b **§2 暴露的字段口径待确认项**（`17` 只列字段名、未给取值域或区分口径，故本阶段按「可空 + 文本 / 不加 CHECK」处置，**不编造取值**）：
      ① `Warehouse.plant_code` 与仓库号的区分 —— `19` §3.4 的多厂举例里「单厂编码」与仓库号同值 `GTJ10036`，文档未给区分口径（`17` §2.1 却把「工厂编码」列为独立字段）；
      ② `Location.status` 与 `Batch.status` 的取值域 —— 全文唯一候选是 `item_status`（源数据驱动），但**没有任何原文把二者等同**，故按文本存、不建 CHECK，待与业务方确认；
      ③ `Material.units_per_carton` / `cartons_per_pallet` 的**单位** —— 文档只有样本串「PET500茉莉柚茶15入纸箱（广饮溯源版）102/板」，未说明「102/板」的计量单位是箱还是别的，「箱规用于数量→板数折算」的具体换算亦未给全（`17` §2.3 只说「按单厂换算规则内置」）；
      ④ `Aisle.total_cells` 在巷道主数据到位前的**近似值不标注来源** —— `16` §6.1 要求「在推荐理由中标注'容量基于快照近似'」，但 `Aisle` 表没有承载该标注的列，阶段四实现 cap 计算时需决定标注落在哪（该列？`AisleCap`？还是理由 JSON）
- [ ] 9.4c **§3 暴露的口径待确认项**（同样只记录、不擅自补设计）：
      ① `InventoryItem.zone` / `production_date` 的**文档冲突** —— `17` §3.3 列了这两列，而 `16` A.1 的 INV 模版已移除它们（原文「缺号为模版中已移除的字段（库区号、生产日期不再需要）」）。本阶段按 17 保留列、按 A.1 置可空。若确认不再需要应改 `17` 后删列；若仍需要 `zone`，须定其来源 —— `14` §3.4 把「库区」列为**可行巷道集**的匹配条件之一，长期为空会让该条件静默失效；
      ② ~~`CapAlert.ledger_txn_id` 的**外键待 §4 补**~~ —— **§4 已销账**：迁移 `f01b0406d12c` 用 `op.batch_alter_table("cap_alerts")` 补上外键（SQLite 改约束只能 batch 重建），`tests/logic/test_cap_alert.py` 随之收紧三条：「只引用台账事务」不再随手编 `ledger_txn_id=42` 而造一行真台账、「两个都非空」断言改为点名 CHECK（`alert_source_exactly_one`，与「撞外键」区分开 —— 撞外键说明口径已被绕开）、新增真外键的负例，「指向 `ledgers.id`」的断言从方向收紧成等号；
      ③ `ImportSession` 的「业务版本」口径 —— spec 要求它与乐观锁分列，而 `17` §3.1 没有给 `ImportSession` 的 `version_no`；本实现取 `16` §3.3「分流去向：快照 → cap 基线版本号」落为 `snapshot_version_no`。若评审认为该值只应活在 `receipt_json` 里（D4），删列即可 —— 但那样 spec 的这条场景要一并改；
      ④ `ImportSession.session_no` 与 `import_batch_no` 是否同值 —— `17` §3.1 与 `16` §3.3 都并列列了「导入会话 ID」与「批次号」，未说差异；本阶段按「会话可重试（`FAILED → DRAFT`）→ 一批次一会话、可多对一」处置（故批次号不唯一）。若确认一会话一批次，应加唯一约束；
      ⑤ `InventoryItem` 是否要为 16 A.4 的**解析时派生字段**（巷道、占用格数）落列 —— 该表只给了派生逻辑与时机，没给存储口径，`17` §3.3 的字段表也没有。本阶段不落列：占用格数的「板-格」换算规则属 `CapacityConfig`（任务 §6，实体尚未建模），口径未定前连列类型都只能猜。阶段四按实际查询计划定（cap 聚合、同物料跨巷道 是否需要列级索引）；
      ⑥ `ImportSession.files_json` 超出 D4 的 JSON 映射表 —— D4 只映射 `17` §10 的 6 类结构，而 `17` §3.1 / `16` §3.3 要求三类文件清单（含**校验和**，是 16 §11.5 判重的依据）在库。本阶段按实体章节落列，若评审要求严格对齐 D4，需补 D4 或改述
- [ ] 9.4d **§4 暴露的口径待确认项**（同样只记录、不擅自补设计）：
      ① ~~`job_orders.confirmed_by_id` 与 `ledgers.operator_id` 的**外键待 §5 补**~~ —— **§5 已销账**：
      迁移 `0559bebb5207` 用 `op.batch_alter_table` 补上两条 → `accounts.id` 的外键，
      `tests/models/test_job.py` 同步加了两条负例（指向不存在的账号被拒）与一条「目标恒为 `accounts.id`」的模型侧断言，
      并把 `_ledger` 夹具从占位整数 `1` 改成**真账号**（`_account` 工厂，先查后建以免撞 `username` 唯一约束）；
      `test_cap_alert.py` 的台账夹具同改。**顺序前提已核**：开发库此刻无台账行，
      `ledgers.operator_id` 虽是 NOT NULL，补外键不会撞历史脏数据；
      ② `verifications.metric_kind` 的**取值域未定** —— 17 §4.4 只列字段名（`metric_kind` / 实际值 / 阈值 /
      判定结果），未给取值清单，指标定义在 `18` 号。本阶段按 `String(32)` + 无 CHECK 落列，
      **不编造指标名**。待 18 号确认后：若取值封闭，应改 `enum_column`（届时 CHECK 变化不会产生
      autogenerate diff，须手工写迁移 —— 见 D8 的代价说明）；若指标可扩展，则保持文本并列进
      `FieldMappingConfig` 一类配置；
      ③ `JobOrder.disposition`（确认卡三列）与 `RecommendationPlan` 的关系 —— 17 §4.1 把「确认 / 调整 /
      拒绝」列在 `JobOrder` 上，15 §7.2 的二次确认卡又同时回填方案调整明细。本阶段按「处置结果记账在
      `JobOrder`（单据当前态）、方案原文留在 `RecommendationPlan.payload_json`（不可变方案）」处置，
      两者的先后与覆盖关系文档未明说。若评审认为调整后的方案也要留痕，需在 §4.3 侧增列而非改本表
- [ ] 9.4e **§5 暴露的口径待确认项**（同样只记录、不擅自补设计）：
      ① `KpiSnapshot.status` 的**出处是 18 而非 17** —— 17 §5.1 与 18 §3.3 的字段表都**没有**状态列，
      但 18 §3.1 给了五态状态机（`PENDING`/`COMPUTING`/`READY`/`STALE`/`ERROR`）、§3.2 逐行写了每个
      状态「此时可获取的信息」，§「看板展示上一次 READY 值并标注数据过期」更要靠它区分。本阶段落列
      （不落则阶段六要再补一次迁移，与 design.md Goals「阶段三~七不必回头改模型」相抵）。
      若评审认为状态应只活在计算层内存而不入库，删列即可 —— 但 18 §3.2 的「STALE 时看板展示上一次
      READY 值」将无从判定。同理**错误描述**（18 §3.2：`ERROR` 时「可获取的信息 = 错误描述」）未给落点，
      本阶段不落列 —— 待 18 明确它进 `card_json` 还是单列；
      ② `KpiSnapshot` 的**「环比」口径未定** —— 字段在 17 §5.1 列了，但 17 与 18 全文都没说它是哪个指标的
      环比、是单值还是分指标各一个（全文只出现 3 次「环比」，全是列举）。本阶段落 `period_over_period_json`
      （JSON 能装下任何口径，不替文档定口径）；口径明确后应收紧成数值列 + 手工迁移（CHECK/类型变化
      不产生 autogenerate diff，见 D8 的代价说明）。另需一并确认：环比是否干脆**不落库**而由历史行派生
      （18 §151 把「趋势、环比」列为从 `KpiSnapshot` 历史算出的东西）—— 那样可从 9.4 的勘误侧改 17 §5.1；
      ③ ~~`KpiSnapshot` 的**口径版本引用**未落列~~ —— **§6 已销账**（2026-09-11）：
      18 §「阈值（N、预留比例）或口径变更须走配置页并记录口径版本；历史 KpiSnapshot 保留旧口径，
      趋势对比须同口径」要求每一行能自证「按哪一版口径算的」。§6 落地 `CapacityConfig` 后，
      由迁移 `62cdb8b54011` 用 `batch_alter_table` 给 `kpi_snapshots` 补上可空的
      `capacity_config_id` → `capacity_configs.id`（可空：`PENDING`/`ERROR` 的行还没算）。
      **指向行而不是 `version_no`**：行有唯一约束、可被外键看住，与 D1 拒绝把时间戳串当配置引用同一理由；
      也不按 `effective_at` 反查 —— 反查得到的是「现在生效的版本」，而本列要记的是「当时生效的版本」，
      两者在口径变更后必然不同，而趋势图正是要跨过那次变更。集中度阈值 N 仍不落本表：
      它属 `CapacityConfig`（17 §七），卡片 JSON 里带的是当次生效的那一份；
      ④ `Account` 与 `ImportSession` 的**关系未落列** —— 26 附录A 的依赖链写 `Account → ImportSession`，
      而 17 §3.1 的字段清单里**没有**导入人 / 操作人列（只有会话 ID、批次号、数据时点、导入操作时间、
      文件清单、校验结果、状态、分流去向、cap 重算结果、乐观锁、时间戳）。故本阶段不添列（17 是实体
      字段的单一事实来源）。若确认需要「谁发起的导入」，加列 + 迁移，并在 §3.1 侧登记；
      ⑤ `Account` 的**密码长度上界与凭据时钟容忍窗口** —— design.md Open Questions 的既有两项
      （默认 64 字符 / 0 秒，可延后），本阶段未落 CHECK：它们的落点是 §7 的凭据设置入口与校验函数
      （届时用 `bcrypt` 的 72 字节输入界兜底），落到列上会把「值域」与「校验时机」混在一起
- [ ] 9.4f **§6 暴露的口径待确认项**（同样只记录、不擅自补设计）：
      ① **6 项权重之和是否必须为 1 未定** —— 17 §10.1 只给了一组恰好和为 1.00 的示例值，全文没有
      「归一」的字样。本阶段对每项落 `[0,1]` 的 CHECK（1.5 的权重让得分能超过 1，而 17 §10.1 的示例
      分值 0.86 / 0.81 显然在 [0,1] 内 —— 那是**结构性**下界，不是口径），**不加「和 = 1」的 CHECK**。
      待确认：是否允许「某项故意留空、由其余项归一」或「临时放大某项看重排效果」。若确认为必须归一，
      加 CHECK 须**手工写迁移**（CHECK 变化不产生 autogenerate diff，见 D8 的代价说明）；
      ② **`FieldMappingConfig` 的内容未落种子** —— 映射在 16 附录 A.1/A.2/A.3 的三张表里，消费它的是
      阶段四的导入管线。不种**半份**映射：缺列的映射会让导入校验按一条不存在的规则跑，
      比「缺映射配置」这个明确的错误更难发现（与 `AisleStation` 同一处置，空表是正确状态）。
      待确认：映射内容是由阶段四随导入管线一起落种子，还是写成随包内置的只读数据；
      ③ **`Capability` 的中文取值与代号的选择** —— 取值照 10 §152 / 15 §8.2 的中文原文落库。
      若评审认为枚举值应为 ASCII 代号（与 17 §九 的 11 个共享枚举风格一致），需改 `enum_column` 的
      `values_callable` 并**手工写迁移**回填存量行（同样是 D8 的「枚举值变化不产生 diff」代价）；
      ④ **`ConversationContext` 的 `recent_turns_json` 无条数上限与单条形状** —— 17 §八 只写「最近问句与
      结果卡片引用」，未给条数、保留策略与单条结构。本阶段按 JSON 容器落列（D4 只映射 17 §10 的
      6 类结构，本项不在其中，故落列理由与那 6 类不同：**文档未给形状时用一个容器装下，
      比替它定一张表安全**）。待确认是否需要一个「最多 N 轮」的口径（它同时决定上下文体积与脱敏范围）；
      ⑤ **`CapacityConfig` 没有变更人** —— 17 §七 只把该列列在权重配置上，本阶段不为它补列。
      待确认：容量阈值同样由人调（且直接影响 N 与预留池），是否需要与权重一致的审计列；
      ⑥ **`WeightConfig` 的列名与 14 号的因子名对应关系** —— 六列名（`weight_abc` / `weight_cap` /
      `weight_existing` / `weight_station` / `weight_batch` / `weight_continuity`）由本阶段按 17 §10.1
      示例的因子顺序拟定，14 号未给英文名。阶段三实现评分时须以本表为准，
      若届时发现需要改名（评分函数、理由 JSON、配置页面三处同时引用），改为手工迁移
- [ ] 9.5 收尾：分支 `phase-2/data-model-permission` 以 `--no-ff` 合并 `main` 并打 `v0.2.0`；验证：`git tag | grep v0.2.0` 命中，且工作区干净
