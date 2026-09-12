"""近站台预留池：默认 40%，仅近站台巷道；非 A 类不得占用；超时（默认 18:00）自动释放给 B/C 类。`14` §3.3。

事实来源：`14` §3.3（近站台预留比例）、§3.5（预留超时释放）、§四（因子集固定）
          `17` §2.1（近站台巷道）、§3.4（`AisleCap` 三列：总格 / 预留 / 可用）、§3.5、
          §11（数据隔离）
          `16` §353~356（预留比例 40% / 释放 18:00 的默认值表）
          `openspec/changes/recommendation-engine/design.md` D7（`available_cap` 的公式与
          三个判据的取数源）、D17（`now` 是现场墙上时间）、D2（时钟注入）
          `tasks.md` 3.1（本文件的任务书）

## 「可用」为什么直接读快照三列，不在引擎里按比例算

`14` §3.3 给的是**扣减口径**（`cap_reserved = cap_total × 预留比例`），那个乘法发生在
**导入期**（阶段四的 cap 自维护），算好的三列落在 `AisleCap` 上（`17` §3.4）。引擎读
快照 —— **快照是权威**：同一个比例若在引擎里再算一遍，历史快照就会随当日配置的变更而
改变含义（今天的分配按今天的比例解释昨天的容量），而「以快照重算基线」这条对账口径
（`16` §6.4）也会失去参照。

## `is_near_station IS NULL`（未导出）为什么不需要特判

预留池**只在近站台非零**：未导出时快照侧的 `cap_reserved` 本身就是 0，于是
`cap_usable == cap_total`，`available_cap` 自然收敛为「所有人可用全额」（D7）。故本文件
**没有**一条 `if is_near_station is None` 的分支 —— 加了它反而会造出第二个口径，且那个
口径只在「`is_near_station` 为空而 `cap_reserved > 0`」这种自相矛盾的行上才与三列不同，
而那种行按 D7 仍**以 `cap_*` 三列为准**（记明在因子级降级里是 7.1 的事，不是这里的）。
"""
from __future__ import annotations

from datetime import datetime, time

from app.core.clock import require_wall_clock
from app.core.enums import AbcClass
from app.models.linkage import AisleCap

__all__ = ["available_cap", "is_reserve_released"]


def is_reserve_released(*, release_at: time, now: datetime) -> bool:
    """预留池是否已到**当日释放钟点**（`14` §3.5 / `16` §353~356 的默认 18:00）。

    **边界闭区间**：`now` 的钟点恰好等于 `release_at` 即已释放。与 `is_effective`
    （配置版本生效）同一手法，理由也同源：开区间会让「18:00 释放」比预期晚一个瞬间，
    而那次判断只发生一次 —— 晚的那次会被当成 bug 报回来，早的那次不会有人注意到。

    **`now` 必须是现场墙上时间**（D17）：`release_at` 是「现场的 18:00」，按 UTC 比会让
    现场变成**次日 02:00** 释放（`configuration.py` 的列注释已就此论证）。

    每日重复发生，故只看钟点、不看日期（`reserved_release_at` 是 `Time` 而非 `DateTime`，
    见 `configuration.py` 模块 docstring 第 4 条）。
    """
    require_wall_clock(now, purpose="预留池的释放钟点")
    return now.time() >= release_at


def available_cap(
    *,
    cap: AisleCap,
    abc_class: AbcClass | None,
    release_at: time,
    now: datetime,
) -> int:
    """该巷道对本单**可用**的格数（D7 的公式，判据 1「cap 足够」与判据 3 的分支都读它）。

    - **A 类**：总额 —— 预留池本就是为 A 类爆款留的（`14` §2.2 / `CLAUDE.md` §四）。
    - **非 A 类**：释放钟点已过则同享总额，否则只给 `cap_usable`（扣掉预留池的部分）。

    **`abc_class` 为 `None` 时按「非 A 类」处理** —— 这是保守读法，不是为了省事：红线
    「非 A 类不得占用近站台预留池」的**目的**是把近站台的稀缺容量留给爆款，而「分类未派生」
    并不构成「它是 A 类」的证据。按 A 类放行会让一个来路不明的单子占掉预留池，而那条红线
    一旦被违反是**不可追溯**的（预留池的占用没有独立台账）。代价是 ABC 未导出时 A 类单子
    也可能落到远巷道 —— 那会走 5.2 的告警（「近站台缺口」），是**可见**的；两者相较，
    可见的那一侧才是可以接受的错。

    本函数**只管容量**，不管「这个巷道该不该被考虑」：主数据存在性由调用方（3.2 的可行
    巷道集）判，本函数收到的 `cap` 已经意味着那条 `AisleCap` 行存在。
    """
    if abc_class is AbcClass.A:
        return cap.cap_total
    if is_reserve_released(release_at=release_at, now=now):
        return cap.cap_total
    return cap.cap_usable
