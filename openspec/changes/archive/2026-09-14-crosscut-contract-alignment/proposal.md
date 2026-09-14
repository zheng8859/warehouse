## Why

阶段四按 28 号拆成 5 个 scope，`28-05`（横切）承载「接口契约通用约定 + 权限审计」。落地过程中发现：接口契约与权限矩阵的**大部分已在阶段二~四实现**（`/api` 前缀、8 类错误码分类、批量 ≤50、G1 认证 / G4 状态机 / G5 乐观锁、幂等键、二次确认字段、冲正 `reversal_ref`），真正欠下的不是新功能，而是**一处响应字段缺失 + 两份 spec 落后于代码**。本变更只做这三件事的收敛，不做新功能。

## What Changes

- 补 `POST /api/job/batch/confirm` 响应的**部分成功聚合**：在既有 `results[]`（逐单 status/error）之上新增 `summary{total,success,failed}`，让前端渲染「成功 N / 失败 M」无需自行遍历计数。
- 校正 `permission` spec 的「v1 端点级资源鉴权仅限分配端点」：现实现已对 **3 个方案生成端点**施加 `require_permission`（`allocate`→`inbound.operate`、`pick-sequence`→`outbound.operate`、`relocate-plan`→`relocate.operate`）；确认类写端点（`batch/confirm`、`reject`、`retry`、`void`）按 `design.md` D6 仍**不挂端点级鉴权**（确认卡即操作员确认入口）。spec 需据此修正，消除「仅限分配端点」的失真。
- **不采纳** `15-05` §5 的 `{code, summary, batch_id}` 信封草图：错误信封以代码现状 `{error, message, detail?}`（slug 区分错误种类）+ `bulk_batch_no` / `snapshot_version` / `plans` 为准（`15-05` §5.2 标「建议，需确认」、§5.3 标「补充设计」，是代码成型前的草图，回写方向是设计文档而非代码）。

## Capabilities

### New Capabilities

（无 —— 本变更不引入新能力。）

### Modified Capabilities

- `transaction-base`: 「作业单写操作端点」要求新增批量确认响应的 `summary{total,success,failed}` 部分成功契约（F7 落位执行与台账的响应侧）。
- `permission`: 「v1 端点级资源鉴权仅限分配端点」要求修正为「3 个方案生成端点挂 `*.operate` 鉴权，确认类写端点不挂端点级鉴权」。

## 非目标

- **不做** 完整 `AuditLog` 实体（`13` §4.4 / `15-05` §7.3 明列 M6 路线图，生产前补齐）。
- **不做** 503（外部 LLM 不可用）错误类 —— 属阶段五冷路径。
- **不做** `ai.*` 权限枚举（`ai.assist` / `weight.update` / `relocate.propose` / `toggle`）—— 属阶段五。
- **不做** p6（KPI 看板）/ p7（配置页）/ p8（对话台）数据层接线与 `/qa` 全链路验收 —— 分别归阶段六 / 阶段五。
- **不做** 任何 breaking 改名（`bulk_batch_no`→`batch_id`、`error`→`code`）。

## Impact

- **代码**：`app/schemas/job.py`（`BatchConfirmResponse` 加 `summary`）、`app/api/routes/job.py`（`batch_confirm` 聚合 `summary`）、`tests/api/`（部分成功计数的测试）。
- **API 路由**：`POST /api/job/batch/confirm`（响应体新增字段，向后兼容，只加不删）。
- **实体**：无新增 / 无字段变更（`JobOrder` / `Ledger` 不动）。
- **KPI 指标**：无变化。
- **依赖系统**：无。
