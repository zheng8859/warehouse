"""近站台预留池的用例：`available_cap()` 的三个分支与释放判定（3.1）。

事实来源：`14` §3.3（近站台预留比例）、§3.5（预留超时释放给 B/C）、§2.2（近站台巷道的稀缺性）
          `17` §2.1（`is_near_station` 可空 = 权威值未到位）、§3.4（`AisleCap` 三列口径）
          `16` §353~356（比例 40% / 释放 18:00 的默认值表）
          `20` §六 `AC-001`（近站台紧张 + A 类大量入队 ⇒ A 类保留预留比例、慢流转品不占用）
          `openspec/changes/recommendation-engine/design.md` D7（公式与三个判据的取数源）、
          D17（`now` 是现场墙上时间）、D14（待确认默认值）
          `tasks.md` 3.1 / 10.2（本用例的任务书）

三处口径不是文档直给的（依据写在下面对应用例里）：`abc_class` 为空按非 A 类处理、
`is_near_station` 为空不特判（靠三列自然收敛）、释放钟点读配置而不是常量。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from app.core.enums import AbcClass
from app.engine.reserved import available_cap, is_reserve_released
from tests.logic.conftest import AisleSpec, make_scenario

pytestmark = pytest.mark.logic

#: 现场墙上时间的分配时刻基准（D17）。当天 09:00 —— 远早于默认的 18:00 释放钟点。
_TODAY_0900 = datetime(2026, 9, 11, 9, 0)

#: 一条典型的近站台巷道：80 格总量、其中 20 格是给 A 类爆款留的预留池（`cap_usable` = 60）。
_NEAR = AisleSpec("01", cap_total=80, cap_reserved=20, is_near_station=True)


def _first_cap(scenario):
    """造数里那条近站台巷道的 `AisleCap` 行 —— 本文件的断言全落在它身上。"""
    return scenario.aisle_caps["01"]


# --- 三个分支 ------------------------------------------------------------------------------


def test_ac_001_slow_moving_orders_do_not_touch_the_reserved_pool(session) -> None:
    """`AC-001`：近站台紧张时，慢流转品（B / C 类）不得占用预留池。

    预留池的存在意义就是「把近站台留给爆款」（`14` §2.2）—— 若 B/C 也能吃到它，A 类
    大量入队时爆款会被挤到远巷道，而这一条在库里**看不出异常**（占用没有独立台账）。

    `20` §六的预期列有两半，两条断言各钉一条：B / C 拿 `cap_usable`（= 60，**不占用**预留
    池）与 A 拿总额（= 80，**保留的那一份本就是它的**）。只断前者会漏掉一种实现：把预留池
    对所有角色都藏起来（大家都拿 60）—— 那在「慢流转品不占用」这一句上照样成立，而爆款
    反而更挤。候选集那一侧的形态见 `test_scoring.py` 的
    `test_a_non_a_order_cannot_reach_the_reserved_pool_of_a_near_station_aisle`。
    """
    scenario = make_scenario(session, aisles=[_NEAR])
    cap = _first_cap(scenario)
    # 释放钟点读**配置行**而不是常量：`16` §353~356 把它列为默认值表的一项，
    # 即它是可改的口径。默认 18:00 的出处也在此断言一次。
    release_at = scenario.capacity_config.reserved_release_at
    assert release_at == time(18, 0)

    for abc_class in (AbcClass.B, AbcClass.C):
        assert (
            available_cap(
                cap=cap, abc_class=abc_class, release_at=release_at, now=_TODAY_0900
            )
            == 60
        )

    # A 类拿总额：预留池本就是它的
    assert (
        available_cap(cap=cap, abc_class=AbcClass.A, release_at=release_at, now=_TODAY_0900)
        == 80
    )


def test_after_the_release_time_bc_shares_the_reserved_pool(session) -> None:
    """`14` §3.5：过了释放钟点，预留池释放给 B/C —— 于是 B/C 也拿到总额。

    边界**闭区间**（18:00 整即已释放），与 `is_effective` 同一手法；并且**每日重复**：
    只看钟点、不看日期，故次日的 09:00 又回到「B/C 仅 `cap_usable`」。
    """
    scenario = make_scenario(session, aisles=[_NEAR])
    cap = _first_cap(scenario)
    release_at = scenario.capacity_config.reserved_release_at

    def available_at(moment: datetime) -> int:
        return available_cap(
            cap=cap, abc_class=AbcClass.B, release_at=release_at, now=moment
        )

    assert available_at(datetime(2026, 9, 11, 17, 59, 59)) == 60
    assert available_at(datetime(2026, 9, 11, 18, 0, 0)) == 80  # 闭区间
    assert available_at(datetime(2026, 9, 11, 23, 30, 0)) == 80
    # 次日同一钟点仍然释放（`reserved_release_at` 是当日钟点，不是某个时刻）
    assert available_at(datetime(2026, 9, 12, 18, 0, 0)) == 80
    # …但次日早上又回到收紧状态 —— 「每日重复」的完整形态
    assert available_at(datetime(2026, 9, 12, 9, 0, 0)) == 60


def test_a_zero_reserved_pool_converges_to_the_full_cap(session) -> None:
    """`is_near_station` 未导出（`17` §2.1 的可空）时**不需要特判**：预留池只在近站台非零，
    未导出时快照侧的 `cap_reserved` 本身就是 0 ⇒ `cap_usable == cap_total` ⇒ 收敛为全额。

    这条守的是「不要加那条 `if is_near_station is None`」：加了它，本用例与下一条用例
    （自相矛盾的三列）就会走出两个不同的口径。
    """
    scenario = make_scenario(
        session, aisles=[AisleSpec("01", cap_total=80, cap_reserved=0, is_near_station=None)]
    )
    cap = _first_cap(scenario)
    release_at = scenario.capacity_config.reserved_release_at

    before = available_cap(
        cap=cap, abc_class=AbcClass.B, release_at=release_at, now=_TODAY_0900
    )
    after = available_cap(
        cap=cap,
        abc_class=AbcClass.B,
        release_at=release_at,
        now=_TODAY_0900.replace(hour=18),
    )

    assert before == after == 80


def test_the_snapshot_three_columns_are_authoritative_when_they_contradict_the_flag(
    session,
) -> None:
    """自相矛盾的行（`is_near_station` 为空、`cap_reserved > 0`）仍**以三列为准**（D7）。

    快照是权威：那三列是导入期按当日比例算好、与本次分配同源的量；一个未导出的**布尔**
    标志不足以推翻它。这种行要在**因子级降级**里记明（7.1 的事），不是在这里静默改判。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec("01", cap_total=80, cap_reserved=20, is_near_station=None)],
    )
    cap = _first_cap(scenario)

    assert (
        available_cap(
            cap=cap,
            abc_class=AbcClass.B,
            release_at=scenario.capacity_config.reserved_release_at,
            now=_TODAY_0900,
        )
        == 60
    )


# --- 两处口径：`abc_class` 为空、释放钟点从配置来 --------------------------------------------


def test_an_undetermined_abc_class_is_treated_as_non_a(session) -> None:
    """`abc_class is None`（ABC 未派生）按**非 A 类**处理 —— 保守读法。

    红线「非 A 类不得占用近站台预留池」的目的，是把稀缺的近站台留给爆款；而「分类未派生」
    **不构成「它是 A 类」的证据**。按 A 放行的代价是不可追溯的（预留池的占用没有独立台账），
    而按非 A 处理的代价是可见的（爆款落远巷道 ⇒ 5.2 的「近站台缺口」告警）。
    """
    scenario = make_scenario(session, aisles=[_NEAR])
    cap = _first_cap(scenario)
    release_at = scenario.capacity_config.reserved_release_at

    assert (
        available_cap(cap=cap, abc_class=None, release_at=release_at, now=_TODAY_0900) == 60
    )
    # 释放之后与 B/C 同路 —— 说明它不是被永久挡在门外，只是不享受预留
    assert (
        available_cap(
            cap=cap, abc_class=None, release_at=release_at, now=_TODAY_0900.replace(hour=18)
        )
        == 80
    )


def test_the_release_clock_comes_from_the_config_row(session) -> None:
    """释放钟点**读 `CapacityConfig.reserved_release_at`**，不是写死的 18:00。

    `16` §353~356 把它列为默认值表的一项 ⇒ 它是可改的口径。把 18:00 写进代码的表现是：
    现场改了配置却不生效，且没有任何地方会报错。故这里把释放时间挪到 06:00 再验一次。
    """
    scenario = make_scenario(
        session, aisles=[_NEAR], capacity={"reserved_release_at": time(6, 0)}
    )
    cap = _first_cap(scenario)
    release_at = scenario.capacity_config.reserved_release_at

    assert release_at == time(6, 0)
    # 07:00 在默认配置下**未**释放，在这份配置下已释放
    assert (
        available_cap(
            cap=cap, abc_class=AbcClass.B, release_at=release_at, now=_TODAY_0900.replace(hour=7)
        )
        == 80
    )
    # 05:00 仍收紧
    assert (
        available_cap(
            cap=cap, abc_class=AbcClass.B, release_at=release_at, now=_TODAY_0900.replace(hour=5)
        )
        == 60
    )


# --- 判定函数本身 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "released"),
    [
        (time(17, 59, 59), False),
        (time(18, 0), True),
        (time(18, 0, 1), True),
        (time(0, 0), False),
    ],
)
def test_is_reserve_released_compares_the_wall_clock(moment: time, released: bool) -> None:
    now = datetime.combine(datetime(2026, 9, 11).date(), moment)
    assert is_reserve_released(release_at=time(18, 0), now=now) is released


def test_an_aware_now_fails_loudly_instead_of_silently_comparing_two_clocks() -> None:
    """带时区的 `now` 在释放判定这一步就报错，而不是指望 `time` 比较拦住它。

    这条用例的由来是一次想当然：以为 aware 值与朴素 `time` 不可比、会当场抛 `TypeError`。
    **不会** —— `datetime.time()` 丢掉 `tzinfo`（`.timetz()` 才保留），于是 UTC 的钟点
    被拿去与 18:00 比，静默给出偏差 8 小时的释放判定（现场 19:00 时不释放、凌晨 02:00
    就释放），而日志里什么都看不出来。故 `require_wall_clock` 显式挡，并给出 D17 的换法。

    判据自 2026-09-12 起在 `app/core/clock.py`（D11 的批次号要的是同一条，两处各写一份会
    各自演化）—— 本用例因此验的是「释放判定这条路走得到它」，不是它的实现。
    """
    aware = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)

    with pytest.raises(TypeError, match="现场墙上时间"):
        is_reserve_released(release_at=time(18, 0), now=aware)

    # 同一个物理时刻，两种读法差 8 小时（本机/现场 UTC+8，此处把偏移钉死，免得用例
    # 结果随跑它的机器而变）：UTC 读作 18:00，现场读作次日 02:00。
    utc_read = aware.replace(tzinfo=None)
    local_read = aware.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert utc_read == datetime(2026, 9, 11, 18, 0)
    assert local_read == datetime(2026, 9, 12, 2, 0)
    assert is_reserve_released(release_at=time(18, 0), now=utc_read) is True
    assert is_reserve_released(release_at=time(18, 0), now=local_read) is False


def test_available_cap_reads_the_row_it_was_given(session) -> None:
    """容量取数读的是**指定快照的 `AisleCap` 行**，且**不改库**（D4：本阶段容量扣减不落库）。

    读操作若顺手写回（例如把 `cap_reserved` 清零表示「已释放」），历史快照就被篡改了，
    而「以快照重算基线」的对账口径（`16` §6.4）会跟着失真 —— 释放是**按钟点算出来的
    事实**，不是要落库的状态。
    """
    scenario = make_scenario(session, aisles=[_NEAR])
    cap = _first_cap(scenario)
    before = (cap.cap_total, cap.cap_reserved, cap.cap_usable)

    available_cap(
        cap=cap,
        abc_class=AbcClass.A,
        release_at=scenario.capacity_config.reserved_release_at,
        now=_TODAY_0900 + timedelta(hours=12),
    )

    assert (cap.cap_total, cap.cap_reserved, cap.cap_usable) == before
    assert scenario.aisle_caps["01"] is cap
