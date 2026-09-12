"""`station`（站台就近）因子的取数与降级判据用例 —— 6 因子里唯一不读库存快照的那个。

事实来源：`17` §2.2（巷道-站台主数据 `AisleStation`：`distance_weight` —— 列注释给了 `0.9`
          （近）/ `0.3`（远）两个示例，且**未声明值域上界**）、§10.1（`factor_degraded["station"]`
          的示例原因「巷道-站台主数据未导出」）、§11（数据隔离）
          `14` §3.1（队列输入取数）、§四（因子集合固定不得增删）
          `16` §394 / `A.4`（该表首期为空是**预期状态**，不是漏种）
          `openspec/changes/recommendation-engine/design.md` D16 的 `station` 行
          `tasks.md` 2.2（本用例的任务书）

## 判据是「该巷有没有行」，且它是**方案级**的

`station_factor` 只有一条巷道的视野，它回答「这一巷有没有站台数据」。而 `17` §10.1 要求
每条候选巷道的 `breakdown` 键集都恰为「六因子 − `factor_degraded`」—— 于是**部分参与**是
一种自相矛盾的形态：`01` 巷的分解里有 `station`、`02` 巷没有，两条巷的键集就不相等，理由
一落库就被 `schemas/reason.py` 的契约拒。所以判据必须在**候选集**上成立：要么全部参与、
要么该因子整方案降级。**这条聚合归 3.3 的 `scoring.py`**（它才知道候选集），本文件只把
「一条巷缺行」这个事实钉住，并把两个方向（有行 / 无行）都断言到。

`design.md` D16 原来把这条写成「本阶段恒降级（表为空）」—— 那是**首期表空**这一事实的
推论，不是判据本身；已按铁律先改正本（D16 该行），代码与用例照改后的判据写。
"""
from __future__ import annotations

import pytest

from app.engine.factors import load_station_weights, station_factor
from tests.logic.conftest import DEFAULT_WAREHOUSE_ID, AisleSpec, make_scenario

pytestmark = pytest.mark.logic

#: 首期形态：`Aisle` 行都在，`AisleStation` 一条都没有（`16` §394 / `A.4`）。
#: 两条巷道里一条近站台、一条不是 —— 「有没有站台行」与「是不是近站台巷道」是两件事
#: （前者是主数据导出，后者是预留池资格，`14` §2.2），首期形态必须让这两者分得开。
_FIRST_PHASE_AISLES = [
    AisleSpec("01", is_near_station=True),
    AisleSpec("02", is_near_station=False),
]


# --- 首期预期状态：表为空 ⇒ 该因子不参与 ---------------------------------------------------


def test_the_empty_table_degrades_every_aisle(session) -> None:
    """首期该因子在**每个**候选巷道上都降级 —— 于是它在整批里不参与评分（`16` §394）。

    断言的是「全部候选都降级」而不是「某一条降级」：只要有一条巷有值，方案级判据就会
    让整个因子降级 —— 两个方向都算出来了，`scoring.py` 的聚合（3.3）没有第三种选择。
    """
    scenario = make_scenario(session, aisles=_FIRST_PHASE_AISLES)
    weights = load_station_weights(session, warehouse_id=scenario.warehouse_id)

    # 空表是**合法返回**，不是错误、也不是 `None`：调用方不该为它写 try/except
    assert weights == {}

    for aisle_no in scenario.aisles:
        outcome = station_factor(distance_weight=weights.get(aisle_no))
        assert outcome.is_degraded is True
        assert outcome.term is None
        # 原因要能指回是哪份数据没到位 —— 「降级不静默」（CLAUDE.md §四）
        assert "站台主数据" in outcome.degrade_reason


def test_a_missing_row_degrades_even_when_the_aisle_exists(session) -> None:
    """「有 `Aisle` 行」不等于「有 `AisleStation` 行」—— 判据读的是后者（`17` §2.2）。

    这条防的是取数写歪：若实现改去 `Aisle` 上找权重（那一行确实在），首期就会变成
    「所有巷道满分参与」，而理由里看不出任何异常 —— 恰恰是最坏的一种错。
    """
    scenario = make_scenario(session, aisles=[AisleSpec("01")])
    assert scenario.aisles["01"] is not None  # 巷道主数据在
    assert scenario.stations == {}  # 站台主数据不在

    outcome = station_factor(
        distance_weight=load_station_weights(
            session, warehouse_id=scenario.warehouse_id
        ).get("01")
    )
    assert outcome.is_degraded is True


# --- 有行：取值 + 夹取 ---------------------------------------------------------------------


@pytest.mark.parametrize(("distance_weight", "expected"), [(0.9, 0.90), (0.3, 0.30), (0.0, 0.0)])
def test_the_documented_example_values_pass_through(
    session, distance_weight: float, expected: float
) -> None:
    """`17` §2.2 列注释的两个示例值（0.9 近 / 0.3 远）原样成为因子取值。

    `0.0` 也在这里：**它是合法取值，不是降级**（有行就有数据）。把 0 当成「没有数据」
    会让「站台就在巷道尽头」与「主数据没导出」在库里长得一样。
    """
    scenario = make_scenario(session, aisles=[AisleSpec("01", station_weight=distance_weight)])
    outcome = station_factor(
        distance_weight=load_station_weights(session, warehouse_id=scenario.warehouse_id)["01"]
    )

    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(expected)
    assert outcome.term.note == f"站台距离权重 {distance_weight}"


@pytest.mark.parametrize(("distance_weight", "expected"), [(1.2, 1.0), (40.0, 1.0), (-0.5, 0.0)])
def test_a_weight_outside_the_unit_interval_is_clamped(
    session, distance_weight: float, expected: float
) -> None:
    """列注释**未声明值域上界**（模型也刻意不加 CHECK），而 spec 要求因子归一化到 `[0,1]`，
    故在读取侧夹取 —— 见 `factors.station_factor` 的 docstring 与 `design.md` D14 的
    Open Questions 第 3 条（口径到齐后只改那一个函数）。

    夹取**不降级**：数据在，只是量纲还没定。40 米不是「没有站台」。
    """
    scenario = make_scenario(session, aisles=[AisleSpec("01", station_weight=distance_weight)])
    outcome = station_factor(
        distance_weight=load_station_weights(session, warehouse_id=scenario.warehouse_id)["01"]
    )

    assert outcome.term is not None
    assert outcome.term.value == pytest.approx(expected)
    # 取值说明里两个数都在：操作员要能看出「1.00 是夹出来的」，否则一次米数导出
    # 会看起来像「所有巷道都满分」
    assert str(distance_weight) in outcome.term.note
    assert f"夹取到 {expected:.2f}" in outcome.term.note


# --- 部分缺失：形态本身合法，但它注定让整个因子降级 ------------------------------------------


def test_a_partially_exported_table_yields_one_scored_and_one_degraded(session) -> None:
    """只导出了一条巷的站台行（补导过程中的真实形态）：`01` 有值、`02` 降级。

    这正是「方案级判据」要处理的那个输入 —— 对 `station` 而言它不是「一半的因子」，
    而是**该因子退出本方案**（`design.md` D16 的 `station` 行）。本用例只钉输入形态；
    退出动作在 3.3 的聚合处断言（理由层的最终形态另见 7.1 / 9.3）。
    """
    scenario = make_scenario(
        session, aisles=[AisleSpec("01", station_weight=0.9), AisleSpec("02")]
    )
    weights = load_station_weights(session, warehouse_id=scenario.warehouse_id)

    assert weights == {"01": 0.9}
    assert station_factor(distance_weight=weights.get("01")).term is not None
    assert station_factor(distance_weight=weights.get("02")).is_degraded is True


# --- 取数：按仓库过滤 ----------------------------------------------------------------------


def test_load_reads_only_this_warehouse(session) -> None:
    """主数据行**没有归属快照**，一个仓库一份当前态 —— 故只能按 `warehouse_id` 隔离
    （`17` §11）。不过滤的表现是：A 仓的巷道用上 B 仓的站台权重，而两边的巷道号还一样
    （`01` 对 `01`），现场看不出任何异常。
    """
    ours = make_scenario(session, aisles=[AisleSpec("01", station_weight=0.9)])
    other = make_scenario(
        session,
        warehouse_id="GTJ20000",
        aisles=[AisleSpec("01", cap_total=None, station_weight=0.30)],
        # 另一个仓库只造巷道与站台行：cap / 库存挂在快照上、权重与容量是仓库级配置，
        # 本用例一个都不需要（conftest 自我约束第 3 条）。
        snapshot_time=None,
        weights=None,
        capacity=None,
    )

    assert load_station_weights(session, warehouse_id=ours.warehouse_id) == {"01": 0.9}
    assert load_station_weights(session, warehouse_id=other.warehouse_id) == {"01": 0.3}
    assert DEFAULT_WAREHOUSE_ID == ours.warehouse_id
    # 没有任何站台行的仓库：空字典，不是错误
    assert load_station_weights(session, warehouse_id="GTJ99999") == {}
