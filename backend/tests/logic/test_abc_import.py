"""成品清单 ABC 统计导入（tasks.md 5.1 的验证）。

事实来源：16-数据衔接与 cap 自维护 A.4.1（ABC 派生，A 20 / B 31 / C 204）
          spec `data-import`「成品清单 ABC 统计导入」（Scenario：按出库量累计占比分档、
          料号不存在记告警跳过不阻断）
          openspec/changes/data-import/design.md D6（A≤70% / B≤90% / C 其余，upsert 跳过缺失）

分两组钉住：

1. **分档（纯函数 `classify_abc`）**：按出库量降序累计占比，`≤70% → A`、`≤90% → B`、
   其余 → C。阈值边界（恰 70% / 恰 90% / 略超）逐个钉死，含空输入与并列量的确定性。
2. **upsert（`apply_abc_classes`）**：料号在主数据 → 更新 `abc_class`；不在 → 记入
   `skipped` 跳过（不阻断、不建基线、不进状态机）。用 `session` 夹具连内存库钉住。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.enums import AbcClass
from app.importer.abc import apply_abc_classes, classify_abc
from app.models.master_data import Material

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"


# ------------------------------------------------------------------ 分档：累计占比阈值边界

def test_classify_basic_abc_bands() -> None:
    """70 / 20 / 10 → 累计 70% / 90% / 100%：A、B、C 各一位。"""
    result = classify_abc({"M1": 70, "M2": 20, "M3": 10})
    assert result == {"M1": AbcClass.A, "M2": AbcClass.B, "M3": AbcClass.C}


def test_classify_exact_seventy_percent_boundary_is_a() -> None:
    """恰 70% 归 A（≤70% 含边界）。"""
    assert classify_abc({"M1": 7, "M2": 3})["M1"] is AbcClass.A


def test_classify_exact_ninety_percent_boundary_is_b() -> None:
    """恰 90% 归 B（≤90% 含边界）。"""
    result = classify_abc({"M1": 7, "M2": 2, "M3": 1})
    assert result["M2"] is AbcClass.B
    assert result["M3"] is AbcClass.C


def test_classify_just_over_seventy_is_b() -> None:
    """略超 70%（71%）→ B，不是 A。"""
    result = classify_abc({"M1": 71, "M2": 19, "M3": 10})
    assert result["M1"] is AbcClass.B


def test_classify_just_over_ninety_is_c() -> None:
    """略超 90%（91%）→ C，不是 B。"""
    result = classify_abc({"M1": 71, "M2": 20, "M3": 9})
    assert result["M2"] is AbcClass.C


def test_classify_empty_or_zero_total_is_empty() -> None:
    """空输入 / 全零出库量 → 空结果（无可分档）。"""
    assert classify_abc({}) == {}
    assert classify_abc({"M1": 0}) == {}


def test_classify_ties_are_deterministic() -> None:
    """并列出库量按料号升序破平 —— 同样输入必得同样输出（与输入顺序无关）。"""
    assert classify_abc({"M1": 5, "M2": 5}) == {"M1": AbcClass.A, "M2": AbcClass.C}
    assert classify_abc({"M2": 5, "M1": 5}) == {"M1": AbcClass.A, "M2": AbcClass.C}


# ------------------------------------------------------------------ upsert：更新既有 / 跳过缺失

def test_apply_updates_existing_and_skips_missing(session: Session) -> None:
    """料号在主数据 → 更新 `abc_class`；不在 → 记入 `skipped`，不阻断。"""
    m1 = Material(warehouse_id=WAREHOUSE, material_code="M1")
    m2 = Material(warehouse_id=WAREHOUSE, material_code="M2")
    session.add_all([m1, m2])
    session.flush()

    result = apply_abc_classes(
        session,
        warehouse_id=WAREHOUSE,
        classes={"M1": AbcClass.A, "M2": AbcClass.B, "M3": AbcClass.C},
    )

    assert result.updated == 2
    assert result.skipped == ("M3",)

    session.flush()
    assert m1.abc_class is AbcClass.A
    assert m2.abc_class is AbcClass.B


def test_apply_rerun_overwrites_same_material(session: Session) -> None:
    """幂等 + 覆盖：重复跑同一料号，`abc_class` 被覆盖为最新分档。"""
    m1 = Material(warehouse_id=WAREHOUSE, material_code="M1")
    session.add(m1)
    session.flush()

    apply_abc_classes(session, warehouse_id=WAREHOUSE, classes={"M1": AbcClass.A})
    session.flush()
    assert m1.abc_class is AbcClass.A

    apply_abc_classes(session, warehouse_id=WAREHOUSE, classes={"M1": AbcClass.B})
    session.flush()
    assert m1.abc_class is AbcClass.B
