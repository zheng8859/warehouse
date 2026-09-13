## MODIFIED Requirements

### Requirement: JobOrder 状态机

`JobOrder` 状态必须限于 `PENDING` / `PLANNED` / `CONFIRMED` / `REJECTED` / `CANCELLED` / `EXECUTED` / `VERIFYING` / `VERIFIED` / `VERIFY_FAILED` / `VOID` 十值，迁移必须限于：`PENDING → PLANNED`、`PENDING → PENDING`（批量分配失败可重试）、`PENDING → CANCELLED`、`PLANNED → CONFIRMED`、`PLANNED → REJECTED`、`PLANNED → CANCELLED`、`REJECTED → PENDING`、`CONFIRMED → EXECUTED`（写台账成功）、`CONFIRMED → PLANNED`（写台账失败或回滚）、`EXECUTED → VERIFYING`（台账写入成功后自动触发后验）、`VERIFYING → VERIFIED`（后验完成，达标/偏离由 `Verification` 标记）、`VERIFYING → VERIFY_FAILED`（后验失败或超时）、`VERIFY_FAILED → VERIFYING`（重试后验）、`EXECUTED → VOID`（冲正）、`VERIFIED → VOID`（冲正）。系统不得新增未定义状态，也不得放行未列出的迁移；`VERIFY_FAILED` 仅提供重试与告警，不提供「放弃后验」终态。

#### Scenario: 合法迁移被接受
- **GIVEN** 作业单状态为 `PLANNED`
- **WHEN** 操作员确认该单
- **THEN** 状态迁移为 `CONFIRMED`

#### Scenario: 未定义迁移被拒绝
- **GIVEN** 作业单状态为 `PENDING`
- **WHEN** 尝试直接迁移到 `EXECUTED`
- **THEN** 迁移被拒绝，状态保持 `PENDING`

#### Scenario: 后验拆为两段
- **GIVEN** 作业单状态为 `EXECUTED` 且台账已写入
- **WHEN** 触发后验
- **THEN** 状态先迁移为 `VERIFYING`，后验完成后迁移为 `VERIFIED`（达标记 `PASS`、偏离记 `DEVIATION`）

#### Scenario: 冲正置 VOID
- **GIVEN** 作业单状态为 `EXECUTED` 或 `VERIFIED`
- **WHEN** 发起冲正
- **THEN** 状态迁移为 `VOID`，成为终态
