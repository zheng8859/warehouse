"""时钟口径的守卫：`now` 必须是**现场墙上时间**（朴素）。`design.md` D17。

事实来源：`design.md` D17（业务钟 = 现场墙上时间 / 审计钟 = naive UTC，两者不并存于一次
          计算）、D7（释放判定）、D11（批次号的现场日期）
          `app/models/configuration.py`（`reserved_release_at` / `effective_at` 两列的口径）
          `tasks.md` 3.1（`reserved.py` 首次落下这个判据，并注明「D11 的批次号日期需要同一
          判据 —— 到那时把它提到一处共用，不要复制第三份」）、6.3（批次号）

## 为什么是一个独立模块

2026-09-12 从 `reserved.py` 提到这里：D7（预留池释放）与 D11（批次号日期）要的是**同一个
判据**，第二处落地时若各写一份，两份会各自演化 —— 而它们的失效形态恰好一模一样（差 8 小时
且两边的值都长得像合法时间）。`app/core/` 是横切关注点的家（`CLAUDE.md` §三），故判据放这里，
`engine/` 侧按需 import：**引擎的七个模块之间因此不多出一条依赖边**（D1 的调用链是一条单向
图，往里塞 `reasons → reserved` 只为借一个守卫，会把那张图读乱）。

## 挡得住什么、挡不住什么

**挡得住**带时区的 `now`。它必须显式挡，不能指望比较那一步：`datetime.time()` **丢掉
`tzinfo`**（保留时区的是 `.timetz()`），于是 `datetime.now(timezone.utc).time()` 是一个
**朴素**的 UTC 钟点，与 18:00 比得出一个静默的结果 —— 现场 19:00 时它是 11:00，预留池
不释放；现场凌晨 02:00 时它却释放了。偏差 8 小时，而日志里什么都看不出来。

**挡不住**「朴素但其实是 UTC」（例如 `utcnow()`）：那种值在类型上与本该传的值无法区分，
只能靠 D17 的文字与调用点的注入纪律（分配器与路由都只在一个地方取钟）。
"""
from __future__ import annotations

from datetime import datetime

__all__ = ["require_wall_clock"]


def require_wall_clock(now: datetime, *, purpose: str) -> None:
    """`now` 带时区 ⇒ `TypeError`，并给出 D17 的换法。

    `purpose` 说明这一处拿 `now` 做什么（「预留池释放判定」/「批次号的现场日期」）—— 报错
    时读的人才知道是**哪个判据**在拒，否则一条 `TypeError` 在一批调用里指不出位置。
    """
    if now.tzinfo is not None:
        raise TypeError(
            f"now 带时区（{now.tzinfo}），而{purpose}读的是**现场墙上时间**（D17）—— "
            "请在调用侧转换：datetime.now().astimezone().replace(tzinfo=None)。"
        )
