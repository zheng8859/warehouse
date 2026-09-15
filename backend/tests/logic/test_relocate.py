"""收拢方案只读派生的契约测试（tasks.md 1.1 的 RED，验证 `derive_consolidation_plan`）。

事实来源：15-04 §4.2（三重校验：cap 充足 / 批号不变 / 收拢后跨巷道数下降）、§4.3（降级链
          主巷道 → 次选 → 移出批量）
          17 §10.3（ConsolidationPlan 形状：batch_no / material_code / from_aisles /
              target_aisle / plates / source_locations / target_location /
              expected_cross_aisle{before,after} / batch_unchanged）
          spec `transaction-base`「收拢方案只读派生」
          design.md D1（主巷道 = 板数最多、并列取巷道号升序，不动态切换；次选 = 第二多）
                   D2（纯函数：不触会话、不调 engine.invoke、不写 InventoryItem/Ledger）

钉住的口径：

  1. **主巷道**取自 `profile.plates_by_aisle`（物料级板数）的 argmax，并列按巷道号文本升序
     取最小；次选 = 板数第二多。
  2. **`from_aisles` / `plates` 按批号聚合**：`batch_plates_by_aisle` 里非目标巷道，升序；
     `plates` = 其板数之和；`batch_unchanged = true`（移库不改批号，结构性保证）。
  3. **`expected_cross_aisle`**：`before` = `profile.cross_aisle_count`；`after` = `before`
     − 收拢后变空的 from_aisle 数。**物料级判据（缺口 1）**：一条非目标巷道的批号集
     (`profile.batches_by_aisle[a]`) 是 `moving_batches` 的子集才数作「变空」—— 交织散批
     （同巷多批共占）只有整料一并收拢才搬得空；`moving_batches` 缺省退化为 `{batch_no}`
     （单批收拢 = 本批是该巷唯一批）。
  4. **三重校验**：① cap 充足（`available[target] >= plates`）② 批号不变（结构性）③
     集中度改善（`after < before`）。
  5. **真实库位（缺口 2）**：`source_locations` = 逐格 `{location_code, qty}`（箱数），
     由 `batch_locations_by_aisle` 按 from_aisles 展平（库位号升序）；`target_location` =
     `material_locations_by_aisle[target][0]`（目标巷道内该物料的既有库位，确定性取最低
     库位号）。目标巷道无该物料的既有库位时**响亮失败**，不编造库位。
  6. **降级链**：主巷道 cap 不足 → 次选（cap 充足且 `after < before` 仍成立，方案记
     `degrade_reason`）→ 仍不可行 → `moved_out_reason`。集中度不改善（`after >= before`）
     **不降级** —— 那是「已集中于主巷道 / 散落板与其他物料共占」，直接移出批量。
  7. **确定性**：同样输入两次同输出（无随机、无大模型）。
"""
from __future__ import annotations

from collections.abc import Iterable

import pytest

from app.engine.factors import InventoryProfile
from app.services.relocate import ConsolidationPlanResult, derive_consolidation_plan

pytestmark = pytest.mark.logic

MATERIAL = "M1"
BATCH = "B26090801"
OTHER = "B26090802"


def _profile(*, plates: dict[str, int], batches: dict[str, frozenset[str]]) -> InventoryProfile:
    """按「每巷箱数 + 每巷批号集」造一份单料号档案（物料级）。纯函数不读会话，直接造数。"""
    return InventoryProfile(
        snapshot_present=True, plates_by_aisle=plates, batches_by_aisle=batches
    )


def _batch_locations(aisle_qtys: dict[str, int]) -> dict[str, list[tuple[str, int]]]:
    """批号级逐格库位：每巷一格（`{aisle}0101`），箱数 = 该批在该巷的箱数。"""
    return {aisle: [(f"{aisle}0101", qty)] for aisle, qty in aisle_qtys.items()}


def _material_locations(aisles: Iterable[str]) -> dict[str, list[str]]:
    """物料级库位：每巷一个既有库位（`{aisle}0101`），作为目标库位候选。"""
    return {aisle: [f"{aisle}0101"] for aisle in aisles}


def test_derive_main_aisle_is_most_concentrated() -> None:
    """主巷道 = 板数最多的巷道（物料级），`target_aisle` 即主巷道。"""
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.moved_out_reason is None
    assert result.plan["target_aisle"] == "01"


def test_derive_main_aisle_tie_picks_ascending() -> None:
    """主巷道并列（01 / 02 都是 30 板）→ 按巷道号文本升序取最小（01）。"""
    profile = _profile(
        plates={"02": 30, "01": 30, "03": 5},
        batches={"01": {OTHER}, "02": {OTHER}, "03": {BATCH}},
    )
    batch_plates = {"03": 5}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan["target_aisle"] == "01"


def test_derive_aggregates_scattered_plates_by_batch() -> None:
    """`from_aisles` / `plates` 按**批号**聚合：非主巷道的散落板，升序，`batch_unchanged`。"""
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    # 批号 B 散落在 02（10 板）、03（10 板），主巷道 01 没有本批。
    batch_plates = {"03": 10, "02": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan["from_aisles"] == ["02", "03"]
    assert result.plan["plates"] == 20
    assert result.plan["batch_unchanged"] is True
    assert result.plan["batch_no"] == BATCH
    assert result.plan["material_code"] == MATERIAL


def test_derive_expected_cross_aisle_before_after() -> None:
    """`before` = 物料跨巷道数；`after` = `before` − 收拢后变空的 from_aisle 数。

    批号独占 03（该巷批号集 = {BATCH} ⊆ moving={BATCH}），收拢后 03 变空；
    02 与 OTHER 共占（{BATCH, OTHER} ⊄ {BATCH}），收拢后 02 不变空 → after = 3 − 1 = 2。
    """
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan["expected_cross_aisle"] == {"before": 3, "after": 2}


def test_derive_cap_insufficient_main_degrades_to_second() -> None:
    """主巷道 cap 不足 → 降级次选（板数第二多），方案记 `degrade_reason`。

    主巷道 01 可用 5 格，收拢 03 的 10 板不够 → 降级到次选 02（可用 100）。
    """
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {BATCH, OTHER}, "02": {OTHER}, "03": {BATCH}},
    )
    batch_plates = {"01": 5, "03": 10}
    available = {"01": 5, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.moved_out_reason is None
    assert result.plan["target_aisle"] == "02"
    assert result.plan["degrade_reason"]
    assert "容量不足" in result.plan["degrade_reason"]


def test_derive_cap_insufficient_both_moves_out() -> None:
    """主巷道与次选容量均不足 → 移出批量（`moved_out_reason` 非空、`plan` 为 None）。"""
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {BATCH, OTHER}, "02": {OTHER}, "03": {BATCH}},
    )
    batch_plates = {"01": 5, "03": 10}
    available = {"01": 5, "02": 5, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "容量" in result.moved_out_reason


def test_derive_no_concentration_improvement_moves_out() -> None:
    """收拢后跨巷道数不低于收拢前（批号已集中于主巷道）→ 移出批量，不降级。"""
    profile = _profile(
        plates={"01": 30, "02": 20},
        batches={"01": {BATCH}, "02": {OTHER}},
    )
    batch_plates = {"01": 30}  # 批号全部已在主巷道 01，无散落板
    available = {"01": 100, "02": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "跨巷道" in result.moved_out_reason


def test_derive_shared_aisle_blocks_improvement() -> None:
    """散落板与其他物料共占（移出后巷道不变空）→ 集中度不改善，移出批量。"""
    profile = _profile(
        plates={"01": 30, "02": 20},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}},
    )
    batch_plates = {"02": 10}  # 02 巷物料 20 板，本批只占 10，移出后仍剩 OTHER → 不变空
    available = {"01": 100, "02": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "跨巷道" in result.moved_out_reason


def test_derive_deterministic() -> None:
    """同样输入两次同输出（「同样输入必得同样输出」）。"""
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}
    kwargs = dict(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    first = derive_consolidation_plan(**kwargs)
    second = derive_consolidation_plan(**kwargs)

    assert first == second


def test_derive_material_level_concentration_counts_shared_aisle() -> None:
    """缺口 1：`after` 按物料级判据 —— 共占巷道只有整料一并收拢才数作「变空」。

    单批收拢（`moving_batches` 缺省 → `{BATCH}`）：02 巷与 OTHER 共占，搬出 BATCH 后不变空，
    `after` 只从 3 降到 2；整料收拢（`moving_batches={BATCH, OTHER}`）：02 也一并搬空，
    `after` 降到 1。
    """
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}
    kwargs = dict(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle=_material_locations(available.keys()),
    )

    single = derive_consolidation_plan(**kwargs)
    whole = derive_consolidation_plan(**kwargs, moving_batches={BATCH, OTHER})

    assert single.plan["expected_cross_aisle"] == {"before": 3, "after": 2}
    assert whole.plan["expected_cross_aisle"] == {"before": 3, "after": 1}


def test_derive_real_source_and_target_locations() -> None:
    """缺口 2：`source_locations` / `target_location` 取真实库位，不编造「巷道 + 后缀」。

    逐格源库位按 from_aisles 展平（每巷按库位号升序）；目标库位 = 目标巷道内该物料既有
    库位的最低库位号（`material_locations_by_aisle[target][0]`）。
    """
    profile = _profile(
        plates={"01": 30, "02": 20, "03": 10},
        batches={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    batch_plates = {"02": 10, "03": 10}
    batch_locations = {"02": [("020101", 4), ("020205", 6)], "03": [("030101", 10)]}
    material_locations = {"01": ["010101", "010301"], "02": ["020101"], "03": ["030101"]}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=batch_locations,
        material_locations_by_aisle=material_locations,
    )

    assert result.plan["source_locations"] == [
        {"location_code": "020101", "qty": 4},
        {"location_code": "020205", "qty": 6},
        {"location_code": "030101", "qty": 10},
    ]
    assert result.plan["target_location"] == "010101"


def test_derive_target_without_material_location_fails_loudly() -> None:
    """缺口 2 的响亮失败：目标巷道无该物料的既有库位 → 不编造库位，移出批量。"""
    profile = _profile(
        plates={"01": 30, "02": 20},
        batches={"01": {OTHER}, "02": {BATCH}},
    )
    batch_plates = {"02": 10}
    available = {"01": 100, "02": 100}
    # material_locations 缺目标主巷道 01 → 目标库位无从解析（次选 02 亦因集中度不改善失败）。
    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
        batch_locations_by_aisle=_batch_locations(batch_plates),
        material_locations_by_aisle={"02": ["020101"]},
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "无该物料的既有库位" in result.moved_out_reason


def test_consolidation_plan_result_exactly_one_of_plan_or_reason() -> None:
    """`ConsolidationPlanResult` 必须**恰好一个**非空：`plan` 或 `moved_out_reason`。"""
    with pytest.raises(ValueError):
        ConsolidationPlanResult(plan={}, moved_out_reason="both")

    with pytest.raises(ValueError):
        ConsolidationPlanResult(plan=None, moved_out_reason=None)
