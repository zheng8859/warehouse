## Context

阶段四的 `transaction-base` 与 `permission` 两个主规格已由先前变更归档，但落地后发现两处与代码现状的漂移（动机见 proposal.md - Why）：

- `POST /api/job/batch/confirm` 的响应只有 `results[]`，缺少前端渲染「成功 N / 失败 M」所需的聚合字段。`15-05` §5.2 的部分成功示例（`summary{total,success,failed}`）是目标形状。
- `permission` 的「v1 端点级资源鉴权仅限分配端点」断言只有 `allocate` 挂 `require_permission`，但代码已对 3 个方案生成端点施加 `require_permission`（`allocate.py:400`、`job.py:545`、`job.py:668`）。

本变更只做**契约收敛**：补一个响应字段 + 校正一份 spec 的失真，不引入新能力。

## Goals / Non-Goals

**Goals:**
- `batch/confirm` 响应新增 `summary{total,success,failed}` 部分成功聚合，与 `results[]` 逐单一致；只加字段、不改不删既有字段（向后兼容）。
- `permission` spec 修正为 3 方案生成端点挂 `*.operate` 鉴权、确认类写端点不挂端点级鉴权，消除「仅限分配端点」失真。

**Non-Goals:**
- 不改错误信封：保持 `{error,message,detail}` slug 与 `bulk_batch_no`/`snapshot_version`/`plans`。
- 不新增 `AuditLog` / 503 / `ai.*` / p6~p8 接线（均路线图，见 proposal 非目标）。
- 不做任何 breaking 改名（`bulk_batch_no`→`batch_id`、`error`→`code`）。

## Decisions

### D1: `summary` 的 success/failed 口径 = 写台账执行是否成功

`success` = 该单经 `EXECUTED` 进入 `VERIFIED` 或 `VERIFY_FAILED`（写台账执行成功 + 后验完成，后验 `PASS`/`DEVIATION` 均计 success）；`failed` = 写台账失败回退 `PLANNED`，或源状态非 `PLANNED` 被 `StateConflict` 拒绝。不变量：`success + failed = total`，且 `failed` 由 `total - success` 计算，避免与 `results[]` 形成两套口径漂移。

- **为什么 `VERIFY_FAILED` 计 success**：确认这一动作的语义边界是「落位执行 + 写台账」；后验是落账后的独立下游步骤，其结果由 `Verification.verify_result` 与 `status=VERIFY_FAILED` 单独承载，不应污染批量确认的「成功 N」计数。
- **备选**：把 `VERIFY_FAILED` 计 failed —— 但会让「成功 N」与「写台账成功 N」错位；且 `15-05` §5.2 的成功例用 `EXECUTED`，在同步后验下 `EXECUTED` 不可见，等价于 `VERIFIED`/`VERIFY_FAILED`。

### D2: RBAC 边界 = 3 方案生成端点，确认类写端点不挂

端点级资源鉴权只覆盖 3 个**方案生成**端点：`allocate`→`inbound.operate`、`pick-sequence`→`outbound.operate`、`relocate-plan`→`relocate.operate`。确认类写端点（`confirm`/`reject`/`retry`/`void`）**不挂** `require_permission`：二次确认卡即操作员确认入口（L1 决策权在人），越权拦截由角色菜单可见性 + 二次确认承担。这与 CLAUDE.md 第八节「v1 事实」一致；`13` §6.2 把精细 RBAC（M5）列为路线图。

- **备选**：给确认类写端点也挂 `inbound.operate` 等 —— 但确认是「执行既有方案」而非「生成方案」，其授权语义是「操作员确认」而非「资源操作」，且会与「确认卡即确认入口」的红线冲突；留待 M5 精细 RBAC 统一落地。

## Risks / Trade-offs

- [新增 `summary` 字段对旧前端不可见] → 字段只加不删，旧客户端忽略新增键即可，无 breaking。
- [口径误读：`VERIFY_FAILED` 计 success] → spec 与 DTO docstring 双处写明口径，测试覆盖「2 成功 + 1 拒绝」的计数断言。
- [RENAMED + MODIFIED 同现的顺序依赖] → 按 OpenSpec 规则：RENAMED 引用旧名，MODIFIED 引用新名（`specs-apply` 在 rename 存在时要求 MODIFIED 必须引用 NEW header）。
