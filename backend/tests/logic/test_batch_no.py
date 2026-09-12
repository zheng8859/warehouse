"""`reasons.py` 的批次号生成用例（6.3）。

事实来源：`design.md` D11（`BAT-<现场日期 YYYYMMDD>-<当日序数 NN>`；日期用**现场日期**；
          规则必须确定、不能用 `uuid4` / 随机后缀；`bulk_batch_no` **不是唯一键**）、
          D17（`now` = 现场墙上时间）、D2（同样输入必得同样输出）
          `17` §10.7（响应字段 `bulk_batch_no`，示例形状 `BAT-20260908-01`）
          `tasks.md` 6.3（本用例的任务书）

## 为什么拆成「纯函数 + 取数」两半

格式、进位、跨日、断档都是**纯函数**的事，用一串字符串就能验，不必建库；而「哪些批次号算
数」是**取数**的事（仓库过滤、日期前缀过滤），必须落到 SQL 上验 —— 那两条各是一类失效：
前者错了会生出重号，后者错了会把别的仓或昨天的批次算进今天的序号。

## 本文件钉住的两条红线

① **无随机源**（D2）：同一入参两次调用给出同一个号 —— 这条在纯函数上是恒真的，真正
   有意义的是「第二遍从库里读到的入参已经变了」（那是 8.2 的写回），故这里的确定性断言
   只针对**函数本身**，不假装覆盖跨调用的状态。
② **跨日不串号**：昨天的批次号不得让今天的序号从 2 起 —— 而这正是「日期用现场日期」这句
   话的可验形态；按 naive UTC 取日期会让跨日点在现场变成**次日 08:00**（D17）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobType
from app.engine.reasons import load_bulk_batch_nos, next_bulk_batch_no
from app.models.job import JobOrder

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"

#: 现场墙上时间（naive，D17）：日期与 `17` §10.7 的示例形状 `BAT-20260908-01` 对齐。
NOW = datetime(2026, 9, 8, 9, 0)


def _order(order_no: str, *, batch_no: str | None, day: str = "20260908",
           warehouse: str = WAREHOUSE) -> JobOrder:
    """一条作业单，只为它的 `bulk_batch_no` 而造（本用例的取数只读那一列）。"""
    return JobOrder(
        warehouse_id=warehouse,
        order_no=order_no,
        line_no="10",
        job_type=JobType.INBOUND,
        material_code="M1",
        qty=1,
        status=JobStatus.PLANNED,
        bulk_batch_no=batch_no,
    )


# --- 6.3 格式、进位与跨日（纯函数） -------------------------------------------


def test_the_first_batch_of_the_day_is_numbered_one() -> None:
    """当日还没有批次 ⇒ `NN = 01`，形状与 `17` §10.7 的示例逐字一致（D11）。

    两位数补零是文档的形状（`BAT-20260908-01`）而不是美观问题：它是给人念、给
    Excel 排序、给现场对单用的编号，`BAT-20260908-1` 与 `-01` 在同一列里会排到两处。
    """
    assert next_bulk_batch_no([], now=NOW) == "BAT-20260908-01"


def test_the_sequence_advances_within_the_same_day() -> None:
    """同日已有 1 / 2 / 3 个批次 ⇒ 依次给 `02` / `03` / `04`（D11 的「数量 + 1」）。"""
    assert next_bulk_batch_no(["BAT-20260908-01"], now=NOW) == "BAT-20260908-02"
    assert (
        next_bulk_batch_no(
            ["BAT-20260908-01", "BAT-20260908-02", "BAT-20260908-03"], now=NOW
        )
        == "BAT-20260908-04"
    )


def test_a_new_day_restarts_the_sequence_at_one() -> None:
    """跨日**重新从 1 起**，昨天的批次号不进今天的计数（D11「日期用现场日期」）。

    串号不是报错而是**重号**：`bulk_batch_no` 不是唯一键（D11 明写），所以昨天的 4 个批次
    若把今天的首个批次顶到 `05`，没有任何约束会拦住；反过来，若日期取错一天（例如按 naive
    UTC 取），同一天的批次会被劈成两段编号，而两段各自都「看着合理」。
    """
    yesterday = ["BAT-20260907-01", "BAT-20260907-02", "BAT-20260907-03"]
    assert (
        next_bulk_batch_no(yesterday, now=datetime(2026, 9, 8, 9, 0))
        == "BAT-20260908-01"
    )


def test_the_same_inputs_always_give_the_same_number() -> None:
    """**无随机源**（D2）：同一入参两次调用逐字相同。

    这条是「不得用 `uuid4` / 随机后缀」的可验形态（D11）—— 随机源的表现正是「同输入不同
    输出」，而它的后果要等到追溯时才显形：同一批次的单挂着两个号，或两批共用一号。
    """
    taken = ["BAT-20260908-01", "BAT-20260908-02"]
    assert next_bulk_batch_no(taken, now=NOW) == next_bulk_batch_no(taken, now=NOW)


def test_numbers_that_are_not_ours_do_not_shift_the_sequence() -> None:
    """别的形状（导入会话的 `IMP-` 前缀、大小写不同、缺序号段）不参与计数。

    取数侧已按前缀过滤（那个 `LIKE` 与这里同源），本函数再认一次形状不是多余的：`taken` 是
    调用方给的串，而「认不出就当 0 参与进位」会让下一个号从 1 起 —— 与一个已经存在的号相撞。
    """
    taken = ["BAT-20260908-01", "", "IMP-20260908-09", "bat-20260908-05", "BAT-20260908"]
    assert next_bulk_batch_no(taken, now=NOW) == "BAT-20260908-02"


def test_a_gap_in_the_sequence_does_not_produce_a_duplicate() -> None:
    """编号出现断档时取**最大值 + 1**，不是「数量 + 1」。

    D11 写的是「当日已有批次号的数量 + 1」，在本仓的口径下它与最大值同进 —— 因为批次号
    只由 8.2 在同一个事务里写回，整批回滚不留行，故编号从 `01` 起**稠密**。取最大值是把
    口径的**结果**照办、把安全性押在「回滚必须干净」之外的另一种写法：一旦哪天出现了断档
    （`01` 与 `03` 并存），「数量 + 1」会生出 `03` 这个**已经在用的号**，而
    `bulk_batch_no` 没有唯一约束，撞号不会有任何东西报错。
    """
    assert (
        next_bulk_batch_no(["BAT-20260908-01", "BAT-20260908-03"], now=NOW)
        == "BAT-20260908-04"
    )


def test_a_batch_number_beyond_two_digits_keeps_counting() -> None:
    """序号过 99 自然变三位（两位数不是上限）—— 单日 100 批不是天方夜谭，而截断回
    `00` 会让第 101 批与第 1 批同号。"""
    assert next_bulk_batch_no(["BAT-20260908-99"], now=NOW) == "BAT-20260908-100"


def test_an_aware_now_is_rejected_with_the_conversion_recipe() -> None:
    """带时区的 `now` 当场报错（D17）：批次号的日期是**现场日期**。

    挡在这里而不是「先算出来再说」：aware 的 `now` 取 `strftime` 得到的是**它自己时区的**
    日期，与本该传的现场日期哪天不是一回事，而两个结果都长得像一个合法日期。
    """
    with pytest.raises(TypeError) as excinfo:
        next_bulk_batch_no([], now=datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc))
    assert "带时区" in str(excinfo.value)


# --- 6.3 取数：仓库与日期前缀（SQL） ------------------------------------------


def _order_and_seed(session: Session, rows) -> None:
    session.add_all(rows)
    session.flush()


def test_the_loader_collects_only_this_warehouse_and_this_day(session: Session) -> None:
    """取数按**本仓 × 本日**过滤，且去重（D11 的「日期前缀计数」）。

    两种过滤各是一类失效：漏掉仓库过滤会让另一个厂的批次顶高本厂的序号（`17` §11 的数据
    隔离在引擎侧就这一处），漏掉日期过滤就是跨日串号（纯函数那条只钉了函数，钉不住取数）。
    """
    _order_and_seed(
        session,
        [
            _order("A1", batch_no="BAT-20260908-01"),
            _order("A2", batch_no="BAT-20260908-01"),  # 同批第二条：去重后仍算一个号
            _order("A3", batch_no="BAT-20260908-02"),
            _order("A4", batch_no="BAT-20260907-09"),  # 昨天
            _order("A5", batch_no=None),  # 还没批量
            _order("B1", batch_no="BAT-20260908-05", warehouse="GTJ99999"),  # 别的仓
        ],
    )

    taken = load_bulk_batch_nos(session, warehouse_id=WAREHOUSE, now=NOW)
    assert taken == ("BAT-20260908-01", "BAT-20260908-02")
    # 端到端：取数与生成接起来，序号接着本仓本日往下走。
    assert next_bulk_batch_no(taken, now=NOW) == "BAT-20260908-03"


def test_many_orders_sharing_one_batch_count_as_one_batch(session: Session) -> None:
    """同批 50 条单**只算一个批次号** —— 这是本口径最容易读错的一处。

    `bulk_batch_no` 是**分组标签**（`17` §4.1「一次批量生成一个批次」）：一次请求最多 50 单
    （D10），它们共用同一个批次号。按**行数**计数会让当日的第二个批次变成 `51`，而号段里
    没有 `03`~`50` —— 追溯时看到的是「这一天只跑了两批，却编到了 51 号」。
    """
    _order_and_seed(
        session,
        [_order(f"PO-{index:02d}", batch_no="BAT-20260908-01") for index in range(50)],
    )

    assert load_bulk_batch_nos(session, warehouse_id=WAREHOUSE, now=NOW) == (
        "BAT-20260908-01",
    )
    assert (
        next_bulk_batch_no(
            load_bulk_batch_nos(session, warehouse_id=WAREHOUSE, now=NOW), now=NOW
        )
        == "BAT-20260908-02"
    )


def test_the_loader_returns_nothing_when_the_day_is_untouched(session: Session) -> None:
    """本仓本日一条都没有（含「库里只有别的仓」）⇒ 空元组 ⇒ 生成 `01`。"""
    _order_and_seed(session, [_order("B1", batch_no="BAT-20260908-05", warehouse="GTJ99999")])

    taken = load_bulk_batch_nos(session, warehouse_id=WAREHOUSE, now=NOW)
    assert taken == ()
    assert next_bulk_batch_no(taken, now=NOW) == "BAT-20260908-01"
