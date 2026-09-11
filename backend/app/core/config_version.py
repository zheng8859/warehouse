"""配置型业务版本的选取（「当前生效的是哪一版」）。

事实来源：17-数据模型设计 §七（配置实体：版本号 + 生效时间 + 变更人）
          17 §八（对话台上下文，短期留存）
          openspec/changes/data-model-permission/design.md D1（版本语义三分）
          openspec/changes/data-model-permission/specs/data-model/spec.md
            「版本语义三分」

## 这里是哪一种版本

D1 把「版本」拆成三种，**不能混用**：

| 语义 | 承载列 | 用在哪 | 谁读它 |
|---|---|---|---|
| 并发版本 | `lock_version` | `JobOrder` / `ImportSession` | `app/core/concurrency.py` |
| **业务版本** | `version_no` + `effective_at` | `WeightConfig` / `CapacityConfig` | **本模块** |
| 不可变基线 | `snapshot_id` 外键 | `KpiSnapshot` / `AisleCap` 的快照引用 | 各业务模块 |

判据是**冲突的性质**：作业单的冲突是「两个人同时改同一行」，要拦住后一个；配置的冲突
是「两个口径都合法，但要能说清谁在什么时候生效、能回滚」，要**并存**。

## 为什么是函数而不是查询，也不是指针列

「当前版本」= `effective_at <= now` 中 `version_no` 最大者（D1 原文）。这条规则**没有
状态**，因此不需要存储：存下来的那一刻就有了「到点该翻牌」的责任，而翻牌的人要么是
定时任务（本阶段没有队列，见 CLAUDE.md 第五节「无中间件」），要么是每次读取时顺手写
—— 后者让一个**读操作**变成写操作，在单写库上直接把读放大成写冲突。

所以：**不加 `is_current` / `current_version_no` 指针列**（`tests/logic/test_config_version.py`
有一条断言看住这件事），每次取值现算。

## 为什么取 `version_no` 最大而不是 `effective_at` 最新

D1 的原文写的是 `version_no` 最大者。差额情形真实存在：补录一条「其实上个月就该用」的
旧口径时，它编号更大、生效时间更早。此时「当前用哪版」的答案仍是**编号大的那版**
—— 编号是用户可见、可回滚的版本序列（17 §七「历史版本保留可回滚」），生效时间只回答
「什么时候开始能用」。按 `effective_at` 取最新会得到一个用户没选过的版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol, TypeVar, runtime_checkable

__all__ = [
    "EFFECTIVE_AT_ATTR",
    "VERSION_NO_ATTR",
    "SupportsConfigVersion",
    "is_effective",
    "pick_current_version",
]

#: D1 钉死的两个列名。改名要走设计文档，故集中在此，不散落成字面量。
VERSION_NO_ATTR = "version_no"
EFFECTIVE_AT_ATTR = "effective_at"


@runtime_checkable
class SupportsConfigVersion(Protocol):
    """配置型版本化的实例。

    用 Protocol 而非抽象基类（与 `SupportsLockVersion` 同一处置）：`version_no` /
    `effective_at` 是**两列**，不是能力实现。**刻意要求两个属性都存在** —— 只认
    `version_no` 会让 `JobOrder.lock_version` 那种实体在调用方眼里「也能用」，
    而它俩的语义是不可互换的（见模块 docstring 的表）。
    """

    version_no: int
    effective_at: datetime


_T = TypeVar("_T", bound=SupportsConfigVersion)


def _require_awareness(name: str, value: datetime, *, aware: bool) -> None:
    """挡掉「带时区」与「不带时区」的混比。

    `Timestamp` 列（`created_at` / `effective_at`）在 SQLite 里是**朴素**时间
    （见 `app/models/base.py`），而 `datetime.now(timezone.utc)` 是带时区的。两者
    相比，Python 抛的 `TypeError` 说得清「不能比较」，但说不清**该改哪一边**：
    读的人很容易去改模型，而正确的动作是把 `now` 转成朴素时间。
    """
    if value.tzinfo is not None and value.utcoffset() is not None and not aware:
        raise TypeError(
            f"{name} 带时区（{value.tzinfo}），而配置列是朴素时间 —— "
            f"请在调用侧转换：datetime.now().astimezone().replace(tzinfo=None)。"
        )
    if not (value.tzinfo is not None and value.utcoffset() is not None) and aware:
        raise TypeError(f"{name} 不带时区，但另一侧带 —— 同一侧必须都是朴素时间去比较。")


def is_effective(row: SupportsConfigVersion, *, now: datetime) -> bool:
    """这一版配置在当前时刻是否已生效。边界**闭区间**（`effective_at == now` 即生效）。

    闭区间是刻意的：开区间会让「预约在整点生效」的版本比用户预期晚一个瞬间可用，
    而这个判断不会被第二次执行，早一个瞬间还是晚一个瞬间都只发生一次 —— 晚的那次
    会被当成 bug 报回来，早的那次不会有人注意到。
    """
    effective_at = row.effective_at
    if not isinstance(effective_at, datetime):
        raise TypeError(
            f"{type(row).__name__}.{EFFECTIVE_AT_ATTR} 不是 datetime，"
            f"实际是 {type(effective_at).__name__} —— 若刚从原始 SQL 读出，"
            "请确认读的是模型而不是裸 text()。"
        )
    _require_awareness(EFFECTIVE_AT_ATTR, effective_at, aware=False)
    _require_awareness("now", now, aware=False)
    return effective_at <= now


def pick_current_version(rows: Iterable[_T], *, now: datetime) -> _T | None:
    """返回当前生效的版本行；**没有任何一版生效时返回 `None`**。

    返回 `None` 而不是「退到最早那版」：库里有配置行却无一版生效，说明发布流程出了问题，
    调用方应当据此走「无配置」分支（阶段三降级链）并写明 `degrade_reason`，而不是拿着
    一个用户从未启用的版本继续算分。

    纯函数：不改动入参、不依赖行的到达顺序（查询返回顺序不是契约），
    同样输入必得同样输出（红线「同样输入必得同样输出」）。
    """
    current: _T | None = None
    for row in rows:
        if not is_effective(row, now=now):
            continue
        if current is None or row.version_no > current.version_no:
            current = row
    return current
