"""6 因子中五个（`abc` / `cap` / `existing` / `batch` / `continuity`）的取数与归一化用例。

事实来源：`14` §3.1（队列输入取数）、§3.2（等级分 A=3/B=2/C=1）、§3.3（三列 cap 的口径）、
          §四（因子集合固定不得增删）
          `17` §10.1（**归一化口径的唯一反推依据** —— 示例值与算例）、§3.3（库存分布）、
          §3.4（cap 三列）
          `20` §六 `SC-003`（批次因子比对「本单批号 ∈ 该巷既有批号集」：既有集读**库存快照**）、
          `SC-005`（既有同物料落位抬升同巷道得分）
          `openspec/changes/recommendation-engine/design.md` D16（六因子的归一化口径表）
          `tasks.md` 2.1 / 10.1（本用例的任务书；10.1 的场景编号与文件归属见
          `test_scoring.py` 模块 docstring 里那张表）

**`station` 不在这里** —— 它的降级判据是 2.2 的任务，单独一条用例文件。

三处口径不是文档直给的，各自在下面的用例里写明了依据：

- `cap` 在 `cap_total == 0` 时**不降级**（给 0.00）。`cap_total` 按 `17` §3.4 已是「总格数 −
  已占格数」的**净额**，为 0 是「满巷道」这一合法状态，不是数据缺失 —— 而为它降级会连带
  把整个方案里 `cap` 这个因子拉掉（因子级降级是**方案级**的，见 `17` §10.1 的
  `breakdown` 键集约束），那是把一条巷道的事实放大成整批的事实。
- `existing` 的分母是本单占用格数，为 0 时**报错**而不是给 0：`16` §171 把「数量 > 0」
  列为导入期必须阻断的口径异常，所以 0 格的单不该走到评分这一步；静默给 0 会让
  「分母写错」与「本单真的没有集中度」两种成因长得一样。
- `batch` 在本单批号为空时**降级**（**2026-09-11 更正后的口径**）：批号由系统在**入库单
  建立时按生产批规则生成**（同一生产批共用同一批号，`14` §3.1），分配时刻本单批号**已知**，
  故该因子正常参与评分；`None` 只剩「登记侧该生成而没生成」这一种成色，属数据异常。
  降级而不是记 0.00，是因为「无从比较」与「比过了、没有」必须分得开 —— 后者是合法的
  0.00，前者要进 `factor_degraded`（见下面那条用例）。
"""
from __future__ import annotations

import inspect

import pytest

from app.core.enums import AbcClass
from app.engine.factors import (
    FactorOutcome,
    abc_factor,
    batch_factor,
    cap_factor,
    continuity_factor,
    existing_factor,
    load_snapshot_index,
)
from app.models.linkage import InventoryItem
from tests.logic.conftest import InventorySpec, MaterialSpec, make_scenario

pytestmark = pytest.mark.logic


# --- abc：等级分 ÷ 3（D16 反推自「A 类（3/3）→1.00」「B 类（2/3）→0.67」） -----------------
#
# `17` §10.1 的示例原写 `"value": 0.50` 而同一行的 `note` 写「B 类（2/3）」—— 同一行内
# 不自洽（2/3 = 0.67）。取 `0.67`：`note` 与 `01` 巷「可用 58 / 80 板」是同一范式
# （分子/分母即取值背后的原始比，`17` §10.1 的「字段语义」明确 note 用**原始口径**），
# 而 `14` §3.2 的等级分 A=3/B=2/C=1 也只有除以 3 才归一到 `[0,1]`、A 类才恰好满值。
# 已按铁律先改正本（该巷总分随之 `0.38 → 0.43`），订正记录见 `design.md` D16 的同一行。


@pytest.mark.parametrize(
    ("abc_class", "value", "note"),
    [
        (AbcClass.A, 1.00, "A 类（3/3）"),
        (AbcClass.B, 2 / 3, "B 类（2/3）"),
        (AbcClass.C, 1 / 3, "C 类（1/3）"),
    ],
)
def test_abc_grade_is_three_two_one(abc_class: AbcClass, value: float, note: str) -> None:
    outcome = abc_factor(order_abc_class=abc_class, material_abc_class=None)
    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(value)
    assert outcome.term.note == note


def test_abc_missing_degrades_instead_of_scoring_zero() -> None:
    """没有 ABC 分类是**数据缺失**，不是「C 类」—— 两者都必须可分辨（9.3 的四类空输入之一）。

    给 0.00 会让「ABC 未派生」与「C 类」在库里长得一样，而前者要标 `factor_degraded`。
    """
    outcome = abc_factor(order_abc_class=None, material_abc_class=None)
    assert outcome.term is None
    assert outcome.degrade_reason


def test_abc_falls_back_to_the_material_master() -> None:
    """单据上的 ABC 可空（`17` §4.1：派生完成前单子已经可以入队），物料主数据上是同一份
    成品清单聚合的结果（`16` A.4）—— 单据侧为空时读物料侧，两者都空才降级。"""
    outcome = abc_factor(order_abc_class=None, material_abc_class=AbcClass.A)
    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(1.00)

    # 单据侧有值时以它为准（它就是这条队列项的等级）
    outcome = abc_factor(order_abc_class=AbcClass.B, material_abc_class=AbcClass.A)
    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(2 / 3)


# --- cap：可用 ÷ cap_total（D16 反推自「可用 58/80→0.72」等三例） --------------------------


def test_cap_normalizes_against_cap_total() -> None:
    outcome = cap_factor(available=58, cap_total=80)
    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(0.725)
    assert outcome.term.note == "可用 58 / 80 板"


def test_cap_of_a_full_aisle_is_zero_not_a_degradation() -> None:
    """满巷道（净额 cap_total = 0）是合法状态 —— 见模块 docstring。"""
    outcome = cap_factor(available=0, cap_total=0)
    assert outcome.term is not None
    assert outcome.term.value == 0.0
    assert outcome.degrade_reason is None


# --- existing：min(1, 该巷既有板数 ÷ 本单占用格数)（D16 反推自「6 板→0.60」「0→0.00」） -----


def test_existing_normalizes_against_this_order_size() -> None:
    """分母是**本单**占用量，不是巷道容量 —— `17` §10.1 的算例里两条巷道的总容量都是 80，
    而既有板数 6 / 2 / 0 分别给出 0.60 / 0.20 / 0.00，同分母 10（本单 10 板）。"""
    profile = _profile({"01": 6}, {"01": frozenset({"GJP2571221"})})
    outcome = existing_factor(profile=profile, aisle="01", order_cells=10)
    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(0.60)
    assert outcome.term.note == "既有 6 板集中于此"


def test_existing_is_capped_at_one_and_zero_when_empty() -> None:
    profile = _profile({"01": 40}, {"01": frozenset({"GJP2571221"})})
    outcome = existing_factor(profile=profile, aisle="01", order_cells=10)
    assert outcome.term is not None
    assert outcome.term.value == 1.0

    # 有快照、但该物料不在这个巷道 —— 合法取值 0.00（`17` §10.1 的 `21` 就是这一形态）
    outcome = existing_factor(profile=profile, aisle="21", order_cells=10)
    assert outcome.term is not None
    assert outcome.term.value == 0.0
    assert outcome.term.note == "无既有库存"


def test_existing_rejects_a_zero_cell_order() -> None:
    """见模块 docstring：0 格的单不该走到评分这一步，静默给 0 会让两种成因混同。"""
    profile = _profile({"01": 6}, {"01": frozenset({"GJP2571221"})})
    with pytest.raises(ValueError, match="占用格数"):
        existing_factor(profile=profile, aisle="01", order_cells=0)


# --- batch：本单批号 ∈ 该巷既有批号集（SC-003） -------------------------------------------


def test_batch_reads_the_snapshot_batch_distribution() -> None:
    """`SC-003`：既有批号集中度被计入 —— `17` §10.1 的算例里 `01` 有批号 GJP2571221 ⇒ 1.00，
    `02` 有 2 板却是 0.00「无同批」，说明比的是**具体批号**而不是「有没有这个料」。

    预期列的另一半是「**不读 PO 批号**」（`20` §六 SC-003 行）：既有批号集只可能来自库存
    快照，而 `16` A.4 的 PO 模版根本没有批号列。这一半在下面用**入参形状**钉住 —— 不是
    修辞：本因子拿不到会话、拿不到单据对象，`16` §4.2 那条「严禁按列序号硬取字段」的
    旧口径若被"复活"成从 PO 文件里读批号，本因子会先多出一个入参。
    """
    # 「不读 PO 批号」：三个入参全是纯数据 —— `profile` 由库存快照建（`load_snapshot_index`），
    # `order_batch_no` 由登记侧给（`14` §3.1：入库单建立时按生产批规则生成）。没有会话、
    # 没有 `JobOrder`、没有导入产物，故它**不可能**去读 PO 文件里的任何一列。
    assert set(inspect.signature(batch_factor).parameters) == {
        "profile",
        "aisle",
        "order_batch_no",
    }

    profile = _profile(
        {"01": 6, "02": 2},
        {"01": frozenset({"GJP2571221"}), "02": frozenset({"GJP2571305"})},
    )
    hit = batch_factor(profile=profile, aisle="01", order_batch_no="GJP2571221")
    assert hit.term is not None
    assert hit.term.value == 1.00
    assert hit.term.note == "同批 GJP2571221 已在此巷道"

    miss = batch_factor(profile=profile, aisle="02", order_batch_no="GJP2571221")
    assert miss.term is not None
    assert miss.term.value == 0.00
    assert miss.term.note == "无同批"


def test_a_null_batch_degrades_defensively() -> None:
    """**2026-09-11 更正后的口径**：批号由系统在**入库单建立时按生产批规则生成**（同一生产批
    共用同一批号，`14` §3.1），分配时刻本单批号已知 —— 故 `batch` 是正常参与的因子，
    比对口径见上一条用例。这条用例守的是 `None` 这唯一的残余分支。

    `None` 不是「时点上拿不到」，而是**登记侧该生成而没生成**的数据异常。它仍然降级：
    给 0.00 会把「无从比较」记成「比过了、没有」—— 两者在 `breakdown` 里长得一样，
    而只有前者该出现在 `factor_degraded` 里（`17` §10.1「两种降级不得混用」的同一精神）。
    """
    profile = _profile({"01": 6}, {"01": frozenset({"GJP2571221"})})
    outcome = batch_factor(profile=profile, aisle="01", order_batch_no=None)
    assert outcome.term is None
    assert outcome.degrade_reason
    assert "批号" in outcome.degrade_reason


# --- continuity：在该巷的既有巷道集内 ⇒ 1 ÷ 跨巷道数（D16 反推自 1/2、1/1、0） -------------


def test_continuity_is_one_over_the_cross_aisle_count() -> None:
    profile = _profile(
        {"01": 6, "02": 2},
        {"01": frozenset({"GJP2571221"}), "02": frozenset({"GJP2571221"})},
    )
    both = continuity_factor(profile=profile, aisle="01")
    assert both.term is not None
    assert both.term.value == pytest.approx(0.50)
    assert both.term.note == "同物料现跨 2 个巷道"

    alone = _profile({"02": 2}, {"02": frozenset({"GJP2571221"})})
    only = continuity_factor(profile=alone, aisle="02")
    assert only.term is not None
    assert only.term.value == pytest.approx(1.00)
    assert only.term.note == "同物料现仅在此巷道"

    outside = continuity_factor(profile=profile, aisle="21")
    assert outside.term is not None
    assert outside.term.value == 0.00
    assert outside.term.note == "同物料不在该巷道"


def test_existing_stock_lifts_the_score_of_its_own_aisle() -> None:
    """`SC-005`：既有同物料落位抬升同巷道得分。

    这里断的是**因子取值**这一层；「因而得分更高、且落位确实落在这一条巷上」那一层隔着
    权重、分母与档内比较三处口径，落在 `test_allocator.py` 的
    `test_sc_005_an_aisle_holding_the_material_wins_on_its_score`（同一条场景，两个层次）。
    **不落 `test_scoring.py`**：那一层的 `score_aisles` 收的是**手写的分解**，在那儿断言
    「得分更高」只是把手填的数按恒等式重算一遍，是一句恒真的同义反复。

    下面两条断言缺一不可，因为 `20` 的预期列把这件事记在了两个因子上：

    | 断言 | 钉的是 |
    |---|---|
    | `existing` 在 `01`（6 板同物料）严格高于 `02`（无同物料） | 「既有同物料落位」直接抬分那一项 |
    | `continuity` 对两条巷道**相同**（都 0.50） | `continuity` 的口径是「1 ÷ 跨巷道数」，它分不开两条**都**在同物料既有巷道集里的巷 |

    第二条不是凑数：本用例的造数里同物料跨 `01`/`02` 两巷，故「既有落位」这件事在
    `continuity` 上表现为**两条巷等分**。`continuity` 真正拉开差距的是「物料**不在**该巷
    ⇒ 0.00」那一支（上面 `test_continuity_is_one_over_the_cross_aisle_count` 与分配器层
    那条 `SC-005` 用的都是那种造数）。
    """
    profile = _profile(
        {"01": 6, "02": 2},
        {"01": frozenset({"GJP2571221"}), "02": frozenset({"GJP2571305"})},
    )
    rich = existing_factor(profile=profile, aisle="01", order_cells=10)
    poor = existing_factor(profile=profile, aisle="02", order_cells=10)
    assert rich.term is not None and poor.term is not None
    assert rich.term.value > poor.term.value

    here = continuity_factor(profile=profile, aisle="01")
    there = continuity_factor(profile=profile, aisle="02")
    assert here.term is not None and there.term is not None
    assert here.term.value == pytest.approx(0.50)
    assert there.term.value == here.term.value


# --- 取数：库位号 → 巷道按 [:2] 文本切片 ---------------------------------------------------


def test_location_code_slices_into_aisle_by_text(session) -> None:
    """`010104` → `01`。**按文本切片、不得数值化**（CLAUDE.md §七：Excel 会把 `010104`
    数值化成 `10104`，前导 0 一丢，巷道就错了 —— 错一条巷道就是错一堆因子）。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec("010104", "3001234", "GJP2571221", qty=6),
            InventorySpec("010208", "3001234", "GJP2571221", qty=4),
            InventorySpec("010301", "3001234", "GJP2571221", qty=1),
        ],
    )
    index = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)

    profile = index.profile("3001234")
    assert profile.aisles == frozenset({"01"})
    assert profile.plates_by_aisle == {"01": 11}
    assert profile.cross_aisle_count == 1
    # 批号集按巷道聚合，而不是全库一份（否则 `02` 的检查会读到 `01` 的批号）
    assert profile.batches_by_aisle == {"01": frozenset({"GJP2571221"})}
    # 库位号本身没有被改写（前导 0 还在）
    assert {row.location_code for row in scenario.inventory} == {"010104", "010208", "010301"}


def test_snapshot_index_separates_materials(session) -> None:
    """索引按**料号**分组：`existing` / `continuity` 都是「同物料」的口径，
    串号会让 A 料的库存为 B 料抬分 —— 而且抬得看不出来。"""
    scenario = make_scenario(
        session,
        materials=[MaterialSpec("3001234"), MaterialSpec("3009999")],
        inventory=[
            InventorySpec("010104", "3001234", "GJP2571221", qty=6),
            InventorySpec("020104", "3009999", "GJP2571305", qty=9),
        ],
    )
    index = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)

    assert index.profile("3001234").plates_by_aisle == {"01": 6}
    assert index.profile("3009999").plates_by_aisle == {"02": 9}
    # 没有库存的料号：合法空档案，不是错误
    assert index.profile("3000000").plates_by_aisle == {}


def test_missing_snapshot_degrades_the_inventory_factors(session) -> None:
    """9.5：无 `Snapshot` 行 ⇒ 三个读库存的因子一律降级，但**不阻断**分配。

    与「有快照、该物料没库存」严格区分：后者是合法取值 0.00（`17` §10.1 的 `21`）。
    """
    make_scenario(session, snapshot_time=None)
    index = load_snapshot_index(session, snapshot_id=None)
    profile = index.profile("3001234")

    assert profile.snapshot_present is False
    assert profile.degrade_reason

    for outcome in (
        existing_factor(profile=profile, aisle="01", order_cells=10),
        continuity_factor(profile=profile, aisle="01"),
        batch_factor(profile=profile, aisle="01", order_batch_no="GJP2571221"),
    ):
        assert outcome.term is None
        assert outcome.degrade_reason


def test_snapshot_index_ignores_other_snapshots(session) -> None:
    """索引只读**指定快照**的行：旧版快照归档不删除（`17` §3.2），若把历史版本也算进来，
    集中度会被历次导入反复加权，而现场看不到任何异常。"""
    first = make_scenario(
        session,
        materials=[MaterialSpec("3001234")],
        inventory=[InventorySpec("010104", "3001234", "GJP2571221", qty=6)],
        snapshot_version_no=1,
    )
    second = make_scenario(
        session,
        inventory=[InventorySpec("210104", "3001234", "GJP2571305", qty=3)],
        snapshot_time=first.snapshot.snapshot_time.replace(day=9),
        snapshot_version_no=2,
        # 第二个场景只补**快照级**的行（新快照 + 它的库存行）：物料主数据、权重与容量配置
        # 都是**仓库级**的（`17` §七），同仓的两份快照共用一份 —— 再造一份会撞
        # `(warehouse_id, material_code)` / `(warehouse_id, version_no)` 的唯一键。
        weights=None,
        capacity=None,
    )

    assert load_snapshot_index(session, snapshot_id=first.snapshot.id).profile(
        "3001234"
    ).plates_by_aisle == {"01": 6}
    assert load_snapshot_index(session, snapshot_id=second.snapshot.id).profile(
        "3001234"
    ).plates_by_aisle == {"21": 3}


# --- 结果类型的自洽 ---------------------------------------------------------------------


def test_a_factor_outcome_is_either_a_value_or_a_reason() -> None:
    """「取值」与「降级原因」恰有其一 —— 两者都给（或都不给）的结果没法被消费方解读。"""
    with pytest.raises(ValueError):
        FactorOutcome()
    with pytest.raises(ValueError):
        FactorOutcome(term=None, degrade_reason=None)
    with pytest.raises(ValueError):
        FactorOutcome(
            term=FactorOutcome.scored(1.0, "x").term,
            degrade_reason="也给了原因",
        )
    assert FactorOutcome.scored(0.5, "说明").is_degraded is False
    assert FactorOutcome.degraded("原因").is_degraded is True


def _profile(plates: dict[str, int], batches: dict[str, frozenset[str]]):
    """手搓一个快照档案（不经库）—— 因子的算术语义与取数是两件事，各测各的。"""
    from app.engine.factors import InventoryProfile

    return InventoryProfile(
        snapshot_present=True, plates_by_aisle=plates, batches_by_aisle=batches
    )


def test_inventory_row_type_is_what_the_index_reads(session) -> None:
    """索引读的就是 `InventoryItem`：这条断言把「取数源」钉在模型上，防止将来
    换成别的行类型而用例仍绿。"""
    scenario = make_scenario(
        session, inventory=[InventorySpec("010104", "3001234", "GJP2571221", qty=6)]
    )
    assert isinstance(scenario.inventory[0], InventoryItem)
