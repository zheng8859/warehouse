## Context

动机见 `proposal.md`。这里只记影响方案的现状与约束。

**现状**：`app/models/` 的 10 个模块只有 docstring 与实体清单，无一张表；`app/core/db.py` 已实现引擎与四条 PRAGMA
（`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout`、`synchronous=NORMAL`），但从未建过表；
`app/api/permissions.py` 的矩阵与纯函数已就绪且零测试；`core/enums.py` 的 11 个枚举已就绪。
`backend/.venv` 可用，`alembic` 已在 `requirements.txt` 中但从未使用。

**约束**：

- `17` §9 表外还有 4 个实体局部值域（见 Decisions D2），`17` §九 与 body 的关系已被订正为「§9 只列跨模块共享枚举」
- 事实来源文档已完成两处订正：`17` §9 的 `account_status` 增 `rejected`、`17` §9 增口径声明、`13` §5.2 增 `rejected`
- 首期单厂 `GTJ10036`；`08` §14.3 的 TD-1「单厂编码写死」要求在 Phase 4 前偿还 —— 全实体携带 `warehouse_id` 是本阶段对该债务的前置让步
- 本阶段**不产出前端**，故「零构建」规则在本阶段无落点（阶段七适用）

## Goals / Non-Goals

**Goals:**

- 23 张表可建、可迁移、可测；字段口径与 `17` §2~§8 逐条对得上
- 让阶段三~七能**直接依赖**字段名与枚举取值，不必回头改模型
- 把「台账只有一套」「`ledger.write` 无手动入口」两条红线变成**可执行的断言**，而非文档里的句子

**Non-Goals:**

- 不做端点级资源鉴权（403）—— 见 Decisions D9
- 不做 cap 的**计算**（全量重算 / 事务内增量），只把其**形状**定死（D5）；计算属阶段四
- 不做任何业务服务层、引擎、导入管线、前端
- 不引入新的第三方依赖（除 `alembic`，已在 `requirements.txt` 内）

## Decisions

### D1 版本语义三分，列名不共用

`17` 号里「版本」承载三种互不相干的语义，若共用一个列名，最典型的故障是把乐观锁当业务版本读，红线「`EXECUTED` 后不重复写台账」当场失效。

| 语义 | 列名 | 类型 | 谁用 | 对用户可见 |
|---|---|---|---|---|
| 乐观锁（并发） | `lock_version` | 整数 | `JobOrder` / `ImportSession` | 否 |
| 配置型业务版本 | `version_no` + `effective_at` | 整数 + 时间 | `WeightConfig` / `CapacityConfig` | 是 |
| 不可变基线 | `snapshot_id`（FK → `Snapshot.id`） | 外键 | `AisleCap` | 是 |

**「当前配置版本」的取法**：`生效时间 <= now` 中 `version_no` 最大者，**不加显式指针列**。备选是加一列 `current_version_id`，被否 —— 指针与版本表会双写，且指针写失败即产生「版本表与指针不一致」的静默错误。

**`Snapshot` 的业务键不放时间戳串**：`17` §10.5 的 JSON 里 `snapshot_version` 形如 `"2026-09-08T00:00"`，那是**展示形状**。存储层用 `Snapshot.id` 作被引用键，JSON 字段由连接渲染。备选是直接把时间戳串当外键存进 `AisleCap`，被否 —— 时间戳串没有唯一约束，且改基线时间会级联污染引用。

### D2 枚举落点：11 个共享的进 `enums.py`，4 个局部值域留在实体模块

`17` §9 声明 11 个枚举，但 `17` 的实体字段表另定义了 4 个值域：`RecommendationPlan` 的**方案类型**（分配 / 顺路取 / 收拢）、`CapAlert` 的**告警类型**（负 cap / 超总格 / 增量失败 / 漂移超阈值）与**是否已处理**、`Deviation` 的**状态**（未处理 / 已发起移库 / 已改善）与**成因分类**（新入库收拢不达标 / 历史库存拖累）。

本阶段把 `17` §9 订正为「§9 只列跨模块共享枚举」，于是 4 个局部值域**定义在各自实体模块内**，不进 `enums.py`。

- 备选一：把 4 个并入 `enums.py`（变 15 个）。被否 —— `26` / `CLAUDE.md` / `config.yaml` 三处都引用「11 枚举」，且 §9 会变成大杂烩，失去「跨模块共享」这个筛选标准。
- 备选二：4 个值域一律用自由字符串。被否 —— 取值只活在散文里，错拼无法拦截，「校验失败阻断」的红线落不到实处。
- `AccountStatus` 补 `REJECTED` 取值（**枚举个数仍为 11**，改的是取值不是数量）。

### D3 取值约束落两层：Python 枚举 + DB CHECK

用 `sa.Enum(PyEnumType, native_enum=False, create_constraint=True, validate_strings=True)`，在 SQLite 上渲染为 `VARCHAR + CHECK`。备选是只在 Python 侧校验，被否 —— 绕过 ORM 的写入（迁移脚本、手工 SQL、将来的批量导入）会带进脏值，而本项目的口径全部要靠取值成立。`Role` 与 `AccountStatus` 保持 lowercase，其余 UPPERCASE —— 不做「顺手统一」。

### D4 JSON 列与取数

`17` §10 的 6 类 JSON 结构落列如下，采用 SQLAlchemy `JSON` 类型并给引擎配 `json_serializer` / `json_deserializer`（`26` 已提示 SQLite 下 JSON 会以字符串读回）：

| `17` §10 结构 | 落列 | 说明 |
|---|---|---|
| 导入校验回执 | `ImportSession.receipt_json` | 导入侧回执，含分流去向 |
| cap 快照 | `Snapshot.cap_snapshot_json` | 按巷道聚合的 cap（`17` §3 快照字段含此项） |
| 推荐理由 | `RecommendationPlan.payload_json` | 含 6 因子分值、`degraded` / `degrade_reason` |
| 顺路取顺序 | 同上，`job_type = OUTBOUND` 时 | 出库是**只读派生**，此处只存派生的顺序结果 |
| 收拢方案 | 同上，`job_type = RELOCATE` 时 | 移库只调巷道，**不改批号** |
| KPI 卡片 | `KpiSnapshot.card_json` | 本阶段只建列，不填口径 |

三类作业方案共用一个 `payload_json` 而非三列，依据 `26` Step 4 的「`JobOrder` 三类共用 `job_type` 区分」口径。

### D5 cap 的量与增量，在表形状上就分开

`16` 的「快照导入全量重算」与「事务内增量」是两回事，模型层必须让它们不可混淆：

- **全量重算的产物** = `AisleCap` 一行/巷道/快照，带 `snapshot_id`，旧行随旧快照归档保留 → 容量演进可追溯（`17` §12）
- **事务内增量** = 与台账同事务写入的 `cap_used` 变动，**不新增平行账**，靠 `Ledger` 作唯一来源
- **漂移与异常** = `CapAlert`，其引用必须**恰好一个**非空：

```
CapAlert
  snapshot_id   (FK, nullable)  ─┐
  ledger_txn_id (FK, nullable)  ─┴─ CHECK( (snapshot_id IS NULL) <> (ledger_txn_id IS NULL) )
  alert_kind    (局部值域)  kind ∈ {负cap, 超总格, 增量失败, 漂移超阈值}
  handled       (是否已处理)
```

备选：把 `CapAlert` 拆成「快照告警」与「事务告警」两表。被否 —— 告警的消费方（阶段四的对账与看板）需要一次查询拿到全部告警，两表会把排序与分页变成 union。备选：不加 CHECK。被否 —— 一条既无快照又无事务的告警无法定位，属于无法处置的脏数据。

`AisleCap` 的 `cap_total` / `cap_reserved` / `cap_usable` 三列由计算层填，本阶段只保证列存在、口径写进列注释；`cap_reserved` 仅在近站台巷道非零（默认 40%）。

### D6 密码哈希直接用 `bcrypt`，不用 passlib

本机 passlib 1.7.4 + bcrypt 5.x + Python 3.14 下调用即抛 `ValueError`。直接用 `bcrypt` 模块，代价因子 12。备选 `argon2-cffi` 被否：引入新依赖，且 `CLAUDE.md` 已指定 bcrypt。

### D7 会话凭据自实现，不做算法协商

用标准库 `hmac` + `hashlib` + `base64` + `json` 实现 HS256 签发的凭据，不引入 PyJWT / python-jose。理由是**收窄攻击面**：自实现只认一种算法、不做 header 里的算法协商，从结构上排除了 `alg=none` 与算法混淆两类攻击；`13` §7.2 的 claim 集合是封闭的（`user_id` / `role` / `status` / `iat` / `exp` / `warehouse_id`），不需要通用 JWT 库的表达力。备选 PyJWT 被否 —— 表达力换来的正是本产品不需要的协商面。

必须做到：签名比较用 `hmac.compare_digest`（常量时间）；解析时**先**固定算法再验签；载荷解码显式指定 UTF-8；`exp` 校验以服务端时钟为准。

### D8 Alembic 独占真实库 schema

- 真实库的唯一建库路径 = `alembic upgrade head`；**禁止** `create_all` 建真实库
- `scripts/init_db.py` 退化为便捷入口：执行 `upgrade head` 后灌开发种子，**不再自带建表逻辑**
- 测试用内存库 + `create_all`（`26` 的「基础设施常见问题」表本就如此开方）—— 测试库是瞬时的，模型即事实来源，不引入迁移
- **规则**：任何模型改动必须与一份 migration 同一个 commit。备选「开发期用 `create_all`、上线前补迁移」被否 —— 两条建库路径必然漂移，且 `autogenerate` 会产生大量假 diff。

### D9 端点级资源鉴权不在本阶段

`13` §6.2 将 `PermissionChecker` 明列为路线图（M5）。本阶段交付**矩阵数据 + 纯检查函数 + 全组合断言**，不给业务端点挂鉴权依赖。备选「顺手加上 403 依赖」被否 —— 那正是 `13` 明说 v1 不要的过度工程，且会给尚未存在的业务端点造出未测表面。spec 中已把该边界写成一条**「系统不得」**，将来 M5 落地时以 MODIFIED 显式改它。

### D10 权限标识与矩阵对照

`resource.action` 标识共 17 个，与 `13` §2.1 的资源表逐条对应：
`account.manage`、`data_import.view|operate`、`inbound.view|operate`、`outbound.view|operate`、
`relocate.view|operate`、`kpi.view`、`config.view|configure`、`conversation.view|operate`、
`ledger.view|write`、`engine.invoke`。

`ledger.write` 与 `engine.invoke` 属 `AUTO_ONLY`，对所有角色恒 `False`。矩阵实现**不改**（已是 `13` §2.2 的逐条搬运），只补测试。

> **已核出的文档冲突（以 `13` §2.2 为准）**：`26` 附录B 把 `outbound.operate` 给主管标为 ✅，与 `13` §2.2 的 ❌、§2.3「主管 outbound 仅查看」、§3.1「✅ 监督」三处不符。`app/api/permissions.py` 站 `13`。同理 `26` 附录A 把 `AisleCap` 归入「主数据」，与 `17` §3「衔接链：`ImportSession` → `Snapshot` → `InventoryItem` / `AisleCap`」不符，分组以 `17` 为准。

### D11 8 组 TDD 与 4 条数据链的映射

`26` Step 3 要求 tasks 覆盖「全部 8 组 TDD 循环」；`17` 的组织是 4 条数据链。两者都要满足，映射如下（tasks.md 按左列排序，`app/models/` 按右列落文件）：

| `26` 附录A 的 TDD 组 | 落哪个文件（`17` 数据链） |
|---|---|
| A 主数据（`Warehouse`/`Aisle`/`Location`/`AisleStation`） | `models/master_data.py` |
| B 物料（`Material`/`Batch`） | `models/master_data.py` |
| C 衔接（`ImportSession`/`Snapshot`/`InventoryItem`/`CapAlert`） | `models/linkage.py` |
| A∩C 的 `AisleCap` | `models/linkage.py`（依 `17` §3 归衔接链） |
| D 作业（`JobOrder`/`RecommendationPlan`/`Ledger`/`Verification`/`Deviation`） | `models/job.py` |
| E 度量（`KpiSnapshot`） | `models/kpi.py` |
| F 身份（`Account`） | `models/identity.py` |
| G 配置（`WeightConfig`/`CapacityConfig`/`FieldMappingConfig`/`PromptTemplate`） | `models/configuration.py` |
| H 对话（`ConversationContext`） | `models/configuration.py` |

### D12 测试夹具

`tests/conftest.py` 提供：内存库引擎（**显式开 `foreign_keys=ON`**，SQLite 默认关闭，不因「内存库」而省略）+
`create_all` + `yield` session；父表记录的建夹具**必须按依赖链顺序**（`26` 已提示外键失败）.夹具缺 `conftest.py` 时 `pytest` 会以 `fixture not found` 报错，故夹具本身就是第一批任务之一。

## Risks / Trade-offs

- [23 张表一次落地，单批粒度过大，中途失败难以定位] → 按 D11 的 8 组分批，每组自带可独立跑绿的测试；每组一个 commit
- [`autogenerate` 与模型漂移，迁移脚本与模型不一致] → 改模型必须带 migration 且同 commit；以「空库 `upgrade head` 后 `autogenerate` 应产生空 diff」作为验收动作
- [自实现 JWT 的经典坑：非常量时间比较、接受未知算法、时钟处理] → D7 已逐条写明；测试覆盖「篡改载荷」「过期」「错误签名」「`alg` 被替换」四个场景（`26` 完成标准 #2 要求前三个）
- [`bcrypt` 5.x 对超过 72 字节的密码**抛错**而非静默截断] → 在凭据设置入口限制密码长度上限并在超限时给出明确错误信息，不把异常泄漏成 500
- [乐观锁被误用为业务版本，红线失效] → D1 的列名三分 + 一条断言「两列不共用」；`EXECUTED` 后重复写台账的用例进 `tests/logic/`
- [权限矩阵测试把错误值固化成「正确」] → 矩阵已与 `13` §2.2 逐条比对；`26` 附录B 的 `outbound.operate` 差异已记录并以 `13` 为准（D10）
- [`openspec validate` 报 21 条「应含 SHALL/MUST」] → 有意为之：项目 spec 规则 1 要求中文「系统必须 / 系统不得」，警告文本本身也限定为 "for English specs"。不修正，以免中英混排违反「规格一律中文」
- [本阶段不建 `AuditLog`，`13` §4.4 的审计字段契约为路线图] → 属已声明的非目标；红线里的可追溯由「台账 + 后验 + 操作记录」在阶段四承担

**性能目标**（引用 `08` §14.1 的接口契约 SLA）：

| 契约 | SLA | 在本阶段的落点 |
|---|---|---|
| F9(cap) → F3(评分)：cap 查询 | ≤ 100ms | `AisleCap` 读路径，需 `(warehouse_id, snapshot_id, aisle)` 索引 |
| F6(库位族) → F7(落位)：落位写入 | ≤ 200ms | `Ledger` 写入路径，需 `(warehouse_id, order_no)` 索引 |
| F1(导入) → F2/F9：文件解析 | ≤ 5min/文件 | `ImportSession` 记录解析起止时间以支撑该 SLA 的度量 |

`08` §14.1 **没有**为登录/凭据校验定义 SLA。本阶段自设工程上界：凭据校验不得引入任何外部调用，单次校验与 `bcrypt` 验证合计 ≤ 500ms（与同表中「F5 → F6 ≤ 500ms」的交互级量级一致）。代价因子 12 的 `bcrypt` 约在百毫秒量级，落在此界内。

**确定性**：本阶段的核心计算（状态机迁移、权限判定、cap 三指标读取）全部是纯函数式查表与比较，同样输入必得同样输出，不含随机搜索、不调外部模型。唯一用到随机数的地方是 `bcrypt` 的**加盐**与凭据的 `iat`/`exp` 时间戳 —— 前者是每个口令独立的存储形态、不影响「同样输入必得同样输出」的业务语义，后者属会话元数据。核心链路零外部依赖：签名与哈希全部走标准库。

**cap 的两条路径**：本阶段只保证 D5 的表形状能让「全量重算」与「事务内增量」在存储上分开，并让漂移可对账（`CapAlert` 的 `alert_kind` 含「漂移超阈值」）。计算逻辑与对账流程属阶段四，不在本阶段验收。

## Migration Plan

- 本阶段**无生产数据、无数据迁移**。首次建库 = `alembic upgrade head`（空库执行必须无错，即 `26` 完成标准 #6）
- 顺序：先 `proposal`/`specs`/`design` 定稿，再按 D11 的 8 组逐组实现；每组绿了才进下一组
- 回滚策略：本阶段**无功能开关需求**（不改变任何对外行为，只新增表与凭据端点）。代码回滚 = 分支不合并；
  数据库回滚 = 删除 SQLite 文件重建（阶段二无数据），**不涉及删除历史台账** —— 该红线自阶段四起才真正生效，
  但「不提供删除台账接口」已在本阶段写成断言（spec `data-model` 末条）
- `openspec/config.yaml` 的 `account_status` 取值需同步补 `rejected` —— 建议作为本阶段**第一笔提交**，因为它是事实来源订正的直接后果，留着就与 `17` §9 漂移

## Open Questions

- `item_status` 的三个已知取值（合格 / 待检 / 冻结）以 GTJ10036 的实际导出为准，首期导入前无法定全集 → 该列按文本存储、**不建 CHECK**，取值由 `FieldMappingConfig` 归一。此为可延后项：不改变 spec、方案或任务拆分
- 凭据 `iat` / `exp` 校验的时钟偏移容忍窗口（leeway）取值 → 默认 0 秒，实测有需要再调。可延后
- 密码长度上限的具体数值（需小于 `bcrypt` 的 72 字节输入界）→ 默认 64 字符，待与现场确认后定。可延后
