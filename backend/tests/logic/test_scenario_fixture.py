"""造数夹具自己的冒烟用例（`tasks.md` 1.3 的验证侧）。

事实来源：`tasks.md` 1.3「夹具自身有冒烟用例（空库能跑、四类输入全空时不抛异常）」、
          9.3（四类输入全空的端到端形态 = 无 `AisleCap` / 无 `InventoryItem` /
          无 `Material.abc_class` / 无 `AisleStation`）、9.5（无快照形态）

**为什么要给夹具写用例。** 夹具是全部后续用例的地基，而它出错的方式恰好是最安静的一种：
少写一行、把 `cap_usable` 算错、把两个规格的字段接反 —— 这些都**不会**让任何一条业务用例
变红，只会让它们断言在一个不存在的场景上。所以这里的四条用例只问两件事：
「该建的都建了吗」「不该建的都没建吗」。

夹具本身在 `tests/logic/conftest.py`；本文件只驱动它，不定义任何模型约定。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.enums import AbcClass, JobStatus
from app.models.linkage import AisleCap, InventoryItem
from app.models.master_data import AisleStation
from tests.logic.conftest import (
    DEFAULT_SNAPSHOT_TIME,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
    make_scenario,
)

pytestmark = pytest.mark.logic

#: 9.3 说的「四类输入」逐类落在哪张表上 —— 夹具的冒烟判据。
#: 「ABC 分类」不是表而是 `Material.abc_class` 列，故单列一条断言，不混进来充数。
_FOUR_INPUT_TABLES = (AisleCap, InventoryItem, AisleStation)


def _row_count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_empty_scenario_runs_on_an_empty_database(session) -> None:
    """空库 + 全默认参数：不抛异常，且四类输入一张行都不建。"""
    scenario = make_scenario(session)

    for model in _FOUR_INPUT_TABLES:
        assert _row_count(session, model) == 0, model.__tablename__
    assert scenario.aisles == {}
    assert scenario.materials == {}
    assert scenario.inventory == ()
    assert scenario.job_orders == ()
    assert scenario.stations == {}

    # 默认建的只有「基准」三样：快照、一版权重、一版容量配置 —— 它们不是被测的四类输入，
    # 而是让引擎能跑起来的最小前提（权重缺失是整批阻断，D3）。
    assert scenario.snapshot is not None
    assert len(scenario.weight_configs) == 1
    assert scenario.capacity_config is not None


def test_four_kinds_of_input_all_empty_does_not_raise(session) -> None:
    """9.3 的形态：有单、有料号，但 cap / 库存 / ABC / 站台**四类输入全空**。

    这是阶段三唯一能跑通的真实形态（`16` §394），所以它不是边界用例而是主用例 ——
    夹具必须能把它造出来，且造得毫不特殊：什么都不传即可。
    """
    scenario = make_scenario(
        session,
        materials=[MaterialSpec("3001234", abc_class=None)],
        job_orders=[JobOrderSpec(order_no="PO-20260908001", material_code="3001234", qty=12)],
    )

    for model in _FOUR_INPUT_TABLES:
        assert _row_count(session, model) == 0, model.__tablename__

    # 「ABC 分类为空」是列级事实，不是表级事实 —— 料号行在，分类为空。
    assert scenario.materials["3001234"].abc_class is None
    # 单子本身照建：队列不因输入缺失而消失，缺的是评分依据（因子级降级 + 四级走尽）。
    assert len(scenario.job_orders) == 1
    assert scenario.job_orders[0].status == JobStatus.PENDING


def test_parameterized_scenario_writes_every_row_it_was_given(session) -> None:
    """反「夹具是个空壳」：逐类参数化一遍，断言行、字段与外键都真的落了。"""
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                "01",
                cap_total=80,
                cap_reserved=32,
                is_near_station=True,
                station_weight=0.9,
            ),
            AisleSpec("21", cap_total=200, is_near_station=False),
        ],
        materials=[MaterialSpec("3001234", abc_class=AbcClass.A, material_name="冰红茶 500ml×15")],
        inventory=[
            InventorySpec("010104", "3001234", "GJP2571221", qty=6),
            InventorySpec("010208", "3001234", "GJP2571221", qty=4),
        ],
        job_orders=[
            JobOrderSpec(order_no="PO-20260908001", material_code="3001234", qty=12, abc_class="A")
        ],
        weights=[{"abc": 0.25, "cap": 0.2, "existing": 0.15, "station": 0.2, "batch": 0.1, "continuity": 0.1},
                 {"abc": 0.30, "cap": 0.20, "existing": 0.15, "station": 0.15, "batch": 0.10, "continuity": 0.10}],
        capacity={"near_station_reserved_ratio": 0.5},
    )

    # 巷道：主数据 + cap 行按规格成对出现；cap_usable 按 17 §3.4 的口径算。
    assert set(scenario.aisles) == {"01", "21"}
    assert scenario.aisle_caps["01"].cap_usable == 48
    assert scenario.aisle_caps["21"].cap_usable == 200
    assert scenario.aisle_caps["01"].is_near_station is True

    # 站台主数据只给 `01` 建（`21` 没给 station_weight）—— 这正是 2.2 要的
    # 「有站台主数据的巷道参与评分、没有的走因子级降级」的形态。
    assert set(scenario.stations) == {"01"}
    assert scenario.stations["01"].distance_weight == 0.9

    # 库存行挂在快照上（外键），库位号按文本原样存 —— 前导 0 不得丢。
    assert scenario.snapshot is not None
    assert {row.location_code for row in scenario.inventory} == {"010104", "010208"}
    assert all(row.snapshot_id == scenario.snapshot.id for row in scenario.inventory)
    assert all(row.snapshot_time == DEFAULT_SNAPSHOT_TIME for row in scenario.inventory)

    assert scenario.materials["3001234"].abc_class is AbcClass.A
    assert scenario.job_orders[0].abc_class is AbcClass.A
    assert scenario.job_orders[0].line_no == "10"

    # 两版权重：版本号从 1 递增，旧版仍在（2.3 的「两版并存取新版且旧版仍可回滚」）。
    assert [row.version_no for row in scenario.weight_configs] == [1, 2]
    assert [row.weight_abc for row in scenario.weight_configs] == [0.25, 0.30]

    assert scenario.capacity_config is not None
    assert scenario.capacity_config.near_station_reserved_ratio == 0.5
    # 没覆盖的字段取模型默认值（16 §353~356），不是被夹具抹成 0。
    assert scenario.capacity_config.concentration_n == 5


def test_cap_rows_without_a_snapshot_are_rejected_loudly(session) -> None:
    """自相矛盾的场景要在造数时就说清楚，而不是让用例撞一条 NOT NULL。

    `AisleCap` / `InventoryItem` 的 `snapshot_id` 是必填外键，故「无快照 + 有 cap 行」
    不是一种可以造出来的形态。9.5 的「无快照」因此必然是「连 cap 行也没有」——
    这条用例把这个推论钉住，免得后来者以为夹具漏了支持。
    """
    with pytest.raises(ValueError, match="snapshot_time"):
        make_scenario(session, snapshot_time=None, aisles=[AisleSpec("01", cap_total=80)])

    with pytest.raises(ValueError, match="snapshot_time"):
        make_scenario(
            session,
            snapshot_time=None,
            inventory=[InventorySpec("010104", "3001234", "GJP2571221", qty=6)],
        )

    # 无快照 + 只有巷道主数据（不给 cap_total）是合法的：这正是「巷道在、cap 基线不在」。
    scenario = make_scenario(session, snapshot_time=None, aisles=[AisleSpec("01", cap_total=None)])
    assert scenario.snapshot is None
    assert scenario.aisle_caps == {}
    assert set(scenario.aisles) == {"01"}
