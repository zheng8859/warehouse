> 排序说明：rules 要求「按关键路径 `F1 → F2 → F9 → F3 → F4 → …` 排序」，但本变更**不落在关键路径的任何一环** ——
> 它是数据模型清理（删一个无消费方的列），不新增也不修改任何 P0 功能。故按**依赖序**排：
> 前置确认 → 模型与迁移 → 测试 → 验收。逐条说明见第 5 组「不适用登记」。
>
> 验证方式的约定：本变更的验证是**模型层 pytest 断言 + 迁移往返 + autogenerate 空 diff**。
> `20-评测体系设计.md` 的场景编号（`SC-*` / `AC-*` / `CL-*` / `TR-*`）**没有一条覆盖 schema 变更**，
> 故本变更**不标注任何场景编号，也不声称达成任何一条**（见第 5 组第 2 条）。

## 1. 前置确认

- [x] 1.1 复核事实来源侧的改动**已先行完成且未回退**：`17` §3.3 字段枚举不含「库区号」、`14` §3.4 步 1 只有三判据、`16` 附录 A.1~A.3 与关键字段总表不含、`PRD` 数据来源表两行不含、`CONTEXT.md` 术语表无「库区」行。验证：`grep -rn "库区" "D:/成品库位智能推荐/产品设计/" --include="*.md" | grep -v ".bak"` 的残留只剩 `16` 三处「编号说明」与 `PRD`/`03`/`04` 的口语用法（后者指两个 WMS 仓库代码，是另一含义）；对 `.<时间戳>.bak` 做 `diff` 复核每份改动面
- [x] 1.2 确认迁移基线与 head：新迁移的 `down_revision` 必须指向 `2c10a7d2b5bf`（`InventoryItem` 所在那支）或其后续 head。验证：`cd backend && alembic heads` **只输出一个 head**（链无分叉）；`alembic history` 与 `migrations/versions/` 的 6 个文件对得上
- [x] 1.3 确认环境前置：`sqlite3` 版本 ≥ **3.35**（原生 `ALTER TABLE … DROP COLUMN` 的最低版本，`design.md` D2）。验证：`python -c "import sqlite3; print(sqlite3.sqlite_version)"`（本机实测 3.50.4）

## 2. 模型与迁移

- [x] 2.1 从 `backend/app/models/linkage.py` 的 `InventoryItem` 删除 `zone` 列及其逐列注释；改写模块 docstring **第 5 条** —— 从「保留但可空」改为「已于 `retire-zone-column` 移除」，并保留对 `16` A.1「库区号、生产日期不再需要」的引用（那句**仍然成立**，A.1 编号说明未动）。验证：该文件除 docstring 留痕句外无 `zone` 的**列定义或代码引用**；docstring 第 5 条不再引用 `14` §3.4（该判据已不存在）。**订正**：原判据写作「`grep -n "zone" app/models/linkage.py` 无残留」——与同一任务的动作要求（「改为『已于 `retire-zone-column` 移除』」即留痕）自相矛盾，且变更名本身含 `zone`，字面零命中不可能成立。已按**动作优先**执行，实测全文件仅 1 行命中（第 49 行的留痕句）
- [x] 2.2 把 `production_date` 的逐列注释改写为**独立成立**的表述 —— 原文写作「**同 `zone`**：17 列了、A.1 模版已移除 → 可空」，`zone` 一删这个比较就指空（`design.md` D4）。验证：`grep -n "同 \`zone\`" app/models/linkage.py` 无命中；该条注释**单独可读自证**（引 `17` §3.3 与 `16` A.1，不引 `zone`）
- [x] 2.3 新增 Alembic 迁移 `backend/migrations/versions/<rev>_衔接链_移除库存分布库区号.py`（命名沿用既有「链名_实体」中文风格）：`upgrade` 用**非 batch** 的 `op.drop_column('inventory_items', 'zone')`；`downgrade` 用 `op.add_column('inventory_items', sa.Column('zone', sa.String(32), nullable=True))`（`design.md` D2 / D3）。**不得**包 `with op.batch_alter_table(...)`。验证：空库 `alembic upgrade head` 成功 → `PRAGMA table_info(inventory_items)` 无 `zone` → `alembic downgrade -1` 列以可空形态回来 → 再 `upgrade head` 成功（往返）
- [x] 2.4 确认迁移走的是**原生 ALTER 而非整表重建**：迁移后 `sqlite_master` 中 `inventory_items` 的建表 SQL 仍逐字含 `CHECK(length(location_code) = 6)`、`CHECK(qty > 0)`、外键 `snapshot_id → snapshots.id`、复合唯一约束 `uq_inventory_items_snapshot_location_batch_material`。验证：一条查询断言 `sqlite_master.sql` 含上述四个片段（这是 D2 选择的**直接证据**；若走了 batch 重建，约束的书写形式会变）

## 3. 测试

- [x] 3.1 改 `backend/tests/models/test_linkage.py`：把 `test_inventory_item_zone_and_production_date_are_nullable` **拆开** —— 删掉 `zone` 的赋值与断言，保留 `production_date` 可空断言并改名 `test_inventory_item_production_date_is_nullable`（`design.md` D5）。验证：`python -m pytest tests/models/test_linkage.py -q` 全绿
- [x] 3.2 **新增**反向断言用例：`inventory_items` 的列名集合**不含** `zone` —— 写法照 `data-model` 规格「表结构不含软删除列」那条场景（`SELECT` 列名集合后断言不含）。验证：用例通过；并**人工核对一次**：把 `zone` 临时加回模型后该用例必须**失败**（防写出恒真的断言）
- [x] 3.3 复核全仓无 `zone` 的读写残留路径（`app/schemas/`、`app/importer/`、`app/engine/`、`app/api/`、`app/cap/`、`app/services/`）。验证：**`grep -rnE "\bzone\b" backend/app backend/tests --include="*.py"`** 的命中全为「说明 `zone` 已不存在」的注释与断言，**零读写路径**。**订正**：原判据漏了词边界，字面命中 24 行、其中 18 行是 `timezone`，故「零命中」不成立。加词边界后余 6 行 —— 第 6 行是测试函数名 `test_inventory_item_has_no_zone_column`（`_zone_` 两侧都是词字符，`\b` 同样漏掉它），逐行核对后确认全部是留痕

## 4. 验收与收尾

- [x] 4.1 跑阶段一既定的**迁移一致性验收动作**：空库 `alembic upgrade head` 后 `alembic revision --autogenerate -m "probe"`，确认产生**空 diff**（证明模型与迁移链一致；这是「删列」最容易出错的收尾 —— 模型删了而迁移没跟上，或反之）。验证：生成的迁移体除空 `upgrade()` / `downgrade()` 外**无任何 op**，核对后删除该探针文件
- [x] 4.2 全量回归：`cd backend && PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/ --tb=short -q`。验证：全绿、无新增跳过；用例数变化恰为 **+1**（3.2 新增）。**订正**：原判据的绝对数取自**过期基线** —— 它按 v0.2.0 的 546 passed 推算期待 547，实测 **548 passed**（`3.63s`，退出码 0）。原因是 `clarify/401-failure-semantics` 在 `v0.2.0` **打标之后**才并入本分支（净 +1），故 HEAD 的真实基线是 **547**；548 = 547 + 1，判据的**实质（变化恰为 +1）成立**。已实测钉死：临时换回 HEAD 版的 `test_linkage.py` 得 `1 failed, 546 passed`（失败的那条恰是旧 `zone` 用例，反证它确实依赖被删的列），即 HEAD 收 547 条
- [x] 4.3 更新 `CLAUDE.md` §11「当前状态」：补「`InventoryItem.zone` 已清退（`retire-zone-column`）」一句。若 `recommendation-engine` 的任务 10.4 已先在跑，则**合并进同一节**而不是各写一段。验证：`git diff CLAUDE.md` 只落在 §11
- [x] 4.4 交付前验证 `openspec validate retire-zone-column`（**不加 `--strict`** —— 中文规格会触发 RFC2119 假警报，见 `CLAUDE.md` 与既有记忆）。验证：退出码 0、`is valid`；本变更 `skip_specs: true`，故 `validate --specs` 不受影响

## 5. 不适用登记（rules 是 F1~F10 全链口径，逐条登记而非静默删）

| rule | 处置与归属 |
|---|---|
| 按关键路径 `F1 → F2 → F9 → F3 → …` 排序 | **不适用**：本变更不落在任何一环。它是数据模型清理，不新增/修改任何 P0 功能，故按依赖序排（前置 → 模型 → 迁移 → 测试 → 验收） |
| 每个任务标注**评测场景编号** | **无适用编号**。`20` 的 `SC-*`（评分正确性）/ `AC-*`（分配合规）/ `CL-*`（作业闭环）/ `TR-*`（集中度趋势）**没有一条覆盖 schema 变更**。本变更的验证是模型层断言 + 迁移往返 + autogenerate 空 diff，**不声称达成任何场景编号** |
| 必须包含基线层评测场景的落地任务 | **不适用**：本变更无行为变化（零 spec delta 的论证见 `proposal.md` 的 Capabilities 段） |
| 必须包含异常/边界任务（导入校验失败阻断 / cap 不足降级 / 快照过期阻断 / 乐观锁并发冲突） | **四条全不适用**，分别属 F1·F2（阶段四）、F3（阶段三 `recommendation-engine`）、出库侧（阶段四）、乐观锁（v0.2.0 已实现）。本变更唯一的异常边界是「SQLite < 3.35 时原生删列抛错」，已写为 `design.md` Risks 第一条 + 任务 1.3 |
| 必须包含后验与 `KpiSnapshot` 聚合的实现任务 | **不适用**：F8 / F10，属阶段四 |
| 单个任务拆到 ≤2 天 | **适用**：本变更全部任务为分钟~小时级 |
