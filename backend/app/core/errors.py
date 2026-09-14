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
    """凭据缺失 / 过期 / 无效。

    两条路径都产生 401：登录端点**抛本异常**（走 `app/main.py` 的处理器），
    中间件**构造本异常**再渲染（它在处理器之外，见 `error_body`）。同一份响应体
    必须逐字一致，否则前端拦截器得分两个分支按 `error` 分流。
    """

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

    典型两处：

    - 出库时库存快照缺失或过期 —— 阻断并提示重新导入，**不猜测落位**（15 §4.4）。
    - 评分时**无生效的权重版本** —— 阻断并提示配置未就绪，**不拿一套没人批准过的权重
      去排谁先挑黄金库位**（`17` §七 / `17` §10.1 的 `factors`；引擎侧取值见
      `app/engine/scoring.py:load_weights`，`design.md` D4 的配置缺席处置表）。

    两处的共同形状：**数据/口径没到位时，宁可不产出方案** —— 与「降级」明确不同
    （降级是有取值的，只是取值不全；见模块 docstring 末条）。
    """

    http_status = 409
    code = "blocked_missing_prerequisite"


class ValidationBlocked(DomainError):
    """校验失败 → 阻断。16 §二：不得带病入库。"""

    http_status = 422
    code = "validation_blocked"


class ColdPathDisabled(DomainError):
    """冷路径开关关闭（`cold_path_enabled=false`）—— 可纠正客户端错误：先开开关再调用。

    与「LLM 侧失败降级」（200 + `degraded_reason`）是两种处置：开关关闭 = 「能力整体
    不可用」是 409（前端据此提示去开开关）；LLM 没吐字 = 「能力开了但模型不可用」是
    降级（200 + 规则卡片）。两者不能都 200（design.md D2）。
    """

    http_status = 409
    code = "cold_path_disabled"


def error_body(exc: DomainError) -> dict[str, Any]:
    """领域异常 → 响应体。**唯一定义处**。

    401 有两个产生方（中间件、登录端点），而中间件在异常处理器**之外**（它包着整个
    应用），拿不到 `app.exception_handler` 那条路径 —— 所以它自己构造异常、自己渲染。
    「两处形状一致」不能靠两处各自写对，否则下一次有人给其中一处加 `detail`，
    前端拦截器就得为同一个 401 写两个分支。故渲染只有这一份，两处都调它。
    """
    body: dict[str, Any] = {"error": exc.code, "message": exc.message}
    # `detail` 为 None 时**不出现**这个键：无 detail 的（401）与有 detail 的
    # （409 状态冲突、403 权限拒绝）形状由此分开，且这个分法只有一处定义。
    if exc.detail is not None:
        body["detail"] = exc.detail
    return body
