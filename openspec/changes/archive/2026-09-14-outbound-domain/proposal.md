## Why

出库是兑现「取得顺不顺路」的环节：入库把同物料/同批收拢到少且相邻巷道，出库才能「顺路取」而非「全库筛」。当前后端已具备出库作业单的确认链（`confirm_outbound` 走 `CONFIRMED → EXECUTED → VERIFYING → VERIFIED`）与台账/后验底座，但缺三样：**顺路取只读派生**（按巷道聚合既有库存生成拣货顺序，不评分、不改落位）、**批量顺路取接口**（把一组 `OUTBOUND` 作业单批量派生成拣货顺序），以及**出库确认把最终拣货路径写入台账**（供后验算加权集中度）。补齐后，出库域从「导入 DO → 生成顺路取 → 逐单处置 → 确认 → 写台账 + 后验」这条 7 步管线才能在后端走通。

本变更落在关键路径 `F1 → F2 → F9 → F3 → F4 → F5 → F6 → F7 → F8 → F10` 的**出库兑现段**：出库作业管线复用已落地的 `F1`（DO 导入）、`F5`（确认 L1）、`F7`（台账）、`F8`（后验），本变更补的是 `F10` 的「出库顺路取」派生与其接口，以及出库侧的台账/后验口径细化。

## What Changes

- **新增顺路取只读派生**（15-03-F02）：以系统自持库存视图（`version_no` 最大快照的 `InventoryItem` 分布，台账增量维护）为输入，按巷道（库位号 `[:2]`）聚合既有库位/批次，产出拣货顺序 + 需遍历巷道数 + 加权集中度是否超标（80% 拣货量落 ≤N 巷道，N=5 可配）。**不调 `engine.invoke`、不重新决定落位、不写 `InventoryItem` / `Ledger`**（只读派生）。
- **新增批量顺路取接口 `POST /api/job/batch/pick-sequence`**（15-03-F08）：对一组 `OUTBOUND` 作业单批量派生顺路取；货未入库（库存视图无对应分布）分列 `not_in_stock` 提示、不阻断（DO 停留 `PENDING`）；幂等键 = `bulk_batch_no` × 库存视图版本（视图未变返回既有方案、视图推进重新派生）；集中度超标仅高亮放行、不拦截。
- **出库确认记录最终拣货路径**：`ConfirmItem` 增 `pick_path` 字段（操作员确认/微调后的最终拣货顺序），出库确认落台账时把确认值写入 `pick_path_json`，出库侧 `source_location_code` 落空（多巷无单一源库位）。
- **出库后验改读台账拣货路径**：`verify` 出库后验从 `pick_path_json` 反解 `pick_qty_by_aisle` 算加权集中度，替换当前「单源库位、集中度恒为 1」的口径。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `transaction-base`：新增两条需求——①「顺路取只读派生」（15-03-F02，按巷道聚合、不评分不改落位不写账）；②「批量顺路取接口」（`POST /api/job/batch/pick-sequence`，货未入库分列提示、幂等键、超标高亮）。修改两条既有需求——③「三类作业确认与落位执行」补出库侧「确认记录最终拣货路径（`pick_path_json`）、`source_location_code` 可空」；④「同步后验与三口径判定」补出库后验「加权集中度取自台账拣货路径」的数据来源口径。

## Impact

- **实体**：`JobOrder`（只读 + 回写 `bulk_batch_no`）、`RecommendationPlan`（新增 `plan_kind=PICK` 方案写入）、`Ledger`（`pick_path_json` 写入确认值）。无新字段、无迁移。
- **API 路由**：新增 `POST /api/job/batch/pick-sequence`（挂 `require_permission(outbound.operate)`，对齐 `POST /api/allocate/batch` 的端点级鉴权）；修改 `POST /api/job/batch/confirm`（`ConfirmItem` 增 `pick_path`）。复用 `GET /api/jobs?type=outbound`（由兄弟变更 `inbound-domain` 以类型无关端点提供，本变更不 spec 不实现）。
- **服务**：新增顺路取派生函数（`app/services/outbound.py` 域内）；改 `confirm` 链传递 `pick_path`；改 `verify.py::_pick_qty_from_ledger` 读 `pick_path_json`；改 `cap/increment.py` 出库按 `pick_path_json` 逐巷扣减（放宽出库源库位可空后的配套）。
- **schema**：新增 `PickSequence`（17 §10.2 形状）；改 `ConfirmItem`。
- **权限**：沿用既有资源标识 `outbound.operate`（pick-sequence 端点级鉴权）与 `outbound.view`（队列查询），不新增资源。
- **KPI**：支撑 `拣货量加权集中度 80% ≤5 巷道` 在出库侧的落地度量（F10 出库顺路取区）。

## 非目标

- 不重做出库确认链 / 台账 / 后验的底座（`transaction-base` 已落地，本变更只补出库侧的派生、接口与口径细化）。
- 不做「主单 + 明细行」台账（`LedgerLine`）——15-01 §4.2 / 15-03 §3.2 的「源库位多行」在 v1 以扁平 `Ledger` 的 `pick_path_json` 承载，`LedgerLine` 属跨域底座升级，另立变更。
- 不做出库队列查询端点（`GET /api/jobs?type=outbound` 由 `inbound-domain` 以类型无关端点提供）。
- 不做移库域（28-04）、不做入库队列前端 p3（属 `inbound-domain`）。
- 不新增写端点之外的实体字段；仅一处 CHECK 迁移（放宽出库 `source_location_code` 可空，见 design D7）；不引入前端框架/构建工具。
- 出库不建降级链（顺路取只读派生，质量由入库推荐决定）；微调不校验 80%≤N（仅后验度量）。
