"""账号状态机（**纯函数，无 IO**）。

事实来源：13-权限分级与访问控制系统 §5.1（状态流转图）、§5.2（账号数据模型）、§8.3（紧急吊销）
          spec `auth`「账号状态迁移」（四条迁移，逐字）
          design.md D2（`AccountStatus` 四值，补 `REJECTED`）
          openspec/changes/data-model-permission/tasks.md 7.3

## 为什么与 `state_machine.py` 分成两个模块

两张表服务的是**两个不同的主体**：作业单的状态由操作员按流程推进（15 §3.1），
账号的状态由管理员按权限动作推进（13 §5.1）。合成一张表会让「这张表的键是哪种状态」
变成调用方要先判断的事，而两边的取值域完全不同（七值 vs 四值）—— 合在一起后，
一个 `JobStatus.CANCELLED` 传进账号分支也只会在查表时失败。

共用的是**形状**（只读迁移表 + 类型守卫 + `assert_transition` 返回目标态），
不是内容；形状的复用靠约定而非继承 —— 这两张表都短到可以一眼读完，
抽象出一个基类只会让「这条边是谁定义的」变难回答。

## 四条边（spec `auth` 逐字）

    pending  → active    管理员激活（13 §5.1：创建后须显式激活）
    pending  → rejected  管理员驳回申请
    active   → disabled  停用 —— 也是**紧急吊销**的唯一手段（13 §8.3：无服务端黑名单）
    disabled → active    重新启用

## 三条读这张表时要知道的事

1. **没有合法自环**。作业单那边 `PENDING → PENDING` 是「批量分配失败可重试」（15 §3.1），
   这里四条边里没有自环 —— 重复点一次「激活」不该再写一遍 `last_login_at` / 审计。
2. **终态只有 `rejected`**，不是 `disabled`。`disabled → active` 明确存在，
   所以「停用」是**可达回**的中转态，不是终态。判据是图上的出边，不是
   「这个账号还能不能登录」这种业务语感。
3. **`rejected` 无出边**：要恢复只能重新提交申请（走一次新的 `pending`），
   不是在这张图上加一条 `rejected → active` —— 加了之后驳回就只是装饰性动作，
   申请人的账号会跳过复评直接可用（spec 场景「已驳回账号不得直接激活」）。

**本阶段没有消费者**：账号管理的写端点（创建 / 激活 / 停用 / 驳回）属路线图，
与 D9「端点级资源鉴权不在本阶段」同批。本模块与 §8 的权限矩阵一样，本阶段交付的是
数据 + 纯判定函数 + 全组合断言；端点落地时直接复用这张表，而不是各写一份 if 判断。

本模块**不得引入任何 IO 依赖**，`tests/logic/test_account_state.py` 用 AST 盯着 import 清单。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.core.enums import AccountStatus
from app.core.errors import StateConflict

__all__ = [
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
]

#: 当前状态 → 允许迁移到的状态集。内容即 spec `auth` 的四条边。
#: 外层 `MappingProxyType` + 内层 `frozenset` 都是**只读**：这张表是判定基准，
#: 任何一处的「临时放宽」都必须改到本文件，而不是在调用点改一个副本。
LEGAL_TRANSITIONS: Final[Mapping[AccountStatus, frozenset[AccountStatus]]] = MappingProxyType(
    {
        AccountStatus.PENDING: frozenset(
            {
                AccountStatus.ACTIVE,     # 管理员激活
                AccountStatus.REJECTED,   # 管理员驳回申请
            }
        ),
        AccountStatus.ACTIVE: frozenset(
            {
                AccountStatus.DISABLED,   # 停用（紧急吊销：无服务端黑名单，13 §8.3）
            }
        ),
        AccountStatus.DISABLED: frozenset(
            {
                AccountStatus.ACTIVE,     # 重新启用
            }
        ),
        AccountStatus.REJECTED: frozenset(),   # 终态：恢复 = 重新提交申请，不是回边
    }
)

#: 没有出边的状态。留作常量是因为「这个账号还能不能动」是调用方
#: （账号管理页、登录端点）会问的问题，而答案不该由调用方各自推导。
TERMINAL_STATUSES: Final[frozenset[AccountStatus]] = frozenset(
    status for status, targets in LEGAL_TRANSITIONS.items() if not targets
)


def _require_status(name: str, value: object) -> AccountStatus:
    """把非 `AccountStatus` 的入参挡在类型错上，**不要**让它退化成「非法迁移」。

    `AccountStatus` 是 lowercase 的 `str` 枚举，于是同一份数据有两套语义：
    `AccountStatus.ACTIVE == "active"` 为真（str 混合枚举按**值**比较），
    而 `Enum.__hash__` 取的是**成员名** `ACTIVE` —— 拿 `"active"` 去查
    `LEGAL_TRANSITIONS` 必然 `KeyError`。也就是说 `if status == "active"` 这类写法
    在别处看着完全正确，到了这里却会炸在查找上。

    这个陷阱比 `JobStatus` 那边更深一层：那边成员名与取值同形（`PENDING`/`PENDING`），
    裸字符串**恰好**查得到，错误只在改取值时才显形；这里则是**当场**就查不到 ——
    而 `KeyError` 会被误读成「表里没这个状态」。**类型错了就说类型错了。**
    """
    if not isinstance(value, AccountStatus):
        raise TypeError(
            f"{name} 必须是 AccountStatus 成员，实际是 {type(value).__name__}：{value!r}。"
            "请在入口（请求解析 / 登录查询）就转换，不要在状态机里比较裸字符串 —— "
            "str 枚举比较值、查表却按成员名散列，两者的差异会把类型错误伪装成状态冲突。"
        )
    return value


def can_transition(current: AccountStatus, target: AccountStatus) -> bool:
    """判断 `current → target` 是否是 spec `auth` 列出的迁移。不抛异常。"""
    current = _require_status("current", current)
    target = _require_status("target", target)
    return target in LEGAL_TRANSITIONS[current]


def assert_transition(current: AccountStatus, target: AccountStatus) -> AccountStatus:
    """守卫式判定：合法则返回 `target`，非法则抛 `StateConflict`（409）。

    返回 `target` 是为了让调用点能写成 `account.status = assert_transition(account.status, X)`
    —— 判定与赋值之间没有缝隙，就不会出现「判定过了但忘了改」这种静默失败。

    不提交、不落库：事务边界属于调用方。
    """
    current = _require_status("current", current)
    target = _require_status("target", target)

    if target not in LEGAL_TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[current]))
        raise StateConflict(
            f"账号不能从 {current.value} 迁移到 {target.value}；"
            f"{current.value} 只能迁移到：{allowed or '（终态，无后续迁移）'}",
            detail={"current": current.value, "target": target.value},
        )
    return target
