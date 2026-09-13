"""作业单状态机（**纯函数，无 IO**）。

事实来源：15-入库出库移库与后验流程设计 §3.1（状态机图）、§3.2（三类状态详解）、§11.7（幂等）
          17-数据模型设计 §4.1（状态字段）、§九②（`job_status` 十值）
          openspec/changes/data-model-permission/specs/data-model/spec.md「JobOrder 状态机」
          openspec/changes/transaction-base/specs/data-model/spec.md「JobOrder 状态机」（10 态）

## 为什么单独成一个模块，而不是长在 `JobOrder` 上

判定「两个状态之间有没有边」只用到两个枚举值，与那一行的其它字段无关。抽出来之后，
三类作业的服务层、批量确认的前置检查、以及将来的一次性修数脚本可以复用**同一张表** ——
判定逻辑在多处各抄一份，正是「`EXECUTED` 后不重复写台账」这条红线最现实的失效方式：
某一条路径漏了检查，而它看起来也在「按文档办事」。

放在模型上则会反向依赖：`JobOrder` 的实例方法要读会话才知道当前状态，状态机于是
不再是纯函数，「同样输入必得同样输出」也就无从断言。

本模块**不得引入任何 IO 依赖**，`tests/logic/test_job_state.py` 用 AST 盯着 import 清单。

## 三条读这张表时要知道的事

1. **自环是特例**：只有 `PENDING → PENDING` 允许（15 §3.1「批量分配失败可重试」）。
   其余状态的自环一律不合法 —— 尤其 `EXECUTED → EXECUTED`，15 §11.7 要求网络重试
   撞上已执行的单子时**返回既有台账**，而不是再走一次迁移（那会写出第二条台账）。
2. **两个终态**：`CANCELLED`（移出本次批量）与 `VOID`（冲正完成）没有出边。
   注意 `CANCELLED` 不是「可恢复的暂停」：要重新纳入，是一次新的入队，不是状态机上的回边。
   `VERIFIED` **不再是终态**：它可被冲正拉回 `VOID`，故终态集由 `TERMINAL_STATUSES` 推导时
   自然从 `{CANCELLED, VERIFIED}` 变为 `{CANCELLED, VOID}`。
3. **`CONFIRMED → PLANNED` 是回边，`EXECUTED → PLANNED` 不是**：前者对应「写台账失败/回滚」
   （15 §3.1，事务整体回滚后单子回到已出方案），后者会造出「台账已存在、单子却回到待确认」
   的状态 —— 台账只有一套，这一步退不得。
4. **后验拆成两段**：`EXECUTED → VERIFYING → VERIFIED / VERIFY_FAILED`，且 `VERIFY_FAILED →
   VERIFYING` 是唯一重试边（无「放弃后验」终态）。`VERIFYING` 是内部中间态，确认请求内一次
   走完，不对外停留。
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Final

from app.core.enums import JobStatus
from app.core.errors import StateConflict

__all__ = [
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
]

#: 当前状态 → 允许迁移到的状态集。内容即 15 §3.1 的图，15 条边。
#: 外层 `MappingProxyType` + 内层 `frozenset` 都是**只读**：这张表是判定基准，
#: 任何一处的「临时放宽」都必须改到本文件，而不是在调用点改一个副本。
LEGAL_TRANSITIONS: Final[Mapping[JobStatus, frozenset[JobStatus]]] = MappingProxyType(
    {
        JobStatus.PENDING: frozenset(
            {
                JobStatus.PLANNED,     # 批量分配/生成方案成功
                JobStatus.PENDING,     # 批量分配失败可重试（唯一的合法自环）
                JobStatus.CANCELLED,   # 移出本次批量
            }
        ),
        JobStatus.PLANNED: frozenset(
            {
                JobStatus.CONFIRMED,   # 操作员确认（二次确认卡通过）
                JobStatus.REJECTED,    # 操作员驳回
                JobStatus.CANCELLED,   # 移出本次批量
            }
        ),
        JobStatus.CONFIRMED: frozenset(
            {
                JobStatus.EXECUTED,    # 写台账成功
                JobStatus.PLANNED,     # 写台账失败/回滚（回边，见模块 docstring 第 3 条）
            }
        ),
        JobStatus.REJECTED: frozenset({JobStatus.PENDING}),   # 重新入队（可再次分配）
        JobStatus.EXECUTED: frozenset(
            {
                JobStatus.VERIFYING,    # 台账写入成功后自动触发后验
                JobStatus.VOID,         # 冲正
            }
        ),
        JobStatus.VERIFYING: frozenset(
            {
                JobStatus.VERIFIED,     # 后验完成（达标/偏离由 Verification 标记）
                JobStatus.VERIFY_FAILED,  # 后验失败/超时
            }
        ),
        JobStatus.VERIFY_FAILED: frozenset({JobStatus.VERIFYING}),  # 重试后验（无放弃终态）
        JobStatus.CANCELLED: frozenset(),                      # 终态
        JobStatus.VERIFIED: frozenset({JobStatus.VOID}),       # 冲正（不再是终态）
        JobStatus.VOID: frozenset(),                           # 终态
    }
)

#: 没有出边的状态（15 §3.1 的结束节点）。留作常量是因为「这单还能不能动」是
#: 调用方（批量确认页、看板）会问的问题，而这个答案不该由调用方各自推导。
TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    status for status, targets in LEGAL_TRANSITIONS.items() if not targets
)


def _require_status(name: str, value: object) -> JobStatus:
    """把非 `JobStatus` 的入参挡在类型错上，**不要**让它退化成「非法迁移」。

    `JobStatus` 是 `str` 枚举，`JobStatus.PENDING == "PENDING"` 为真，于是「传字符串」
    看起来能跑；但查表用的是 `hash`，而 `Enum.__hash__` 取的是**成员名**——
    今天成员名与取值同形（`PENDING` / `PENDING`）所以恰好查得到，改天有人把取值调成
    lowercase，同一次调用就会从「合法」翻成「非法」，且报出来的是 `StateConflict`
    （「状态冲突」），排查会一路查到并发去。**类型错了就说类型错了。**
    """
    if not isinstance(value, JobStatus):
        raise TypeError(
            f"{name} 必须是 JobStatus 成员，实际是 {type(value).__name__}：{value!r}。"
            "请在入口（请求解析 / 导入映射）就转换，不要在状态机里比较裸字符串 —— "
            "str 枚举比较值、查表却按成员名散列，两者的差异会把类型错误伪装成状态冲突。"
        )
    return value


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    """判断 `current → target` 是否是 15 §3.1 列出的迁移。不抛异常。"""
    current = _require_status("current", current)
    target = _require_status("target", target)
    return target in LEGAL_TRANSITIONS[current]


def assert_transition(current: JobStatus, target: JobStatus) -> JobStatus:
    """守卫式判定：合法则返回 `target`，非法则抛 `StateConflict`（409）。

    返回 `target` 是为了让调用点能写成 `order.status = assert_transition(order.status, X)`
    —— 判定与赋值之间没有缝隙，就不会出现「判定过了但忘了改」这种静默失败。

    不提交、不落库：事务边界属于调用方（`CONFIRMED → EXECUTED` 的迁移必须与台账写入
    在同一个事务里，见 `app/core/db.py` 与 16 §6.3）。
    """
    current = _require_status("current", current)
    target = _require_status("target", target)

    if target not in LEGAL_TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[current]))
        raise StateConflict(
            f"作业单不能从 {current.value} 迁移到 {target.value}；"
            f"{current.value} 只能迁移到：{allowed or '（终态，无后续迁移）'}",
            detail={"current": current.value, "target": target.value},
        )
    return target
