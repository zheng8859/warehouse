# transaction-base — 交易底座

## Why

阶段三已交付评分引擎（`recommendation-engine`，产出 `PLANNED` 软推荐方案），但「确认 → 落位 → 写台账 → 后验」这条交易闭环仍未实现：引擎产出无人消费、无台账、`AisleCap` 也不随落位增量更新，推荐停在「纸上方案」。本 change 打通关键路径 `F5 → F7 → F8` 的交易后半段（`F6` 库位族已随阶段三交付），并落地 `F9` 的事务增量维护，使推荐从「可读方案」变成「可审计、可回冲、可后验」的台账与容量事实。

## What Changes

- **BREAKING** `JobStatus` 枚举 7→10 值：新增 `VERIFYING` / `VERIFY_FAILED` / `VOID`；`JobOrder` 状态机由 7 态扩为 10 态，`EXECUTED → VERIFIED` 直达边拆为 `EXECUTED → VERIFYING → VERIFIED` 与 `VERIFYING → VERIFY_FAILED`（可重试回 `VERIFYING`），并新增 `EXECUTED / VERIFIED → VOID` 冲正边。
- 实现交易服务层 `app/services/` 六模块（`inbound` / `outbound` / `relocate` / `verify` / `ledger` / `kpi`，现为桩），承接三类作业的确认、执行与落位。
- 写台账与 cap 增量**同事务**写入（`app/cap/increment.py`），板-格换算复用 `to_occupied_cells()` 恒等占位（D14 板-格规则未确认前的明确选择）。
- 同步后验：确认请求内 `EXECUTED → VERIFYING → VERIFIED / VERIFY_FAILED`，三口径（入库同物料跨巷道 ≤5 且同批 ≤3、出库拣货量加权集中度 80% ≤ N=5、移库后同物料跨巷道数下降），不达标标记 `Deviation`。
- 冲正（v1 简化）：整单置 `VOID` + 写反向台账行（`is_reversal`），同事务释放 cap 并减少库存；**不创建反向作业单**，重新入库走标准流程。
- 新增写路径 API 与两个验证读（`GET /ledger`、`GET /verification/{job_id}`）。

## 非目标

- **F1 文件级数据导入 / F2 数据衔接契约配置** → 独立 `import-base` change；`GET /jobs` 队列读取端点随导入侧交付（队列由导入管线填充，本 change 直接消费已 `PLANNED` 的作业单）。
- **F9 的 cap 基线全量重算 / 对账 / 告警** → 后续 4b（`app/cap/baseline.py` / `reconcile.py` / `alerts.py` 不在本 change）。
- **F10 集中度 KPI 看板（含出库顺路取前端）** → 阶段六；本 change 只写入 `Deviation`，不做看板读取。
- 出库「顺路取」与移库「收拢」的**生成方案**端点（pick-sequence / relocate-plan）→ 引擎侧后续 change。
- 冷路径 LLM（阶段五）、细粒度 RBAC（M5 路线图，本 change 沿用二次确认卡 + 已有端点级鉴权模式）。

以上均不与「不做清单」11 项冲突；冲正 v1 简化（不建反向作业单）是对 `15-01` §6.3 的**有意偏离**，将在 `design.md` 显式记录，理由为 v1 简化收口（台账仍是 cap 与库存分布增量的唯一来源，反向行同样落 `Ledger`，不新增平行台账）。

## Capabilities

### New Capabilities

- `transaction-base`: 交易底座契约 —— 三类作业的确认 / 执行 / 冲正、台账写入、cap 事务增量、同步后验与偏离标记的端到端行为。

### Modified Capabilities

- `data-model`: 「JobOrder 状态机」需求 7 态 → 10 态（新增 `VERIFYING` / `VERIFY_FAILED` / `VOID` 及对应迁移边）。

## Impact

- **实体**：`JobOrder`（+`void` 冲正标记、状态机 10 态）、`Ledger`（+`is_reversal` 反向行标记，唯一约束放宽为 `(job_order_id, is_reversal)`，仍守住「同单不重复写正常台账」）、`Verification`（三口径后验结果）、`Deviation`（偏离标记）、`AisleCap`（事务增量读写）、`InventoryItem`（落位增库存 / 冲正减库存）。
- **枚举**：`job_status`（`app/core/enums.py`，权威来源）+3 值。
- **API 路由**（按本仓 `/api/*` 约定，无 `/api/v1` 前缀）：`POST /api/job/batch/confirm`、`POST /api/job/{id}/reject`、`POST /api/job/{id}/retry`、`POST /api/job/{id}/void`、`GET /api/ledger`、`GET /api/verification/{job_id}`。注：`15-01` §5.1 写作 `/api/v1/jobs/*`，与本仓约定不符，以 config.yaml 的 `/api/job/*` 为准。
- **服务层**：实现 `app/services/` 六模块桩与 `app/cap/increment.py`（cap 事务增量）。
- **KPI 指标**：同物料跨巷道 ≤5、同批跨巷道 ≤3、拣货量加权集中度 80% ≤ N=5、移库后同物料跨巷道数下降。
