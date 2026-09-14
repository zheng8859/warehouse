"""收拢方案只读派生的契约测试（tasks.md 1.1 的 RED，验证 `derive_consolidation_plan`）。

事实来源：15-04 §4.2（三重校验：cap 充足 / 批号不变 / 收拢后跨巷道数下降）、§4.3（降级链
          主巷道 → 次选 → 移出批量）
          17 §10.3（ConsolidationPlan 形状：batch_no / material_code / from_aisles /
              target_aisle / plates / expected_cross_aisle{before,after} / batch_unchanged）
          spec `transaction-base`「收拢方案只读派生」
          design.md D1（主巷道 = 板数最多、并列取巷道号升序，不动态切换；次选 = 第二多）
                   D2（纯函数：不触会话、不调 engine.invoke、不写 InventoryItem/Ledger）

钉住的口径：

  1. **主巷道**取自 `profile.plates_by_aisle`（物料级板数）的 argmax，并列按巷道号文本升序
     取最小；次选 = 板数第二多。
  2. **`from_aisles` / `plates` 按批号聚合**：`batch_plates_by_aisle` 里非目标巷道，升序；
     `plates` = 其板数之和；`batch_unchanged = true`（移库不改批号，结构性保证）。
  3. **`expected_cross_aisle`**：`before` = `profile.cross_aisle_count`；`after` = `before`
     − 收拢后变空的 from_aisle 数（判据 = `batch_plates_by_aisle[a] ==
     profile.plates_by_aisle[a]`，即该批号是该巷唯一物料板）。
  4. **三重校验**：① cap 充足（`available[target] >= plates`）② 批号不变（结构性）③
     集中度改善（`after < before`）。
  5. **降级链**：主巷道 cap 不足 → 次选（cap 充足且 `after < before` 仍成立，方案记
     `degrade_reason`）→ 仍不可行 → `moved_out_reason`。集中度不改善（`after >= before`）
     **不降级** —— 那是「已集中于主巷道 / 散落板与其他物料共占」，直接移出批量。
  6. **确定性**：同样输入两次同输出（无随机、无大模型）。
"""
from __future__ import annotations

import pytest

from app.engine.factors import InventoryProfile
from app.services.relocate import ConsolidationPlanResult, derive_consolidation_plan

pytestmark = pytest.mark.logic

MATERIAL = "M1"
BATCH = "B26090801"


def _profile(*, plates: dict[str, int]) -> InventoryProfile:
    """按「每巷板数」直接造一份单料号档案（物料级）。纯函数不读会话，直接造数即可。"""
    return InventoryProfile(snapshot_present=True, plates_by_aisle=plates)


def test_derive_main_aisle_is_most_concentrated() -> None:
    """主巷道 = 板数最多的巷道（物料级），`target_aisle` 即主巷道。"""
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.moved_out_reason is None
    assert result.plan["target_aisle"] == "01"


def test_derive_main_aisle_tie_picks_ascending() -> None:
    """主巷道并列（01 / 02 都是 30 板）→ 按巷道号文本升序取最小（01）。"""
    profile = _profile(plates={"02": 30, "01": 30, "03": 5})
    batch_plates = {"03": 5}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan["target_aisle"] == "01"


def test_derive_aggregates_scattered_plates_by_batch() -> None:
    """`from_aisles` / `plates` 按**批号**聚合：非主巷道的散落板，升序，`batch_unchanged`。"""
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    # 批号 B 散落在 02（10 板）、03（10 板），主巷道 01 没有本批。
    batch_plates = {"03": 10, "02": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan["from_aisles"] == ["02", "03"]
    assert result.plan["plates"] == 20
    assert result.plan["batch_unchanged"] is True
    assert result.plan["batch_no"] == BATCH
    assert result.plan["material_code"] == MATERIAL


def test_derive_expected_cross_aisle_before_after() -> None:
    """`before` = 物料跨巷道数；`after` = `before` − 收拢后变空的 from_aisle 数。

    批号独占 03（10 板 == 物料 03 巷的 10 板），收拢后 03 变空 → after = 3 − 1 = 2。
    """
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan["expected_cross_aisle"] == {"before": 3, "after": 2}


def test_derive_cap_insufficient_main_degrades_to_second() -> None:
    """主巷道 cap 不足 → 降级次选（板数第二多），方案记 `degrade_reason`。

    主巷道 01 可用 5 格，收拢 03 的 10 板不够 → 降级到次选 02（可用 100）。
    """
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    batch_plates = {"01": 5, "03": 10}
    available = {"01": 5, "02": 100, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.moved_out_reason is None
    assert result.plan["target_aisle"] == "02"
    assert result.plan["degrade_reason"]
    assert "容量不足" in result.plan["degrade_reason"]


def test_derive_cap_insufficient_both_moves_out() -> None:
    """主巷道与次选容量均不足 → 移出批量（`moved_out_reason` 非空、`plan` 为 None）。"""
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    batch_plates = {"01": 5, "03": 10}
    available = {"01": 5, "02": 5, "03": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "容量" in result.moved_out_reason


def test_derive_no_concentration_improvement_moves_out() -> None:
    """收拢后跨巷道数不低于收拢前（批号已集中于主巷道）→ 移出批量，不降级。"""
    profile = _profile(plates={"01": 30, "02": 20})
    batch_plates = {"01": 30}  # 批号全部已在主巷道 01，无散落板
    available = {"01": 100, "02": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "跨巷道" in result.moved_out_reason


def test_derive_shared_aisle_blocks_improvement() -> None:
    """散落板与其他物料共占（移出后巷道不变空）→ 集中度不改善，移出批量。"""
    profile = _profile(plates={"01": 30, "02": 20})
    batch_plates = {"02": 10}  # 02 巷物料 20 板，本批只占 10，移出后仍剩 10 → 不变空
    available = {"01": 100, "02": 100}

    result = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert result.plan is None
    assert result.moved_out_reason
    assert "跨巷道" in result.moved_out_reason


def test_derive_deterministic() -> None:
    """同样输入两次同输出（「同样输入必得同样输出」）。"""
    profile = _profile(plates={"01": 30, "02": 20, "03": 10})
    batch_plates = {"02": 10, "03": 10}
    available = {"01": 100, "02": 100, "03": 100}

    first = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )
    second = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=profile,
        batch_plates_by_aisle=batch_plates,
        available=available,
    )

    assert first == second


def test_consolidation_plan_result_exactly_one_of_plan_or_reason() -> None:
    """`ConsolidationPlanResult` 必须**恰好一个**非空：`plan` 或 `moved_out_reason`。"""
    with pytest.raises(ValueError):
        ConsolidationPlanResult(plan={}, moved_out_reason="both")

    with pytest.raises(ValueError):
        ConsolidationPlanResult(plan=None, moved_out_reason=None)
