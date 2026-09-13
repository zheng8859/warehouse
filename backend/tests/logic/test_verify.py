"""三口径后验纯函数的契约测试（tasks.md 4.1 的验证）。

事实来源：15-00 §六（阈值表）/ §1.3（逐单口径 vs 聚合口径）
          15-01 §8.1（后验口径）/ 15-02 §8 / 15-03 §8.1 / 15-04 §8.1（三类口径）
          spec `transaction-base`「同步后验与三口径判定」

三条要钉住的口径：

  1. **入库**（逐单绝对阈值）：同物料跨巷道 ≤5 且 同批跨巷道 ≤3，两条各自判
     `PASS / DEVIATION`（任一超标即该口径 DEVIATION，`17` §4.4 一行一条指标）。
  2. **出库**（加权集中度 80% ≤ N=5）：按拣货量降序累加至 80% 所覆盖巷道数；超标
     只判 `DEVIATION` 不阻断（高亮放行，`15-03` §6.2）。
  3. **移库**（相对阈值）：移库后同物料跨巷道数**低于**移库前；持平或恶化即
     `DEVIATION`（`15-04` §8.1）。

快照缺失时 `verify_inbound` 抛 `ValueError` 而不是假达标（模块 docstring 的口径）。
"""
from __future__ import annotations

import pytest

from app.core.enums import VerifyResult
from app.engine.factors import SnapshotIndex
from app.services.verify import (
    concentration_aisle_count,
    verify_inbound,
    verify_outbound,
    verify_relocate,
)

pytestmark = pytest.mark.logic

MATERIAL = "M1"
BATCH = "B1"


def _snapshot(
    *,
    material_aisles: list[str],
    batch_aisles: list[str],
    material_code: str = MATERIAL,
    batch_no: str = BATCH,
) -> SnapshotIndex:
    """造一份**单物料**快照：物料铺在 `material_aisles`、批号铺在 `batch_aisles`。

    批号铺的巷道是物料巷道的子集（同批必然有该物料的板），直接构造两张映射即可 ——
    `cross_aisle_count` 只读 `plates_by_material`、`_batch_aisle_count` 只读
    `batches_by_material`，二者互不干扰。
    """
    return SnapshotIndex(
        snapshot_present=True,
        plates_by_material={material_code: {a: 10 for a in material_aisles}},
        batches_by_material={
            material_code: {a: frozenset({batch_no}) for a in batch_aisles}
        },
    )


# ------------------------------------------------------------------ 入库：绝对阈值（≤5 / ≤3）

def test_verify_inbound_pass() -> None:
    """同物料 5 巷道、同批 3 巷道 → 两口径都 PASS（正好压在阈值上也算达标）。"""
    snapshot = _snapshot(
        material_aisles=["01", "02", "03", "04", "05"],
        batch_aisles=["01", "02", "03"],
    )

    material, batch = verify_inbound(
        material_code=MATERIAL, batch_no=BATCH, snapshot=snapshot
    )

    assert material.metric_kind == "同物料跨巷道"
    assert material.actual_value == 5
    assert material.threshold_value == 5
    assert material.verify_result is VerifyResult.PASS

    assert batch.metric_kind == "同批跨巷道"
    assert batch.actual_value == 3
    assert batch.threshold_value == 3
    assert batch.verify_result is VerifyResult.PASS


def test_verify_inbound_material_deviation() -> None:
    """同物料跨 6 巷道（>5）→ 主口径 DEVIATION，同批仍 PASS（两条各自判）。"""
    snapshot = _snapshot(
        material_aisles=["01", "02", "03", "04", "05", "06"],
        batch_aisles=["01", "02", "03"],
    )

    material, batch = verify_inbound(
        material_code=MATERIAL, batch_no=BATCH, snapshot=snapshot
    )

    assert material.actual_value == 6
    assert material.verify_result is VerifyResult.DEVIATION
    assert batch.actual_value == 3
    assert batch.verify_result is VerifyResult.PASS


def test_verify_inbound_batch_deviation() -> None:
    """同批跨 4 巷道（>3）→ 辅口径 DEVIATION，同物料仍 PASS。"""
    snapshot = _snapshot(
        material_aisles=["01", "02", "03", "04"],
        batch_aisles=["01", "02", "03", "04"],
    )

    material, batch = verify_inbound(
        material_code=MATERIAL, batch_no=BATCH, snapshot=snapshot
    )

    assert material.actual_value == 4
    assert material.verify_result is VerifyResult.PASS
    assert batch.actual_value == 4
    assert batch.verify_result is VerifyResult.DEVIATION


def test_verify_inbound_missing_snapshot_raises() -> None:
    """快照缺失 → `ValueError`（「没算」不是「达标」，交给编排迁 `VERIFY_FAILED`）。"""
    absent = SnapshotIndex.absent("当前无库存快照")
    with pytest.raises(ValueError):
        verify_inbound(material_code=MATERIAL, batch_no=BATCH, snapshot=absent)


# ------------------------------------------------------------------ 出库：加权集中度（80% ≤ N）

def test_concentration_aisle_count_covers_80_percent() -> None:
    """60/25/10/5 → 80% 的 100 是 80：60 不够、加 25 到 85 够 → 覆盖 2 巷道。"""
    assert (
        concentration_aisle_count(
            pick_qty_by_aisle={"01": 60, "02": 25, "03": 10, "04": 5}
        )
        == 2
    )


def test_verify_outbound_pass() -> None:
    """集中度 2 ≤ N=5 → PASS。"""
    result = verify_outbound(
        pick_qty_by_aisle={"01": 60, "02": 25, "03": 10, "04": 5}
    )

    assert result.metric_kind == "拣货量加权集中度"
    assert result.actual_value == 2
    assert result.threshold_value == 5
    assert result.verify_result is VerifyResult.PASS


def test_verify_outbound_deviation() -> None:
    """7 巷道各 10 板 → 80% 的 56 需累加 6 条巷道（50 不够、60 够）→ 6 > 5 判偏离。"""
    result = verify_outbound(
        pick_qty_by_aisle={f"0{i}": 10 for i in range(1, 8)}
    )

    assert result.actual_value == 6
    assert result.verify_result is VerifyResult.DEVIATION


def test_verify_outbound_empty_is_pass() -> None:
    """无拣货量 → 集中度 0，`0 ≤ N` 是「没覆盖巷道」而非「完美集中」，仍 PASS。"""
    result = verify_outbound(pick_qty_by_aisle={})

    assert result.actual_value == 0
    assert result.verify_result is VerifyResult.PASS


# ------------------------------------------------------------------ 移库：相对阈值（后 < 前）

def test_verify_relocate_pass() -> None:
    """移库后 4 巷道 < 移库前 8 巷道 → PASS。"""
    result = verify_relocate(cross_aisle_before=8, cross_aisle_after=4)

    assert result.metric_kind == "同物料跨巷道是否下降"
    assert result.actual_value == 4
    assert result.threshold_value == 8
    assert result.verify_result is VerifyResult.PASS


def test_verify_relocate_deviation_on_tie() -> None:
    """移库后与移库前持平（4 == 4）→ 未下降 → DEVIATION。"""
    result = verify_relocate(cross_aisle_before=4, cross_aisle_after=4)

    assert result.verify_result is VerifyResult.DEVIATION


def test_verify_relocate_deviation_on_worse() -> None:
    """移库后反而更散（6 > 4）→ 恶化 → DEVIATION（移库不应让情况变差）。"""
    result = verify_relocate(cross_aisle_before=4, cross_aisle_after=6)

    assert result.verify_result is VerifyResult.DEVIATION
