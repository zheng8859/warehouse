"""11 个领域枚举（权威取值）。

事实来源：17-数据模型设计 §九

**取值不得增删。** 新增状态必须先改 17 号文档，再改这里 —— 本模块是设计文档的
逐条搬运，不是实现者的设计空间。

注意大小写不一致是有意的：多数枚举为 UPPERCASE，但 `Role` 与 `AccountStatus`
在 17 号中为 lowercase，不要顺手改成大写（会影响数据库中的既有取值）。
"""
from __future__ import annotations

from enum import Enum


class JobType(str, Enum):
    """作业类型。LEDGER_TYPE 复用本取值域（17 §九⑤）。"""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    RELOCATE = "RELOCATE"


class LedgerType(str, Enum):
    """台账类型。17 §九⑤ 明确「复用 job_type 取值域」，故取值与 JobType 一致。

    此处独立定义而非 `LedgerType = JobType`，是为了让台账列的枚举类型在
    数据库层显式可见，便于后续演进。
    """

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    RELOCATE = "RELOCATE"


class JobStatus(str, Enum):
    """作业单状态机（7 态）。

    合法迁移见 15 §3.1；任何新增状态都需要同步改状态机守卫，
    且 EXECUTED 后不允许重复写台账。
    """

    PENDING = "PENDING"
    PLANNED = "PLANNED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"


class ImportStatus(str, Enum):
    """导入会话状态机（8 态）。16 §3.1。

    BASELINE 为终态；FAILED 可经修正后重试回到 DRAFT。
    """

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"
    IMPORTING = "IMPORTING"
    IMPORTED = "IMPORTED"
    BASELINE = "BASELINE"
    DISCARDED = "DISCARDED"


class FileType(str, Enum):
    """导入文件类型。16 §五。

    INV 之外的三类中，成品清单·历史出入库流水为统计类（不产生队列、不建基线），
    它没有独立的 FileType 取值，走 PO/DO/INV 之外的统计通道 —— 见 importer/loaders.py。
    """

    PO = "PO"
    DO = "DO"
    INV = "INV"


class Disposition(str, Enum):
    """操作员逐单处置类型。17 §4.5 / 15 §2.1。

    注意：「操作与确认记录」不是 23 实体之一（无英文实体名、不在 ER 图），
    处置结果以本枚举落在 JobOrder 上。
    """

    ACCEPT = "ACCEPT"
    TUNE = "TUNE"
    REJECT = "REJECT"


class AbcClass(str, Enum):
    """ABC 分类。由成品清单·历史出入库流水按料号聚合出库量派生。"""

    A = "A"
    B = "B"
    C = "C"


class Role(str, Enum):
    """角色（4 个）。13 号 §一。**lowercase** —— 不要改成大写。"""

    WAREHOUSE_KEEPER = "warehouse_keeper"
    PLANNER = "planner"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class AccountStatus(str, Enum):
    """账号状态。13 §5.1。**lowercase**。

    紧急吊销 = 置 disabled，下次请求校验即失败（无服务端黑名单，13 §8.3）。
    """

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class VerifyResult(str, Enum):
    """后验结果。"""

    PASS = "PASS"
    DEVIATION = "DEVIATION"


class ItemStatus(str, Enum):
    """库存品质状态 —— **源数据驱动，不是固定取值域**。

    17 §九⑪：取值以 GTJ10036 导出为准，导入时按 FieldMappingConfig 归一。
    待检状态转换**不归本系统**（属品管流程，独立主线另做）。

    下面仅列出文档中出现的取值，作为映射参考；校验时不得把它们当作穷举集合。
    """

    QUALIFIED = "合格"
    PENDING_INSPECTION = "待检"
    FROZEN = "冻结"
