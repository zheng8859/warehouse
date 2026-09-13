"""成品清单 ABC 统计导入的核心（分档 + upsert）。

事实来源：16-数据衔接与 cap 自维护 A.4.1（ABC 派生，A 20 / B 31 / C 204）
          spec `data-import`「成品清单 ABC 统计导入」
          openspec/changes/data-import/design.md D6（A≤70% / B≤90% / C 其余）

## 职责边界

本模块是**脚本通道**（`scripts/import_abc.py`）的两段可测核心，**不进 `ImportSession`
状态机、不建 `Snapshot`、不产 `JobOrder`**：

- `classify_abc`：按料号聚合出库量 → 降序累计占比分档。**纯函数**，无 IO。
- `apply_abc_classes`：把分档结果 upsert 到 `Material.abc_class`；料号不在主数据 → 记入
  `skipped` 跳过（不阻断，design.md 风险表「ABC 导入依赖 Material 已存在」的处置）。

分档阈值 `A=0.70 / B=0.90` 首期硬编码在脚本内（单厂内置，不做可视化配置，design.md D6）；
本模块暴露为可传参的默认值，是为了让 `tests/logic` 直接喂阈值边界，而非引入配置依赖。

## 分档口径（spec「按出库量累计占比分档」）

出库量降序、累计占比**到该料号为止（含自身）** `≤70% → A`、`≤90% → B`、其余 → C。
「占比 ≤ 阈值」用整数交叉相乘（`累计 × 100 ≤ 总量 × 阈值百分比`）判定，避免浮点
`0.70` 在二进制里不精确造成的边界漂移 —— 这是「同样输入必得同样输出」红线要求的确定性。
并列出库量按料号升序破平（`(-qty, code)`），保证结果与输入顺序无关。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AbcClass
from app.models.master_data import Material

__all__ = [
    "DEFAULT_A_SHARE",
    "DEFAULT_B_SHARE",
    "AbcApplyResult",
    "apply_abc_classes",
    "classify_abc",
]

#: A 类累计占比阈值（design.md D6：A≤70%）。
DEFAULT_A_SHARE: float = 0.70

#: B 类累计占比阈值（design.md D6：B≤90%，C 其余）。
DEFAULT_B_SHARE: float = 0.90

#: 百分比换算的分母（阈值份额 × 100 → 整数百分比，配合整数交叉相乘判边界）。
_PERCENT_SCALE = 100


@dataclass(frozen=True)
class AbcApplyResult:
    """`apply_abc_classes` 的结果：更新了多少料号、跳过了哪些缺失料号。"""

    updated: int
    skipped: tuple[str, ...]


def classify_abc(
    totals: Mapping[str, int],
    *,
    a_share: float = DEFAULT_A_SHARE,
    b_share: float = DEFAULT_B_SHARE,
) -> dict[str, AbcClass]:
    """按出库量降序累计占比分档：`≤ a_share → A`、`≤ b_share → B`、其余 → C。

    `totals`：料号 → 出库量（只含有出库量的料号，即 qty > 0；脚本侧聚合时已过滤）。
    空输入 / 全零 → 空结果（无可分档）。**纯函数**，无 IO、无副作用，结果确定（见模块
    docstring「分档口径」）。
    """
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(qty for _, qty in ranked)
    if total <= 0:
        return {}

    a_pct = round(a_share * _PERCENT_SCALE)
    b_pct = round(b_share * _PERCENT_SCALE)

    result: dict[str, AbcClass] = {}
    cumulative = 0
    for code, qty in ranked:
        cumulative += qty
        if cumulative * _PERCENT_SCALE <= total * a_pct:
            result[code] = AbcClass.A
        elif cumulative * _PERCENT_SCALE <= total * b_pct:
            result[code] = AbcClass.B
        else:
            result[code] = AbcClass.C
    return result


def apply_abc_classes(
    session: Session,
    *,
    warehouse_id: str,
    classes: Mapping[str, AbcClass],
) -> AbcApplyResult:
    """把分档结果 upsert 到 `Material.abc_class`；料号不在主数据 → 记入 `skipped` 跳过。

    幂等：重复跑覆盖同料号的 `abc_class`（upsert 语义，design.md D6）。不 flush / 不
    commit —— 事务边界属于调用方（脚本在全部更新后一次 commit）。逐料号 `select` 而非
    一次 `IN` 批量读，是因为跳过项要**逐条留名**，批量读还得二次对账，不如就地判定。
    """
    updated = 0
    skipped: list[str] = []
    for material_code, abc_class in classes.items():
        material = session.scalar(
            select(Material).where(
                Material.warehouse_id == warehouse_id,
                Material.material_code == material_code,
            )
        )
        if material is None:
            skipped.append(material_code)
            continue
        material.abc_class = abc_class
        updated += 1
    return AbcApplyResult(updated=updated, skipped=tuple(skipped))
