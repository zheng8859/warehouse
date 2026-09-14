## Why

移库是补救「历史欠账」的环节：入库推荐只能管新货，旧货散落需主动收拢，把散落板收回主巷道以改善历史库存的集中度。当前后端已具备移库作业单的确认链（`confirm_relocate` 走 `CONFIRMED → EXECUTED → VERIFYING → VERIFIED`）、台账/后验底座与类型无关的队列查询端点（`GET /api/jobs?type=relocate`），但缺两样：**收拢方案生成**（目标巷道 = 主巷道的本地规则 + 三重校验 + 降级链）与**批量收拢方案接口**。补齐后，移库域从「KPI 偏离 → 生成方案 → 逐单处置 → 确认 → 写台账 + 后验」这条 7 步管线才能在后端走通。

本变更落在 `08` 号 **M1 移库补救**（P1 路线图，**不在 P0 关键路径 `F1→F10` 上**），属补救成本、不计入北极星；它复用已落地的 `F5`（确认 L1）、`F7`（台账）、`F8`（后验），本变更补的是「移库收拢方案」派生与其接口。

## What Changes

- **新增收拢方案只读派生**（15-04-F02）：以系统自持库存视图（`version_no` 最大快照的 `InventoryItem` 分布，台账增量维护）为输入，按**批号**聚合该批号在非主巷道的散落板，产出主巷道（= 该物料库存最集中的巷道）+ 三重校验 + 预期跨巷道改善（`17` §10.3 形状）。**不调 `engine.invoke`、不写 `InventoryItem` / `Ledger`**（本地规则）。
- **新增批量收拢方案接口 `POST /api/job/batch/relocate-plan`**（15-04-F08）：对一组 `RELOCATE` 作业单批量生成收拢方案；目标巷道 cap 充足性校验失败时降级到次选巷道（板数第二多），仍不可行则该单移出批量（`degrade_reason` 可追溯，降级不静默）；幂等键 = `bulk_batch_no` × 库存视图版本（视图未变返回既有方案、视图推进重新派生）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `transaction-base`：新增两条需求——①「收拢方案只读派生」（15-04-F02，按批号聚合、主巷道 + 三重校验、不评分不改落位不写账）；②「批量收拢方案接口」（`POST /api/job/batch/relocate-plan`，cap 不足降级次选、仍不足移出批量、幂等键）。

## Impact

- **实体**：`JobOrder`（只读 + 回写 `bulk_batch_no`）、`RecommendationPlan`（新增 `plan_kind=CONSOLIDATE` 方案写入）。无新字段、无迁移。
- **API 路由**：新增 `POST /api/job/batch/relocate-plan`（挂 `require_permission(RELOCATE_OPERATE)`，对齐 `POST /api/allocate/batch` 的端点级鉴权）。复用 `GET /api/jobs?type=relocate`（由兄弟变更 `inbound-domain` 以类型无关端点提供，本变更不 spec 不实现）。
- **服务**：新增收拢方案派生函数（`app/services/relocate.py` 域内 `derive_consolidation_plan`）。
- **schema**：新增 `ConsolidationPlan` / `BatchRelocatePlanRequest` / `BatchRelocatePlanResponse`（`app/schemas/reason.py`，与 `BatchPickSequenceRequest` 同文件）。
- **权限**：沿用既有资源标识 `relocate.view`（队列查询）与 `relocate.operate`（relocate-plan 端点级鉴权），不新增资源。
- **KPI**：支撑 `移库后同物料跨巷道数下降` 在移库侧的落地度量（M1 移库补救）。

## 非目标

- 不重做移库确认链 / 台账 / 后验底座（`transaction-base` 已落地，本变更只补收拢方案的派生与接口）。
- 不做「主单 + 明细行」台账（`LedgerLine`）——15-01 §4.2 / 15-04 §3.2 的「源 / 目标库位多行」在 v1 以扁平 `Ledger` 承载（每板一条 `JobOrder` 行），`LedgerLine` 属跨域底座升级，另立变更（与 `outbound-domain` 同一登记）。
- 不做移库队列查询端点（`GET /api/jobs?type=relocate` 由 `inbound-domain` 以类型无关端点提供）。
- 不做移库作业页 p5 前端数据层（属 28-04 Phase B，另立变更）。
- 不调评分引擎（收拢方案为本地规则，`engine.invoke` 仅用于入库批量分配）。
- 不做「偏离批次 → 移库作业单」的创建（KPI 看板「一键发起移库」属 28-05 诊断联动）。
- 不新增实体字段、不做迁移、不引入前端框架/构建工具。
