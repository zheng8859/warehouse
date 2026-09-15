"""生产批号生成（D11）：入库单建立时按生产批规则生成 10 位批号。**纯函数，无 IO**。

事实来源：openspec/changes/data-import/design.md D11（批次号的现场日期）
          app/core/clock.py（`require_wall_clock` 守卫 —— D17，批次号与预留池释放共用同一判据）
          `JobOrder.batch_no` 注释（入库单的批号由系统在入库单建立时按生产批规则生成）

## 批号口径（业务方 2026-09-15 确认）

10 位，逐位：

| 位 | 含义 | 取值 |
|:--:|---|---|
| 1–3 | 公司别 | 固定 `GJP`（广州顶津） |
| 4–5 | 生产日期·年 | 2 码 |
| 6 | 生产日期·月 | 1 码，10/11/12 月以 `A`/`B`/`C` 代替 |
| 7–8 | 生产日期·日 | 2 码（补零） |
| 9 | 生产线代码 | 默认 `7` |
| 10 | 班别 | 默认 `1` |

例：`2026-09-15` → `GJP2691571`；`2025-10-03` → `GJP25A0371`。

生产日期取 `now` 的**日期**（入库单建立当天，24 点自动切换），不是 PO 模版的
`production_date` 列 —— 那列只是选填参考，批号不读它。月份编码不是十六进制（1–9 仍是
数字本身，只有 10/11/12 溢出到字母），故 `month` 直接查表而非 `chr` 换算。
"""
from __future__ import annotations

from datetime import datetime

from app.core.clock import require_wall_clock

__all__ = ["generate_batch_no"]

#: 公司别（广州顶津）—— 批号前 3 位，10 位批号的固定前缀。
_COMPANY = "GJP"

#: 生产线代码（第 9 位）默认值。
_DEFAULT_LINE = "7"

#: 班别（第 10 位）默认值。
_DEFAULT_SHIFT = "1"

#: 10/11/12 月的单字符编码（其余月份直接用数字本身，见模块 docstring）。
_MONTH_LETTERS: dict[int, str] = {10: "A", 11: "B", 12: "C"}


def generate_batch_no(
    now: datetime,
    *,
    company: str = _COMPANY,
    line: str = _DEFAULT_LINE,
    shift: str = _DEFAULT_SHIFT,
) -> str:
    """按生产批规则生成 10 位批号。`now` 必须是**现场墙上时间**（朴素，D17/D11）。

    生产日期取 `now` 的日期：年份 2 码、月份 1 码（10/11/12 → A/B/C）、日 2 码补零。
    公司别 / 生产线 / 班别默认 `GJP` / `7` / `1`，可经关键字参数覆盖（便于将来多厂
    或多线扩展，且不破坏纯函数签名）。
    """
    require_wall_clock(now, purpose="批次号的现场日期")
    month_char = _MONTH_LETTERS.get(now.month, str(now.month))
    return f"{company}{now.year % 100:02d}{month_char}{now.day:02d}{line}{shift}"
