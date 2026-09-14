"""作业写路径与验证读的对外契约（`app/api/routes/job.py` 的 DTO）。

事实来源：17-数据模型设计 §4.1（作业单标识）、§4.3（Ledger）、§4.4（Verification）
          openspec/changes/transaction-base/design.md D6（端点清单）、D7（逐单独立提交）
          spec `transaction-base`「作业单写操作端点」「台账与后验验证读」

## 三处口径与 allocate 契约的对齐

1. **作业单标识 = `str(JobOrder.id)`**（十进制正整数、无前导零）。与
   `schemas/reason.py` 的 `BatchAllocateRequest.job_order_ids` 同一形态 —— 同一个
   操作员在「分配 → 确认」这条链上看到的单号是同一个串，不因端点换一种写法。
2. **库位号 6 位文本**。DB 层有 `_LOCATION_LEN` 的 CHECK（`length(x) = 6`），但那是一条
   `IntegrityError`（500）—— 报文层先拦成 422，让「库位号少个前导 0」这类错当场指回
   调用方，而不是绕到 DB 层炸一个 500。
3. **读契约**（`LedgerItem` / `VerificationItem`）只投影「可查」的列，不引入任何
   写入侧才有的语义（如 `Verification` 的 `metric_kind` 不建枚举，读侧同样只透传
   字符串）。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import AbcClass, JobStatus, LedgerType, VerifyResult
from app.models.job import DeviationCauseKind, DeviationStatus
from app.schemas.reason import PickPathItem

#: 库位号一律 6 位文本（CLAUDE.md §七）。可空字段不给即「不提供」，给了就得 6 位 ——
#: 与 DB 的 `_LOCATION_LEN` 同一口径，报文层先拦，避免 500。
_LOCATION_CODE = Field(default=None, min_length=6, max_length=6)


class ConfirmItem(BaseModel):
    """批量确认里的一条作业单（`design.md` D6 的 `POST /api/job/batch/confirm`）。

    `source/target_location_code` 哪一列必填由 `job_type` 决定（入库只目标、出库只源、
    移库两者都有，`17` §4.3 的台账矩阵）—— 报文层不做这个推断，端点按已读到的
    `job_type` 校验，避免「填错哪一格」被静默吞成「看着合法的正确值」。
    """

    model_config = ConfigDict(extra="forbid")

    job_order_id: str = Field(min_length=1)
    source_location_code: str | None = _LOCATION_CODE
    target_location_code: str | None = _LOCATION_CODE
    #: 出库最终拣货路径（巷道序，`17` §10.2 的 `pick_sequence` 元素形）。可空 = 未微调，
    #: 编排读该单当前方案的 `pick_sequence` 回退（D4）。
    pick_path: list[PickPathItem] | None = None
    #: 实际执行数量。可空 = 取作业单上的计划量（`confirm._confirm_and_execute` 的默认）。
    actual_qty: int | None = Field(default=None, ge=1)
    #: 乐观锁版本号（spec `data-model`「并发确认仅一方成功」）：调用方**读的时候**看到的
    #: `lock_version`，原样带回提交。可空 = 未携带版本（退回「只推进不校验」，见
    #: `confirm._confirm_and_execute`）。
    lock_version: int | None = Field(default=None, ge=0)


class BatchConfirmRequest(BaseModel):
    """`POST /api/job/batch/confirm` 的请求体。

    `orders` **必填且非空**：与 `BatchAllocateRequest.job_order_ids` 同一原则，没有
    「空即全量」的隐含默认 —— 空数组要么是明确的空批，要么是调用方拼错了键名。
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    orders: list[ConfirmItem] = Field(min_length=1)


class ConfirmOutcome(BaseModel):
    """批量确认里一条作业单的结果（`design.md` D7：逐单独立提交的响应侧）。

    `status` = 该单本次请求结束后的状态：`VERIFIED` / `VERIFY_FAILED`（成功执行 + 后验）、
    `PLANNED`（写台账失败回退），或「非 `PLANNED` 源状态被拒」时的原状态（`error` 非空）。
    """

    job_order_id: str = Field(min_length=1)
    status: JobStatus
    #: 仅当该单被逐单拒绝（如源状态非 `PLANNED`）时非空 —— 「写台账失败回 PLANNED」是
    #: 正常分支（`status=PLANNED`、`error=None`），不是错误。
    error: str | None = None


class BatchConfirmResponse(BaseModel):
    """`POST /api/job/batch/confirm` 的响应体。`results` 沿请求序返回。"""

    results: list[ConfirmOutcome]


class RejectRequest(BaseModel):
    """`POST /api/job/{id}/reject` 的请求体。`reason` 落 `job_orders.reject_reason`。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    reason: str | None = None


class RetryRequest(BaseModel):
    """`POST /api/job/{id}/retry` 的请求体。后验重试只需要仓库号（快照取当前基线）。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)


class VoidRequest(BaseModel):
    """`POST /api/job/{id}/void` 的请求体。冲正即整单回冲，除仓库号外无附加参数。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)


class JobStatusResponse(BaseModel):
    """单作业单动作（reject / retry / void）的响应体：动作结束后的状态。"""

    job_order_id: str = Field(min_length=1)
    status: JobStatus


class LedgerItem(BaseModel):
    """`GET /api/ledger` 返回的一行台账（含 `is_reversal` 反向行）。"""

    model_config = ConfigDict(from_attributes=True)

    ledger_type: LedgerType
    is_reversal: bool
    order_no: str
    material_code: str
    material_name: str | None
    batch_no: str
    qty: int
    source_location_code: str | None
    target_location_code: str | None
    operator_id: int
    executed_at: datetime


class VerificationItem(BaseModel):
    """`GET /api/verification/{job_id}` 返回的一行后验记录（一行一条指标）。"""

    model_config = ConfigDict(from_attributes=True)

    metric_kind: str
    actual_value: float
    threshold_value: float
    verify_result: VerifyResult


class DeviationItem(BaseModel):
    """`GET /api/deviation` 返回的一行偏离批次（移库任务来源，`17` §4.4）。

    `material_code` / `batch_no` 至少一个非空（`identifier_required` CHECK），故这里
    各自可空 —— 读侧只透传，不重述那条 DB 约束。`cause_kind` / `status` 是 `str, Enum`
    （中文取值），序列化即其 `.value`。
    """

    model_config = ConfigDict(from_attributes=True)

    material_code: str | None
    batch_no: str | None
    actual_cross_aisle: int
    threshold_cross_aisle: int
    cause_kind: DeviationCauseKind
    status: DeviationStatus
    created_at: datetime


class JobQueueItem(BaseModel):
    """`GET /api/jobs` 返回的一行作业队列（入库作业页 p3 的多选队列）。

    投影 spec `transaction-base`「入库作业队列查询」点名的字段，并额外带
    `job_order_id`（`str(JobOrder.id)` 形态）与 `lock_version`（openspec/changes/
    inbound-domain/design.md D2）—— 队列是「分配 → 确认」写链的入口视图，缺了这两列
    p3 无法把选中项喂给 `POST /api/allocate/batch`（要 `job_order_ids`）与
    `POST /api/job/batch/confirm`（要 `job_order_id` + `lock_version` 乐观锁）。

    `job_order_id` 用 `validation_alias="id"` 从 ORM 行取 `id` 并 `str()` 化，
    与本模块第 1 条口径一致（线上形态统一为十进制正整数串）。
    """

    model_config = ConfigDict(from_attributes=True)

    job_order_id: str = Field(validation_alias="id")
    order_no: str
    line_no: str
    material_code: str
    material_name: str | None
    qty: int
    abc_class: AbcClass | None
    batch_no: str | None
    status: JobStatus
    bulk_batch_no: str | None
    lock_version: int

    @field_validator("job_order_id", mode="before")
    @classmethod
    def _stringify_id(cls, value: object) -> str:
        return str(value)
