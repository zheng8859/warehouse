## Context

动机见 `proposal.md` 的 Why。本节只记**本设计必须迁就的现状**。

**代码侧**：`zone` 在 `backend/` 下**只有三处**，且**没有一处是读写路径** ——

| 位置 | 性质 |
|---|---|
| `app/models/linkage.py` 模块 docstring 第 5 条 | 论证「按 `17` 保留列、按 A.1 置可空」的文字 |
| `app/models/linkage.py` 的 `InventoryItem.zone` 逐列注释 | 同上（并引用了现已不存在的 `14` §3.4 判据） |
| `tests/models/test_linkage.py::test_inventory_item_zone_and_production_date_are_nullable` | 断言其可空 |

没有 DTO 字段、没有 importer 映射、没有 engine 取数、没有 API 出参 —— `17` §10 的 6 类 JSON 结构里没有它，`14` 的 6 因子没有一个读它。

**表结构侧**（决定了删列能不能走原生路径）：`inventory_items` 落在迁移 `2c10a7d2b5bf`，含 `CHECK(length(location_code) = 6)`、`CHECK(qty > 0)`、外键 `snapshot_id → snapshots.id`、主键 `id`、复合唯一约束 `uq_inventory_items_snapshot_location_batch_material`（5 列），**且没有任何 `create_index`**。`zone` 不在主键 / 唯一约束 / 外键 / CHECK / 任何索引里。

**环境侧**（实测，非推断）：本机 `sqlite3` 库版本 **3.50.4**、Alembic **1.19.0**、Python 3.14.4。实测两项：

1. `sqlite3` 原生 `ALTER TABLE … DROP COLUMN` 可用（需 ≥ 3.35）。
2. 用 `MigrationContext` + `Operations` 对一张带 `UNIQUE` 与 `CHECK` 的表跑**非 batch** 的 `op.drop_column` ⇒ 成功，且建表 SQL 里 `UNIQUE` / `CHECK` / `NOT NULL` **逐字保留**（证明走的是原生 ALTER，不是整表重建）。

**事实来源侧**：`17` §3.3 / `14` §3.4 / `16` 总表与 A.2·A.3 / `PRD` 数据来源表 / `CONTEXT.md` 的改动**已先行完成**（含 `.bak` 与 `diff` 核对），逐处见 `recommendation-engine/design.md` D7 的改动面表。本变更是那次「先改设计文档」的**后半段**。

## Goals / Non-Goals

**Goals:**

- 让代码与已改的文档重新一致：`17` §3.3 不再列该字段 ⇒ 表里也不该有它，注释也不该再引用已删的判据
- 用**最不可逆面最小**的方式落这次 schema 变更：不重建表、不复制数据、不触碰台账
- 让「删了就真没了」成为**可被测试看住的性质**，而不是一次性的手工动作

**Non-Goals:**

- 不引入替代字段，也不为「库区」另立表达（`proposal.md` 非目标已述）
- 不动 `production_date`、不动库存行唯一键、不动其他 22 个实体
- 不清洗 `16` 三处「编号说明」与 `PRD` / `03` / `04` 的口语「库区」
- 不涉权限、API、前端、KPI；不新增 spec（零 delta 的论证见 `proposal.md` 的 Capabilities 段）

## Decisions

### D1 删列，而不是保留可空列

**决策**：从 `InventoryItem` 删除 `zone`。

**备选一：保留列，只把注释改对。** 被否 —— 列的存在本身即是漂移源：`17` §3.3 已不列它，下一个读模型的人会问「这列文档里怎么没有」，然后要么去 `17` 里加回来（把刚删的概念请回），要么把注释再改一遍。**删除是唯一能让文档与代码收敛的动作**。且它零成本：无读写路径、无 DTO、无存量数据。

**备选二：保留列并标 deprecated。** 被否 —— 本项目**不做软删除**（`data-model` 规格「代理键、业务键与无软删除」；`CLAUDE.md` §七「不做软删除，设计用『归档不删除 + 版本化』」）。给一个死列挂 deprecated 标记，正是软删除的变体。归档不删除针对的是**有历史价值的记录**（台账 / 快照 / 配置版本），不是**从未被写入过的列**。

### D2 迁移走**原生** `op.drop_column`，不走 `batch_alter_table`

**决策**：迁移体为 `op.drop_column('inventory_items', 'zone')`，**不**包 `with op.batch_alter_table(...)`。

**为什么这不是随手的选择**：Alembic 的 SQLite 方言里 `requires_recreate_in_batch()` 对 `drop_column` 返回 **True**（它对 `add_column` / `create_index` / `drop_index` 之外的操作一律返回 True）。也就是说，**一旦包进 batch 模式，就会走整表重建** —— 建新表、把数据搬过去、再重建外键与那 5 列复合唯一约束。对一次纯删列，这是把「改一个 schema 字符串」升级成「一次数据迁移」。

**而原生路径在本环境可用**（Context 已实测）：SQLite 3.50.4 支持 `ALTER TABLE … DROP COLUMN`，且实测约束逐字保留。所以选原生。

**前置条件与失败形态**：原生 DROP COLUMN 需 SQLite ≥ 3.35。若目标环境更低，`op.drop_column` 会**抛错**（而非静默降级成重建）—— 迁移快速失败，不留半成品，这是可接受的失败形态。已登记为 Risks 第一条。

**这一处与既有迁移的写法不同，是有意的**：`2c10a7d2b5bf` 与 `f01b0406d12c` 用了 `batch_alter_table`，因为那两处是**加外键**与**建表**（SQLite 改约束/加约束确实必须重建）。本次是删一个不被任何约束引用的普通列，**性质不同，故写法不同** —— 不是不一致。

### D3 `downgrade` 以可空文本列恢复，且**只恢复列、不恢复数据**

**决策**：`op.add_column('inventory_items', sa.Column('zone', sa.String(32), nullable=True))` —— 与 `17` 改前、`linkage.py` 改前的定义逐字一致（`String(32)` / `nullable=True`）。

**只恢复列不恢复数据**是必须写明的：删列即丢值，`downgrade` 无法把值变回来。声明这一点，是为了让「回滚」不被误读为「无损撤销」—— 真正无损的只有 schema 形状。**可接受**：`16` §394 已写明阶段三「四类输入全空」是预期状态，本列在任何已建库中都为空。

### D4 连带改动：注释里那个失效的「参照物」

模块 docstring 第 5 条与逐列注释都靠**两个已失效的引用**论证：`17` §3.3 的字段枚举、`16` A.1 的「已移除」。前者已随本次文档改动消失，后者**仍然成立**（A.1 的编号说明原文未动）。

逐列注释里还有一处**连带**：`production_date` 的注释写作「**同 `zone`**：17 列了、A.1 模版已移除 → 可空」—— `zone` 一删，这个「同」就没了参照物。故该注释必须改写成**独立成立**的表述（`17` §3.3 列了它、`16` A.1 的 INV 模版已移除它，故可空），而不是留一句指空的比较。**这是「改动时同步更新注释」规则的一处真实受力点**，容易漏。

### D5 测试：拆一条、加一条

- `test_inventory_item_zone_and_production_date_are_nullable` → **拆**：`zone` 断言删除；`production_date` 可空断言保留并改名（`test_inventory_item_production_date_is_nullable`）。
- **新增**一条反向断言：表结构**不含** `zone` 列。写法照 `data-model` 规格「表结构不含软删除列」那条场景的同款做法（断言列名集合），让「删了就真没了」被看住 —— 否则下一次有人从旧模版把它加回来时，没有任何用例会响。

### D6 迁移不影响 `08` §14.1 的任何 SLA

`08` §14.1 是**模块间接口契约**表（粒度是「单物料评分 ≤1s」「cap 查询 ≤100ms」等运行时路径）。本变更**不新增任何运行时路径**，也不改变任何既有路径的输入集，故该表数值**全部不受影响**，无需重述。

**`08` §14.1 没有为迁移定义 SLA**（它不描述 schema 变更）。故本变更不虚构迁移 SLA，只给**验收动作**：空库 `alembic upgrade head` → `PRAGMA table_info` 中 `zone` 消失 → `alembic downgrade -1` → 列以可空形态回来 → 再 `upgrade head`。这才是这次变更真正该被衡量的东西。

### D7 确定性不受影响（论证，不是承诺）

删列不触及任何评分/分配路径：`14` 的 6 因子没有一个读 `zone`（`cap` 因子读 `AisleCap`，`existing` 读 `location_code`，`batch` 读 `batch_no`，`continuity` 读 `material_code` + `location_code`，`station` 读 `AisleStation`，`abc` 读 `Material`）。**同样输入必得同样输出**这一性质不变，且本次**没有引入**任何新的不确定性来源（无随机、无时钟、无哈希序依赖）。

## Risks / Trade-offs

- [原生 `DROP COLUMN` 需 SQLite ≥ 3.35；更低版本会抛错] → 本机 3.50.4 实测可用。低版本下**快速失败而非静默重建**，可接受；已作为环境前置登记在 Migration Plan。
- [batch 模式会整表重建（复制数据 + 重建 FK 与 5 列复合唯一约束），风险高于收益] → D2 选原生路径，并给出实测证据（约束逐字保留）。
- [已建库中 `zone` 的**值**随删列永久丢失，`downgrade` 找不回来] → D3 显式声明「只恢复列不恢复数据」；且 `16` §394 写明阶段三输入全空，本列无存量。**如实声明这是不可逆的数据丢弃**，不淡化。
- [删列后，某个尚未更新的分支/工作副本若仍读写该列会在运行时报错] → 全仓 grep 已确认无读写路径（Context 表）；唯一风险面是**同名的本地分支**，属工程协作问题而非代码问题。
- [阶段四（F1 / F2 导入管线）若按**旧版** `16` 模版实现，会把库区号映射到一个已不存在的列] → `16` 附录 A.2 / A.3 的删行与「编号说明」就是阻断点；`proposal.md` 的 Impact 已把它写成对阶段四的前置约束。
- [「删列」被误读为「库区概念被否决」，连带去清洗 `16` 的编号说明与 `PRD` 的口语「库区」] → `proposal.md` 的非目标已显式禁止；D4 重申：前者是**移除留痕**（删了会让缺号被误读为漏抄），后者指 GTJ10023 / GTJ10036 两个 WMS 仓库代码，是另一个含义。
- [注释只改一半：删了 `zone` 的行，却留下 `production_date` 里「同 `zone`」的指空比较] → D4 专门点名这一处；tasks 里给它独立一条，避免被「顺手删掉」带过。

## Migration Plan

**部署顺序**（SQLite 单进程约束下）：

1. 停应用（或确认无写入）—— `CLAUDE.md` §四：应用必须单进程运行，不得多 worker 并发写。
2. `alembic upgrade head`（既有入口 `scripts/init_db.py` 亦走同一链）。
3. 验收：`PRAGMA table_info(inventory_items)` 中 `zone` 消失；`alembic revision --autogenerate` 产生**空 diff**（阶段一既定的验收动作，证明模型与迁移一致）；`python -m pytest tests/` 全绿。
4. 起应用。

**回滚**：

- `alembic downgrade -1` —— 恢复可空 `zone` 列（**不恢复数据**，D3）。本变更**不引入功能开关**：该列无消费方，回滚不需要业务侧开关；`CLAUDE.md` 的「功能开关式回滚、不删历史台账」针对的是**推荐功能**，本变更不触碰台账，故无此需求。
- 若已在生产库执行过 `upgrade` 且事后决定保留该列：`downgrade -1` 即可回到形状一致；**值需由源头重新导入**（本列本就无来源）。

**环境前置**：目标环境 `sqlite3` ≥ 3.35（原生 `DROP COLUMN`）。
