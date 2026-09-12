"""推荐理由 JSON 组装（6 因子取值 + 降级原因）与 `17` §10.7 的响应侧字段。

事实来源：`17` §10.1（`payload_json` 的 `breakdown` / `scores` / `priority` /
          `factor_degraded` / 方案级 `degraded`）、§10.1 末节（两条 2026-09-11 的更正）、
          §10.7（响应形状：`bulk_batch_no` / `snapshot_version` / `plans` /
          `degraded_alerts` / `predicted_cross_aisle`）、§4.1（`JobOrder.bulk_batch_no`）
          `design.md` D1（本模块负责「按 `17` §10.1 组装 + `17` §10.7 的响应侧字段」）、
          D2 第 2/3 条（量化比较与时钟注入）、D8（两种降级不共用字段）、
          D9（三条不变量 —— 尤其是「列与 JSON 的同名字段是同一次写入的投影」）、
          D11（批次号规则）、D16（`aisles` 单条）、D17（`now` = 现场墙上时间）
          `tasks.md` 6.3（批次号生成）、7.1（理由体组装）、7.3（同批跨巷道不给）

本文件按任务书分两段落：**批次号**（6.3）与**理由体**（7.1）。批次号先落地，它**不属于
「理由」** —— 落在这里是因为 D1 把 `17` §10.7 的响应侧字段划给了本模块（`bulk_batch_no`
正是其中之一），而引擎的七个模块是既定的切分（`tasks.md` 1.2），不为一个编号新开第八个。

## 调用点：理由体由**端点**组装，不是分配器

D1 的图里写着「allocator **向下**调 `reasons`」，而实际落成的调用序是
`allocate_batch` → `predict_cross_aisles` → **端点**逐单 `build_reason_payload`。这不是
对 D1 的背离，而是把它那条边**指向**的东西写清楚：理由体要的 `predicted_cross_aisle`
是**整批回溯**的产物（`14` §3.4 步 5），单条 `AllocationOutcome` 看不见同批的其它单 ——
分配器若自己调本模块，就需要在循环里拿到一个循环结束后才存在的数。

故依赖边是 `reasons → allocator`（本模块读 `AllocationOutcome` 的字段），**反向没有**：
分配器不认识本模块。调用序与 D1 的图不同这一点已在 `design.md` D9 登记（与 D17 把时钟
提到 `app/core/clock.py` 那处同一个登记风格）；它与那处不同的一点是：这条边是**真实的
依赖陈述**（理由体渲染的就是分配器的决定），而不是纯粹的实现位移。

## 批次号为什么要「纯函数 + 取数」两半

格式、进位、跨日是**算术**，用一串字符串就能验，不必建库；「哪些批次号算数」是**取数**，
必须落到 SQL 上验（仓库过滤 + 日期前缀过滤）。两半各有一类失效：前者错了生出重号，后者
错了把别的仓或昨天的批次算进今天的序号 —— 而这两件事都**不会报错**，`bulk_batch_no`
没有唯一约束（D11 明写它不是业务主键，只是追溯用的分组标签）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.clock import require_wall_clock
from app.engine.allocator import AllocationOutcome
from app.models.job import JobOrder
from app.schemas.reason import PredictedCrossAisle, ReasonPayload

__all__ = [
    "BULK_BATCH_NO_PREFIX",
    "build_reason_payload",
    "load_bulk_batch_nos",
    "next_bulk_batch_no",
]

#: 批次号前缀（`17` §10.7 的示例 `BAT-20260908-01`，D11）。
BULK_BATCH_NO_PREFIX = "BAT"

#: 批次号的形状：`BAT-<8 位日期>-<序号>`。序号用 `+` 而不是定长 —— 见到三位数的号要
#: **认得出来**（`BAT-20260908-100` 是第 100 批，不是「形状不对、跳过」）：跳过会让下一天
#: 从 `001` 重来并与既有号相撞，而这里认不认得出只差一个量词。
_BULK_BATCH_NO_PATTERN = re.compile(rf"^{BULK_BATCH_NO_PREFIX}-(\d{{8}})-(\d+)$")


def next_bulk_batch_no(taken: Iterable[str], *, now: datetime) -> str:
    """当日下一个批次号 `BAT-<现场日期 YYYYMMDD>-<NN>`（D11）。

    `taken` 是**本仓当日**已有的批次号（`load_bulk_batch_nos` 的产物；去重后一串）。不认
    形状的值、以及日期不是本日的值，一律不参与计数 —— 本函数只回答「本日下一个号是几」，
    过滤别的口径是取数侧的事（两端各拦一道，任一端漏了都不会串号）。

    **`NN` = 本日已用序号的最大值 + 1。** D11 写的是「当日已有批次号的数量 + 1」，在本仓
    的口径下两者同进：批次号只由 8.2 在同一个事务里写回，整批回滚不留行，故编号从 `01` 起
    **稠密**。取最大值是把同一条口径的**结果**照办、同时不把安全性押在「回滚必须干净」上：
    一旦出现断档（`01` 与 `03` 并存），「数量 + 1」会生出 `03` 这个**已经在用的号**，而
    `bulk_batch_no` 没有唯一约束，撞号不会有任何东西报错。

    **日期取 `now` 的日期**，而 `now` 是现场墙上时间（D17）：按 naive UTC 取会让跨日点在
    现场变成**次日 08:00**，同一天的批次被劈成两段编号（`tasks.md` 6.3 的「跨日不串号」）。

    **没有随机源**（D2）：同一入参两次调用逐字相同。第二次调用会因为前一次的
    `JobOrder.bulk_batch_no` 已回写而拿到 `NN+1` —— 那是**入参已变**（作业单不再是
    `PENDING`，第二次会被 D10 的「只处理 `PENDING`」拒），不是本函数不确定。
    """
    require_wall_clock(now, purpose="批次号的现场日期")
    day = f"{now:%Y%m%d}"
    used = [
        int(match.group(2))
        for value in taken
        if (match := _BULK_BATCH_NO_PATTERN.match(value)) is not None
        and match.group(1) == day
    ]
    return f"{BULK_BATCH_NO_PREFIX}-{day}-{max(used) + 1 if used else 1:02d}"


def load_bulk_batch_nos(
    session: Session, *, warehouse_id: str, now: datetime
) -> tuple[str, ...]:
    """本仓**当日**已有的批次号（去重、文本升序）—— `next_bulk_batch_no` 的取数侧（D11）。

    **日期前缀过滤落在 SQL 上**，不在纯函数里：跨日不串号要由查询保证，把全表的批次号读进
    内存再筛会随历史增长退化成一次全表扫，而它每天都跑。

    **按仓库过滤**是 `17` §11 的数据隔离在引擎侧的落点之一：漏掉它，另一个厂的批次会顶高
    本厂的序号，而两个厂的号段看起来都「正常」（同日同前缀，只有仓库不同）。

    **同一批次的多条单只算一个号**（`bulk_batch_no` 是分组标签：一次请求最多 50 单共用它，
    D10）—— 故是 `DISTINCT` 而不是按行计数。按行计数会让当日第二个批次变成 `51`，而追溯时
    看到的是「这一天只跑了两批，却编到了 51 号」。

    读的是**已回写**的行：写回与计数须在同一个事务内（D11），否则并发两次请求会拿到同一个
    `NN` —— 那是 8.2 的事务边界要保证的事，本函数只负责读对它。
    """
    require_wall_clock(now, purpose="批次号的现场日期")
    stmt = (
        sa.select(JobOrder.bulk_batch_no)
        .where(
            JobOrder.warehouse_id == warehouse_id,
            JobOrder.bulk_batch_no.is_not(None),
            JobOrder.bulk_batch_no.like(f"{BULK_BATCH_NO_PREFIX}-{now:%Y%m%d}-%"),
        )
        .distinct()
        .order_by(JobOrder.bulk_batch_no)
    )
    return tuple(row[0] for row in session.execute(stmt))


# --- 7.1 理由体组装（`17` §10.1） ---------------------------------------------


def build_reason_payload(
    outcome: AllocationOutcome,
    *,
    weights: Mapping[str, float],
    predicted_cross_aisle: PredictedCrossAisle,
) -> ReasonPayload:
    """一条方案 → `17` §10.1 的 `payload_json`（**整份**，落库前不再增删字段）。

    入参是「一份已经做出的决定」加两个它自己看不见的事实：

    - `weights` 必须是**打分时用的那一份**（`scoring.load_weights` 的产物，整批同一份）。
      重新去库里读一次会让「按这一份算的分、拿那一份写的理由」成立 —— 而 `17` §10.1
      的不变式「`scores` 可由取值复算」要求复算的人只用这份 JSON：他手里没有当时的权重行。
    - `predicted_cross_aisle` 由调用方从 `allocator.predict_cross_aisles` 取好传进来。
      本函数不做回溯 —— 那是**整批**的事（同料号的单要合起来算），单条 `outcome` 看不见
      同批的其它单。模块 docstring 记了这处调用点的来龙去脉。

    **`degraded` / `degrade_reason` 原样取自 `outcome`**，不在这里从 `stop_tier` 或
    `factor_degraded` 重新推一遍：方案级降级只有一个来源（`degradation.tier_stop_reason`），
    重推会给「同一件事两种算法」留出余地，而两种算法都跑得下去、结果只在边角上不同。
    8.2 写 `RecommendationPlan` 那两列时从本函数的结果取值 —— 「列与 JSON 的同名字段是
    同一次写入的投影」（D9 第 3 条）因此在**代码**上成立，而不只是一句约定。

    `aisles` 转成 `list`（`ReasonPayload` 的字段类型）、`breakdown` 逐层转成普通 `dict`：
    入参是只读映射，而报文是要落库的数据 —— 原样塞进去会把「谁还能改它」这件事含糊掉。
    转换不改键序：`scores` / `breakdown` 的键序来自分配器（显式排序或队列序），而 JSON
    对象在 SQLite 里存成文本，键序变了就是两份不同的 `payload_json`（D2 第 2 条）。
    """
    return ReasonPayload(
        job_id=outcome.item.job_order_id,
        aisles=list(outcome.aisles),
        factors=dict(weights),
        scores=dict(outcome.scores),
        breakdown={aisle: dict(terms) for aisle, terms in outcome.breakdown.items()},
        priority=outcome.priority,
        factor_degraded=dict(outcome.factor_degraded),
        degraded=outcome.degraded,
        degrade_reason=outcome.degrade_reason,
        predicted_cross_aisle=predicted_cross_aisle,
    )
