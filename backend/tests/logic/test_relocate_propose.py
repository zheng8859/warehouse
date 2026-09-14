"""④ 移库方案生成（`relocate/propose`）契约测试（tasks.md 3.6 的验证）。

事实来源：10-AI 辅助能力设计 §六（④ 多方案对比 + 量化代价，LLM 只叙事）
          openspec/changes/ai-assist/design.md D8（多方案规则算，复用三重校验）

核心不变量：多方案（激进/均衡/保守）与量化代价（板数/车次/时长）由**规则侧**确定性
计算（`build_relocate_plans`，纯函数），三重校验（cap 充足 / 批号不变 / 集中度改善）
逐档落标志；`relocate_propose` 只读、不写 `JobOrder`（红线 2「LLM 不直接执行写操作」）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.llm.capabilities import relocate_propose
from app.models.job import JobOrder
from app.services.relocate import build_relocate_plans
from tests.logic.conftest import AisleSpec, InventorySpec, MaterialSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 14, 10, 0)

#: doc 10 §六 的示例分布：12 巷 / 340 板，板数降序。
_DOC_PLATES = {
    "01": 85, "05": 60, "08": 45, "12": 38, "15": 30,
    "21": 25, "22": 22, "25": 15, "28": 10, "31": 5, "35": 3, "42": 2,
}


def _plenty() -> dict[str, int]:
    return {aisle: 1000 for aisle in _DOC_PLATES}


# ------------------------------------------------------------------ 多方案确定性 + 量化代价（tasks 3.6 的验证）

def test_build_relocate_plans_deterministic_and_ordered() -> None:
    """同输入必得同输出，且三档按激进程度递减（2 / 5 / 8，与 doc §六 同构）。"""
    available = _plenty()
    first = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=available)
    second = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=available)

    assert first == second
    assert [p["name"] for p in first["plans"]] == ["激进", "均衡", "保守"]
    assert [p["cross_aisle"]["after"] for p in first["plans"]] == [2, 5, 8]
    assert first["current"] == {"aisles": list(_DOC_PLATES), "cross_aisle": 12, "plates": 340}


def test_build_relocate_plans_aggressive_cost_matches_doc() -> None:
    """激进档：收拢到 01+05 → 195 板 / 13 车次 / ~2.5h（doc §六 示例值反推）。"""
    plans = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=_plenty())
    aggressive = plans["plans"][0]

    assert aggressive["target_aisles"] == ["01", "05"]
    assert aggressive["plates_to_move"] == 195
    assert aggressive["trips"] == 13
    assert aggressive["hours"] == pytest.approx(2.6)


def test_build_relocate_plans_conservative_clears_long_tail() -> None:
    """保守档：只清长尾（后 4 巷）→ 12→8，搬移量最小（20 板）。"""
    plans = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=_plenty())
    conservative = plans["plans"][2]

    assert conservative["target_aisles"] == ["01", "05", "08", "12", "15", "21", "22", "25"]
    assert conservative["clear_aisles"] == ["28", "31", "35", "42"]
    assert conservative["plates_to_move"] == 20
    assert conservative["cross_aisle"] == {"before": 12, "after": 8}


# ------------------------------------------------------------------ 三重校验（cap / 批号不变 / 集中度改善）

def test_build_relocate_plans_triple_validation_all_pass() -> None:
    """cap 充足时三档三重校验全过（cap_ok / batch_unchanged / concentration_improved）。"""
    plans = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=_plenty())
    for plan in plans["plans"]:
        assert plan["cap_ok"] is True
        assert plan["batch_unchanged"] is True
        assert plan["concentration_improved"] is True
        assert plan["valid"] is True


def test_build_relocate_plans_cap_insufficient() -> None:
    """目标巷道可用为 0 → 三档 cap_ok 均 False，valid 均 False（试算不落地）。"""
    plans = build_relocate_plans(
        plates_by_aisle=_DOC_PLATES, available={aisle: 0 for aisle in _DOC_PLATES}
    )
    assert [p["cap_ok"] for p in plans["plans"]] == [False, False, False]
    assert [p["valid"] for p in plans["plans"]] == [False, False, False]


def test_build_relocate_plans_single_aisle_no_improvement() -> None:
    """已集中到单巷时收拢不再改善集中度（after == before），集中度改善 False。"""
    plans = build_relocate_plans(plates_by_aisle={"01": 10}, available={"01": 100})
    for plan in plans["plans"]:
        assert plan["cross_aisle"] == {"before": 1, "after": 1}
        assert plan["concentration_improved"] is False
        assert plan["valid"] is False


# ------------------------------------------------------------------ relocate_propose 只读、不写 JobOrder（红线 2）

def test_relocate_propose_reads_snapshot_and_creates_no_job_order(session: Session) -> None:
    """`relocate_propose` 从快照算多方案，且**不产生**任何 `JobOrder`。"""
    aisles = [
        AisleSpec(aisle_no=f"{i:02d}", cap_total=100)
        for i in range(1, 9)  # 8 巷 → 激进 2 / 均衡 5 / 保守 6，三档可分。
    ]
    inventory = [
        InventorySpec(
            location_code=f"{i:02d}0101", material_code="M1", batch_no="B1", qty=60 - i
        )
        for i in range(1, 9)
    ]
    make_scenario(
        session,
        aisles=aisles,
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=inventory,
    )
    assert session.query(JobOrder).count() == 0

    result = relocate_propose(
        session, warehouse_id=WAREHOUSE, material_code="M1", now=NOW
    )

    assert session.query(JobOrder).count() == 0  # 只读，未经确认不产生作业单。
    metrics = result.rule["metrics"]
    assert [p["name"] for p in metrics["plans"]] == ["激进", "均衡", "保守"]
    assert metrics["current"]["cross_aisle"] == 8
    assert result.ai_generated is False  # 未配置 provider → 降级，仍有规则卡片。
    assert result.degraded_reason is not None
