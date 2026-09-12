"""`degradation.py` 的用例：覆盖**分档解析器**（5.1）、**降级原因与 A 类告警文案**（5.2）、
**四级走尽的处置**（5.3）。

事实来源：`14` §3.5（四级链的名字与顺序：近站台 → 次近巷道 → 远巷道 → 溢出区；
          「每一步都记录降级原因并展示给操作员」）
          `17` §2.1 / §3.4（`is_near_station` 的存在与**可空**语义：NULL = 未导出）
          `openspec/changes/recommendation-engine/specs/.../spec.md` 的
          「四级降级链与降级不静默」需求
          `design.md` D5（分档由数据源的可表达性决定，不编造阈值）、
          D2 第 1 条（一切遍历序显式给出）、D4（`AisleCap` 只读）
          `tasks.md` 5.1（本用例的任务书）

## 只验「怎么分组」，不验「怎么下探」

本段是**纯函数**：入参是候选巷道与「谁算近站台」这一列，出参是四个分组。逐档下探、
选档内最高分、记「停在哪一档」属分配器（6.x）—— 那一步要 6 因子评分与容量扣减，本文件
不 import `scoring` / `allocator`（D1 的模块切分），故可测性不依赖它们。

## 为什么 NULL 必须与 `False` **分开可辨**

`17` §2.1 与 `app/models/master_data.py` 第 3 条都写死了：`is_near_station = NULL` 是
「权威值未到位」，**不得当 `False` 用** —— 那会让近站台巷道静默退出预留池。所以本文件
既断言「NULL 的巷仍可达（进档 2，不被丢掉）」，也断言「它被单独报出来（供 5.2 写进
`degrade_reason`）」。
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from app.core.enums import AbcClass
from app.engine.degradation import (
    DEGRADATION_TIER_LABELS,
    allocation_failure,
    is_alarming,
    near_station_alert_message,
    near_station_gap,
    resolve_tiers,
    tier_stop_reason,
)

pytestmark = pytest.mark.logic


def _tier(plan, index: int) -> tuple[str, ...]:
    return plan.tiers[index].aisles


def test_the_chain_has_exactly_four_tiers_in_the_documented_order() -> None:
    """`14` §3.5 的链是**固定四级**，顺序即优先级：近站台 → 次近巷道 → 远巷道 → 溢出区。

    档位是链的骨架：`degrade_reason` 要写「停在哪一级」（spec 的「逐级下探并记录原因」），
    所以每档必须带自己的**标签**，而不是一个只有下标的空壳。
    """
    plan = resolve_tiers([], is_near_station={})
    assert [tier.index for tier in plan.tiers] == [0, 1, 2, 3]
    assert [tier.label for tier in plan.tiers] == list(DEGRADATION_TIER_LABELS)
    assert DEGRADATION_TIER_LABELS == ("近站台", "次近巷道", "远巷道", "溢出区")


def test_near_station_aisles_land_in_the_preferred_tier() -> None:
    """档 0 的成员判据就是 `is_near_station IS TRUE`（D5）—— 布尔列，数据源有。"""
    plan = resolve_tiers(
        ["01", "02", "05"], is_near_station={"01": True, "02": True, "05": False}
    )
    assert _tier(plan, 0) == ("01", "02")
    assert _tier(plan, 2) == ("05",)


def test_non_near_station_aisles_land_in_the_far_tier() -> None:
    """档 2 的成员判据是 `is_near_station IS FALSE`（D5），且它排在档 1 之后。"""
    plan = resolve_tiers(["07"], is_near_station={"07": False})
    assert _tier(plan, 2) == ("07",)
    assert plan.unknown_near_station == ()


def test_the_first_phase_reaches_only_tiers_zero_and_two() -> None:
    """本阶段只有档 0 与档 2 有成员（D5）：档 1 缺分档阈值、档 3 缺物理形态。

    两档**作为结构存在**（`plan.tiers` 里位置齐），只是成员为空 —— 下探逻辑因此照常
    经过它们（空集 ⇒ 直接进下一档），不必为「首期只有两档」写一条分支，也就不会有
    「四档结构」与「实际两档」两套代码。
    """
    plan = resolve_tiers(
        ["01", "02", "05", "07"],
        is_near_station={"01": True, "02": True, "05": False, "07": False},
    )
    assert [tier.index for tier in plan.tiers if tier.aisles] == [0, 2]
    assert _tier(plan, 1) == () and _tier(plan, 3) == ()


def test_a_tier_without_members_is_still_present_in_the_structure() -> None:
    """空档不是「从列表里消失」，而是「有一个空分组」—— 前者会让 `tiers[2]` 变成档 3。"""
    plan = resolve_tiers(["01"], is_near_station={"01": True})
    assert len(plan.tiers) == 4
    assert _tier(plan, 2) == ()


def test_each_tier_is_sorted_and_independent_of_the_input_order() -> None:
    """档内按 `aisle_no` **文本**升序，且与入参排列无关（D2 第 1 条）。

    不显式排序的后果不是报错，而是「换个查询计划就换了推荐次序」—— 而档内次序会逐项
    影响「得分并列时选谁」（D16 的并列裁决按升序全列），最终落进落库的报文。
    """
    flags = {"01": True, "02": True, "09": True, "05": False, "07": False}
    forward = resolve_tiers(["01", "09", "02", "07", "05"], is_near_station=flags)
    backward = resolve_tiers(["05", "07", "02", "09", "01"], is_near_station=flags)
    assert _tier(forward, 0) == _tier(backward, 0) == ("01", "02", "09")
    assert _tier(forward, 2) == _tier(backward, 2) == ("05", "07")


def test_an_unexported_near_station_flag_keeps_the_aisle_reachable_and_reports_it() -> None:
    """`is_near_station = NULL`（巷道主数据未导出）⇒ 进档 2 **且**单独报出来。

    两件事同时成立才算合规（`app/models/master_data.py` 第 3 条）：

    ① **仍可达** —— 丢掉这条巷会让「未导出」变成一次静默的候选集收缩（本可落在这条巷的
       单被迫降级，甚至四级走尽失败），而那正是「降级不静默」要拦的静默失败；
    ② **单独可辨** —— 若它与 `False` 同形，5.2 就没法在 `degrade_reason` 里写出
       「近站台标志未导出 ⇒ 本单按远巷道处理」，操作员看到的就是一条**没有成因**的降级。
    """
    plan = resolve_tiers(
        ["01", "05"], is_near_station={"01": None, "05": False}
    )
    assert _tier(plan, 0) == ()
    assert _tier(plan, 2) == ("01", "05")  # 未导出的那条与「非近站台」同档，但没被丢掉
    assert plan.unknown_near_station == ("01",)


def test_an_aisle_absent_from_the_flag_map_is_reported_as_unknown() -> None:
    """标志映射里没有这条巷（取数侧漏了）与 `NULL` 同处置：可达 + 报出来。

    这两种形态在调用侧都是「这一列的权威值没拿到」，分开处理没有意义；而**默认成
    `False` 静默放过**会让取数缺陷藏进一条看似正常的推荐里。
    """
    plan = resolve_tiers(["01", "05"], is_near_station={"05": False})
    assert _tier(plan, 2) == ("01", "05")
    assert plan.unknown_near_station == ("01",)


def test_an_empty_candidate_set_yields_four_empty_tiers() -> None:
    """候选集为空（该单四级走尽的那一态）⇒ 四个空档，而不是抛异常或返回空列表。

    「一套都不可行」是一条**结果**（spec 的「四级走尽则该单失败而非静默落位」），由
    分配器翻成「分配失败、停留 `PENDING`」；解析器只负责分组，不在这里替它下结论。
    """
    plan = resolve_tiers([], is_near_station={})
    assert len(plan.tiers) == 4
    assert all(tier.aisles == () for tier in plan.tiers)
    assert plan.unknown_near_station == ()


# --- 5.2 降级原因措辞与 A 类告警文案 -----------------------------------------


def test_the_stop_reason_names_the_level_and_why_the_higher_ones_were_skipped() -> None:
    """spec 的「逐级下探并记录原因」：`degrade_reason` 要写明**停在哪一级**及**为什么**。

    停档与跳档的成因必须分得开：档 0 有候选但容量不够（「无可行容量」）与档 1 本身没有
    成员（「无候选巷道」）是两回事 —— 前者要腾容量，后者要补数据源（D5），处置不同。
    """
    plan = resolve_tiers(
        ["01", "07"], is_near_station={"01": True, "07": False}
    )
    # 档 0 有成员（`01`）但容量塞不下 —— 成员来自候选集，故「有候选、无可行」分得开。
    stopped = plan.tiers[2]
    reason = tier_stop_reason(plan=plan, stop_tier=stopped)
    assert reason is not None
    assert "远巷道" in reason  # 停在哪一级
    assert "近站台" in reason and "无可行容量" in reason
    assert "次近巷道" in reason and "无候选巷道" in reason


def test_a_plan_stopping_at_the_preferred_tier_has_no_reason() -> None:
    """停在档 0（近站台）**不是降级** ⇒ `None`。降级标记不能变成"有档位就有原因"。"""
    plan = resolve_tiers(["01"], is_near_station={"01": True})
    assert tier_stop_reason(plan=plan, stop_tier=plan.tiers[0]) is None


def test_the_stop_reason_reports_unexported_near_station_flags_with_their_codes() -> None:
    """`is_near_station` 未导出的巷要点名 —— 成因不同则处置不同（D5 的补记）。

    只写「近站台无可行容量」会把人引去腾容量，而真问题是那一列没导出（补导出即可）。
    点名巷道号是为了让操作员知道去补哪几条的主数据。
    """
    plan = resolve_tiers(
        ["01", "02"], is_near_station={"01": None, "02": None}
    )
    reason = tier_stop_reason(plan=plan, stop_tier=plan.tiers[2])
    assert "未导出" in reason
    assert "01" in reason and "02" in reason


def test_the_gap_is_the_shortfall_not_the_whole_order() -> None:
    """「近站台缺口 X 板」的 X 是**差额**：本单需量 − 近站台剩余可用（不足为零）。

    报整单需量会把「只差一点」说成「完全没有」，而主管据此决定是否移库腾挪 —— 缺 2 板
    与缺 20 板的处置不一样。spec 的示例（缺口 12 板）正是这一形态。
    """
    assert near_station_gap(order_cells=20, near_station_remaining=8) == 12
    assert near_station_gap(order_cells=20, near_station_remaining=0) == 20
    assert near_station_gap(order_cells=20, near_station_remaining=20) == 0
    assert near_station_gap(order_cells=20, near_station_remaining=35) == 0


def test_the_a_class_alert_carries_the_documented_sentence_and_the_gap() -> None:
    """`14` §3.5 的告警文案**逐字**含「近站台缺口 X 板，建议移库腾挪」（spec 的同一条）。

    逐字而不是同义：这句是给主管的操作指令（移库腾挪），换一种说法就不是同一句指令了。
    """
    plan = resolve_tiers(["01", "07"], is_near_station={"01": True, "07": False})
    message = near_station_alert_message(
        plan=plan, order_cells=20, near_station_remaining=8
    )
    assert "近站台缺口 12 板，建议移库腾挪" in message


def test_the_alert_names_capacity_when_near_station_aisles_simply_lack_room() -> None:
    """有近站台巷道、只是塞不下 ⇒ 成因写「可用容量不足」（这时移库腾挪确实有用）。"""
    plan = resolve_tiers(["01", "07"], is_near_station={"01": True, "07": False})
    message = near_station_alert_message(
        plan=plan, order_cells=20, near_station_remaining=8
    )
    assert "容量不足" in message


def test_the_alert_names_the_missing_export_when_near_station_aisles_were_unidentified() -> None:
    """整表未导出（首期形态）⇒ 告警必须点出**成因**，否则它把人引向错误的处置。

    这一态下告警会**天天**触发（每条 A 类单的首次可行档就是档 2），若文案只说「缺口 12 板，
    建议移库腾挪」，主管会去腾容量 —— 而真问题是巷道主数据没导出（`16` §394 的预期状态）。
    """
    plan = resolve_tiers(["01", "07"], is_near_station={"01": None, "07": None})
    message = near_station_alert_message(
        plan=plan, order_cells=12, near_station_remaining=0
    )
    assert "近站台缺口 12 板，建议移库腾挪" in message  # 句式照旧（spec 要求逐字）
    assert "未导出" in message  # 成因另说
    assert "移库腾挪" in message


@pytest.mark.parametrize(
    ("abc_class", "tier_index", "expected"),
    [
        (AbcClass.A, 2, True),  # A 类被迫降到远巷道 ⇒ 告警（spec 的「A 类降级触发告警」）
        (AbcClass.A, 3, True),  # 溢出区同理
        (AbcClass.A, 1, False),  # 次近巷道不算「被迫降到远巷道」
        (AbcClass.A, 0, False),  # 没有降级
        (AbcClass.B, 2, False),  # 非 A 类不告警
        (AbcClass.C, 3, False),
        (None, 2, False),  # ABC 未派生：不知道是不是爆款，不得擅自告警
    ],
)
def test_only_an_a_class_forced_to_far_or_overflow_alarms(
    abc_class: AbcClass | None, tier_index: int, expected: bool
) -> None:
    """告警的触发面 = 「A 类」∩「停在档 2 / 档 3」（spec 的「降级至远巷道或溢出区」）。

    两个方向都要收紧：放宽到任何降级 ⇒ 每条 B/C 类单都告警，告警就不再是告警；放宽到任何
    单据 ⇒ 同上。`abc_class=None` 不告警 —— 未知档不是 A 类（`Material.abc_class`：「视为
    未知，不得当 C 类静默放行」；同理也不得当好料静默告警）。
    """
    plan = resolve_tiers(["01", "07"], is_near_station={"01": True, "07": False})
    assert (
        is_alarming(abc_class=abc_class, stop_tier=plan.tiers[tier_index]) is expected
    )


# --- 5.3 四级走尽（该单失败，不静默落位） ------------------------------------


def test_exhausting_every_tier_fails_the_order_with_a_human_intervention_prompt() -> None:
    """四级走尽 ⇒ **分配失败 + 提示人工介入**（spec 的同名场景 / `15` §11.1）。

    不是降级成功：链的末端没有「再往下」的档，把「哪儿都放不下」记成一次降级会让它在
    后验里与「降级到远巷道」同形，而两者一个是正常结果、一个是待人工处理的失败。
    """
    plan = resolve_tiers([], is_near_station={})
    failure = allocation_failure(job_order_id="PO-09", plan=plan)
    assert failure.job_order_id == "PO-09"
    assert "人工介入" in failure.message
    # 四档都要点过 —— 「四级走尽」这句话得让人看得出确实是四级都走完了。
    for label in DEGRADATION_TIER_LABELS:
        assert label in failure.message


def test_the_failure_says_the_order_stays_pending_and_produced_no_plan() -> None:
    """失败单的**处置**要写在文案里：不出方案、停留 `PENDING`（`15` §3.1 可重试）。

    这是 5.3 与 8.2 的接口：8.2 只把 `plans` 里的单迁 `PLANNED`，失败单不在其中，于是
    「不回写 `bulk_batch_no`」是**不在列表里**的自然结果 —— 文案把它说出来，免得读者以为
    漏了一条。
    """
    failure = allocation_failure(
        job_order_id="PO-09", plan=resolve_tiers([], is_near_station={})
    )
    assert "PENDING" in failure.message
    assert "方案" in failure.message


def test_a_failed_order_carries_no_aisle_so_nothing_can_be_mistaken_for_a_placement() -> None:
    """AC-005 的「**不静默落位到非法巷道**」在数据结构上的兑现：失败记录**没有巷道**。

    它因此塞不进 `degraded_alerts`（`DegradedAlert.aisle` 是必填的 `min_length=1`，
    `17` §10.7 的形状）—— 这是刻意的：给失败单编一条巷道，就等于产出了一条「落在某条
    不存在的巷道」的记录，而落位正是红线里「绝不静默」的那一步。
    """
    failure = allocation_failure(
        job_order_id="PO-09", plan=resolve_tiers([], is_near_station={})
    )
    assert {field.name for field in fields(failure)} == {"job_order_id", "message"}


def test_the_failure_message_reports_unexported_near_station_flags() -> None:
    """走尽的最常见成因（首期）是标志未导出，不是真的没容量 —— 文案必须点出来。"""
    plan = resolve_tiers(["01", "07"], is_near_station={"01": None, "07": None})
    failure = allocation_failure(job_order_id="PO-09", plan=plan)
    assert "未导出" in failure.message
    assert "01" in failure.message and "07" in failure.message


def test_the_failure_message_distinguishes_empty_tiers_from_full_ones() -> None:
    """「无候选巷道」与「无可行容量」在走尽文案里同样要分开（与停档原因同一措辞）。"""
    plan = resolve_tiers(["01", "07"], is_near_station={"01": True, "07": False})
    message = allocation_failure(job_order_id="PO-09", plan=plan).message
    assert "近站台无可行容量" in message
    assert "次近巷道无候选巷道" in message
