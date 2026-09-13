# data-import 变更提案

## Why

数据衔接层是最上游地基：阶段三的评分引擎（`recommendation-engine`）与阶段四的交易底座（`transaction-base`）都已就位，但「系统看到的世界」仍无数据来源——三类作业文件导入管线与 cap 基线在 `app/importer/`、`app/cap/` 里还是 docstring 骨架，数据导入页 p2 也是硬编码「通过」的静态 shell。没有干净的基准与准确的 cap，下游评分、批量分配、后验全是无源之水。

本 change 落地 `16` §1.1 的**本层**（时点标注 → 文件导入 → 口径校验 → 建立基准：PO→入库队列、DO→出库任务、快照→cap 基线 + 库存分布），即 P0 关键路径 `F1 → F2 → F9 → F3 → …` 的 **F1（文件级数据导入）+ F9（巷道 cap 自维护·基线）** 两环（F2 的字段映射已由 `16` §10.2 决策为单厂内置，本 change 落地该内置映射）。它是 `28-02`（入库域）前置检查所列 `28-01` 公共底座的导入/cap 部分。

## What Changes

- 新增 `ImportSession` 状态机（`DRAFT → VALIDATING → VALIDATED / FAILED → IMPORTING → IMPORTED → BASELINE / DISCARDED` 八态）及其乐观锁、重复导入判定（数据时点 + 文件校验和）。
- 新增三类作业文件（PO / DO / INV）的 8 步导入管线：上传 → 格式/编码识别（xlsx/csv，UTF-8/UTF-8-BOM/GBK）→ 字段映射（GTJ10036 内置，别名/去空格/全半角/大小写容错）→ 四层校验 → 回执 → 执行导入 → 建立基准。
- 新增四层校验（结构 / 字段命中 100% / 时点 / 业务）与「校验失败即阻断、不得带病入库」红线；选填字段缺失降级并在回执标注（不静默）。
- 新增建立基准：PO → 入库待推荐队列（`JobOrder` 置 `PENDING`）、DO → 出库拣配任务（`JobOrder` 置 `PENDING`）、INV → cap 基线全量重算 + 既有库位/批次分布（`InventoryItem`）。
- 新增 cap 基线全量重算（`16` §6.2）：按巷道对库位号 `[:2]` 切片聚合，重算 `cap_physical / cap_total / cap_reserved / cap_usable`，生成新 `Snapshot` 版本、旧版归档不删除；重算失败即回滚该批并提示重导。其中 `cap_reserved = cap_physical × 40%`（固定预留带，仅近站台巷道，不随占用波动），故 `AisleCap` 需新增 `cap_physical` 列。
- 新增成品清单 ABC 统计导入（脚本通道，非页面交互）：`scripts/import_abc.py` 读 BI 看板需求「成品清单」历史出入库流水 → 按料号聚合出库量 → ABC 分档（累计出库量占比 A≤70% / B≤90% / C 其余，阈值由 GTJ10036 实测数据自证）→ upsert `Material.abc_class`。统计类、幂等、不建基线、不进 `ImportSession`、不进 p2 页面、失败不阻断。
- 新增数据导入页 p2 数据层：`data-import.html` + `assets/app.js` 接入后端导入 API（时点卡、三文件上传、校验回执表、开始校验/执行导入两步，用既有 `setState` 四态渲染），替换硬编码「通过」结果。

## Capabilities

### New Capabilities

- `data-import`: 数据衔接层本层契约——三类作业文件的导入会话与 8 步管线、四层校验与阻断、建立基准（PO/DO 队列任务 + 快照 cap 基线 + 库存分布）、cap 基线全量重算、成品清单 ABC 统计导入（脚本通道），以及数据导入页 p2 数据层。

### Modified Capabilities

- `data-model`: 新增「ImportSession 状态机」需求（八态与合法迁移清单，对齐既有的「JobOrder 状态机」口径；状态取值已在「枚举登记范围与取值」登记，本 delta 补迁移契约）。

## 非目标

- **成品清单的「以出定入」出库量聚合**：成品清单仅收「ABC 分类」一项（走统计脚本通道，见 What Changes）；其另一派生产物「以出定入」出库量仍留待后续独立 change。
- **cap 对账与重置**（`16` §6.4 盘点导入重新建基线）：期初快照之后才触发的周期性人工盘点场景，非「建立基准」本身。
- **cap 异常告警**（`16` §6.5 负 cap / 超总格 / 漂移 / 预留池释放）：负 cap 与超总格源于台账增量（4a），漂移需盘点对比——均非期初基线重算范围。
- **巷道-站台主数据导入**（`16` §6.1「待补充导出」）：cap_physical 在巷道主数据到位前按快照库位去重格数近似，并在推荐理由标注「容量基于快照近似」；权威物理格数留待主数据导出后切换。
- **字段映射可视化配置**：`16` §10.2 已决策不做（v0.11 移除），不在「不做清单」之外新增。

以上均不与 CLAUDE.md「不做清单」11 项冲突。

## Impact

- **实体**：`ImportSession`（状态机 + 乐观锁 + 文件清单/校验结果/分流去向）、`Snapshot`（快照版本 + 归档）、`InventoryItem`（既有库位/批次分布）、`AisleCap`（快照时点冻结值，由基线全量重算写入；新增 `cap_physical` 列承载 `cap_reserved` 基）、`Material`（`abc_class` 由 ABC 统计脚本 upsert）。
- **API 路由**（按 `/api/*` 约定，`16` 附录B 的 `/api/v1` 前缀按 4a 既有端点惯例去 v1）：
  `POST /api/import/session` · `POST /api/import/upload` · `POST /api/import/validate` · `GET /api/import/result/{session_id}` · `POST /api/import/execute` · `POST /api/import/retry` · `GET /api/snapshot/current` · `POST /api/cap/recompute` · `GET /api/cap?aisle=`。
- **前端**：`backend/frontend/data-import.html` 与 `backend/frontend/assets/app.js`（新增导入页数据层与 API 封装，仅时点卡 + PO/DO/INV 三文件上传，不接线成品清单/ABC 上传位；不动设计令牌与角色菜单过滤等既有共享层）。
- **代码模块**：`app/importer/`（`detect` / `loaders` / `mapping` / `session` / `validate` 五模块，去除骨架 docstring 落地实现）、`app/cap/baseline.py`（基线全量重算）、`scripts/import_abc.py`（成品清单 ABC 统计导入）；`app/cap/reconcile.py`、`app/cap/alerts.py` 保持骨架（属非目标）。
- **KPI 指标**：无新增看板指标（本 change 是数据地基，只为下游提供干净基准）。
