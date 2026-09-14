## MODIFIED Requirements

### Requirement: 作业单写操作端点

系统必须提供作业单写操作端点：`POST /api/job/batch/confirm`（批量确认）、`POST /api/job/{id}/reject`（驳回）、`POST /api/job/{id}/retry`（后验重试）、`POST /api/job/{id}/void`（冲正）。批量确认必须逐单独立提交，个别单失败回 `PLANNED` 且不影响同批其余单。批量确认响应必须包含部分成功聚合 `summary{total,success,failed}`：`total` 为本批提交单数，`success` 为写台账执行成功的单数（作业单经 `EXECUTED` 进入 `VERIFIED` 或 `VERIFY_FAILED`），`failed` 为未执行的单数（写台账失败回退 `PLANNED`，或源状态非 `PLANNED` 被拒且 `error` 非空），且 `success + failed = total`、与 `results[]` 逐单一致。

#### Scenario: 批量确认逐单独立事务
- **GIVEN** 一批多张 `PLANNED` 作业单，其中一张写台账失败
- **WHEN** 调用批量确认
- **THEN** 失败单回 `PLANNED`，其余单正常 `EXECUTED`，互不影响

#### Scenario: 部分成功响应聚合一致
- **GIVEN** 一批共 3 张 `PLANNED` 作业单，其中 2 张确认落账成功、1 张源状态非 `PLANNED` 被拒
- **WHEN** 调用批量确认
- **THEN** 响应 `summary` 为 `{total:3, success:2, failed:1}`，且 `results[]` 中成功单 `status` 为 `VERIFIED` 或 `VERIFY_FAILED`、被拒单保留原状态且 `error` 非空

#### Scenario: 驳回移出批量
- **GIVEN** 一张 `PLANNED` 作业单
- **WHEN** 操作员调用驳回端点
- **THEN** 该单迁移为 `REJECTED`，不产生台账
