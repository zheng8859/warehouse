"""业务异常与错误分类。

事实来源：16-数据衔接与 cap 自维护 §二 与 §4.4（校验失败阻断）、
          19-系统架构与部署视图 §4.2（故障域）、13-权限 §六

约定：
  - 阻断类错误不得被上层吞掉后继续执行 —— 「校验失败阻断，不得带病入库」。
  - **降级不是异常**。降级是正常路径，以 `degraded` / `degrade_reason` 落在推荐理由
    JSON 中（17 §10.1），不通过抛异常表达 —— 这样降级天然可见、可追溯（降级不静默）。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """领域异常基类。http_status 决定 API 层响应码（见 app/main.py）。"""

    http_status: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Unauthenticated(DomainError):
    """凭据缺失 / 过期 / 无效。中间件通常直接返回 401，本类供非中间件路径使用。"""

    http_status = 401
    code = "unauthenticated"


class PermissionDenied(DomainError):
    """第 2 层资源级权限检查未通过。13 §六。"""

    http_status = 403
    code = "permission_denied"


class NotFound(DomainError):
    """目标不存在。"""

    http_status = 404
    code = "not_found"


class StateConflict(DomainError):
    """状态机非法迁移，或乐观锁版本不一致（多端同时确认同一单）。15 §3.1。"""

    http_status = 409
    code = "state_conflict"


class BlockedMissingPrerequisite(DomainError):
    """业务前置条件缺失 → 阻断。

    典型：出库时库存快照缺失或过期 —— 阻断并提示重新导入，**不猜测落位**（15 §4.4）。
    """

    http_status = 409
    code = "blocked_missing_prerequisite"


class ValidationBlocked(DomainError):
    """校验失败 → 阻断。16 §二：不得带病入库。"""

    http_status = 422
    code = "validation_blocked"
