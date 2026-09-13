# data-import 技术设计

## Context

数据衔接层是最上游地基（动机见 proposal.md - Why）。本设计的直接输入：

- `app/importer/` 五模块（`detect` / `loaders` / `mapping` / `session` / `validate`）与 `app/cap/baseline.py` 现为 docstring 骨架；`app/cap/increment.py` 已在 `transaction-base`（4a）落地，负责台账同事务的 cap 增量。
- `data-import.html` 是硬编码「通过」的静态 shell；`assets/app.js` 无 fetch/API 封装，但有 `setState` 四态 hook 与 `MENU` 角色过滤可复用。
- `ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert` 五实体已在阶段二建表（`app/models/linkage.py`）；`AisleCap` 现无 `cap_physical` 列，其模块 docstring 与列注释写的是旧公式 `cap_reserved = cap_total × 40%`。
- 口径已定（本会话需求对齐结论）：**`cap_reserved = cap_physical × 40%`（固定预留带，仅近站台巷道，不随占用波动）**。这与 `openspec/config.yaml` context 里旧写的 `cap_total × 40%` 冲突，须一并修正。
- ABC 数据已实测（`BI看板需求 -GTJ10036.xlsx`「成品清单」sheet，441,194 行）：255 个有出库量的料号 → A 20 / B 31 / C 204，阈值 A≤70% / B≤90% 被数据自证，与 `16` A.4.1 实测吻合。

## Goals / Non-Goals

**Goals:**

- 三类作业文件（PO / DO / INV）的 8 步导入管线 + 四层校验 + 建立基准，可被三层 TDD（models/api/logic）覆盖。
- cap 基线全量重算，含 `cap_physical` 落库与 `cap_reserved = cap_physical × 40%` 口径落地。
- 数据导入页 p2 数据层（时点卡 + 三文件上传 + 校验回执表 + 两步交互，四态渲染）。
- 成品清单 ABC 统计导入（脚本通道，`Material.abc_class` upsert）。

**Non-Goals（设计层边界，不含 proposal 已列的 scope 边界）：**

- cap 对账 / 重置（`16` §6.4）与 cap 告警（`16` §6.5）——`reconcile.py` / `alerts.py` 保持骨架。
- 巷道-站台主数据导入（`16` §6.1「待补充导出」）。
- 字段映射可视化配置（`16` §10.2 已决策不做）。
- 成品清单的「以出定入」出库量聚合。

## Decisions

### D1：ImportSession 状态机 + 乐观锁 + 重复导入幂等

`ImportSession` 走 8 态 / 10 迁移（见 spec `data-import`「数据时点标注与导入即基准」与 `data-model`「ImportSession 状态机」）。并发用 `lock_version`（`ImportSession` 与 `JobOrder` 同为乐观锁目标，`app/core/concurrency.py` 既有辅助）。重复导入以「数据时点 + 文件校验和」判重：同时点同校验和 → 提示「跳过 / 覆盖重算」；已 `BASELINE` 会话再提交 → 幂等返回既有基线。

- 取舍：校验和用 SHA-256（文件内容级），存 `files_json`；「跳过」与「覆盖重算」是两个显式动作，绝不静默覆盖（决策权在人）。

### D2：8 步管线模块分解

按 `app/importer/` 既有五文件落地，职责单一、可独立测试：

| 模块 | 职责 | 输入 → 输出 |
|---|---|---|
| `detect.py` | 格式 + 编码探测 | 文件 → (xlsx/csv, UTF-8/UTF-8-BOM/GBK) |
| `loaders.py` | 解析成行 | 文件 → `list[dict]`（库位号按文本，禁列序号硬取） |
| `mapping.py` | GTJ10036 内置字段映射 | 源列名 → 内部字段（别名/去空格/全半角/大小写容错） |
| `validate.py` | 四层校验 | 行 → (通过 / 异常明细[文件+列+行+原因]) |
| `session.py` | 会话编排 + 回执 | 状态迁移 + `receipt_json` 落库 |

- 取舍：五模块而非单文件，是为了让「格式探测」「字段映射」「校验」三个最易错环节各自被 logic 层 TDD 锁定；`session.py` 只做编排不掺业务规则。

### D3：四层校验与阻断

结构（可解析、非空）→ 字段（模板列 100% 命中）→ 时点（已标注且不晚于当天）→ 业务（数量 > 0、库位号 6 位可切片、批号非空、仓库号 = GTJ10036、状态取值在枚举内）。任一阻断级异常即阻断（不得带病入库）；选填字段缺失降级并在回执标注（不静默）。回执 `receipt_json` 可展开到行级明细。

- 取舍：字段命中要求 100%（而非「尽量」），是防错位硬约束——`16` 明确「严禁按列序号硬取」；错列静默导入的代价远大于一次阻断。

### D4：建立基准分流

校验通过后 `POST /api/import/execute`：PO → `JobOrder`（`PENDING`，入库待推荐队列）、DO → `JobOrder`（`PENDING`，出库拣配任务）、INV → `InventoryItem`（既有库位/批次分布）+ cap 基线。三类文件独立校验与导入，某类失败不影响他类（spec「部分文件失败隔离」）；写入任一失败即回滚该批，不产生半成品基准。

### D5：cap 基线全量重算与 cap_physical 口径

快照导入成功（`IMPORTED → BASELINE`）时按巷道（库位号 `[:2]`）全量重算：

```
cap_physical = 巷道物理总格数（主数据到位前 = 快照库位去重格数近似）
cap_total    = cap_physical − 已占格数
cap_reserved = cap_physical × 40%（固定预留带，仅近站台巷道非零）
cap_usable   = max(cap_total − cap_reserved, 0)
```

- **`AisleCap` 新增 `cap_physical` 列**（迁移，`batch_alter_table`；SQLite 改表走 batch）。`cap_reserved` 基从 `cap_total` 改为 `cap_physical`，必须落库承载，不能只活在算式里。
- 修正 `linkage.py` 模块 docstring（第 13–15 行）与列注释（第 270–274 行）的旧公式，同步修正 `openspec/config.yaml` context 的同款旧公式。
- 生成新 `Snapshot` 版本、旧版归档不删除；重算失败即回滚该批并提示重导。

- 取舍：cap_physical 用「快照库位去重格数」近似而非等主数据，是为了让 F1/F9 不再卡在「巷道主数据待导出」上；近似性在推荐理由标注「容量基于快照近似」（spec 与 proposal 非目标已记）。

### D6：成品清单 ABC 统计导入 = 脚本通道

`scripts/import_abc.py`（非 `ImportSession`、非 p2 页面）：

1. 读 BI 看板需求「成品清单」sheet（`openpyxl` read_only 迭代，441k 行约 40s，路径可配，默认 `D:\成品库位智能推荐\BI看板需求 -GTJ10036.xlsx`）。
2. 按料号聚合出库量（`移动类型 == 出库` 的 `数量` 求和），降序累计占比分档：≤70% → A、≤90% → B、其余 → C。
3. `Material.abc_class` upsert：料号存在于 `materials` 则更新 `abc_class`，不存在则记告警并跳过。
4. 幂等（重复跑覆盖同料号 `abc_class`）；失败不阻断（不写 `Snapshot` / `ImportSession` / `JobOrder`）。

- 取舍：脚本读原始 xlsx 而非预抽 CSV，让「整理 + 导入」留在系统内（用户原话）；阈值 A=0.70 / B=0.90 首期硬编码在脚本内（单厂内置），不做可视化配置。
- 顺序约束：ABC 导入应在至少一次 INV/PO/DO 导入之后跑（`Material` 行由文件导入携带 `料号`+`品名`；缺失时告警跳过，属「失败不阻断」的延伸）。

### D7：前端 p2 数据层

`data-import.html` + `assets/app.js` 接 `/api/import/*`：数据时点卡、三文件上传（PO/DO/INV）、校验结果表（文件/解析条数/字段命中/口径异常/状态）、「开始校验 / 执行导入」两步。复用 `setState` 四态渲染，替换硬编码「通过」。**不接线成品清单/ABC**（无上传位、无结果卡）。不动设计令牌与角色菜单过滤等既有共享层。

### D8：字段映射单厂内置 GTJ10036

模板列（INV 8 列 / PO 8 列 / DO 8 列）→ 内部字段的内置映射，别名 + 去首尾空格 + 全半角归一 + 大小写不敏感。库位号按 6 位文本读取（Excel 数值化丢前导 0、日期序列号先转日期）。不做可视化配置。

## Risks / Trade-offs

- **[cap_physical 近似 → 容量误差]**：主数据到位前用快照库位去重格数近似，若快照只覆盖部分库位，cap_physical 偏小、cap_usable 偏紧。→ 推荐理由标注「容量基于快照近似」，主数据导出后切换权威值（非目标）。
- **[板-格换算未确认 → 已占格数口径不稳]**：`cap_total = cap_physical − 已占格数` 的「已占格数」依赖 `16` A.4「数量→占用格数」折算（D14 待确认）。→ 本 change 以「有库存的库位去重格数」近似已占格数，换算规则确认后单列修正（见 Open Questions）。
- **[ABC 导入依赖 Material 已存在]**：若 `Material` 主数据尚未由文件导入补齐，脚本会大量「告警跳过」，`abc_class` 落不全。→ 顺序约束（先导 INV/PO/DO 再跑 ABC）+ 告警跳过不阻断；主数据补录是独立关注点。
- **[大文件解析耗时]**：441k 行 xlsx 迭代约 40s，3 文件最大场景仍在「解析 ≤5min/文件」SLA 内。→ `openpyxl` read_only + 流式聚合，不整表载入内存。
- **[部分文件失败隔离 vs 一致性]**：INV 失败 = 无 cap 基线，下游容量受限但 PO/DO 可先行。→ 回执明确「成功 N 类 / 失败 M 类」，不静默吞失败。
- **[SQLite 单写]**：导入与台账共用单进程单写。→ 单进程运行红线 + 乐观锁兜底（不新增 worker）。

## Migration Plan

- **新增列**：`aisle_caps.cap_physical`（INTEGER NOT NULL，默认 0 供存量行，新重算覆盖）——`batch_alter_table` 迁移。
- **注释修正**：`linkage.py` 模块 docstring + `cap_reserved` 列注释、`openspec/config.yaml` context 的旧公式 → `cap_physical × 40%`。
- **无数据迁移**：新增骨架落地，`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` 既有结构不变（除上述列）。
- **回滚**：功能开关式；旧公式只影响预留带数值，不影响历史台账（不删台账）。

## Open Questions

- **板-格换算规则（`16` A.4，D14）**：数量（箱）→ 占用格数的折算仍未确认，影响 `cap_total` 的「已占格数」。本 change 以「有库存库位去重格数」近似，确认后单列修正 `cap_total` 计算。此问不改变本 change 的模块分解与任务边界。
- **`Material` 主数据的生产来源**：ABC 脚本只 upsert 既有 `Material.abc_class`；`Material` 行（料号/品名/箱规板规）的生产来源（文件导入侧 upsert 还是独立主数据导入）属另一关注点，不在本 change 内解决，不影响 ABC 脚本契约（缺失告警跳过）。
