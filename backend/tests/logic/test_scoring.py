"""`scoring.py` 的用例：本文件当前覆盖**权重取数**（2.3）与**可行巷道集**（3.2）。

事实来源：`17` §七（配置实体：版本号 + 生效时间 + 变更人，「历史版本保留可回滚」）、
          §10.1（六项权重的示例值 = `factors`）
          `14` §四（6 因子集合固定不得增删）、§3.4 步 1（可行巷道集的三个判据）、
          §3.3 / §3.5（近站台预留池与超时释放）
          `20` §六「1. 评分正确性」里落在这一层的四条：`SC-001`（近站台巷得分最高并入选）、
          `SC-002`（`W_ABC=0` 时该因子贡献为 0、其余因子仍生效）、`SC-004`（cap 已满的巷道
          不进入候选集）、`SC-006`（与 `14` 的 6 因子公式逐项一致）
          `openspec/changes/recommendation-engine/design.md` D1（版本语义三分）、
          D4（配置缺席的处置 + 容量扣减不落库）、D5（降级链分档）、D7（三个判据的取数源）、
          D17（`now` 是现场墙上时间）
          `app/core/config_version.py` 的 docstring（「取哪一版」的唯一口径）
          `tasks.md` 2.3 / 3.2 / 3.3 / 10.1（本用例的任务书；10.1 的交付形态见下）

3.3（综合评分）的用例按任务书也落在本文件，届时**续写**而非另开文件 —— 它与前两段共享
「本单 × 候选巷道」的上下文，断言也常落在同一条造数上（`SC-006` 的「与 `14` 的公式逐项
一致」就要权重与因子取值一起读）。

## 10.1 的场景编号怎么落（`SC-001~006`）

10.1 要的是「**每条一个用例**、注释注明场景编号与 `20` 的原表预期」，**不是**一个
`test_scenarios.py` 的大杂烩：场景编号在这套测试里是**追溯标签**，用例仍按「哪一层的行为」
归位，否则同一条事实会在两个文件里各写一遍（那正是本文件开头「不重复实现」那条所拒的）。
落法是：

| 场景 | 用例在哪 |
|---|---|
| `SC-001` / `SC-002` / `SC-004` / `SC-006` | 本文件（评分与候选集的算术） |
| `SC-003` / `SC-005` | `test_factors.py`（因子取值那一层） |
| `SC-005` 的「**因而选道不同**」那半 | `test_allocator.py`（跨「因子值 → 总分 → 档内比较 → 落位」四处口径） |

后者不能落本文件：本文件的 `score_aisles` 收的是**手写的分解**，在这儿断言「有既有库存的
巷道得分更高」只是把刚填进去的数按恒等式重算一遍 —— 与「库里真有那批库存」无关，是一句
恒真的同义反复。而 `test_factors.py` 只有因子值、没有选道。两处都够不着，故那条只能在
有真实取数的链路上验。

## 这里守的是「复用」，不是「重新实现」

`pick_current_version` 的两条语义（编号最大者胜、未到生效时间不参与）在
`tests/logic/test_config_version.py` 里已经各有用例。本文件**不重复**那些断言，
只断「引擎确实走的是那套口径」：造两版权重，按 D1 的规则看引擎取哪一版。

同理，`available_cap` 的 ABC 三分支与释放判定已在 `tests/logic/test_reserved.py` 里各
有用例；**可行巷道集**这一段只断「那三支与判据 1 合起来之后，候选集是什么样」。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
import sqlalchemy as sa

from app.core.enums import AbcClass
from app.core.errors import BlockedMissingPrerequisite, DomainError
from app.models.configuration import WEIGHT_FACTORS, WeightConfig
from app.schemas.reason import FactorTerm
from app.engine.scoring import (
    AisleState,
    feasible_aisles,
    load_aisle_state,
    load_weights,
    score_aisles,
    select_best,
    station_degrade_reason,
)
from tests.logic.conftest import (
    DEFAULT_CONFIG_EFFECTIVE_AT,
    DEFAULT_WEIGHTS,
    AisleSpec,
    make_scenario,
)

pytestmark = pytest.mark.logic

#: 分配时刻：晚于 `DEFAULT_CONFIG_EFFECTIVE_AT`（2020-01-01），故默认造数下必有生效版本。
#: 09:00 也**早于**默认的释放钟点 18:00，故非 A 类默认走 `cap_usable`（`16` §353~356）。
_NOW = datetime(2026, 9, 11, 9, 0)


def _weights(**overrides: float) -> dict[str, float]:
    """在 `17` §10.1 的示例权重上改一两项 —— 改过的那个因子就是「这一版改了什么」。"""
    return {**DEFAULT_WEIGHTS, **overrides}


# --- 取当前版本 ---------------------------------------------------------------------------


def test_load_returns_the_six_weights_of_the_highest_effective_version(session) -> None:
    """两版并存 ⇒ 取编号大的那版，键序按 `WEIGHT_FACTORS`（`17` §10.1 的 `factors`）。

    键序不是装饰：`design.md` 的红线落地第 1 条要求一切遍历序显式给出、不得依赖 DB
    返回序 —— 而 `factors` 是逐项写进推荐理由、逐项渲染到理由卡上的。
    """
    scenario = make_scenario(
        session, weights=[_weights(), _weights(abc=0.40, cap=0.05)]
    )

    weights = load_weights(session, warehouse_id=scenario.warehouse_id, now=_NOW)

    assert weights == _weights(abc=0.40, cap=0.05)
    assert list(weights) == list(WEIGHT_FACTORS)


def test_the_largest_version_no_wins_over_the_latest_effective_at(session) -> None:
    """D1 的**补录**形态：新一版编号更大，而生效时间更早（「其实上个月就该用」的那份口径）。

    取 `version_no` 最大者而不是 `effective_at` 最新者，是 `config_version` 的口径；
    这条用例断的是**引擎确实复用了它** —— 若引擎自己写查询、按生效时间取最新，这里
    会拿到 `0.25`（v1），而现场拿到的是一个用户没选过的版本。

    它同时就是「旧版仍可回滚」在引擎这一侧的形态：回滚不是把 v1 改回当前，而是**补录
    一版编号更大的旧口径**（`17` §七 全量版本保留）—— 于是旧口径重新成为「当前生效」。
    """
    scenario = make_scenario(
        session, weights=[_weights(abc=0.25), _weights(abc=0.40)]
    )
    # 把后补的那版挪到更早生效：造数默认给所有版本同一个生效时间，而「编号与生效时间
    # 不同调」正是这一条的题眼，夹具刻意不替用例编这个值。
    scenario.weight_configs[1].effective_at = DEFAULT_CONFIG_EFFECTIVE_AT - timedelta(days=30)
    session.flush()
    # 这条前提要自己看住：改写若没落库，两版就同生效时间，断言照样过 —— 用例会静默
    # 退化成「上面那条」，题眼消失而没有任何信号。按列查询读的是库里的值。
    assert session.scalar(
        sa.select(WeightConfig.effective_at).where(WeightConfig.version_no == 2)
    ) == DEFAULT_CONFIG_EFFECTIVE_AT - timedelta(days=30)

    weights = load_weights(session, warehouse_id=scenario.warehouse_id, now=_NOW)

    assert weights["abc"] == 0.40


def test_a_version_scheduled_for_the_future_is_not_in_effect_yet(session) -> None:
    """**可预约生效**（`17` §七）：时间未到即不参与选取，于是引擎取回上一版。

    这是与「权重缺席就阻断」配对的一半：预约期内**有**生效版本，正常运行；预约期
    之后若无任何一版生效，才轮到下面的阻断。
    """
    scenario = make_scenario(
        session, weights=[_weights(abc=0.25), _weights(abc=0.40)]
    )
    scenario.weight_configs[1].effective_at = _NOW + timedelta(days=1)
    session.flush()

    weights = load_weights(session, warehouse_id=scenario.warehouse_id, now=_NOW)

    assert weights["abc"] == 0.25


# --- 无生效版本 ⇒ 阻断 ---------------------------------------------------------------------


def test_no_weight_row_at_all_blocks_with_a_reason_and_a_detail(session) -> None:
    """一版都没配 ⇒ 阻断（`design.md` D4 的「阻断本次评分，提示配置未就绪，不产出方案」）。

    断的是**异常**而不是「返回一套默认权重」：权重缺席时取默认会产出一个看起来正常、
    但没人批准过的排序（见 `scoring.py` 的模块 docstring）。用 `detail` 带上诊断信息
    —— 端点（8.x）由它渲染 409 的响应体，操作员据此知道去哪一版配置上补。
    """
    scenario = make_scenario(session, weights=None)

    with pytest.raises(BlockedMissingPrerequisite) as excinfo:
        load_weights(session, warehouse_id=scenario.warehouse_id, now=_NOW)

    assert isinstance(excinfo.value, DomainError)
    assert excinfo.value.http_status == 409
    assert excinfo.value.detail["weight_config_versions"] == 0
    assert excinfo.value.detail["warehouse_id"] == scenario.warehouse_id
    assert "一版都没有" in excinfo.value.message


def test_rows_that_are_not_in_effect_yet_block_with_a_different_message(session) -> None:
    """配了、但一版都没到生效时间 ⇒ 同样是阻断，**成因分开说**。

    两种成因指向的操作不同（去配一版 / 确认生效时间），措辞混成一句会让操作员按错的
    方向排查。区分的依据就是配置行数 —— 不必为此再查一次库。
    """
    scenario = make_scenario(session, weights=_weights())

    with pytest.raises(BlockedMissingPrerequisite) as excinfo:
        load_weights(
            session,
            warehouse_id=scenario.warehouse_id,
            now=DEFAULT_CONFIG_EFFECTIVE_AT - timedelta(days=1),
        )

    assert excinfo.value.detail["weight_config_versions"] == 1
    assert "无一版生效" in excinfo.value.message


# --- 数据隔离 ------------------------------------------------------------------------------


def test_load_reads_only_this_warehouse(session) -> None:
    """两台设备各有自己的一版权重（`17` §十一）。读串了不会报错，只会让 A 仓按 B 仓的
    口径排谁先挑黄金库位 —— 而两边的版本号都是 1，现场看不出任何异常。"""
    ours = make_scenario(session, weights=_weights(abc=0.25))
    other = make_scenario(
        session,
        warehouse_id="GTJ20000",
        weights=_weights(abc=0.10),
        # 只造权重（仓库级配置）：另一个仓库不需要快照、巷道、容量配置
        snapshot_time=None,
        capacity=None,
    )

    assert load_weights(session, warehouse_id=ours.warehouse_id, now=_NOW)["abc"] == 0.25
    assert load_weights(session, warehouse_id=other.warehouse_id, now=_NOW)["abc"] == 0.10
    assert len(session.scalars(sa.select(WeightConfig)).all()) == 2


def test_weight_config_rows_are_the_ones_the_loader_reads(session) -> None:
    """取数读的就是 `WeightConfig`：把「取数源」钉在模型上，防止将来换成别的行类型
    而用例仍绿。**读操作不改库**（`design.md` 第 3 条要求 `now` 显式注入，正是为了
    让「读」保持是读）—— 读完之后行数与列值都原样。"""
    scenario = make_scenario(session, weights=_weights())
    row = scenario.weight_configs[0]
    before = row.version_no, row.weight_abc

    load_weights(session, warehouse_id=scenario.warehouse_id, now=_NOW)

    assert isinstance(row, WeightConfig)
    assert (row.version_no, row.weight_abc) == before
    assert session.scalars(sa.select(sa.func.count()).select_from(WeightConfig)).one() == 1


# --- 可行巷道集（3.2；D7 的三个判据） --------------------------------------------------------


def _state(session, scenario) -> AisleState:
    """把造数读成一次批量分配的**巷道侧输入** —— 走的是分配器那条真实取数路径。"""
    return load_aisle_state(
        session,
        warehouse_id=scenario.warehouse_id,
        snapshot_id=scenario.snapshot.id,
    )


def _feasible(
    session,
    scenario,
    *,
    abc_class: AbcClass | None,
    order_cells: int,
    now: datetime = _NOW,
    consumed: Mapping[str, int] | None = None,
) -> list[str]:
    """一次 `feasible_aisles` 调用的短写法。释放钟点从**配置行**取（`16` §353~356）。"""
    return feasible_aisles(
        _state(session, scenario),
        abc_class=abc_class,
        order_cells=order_cells,
        release_at=scenario.capacity_config.reserved_release_at,
        now=now,
        consumed=consumed,
    )


def test_sc_004_a_full_aisle_drops_out_of_the_feasible_set(session) -> None:
    """`SC-004`：cap 已满的巷道不进候选集；**刚好装得下**的那条必须留下。

    判据 1 的比较是**闭区间**（`可用 >= 本单格数`）。差一格的代价不是「少一条巷道」，
    而是「刚好装得下的一单被推到远巷道」，而理由卡上会显示成「近站台 cap 不足」——
    一个写成开区间的判据，从输出上看不出与「真的不足」的区别。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec("01", cap_total=0),  # 已满
            AisleSpec("02", cap_total=10),  # 刚好 10 格
            AisleSpec("03", cap_total=80),
        ],
    )

    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=10) == ["02", "03"]
    # 同一份造数，只把单子放大一格 ⇒ 那条「刚好」的巷掉出候选集（判据随本单而定）
    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=11) == ["03"]


def test_the_traversal_order_is_aisle_no_ascending_not_the_input_order(session) -> None:
    """输出序 = `aisle_no` 升序，**与入参的键序无关**（`design.md` 红线落地第 1 条）。

    候选集的次序会逐项落进 `plans` 的报文（`17` §10.1），故它必须**定死**：`caps` 这个
    映射在调用侧怎么排，都不该动到推荐次序。

    这里把键序打乱到与升序**完全相反**再传进去，而**不**依赖「DB 恰好按插入序返回」：
    实测那条 `caps` 查询走的是唯一键 `(warehouse_id, snapshot_id, aisle_no)` 的索引，
    **返回序已经是升序** —— 也就是说「靠 DB 返回序」在当前的查询计划下看起来是对的，
    换个计划才露馅。这正是必须显式排序的理由，也是本用例把 DB 摘出去的原因（用例的前提
    若依赖查询计划，它守的东西会随一次建索引而失效，而它自己不会报错）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec("01", cap_total=80),
            AisleSpec("03", cap_total=80),
            AisleSpec("21", cap_total=80),
        ],
    )
    state = _state(session, scenario)
    # 与升序完全相反：任何「继承入参序」的实现都会给出 ["21", "03", "01"]
    scrambled = {aisle_no: state.caps[aisle_no] for aisle_no in ["21", "03", "01"]}

    assert list(scrambled) == ["21", "03", "01"]  # 前提：键序确实不是升序

    assert feasible_aisles(
        AisleState(master=state.master, caps=scrambled),
        abc_class=AbcClass.B,
        order_cells=1,
        release_at=scenario.capacity_config.reserved_release_at,
        now=_NOW,
    ) == ["01", "03", "21"]


def test_an_aisle_cap_row_without_aisle_master_data_is_not_a_candidate(session) -> None:
    """判据「巷道可用」的前半：`Aisle` 主数据里没有这个 `aisle_no` ⇒ 不是候选。

    `AisleCap` **刻意不建到 `aisles` 的外键**（`linkage.py` 模块文档第 3 条），所以这种
    行在库里是可能存在的：巷道主数据被重导或错导时，cap 基线仍留着旧巷道。放它进来的
    后果是「已被撤销的巷道继续吃到推荐」，而它的 cap 三列看起来完全正常。

    故这里删的是**主数据行**，cap 行原样留着。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec("01", cap_total=80), AisleSpec("02", cap_total=80)],
    )
    session.delete(scenario.aisles["02"])
    session.flush()

    assert "02" in scenario.aisle_caps  # 前提：cap 行还在，掉出候选集的原因是主数据没了
    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=10) == ["01"]


def test_an_aisle_with_master_data_but_no_cap_row_is_not_a_candidate(session) -> None:
    """判据「巷道可用」的后半：主数据在、但**该快照没有对应的 `AisleCap` 行** ⇒ 不是候选。

    造数上这是 `cap_total=None`，与 `cap_total=0` **不是一回事**：后者是「有这一行、容量
    为零」（`SC-004` 用的就是它），前者是「这一版快照里根本没有这条巷」。成因是新巷道还
    没进过快照、或这一版只导了部分巷道 —— 两种情况下它都没有一个**编得出来**的容量数字。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec("01", cap_total=80), AisleSpec("02", cap_total=None)],
    )

    assert "02" in scenario.aisles and "02" not in scenario.aisle_caps  # 前提：两半都在
    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=10) == ["01"]


def test_no_cap_rows_at_all_yields_an_empty_set_not_an_error(session) -> None:
    """一条 `AisleCap` 行都没有（`tasks.md` 9.3 的形态）⇒ **空集**，不是异常。

    「可行集为空」是一条**结果**：每张单走成「四级走尽 → 分配失败」，停在 `PENDING` 等
    人工介入（D5 / `15` §11.1）。若在这里抛异常，整批会 500，而 9.3 要的形态是「没有种子
    数据时行为确定、逐单失败」。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec("01", cap_total=None), AisleSpec("02", cap_total=None)],
    )

    assert _feasible(session, scenario, abc_class=AbcClass.A, order_cells=1) == []


def test_a_non_a_order_cannot_reach_the_reserved_pool_of_a_near_station_aisle(session) -> None:
    """判据 3（非 A 类排除预留池）在**候选集**上的可见形态（`14` §3.3 / §3.5、`AC-001`）。

    近站台 `01`：80 格总量、其中 20 格是 A 类的预留池（`cap_usable` = 60）；远巷道 `02`：
    80 格、无预留。一张 70 格的 B 类单在 09:00 **只能**走 `02`；同一张单在 18:00 之后
    （预留池已释放给 B/C）两条巷道都可行。A 类单任何时候都看得到 `01` —— 预留池本就是它的。

    这是判据 1 与判据 3 **叠在一起**才有的结论：任一处的口径写错，这条都会变红。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec("01", cap_total=80, cap_reserved=20, is_near_station=True),
            AisleSpec("02", cap_total=80, is_near_station=False),
        ],
    )

    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=70) == ["02"]
    assert _feasible(session, scenario, abc_class=AbcClass.A, order_cells=70) == ["01", "02"]
    assert _feasible(
        session,
        scenario,
        abc_class=AbcClass.B,
        order_cells=70,
        now=_NOW.replace(hour=19),
    ) == ["01", "02"]


def test_the_batch_consumption_is_subtracted_from_the_available_cap(session) -> None:
    """批次内已占用的量（D4 的「进程内快照副本」）缩小该巷的可用量 ⇒ 后一张单排不进去。

    单测层面这是「同一份造数、同一时刻，多传一个计数就换候选集」；在分配器里它正是
    「上一张单吃掉的容量对下一张单生效」。边界同样是**闭区间**：恰好减到本单格数仍算装得下。
    """
    scenario = make_scenario(
        session, aisles=[AisleSpec("01", cap_total=80, is_near_station=False)]
    )

    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=30) == ["01"]
    assert (
        _feasible(session, scenario, abc_class=AbcClass.B, order_cells=30, consumed={"01": 50})
        == ["01"]  # 80 − 50 = 30，刚好装得下
    )
    assert (
        _feasible(session, scenario, abc_class=AbcClass.B, order_cells=30, consumed={"01": 51})
        == []
    )
    # 没提到的巷道按 0 算：不该因为别处有占用就波动
    assert (
        _feasible(session, scenario, abc_class=AbcClass.B, order_cells=30, consumed={"02": 50})
        == ["01"]
    )


def test_consumption_is_not_re_split_into_the_two_pools(session) -> None:
    """扣减量从**整条巷道的可用量**里减，不拆「先吃预留池还是先吃可用部分」——
    本阶段**没有**那个口径（`14` §3.4 步 3 只写「扣减该巷道容量」），故取**保守**读法
    （`design.md` D4 的补记）。

    近站台 `01`：80 总量 / 20 预留（`cap_usable` = 60）。A 类单吃掉 30 之后，B 类看到的是
    `60 − 30 = 30`，于是 31 格的 B 类单排不进去。若 A 类实际是**从预留池先吃**的（20 格
    预留用尽、可用部分只少 10），B 类真实剩下的 50 格比这里给的更多 —— 也就是本读法
    **偏保守**：少给是安全的（爆款的预留池没被误派），多给则破红线，而预留池的占用
    没有独立台账、破了不可追溯（`test_reserved.py` 已就此论证同一取舍）。

    这条用例钉住的是「**不做**那件事」：池子的分配规则属分配器的扣减侧（4.x / 6.x），
    口径到齐后改那里，不在这里补一个池子规则。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec("01", cap_total=80, cap_reserved=20, is_near_station=True)],
    )

    assert _feasible(session, scenario, abc_class=AbcClass.B, order_cells=31) == ["01"]
    # 同一份占用对 B 类生效：60 − 30 = 30 < 31
    assert (
        _feasible(session, scenario, abc_class=AbcClass.B, order_cells=31, consumed={"01": 30})
        == []
    )
    # 而 A 类自己仍排得进（它看的是总额 80 − 30 = 50 ≥ 31）
    assert (
        _feasible(session, scenario, abc_class=AbcClass.A, order_cells=31, consumed={"01": 30})
        == ["01"]
    )


@pytest.mark.parametrize("order_cells", [0, -1])
def test_a_non_positive_order_size_is_rejected(session, order_cells: int) -> None:
    """本单占用格数 ≤ 0 ⇒ `ValueError`（同一判据已在 `existing_factor` 上用例化）。

    静默放行的后果是**反向**的：`可用 >= 0` 恒真 ⇒ 只要有一条 cap 行，全部巷道都进候选集，
    一张 0 格的单会拿到一份「近站台最优」的推荐。`16` §171 把「数量 > 0」列为导入期必须
    阻断的口径异常，所以这种单不该走到评分这一步。
    """
    scenario = make_scenario(session, aisles=[AisleSpec("01", cap_total=80)])

    with pytest.raises(ValueError, match="占用格数必须 > 0"):
        _feasible(session, scenario, abc_class=AbcClass.A, order_cells=order_cells)


def test_load_reads_only_this_warehouse_and_this_snapshot(session) -> None:
    """取数两处过滤：`master` 只含**本仓**的巷道主数据；`caps` 只含**该快照**的 cap 行。

    两条过滤各有各的失效形态，且都不报错：少了仓库过滤 ⇒ 隔壁厂的巷道进了候选集（`17`
    §11 数据隔离）；`snapshot_id` 传错（例如拿了上一版的 id）⇒ 候选集变成上一版的巷道。
    两种都是「一份看起来正常的推荐，依据却是别人的 / 过期的基线」。

    **注意另一个仓库要自己造一份快照**：`AisleCap` 挂在快照上，而快照的唯一键是
    `(warehouse_id, version_no)` —— 两个仓库可以各有 `version_no = 1`（`17` §3.2）。
    仓库级与快照级的分层见 `conftest.py` 的自我约束第 3 条。
    """
    ours = make_scenario(session, aisles=[AisleSpec("01", cap_total=80)])
    other = make_scenario(
        session,
        warehouse_id="GTJ20000",
        aisles=[AisleSpec("07", cap_total=80)],
        materials=(),
        weights=None,
        capacity=None,
    )

    state = _state(session, ours)

    assert other.snapshot is not None and other.snapshot.id != ours.snapshot.id
    assert state.master == frozenset({"01"})
    assert list(state.caps) == ["01"]


# --- 综合评分（3.3；`17` §10.1 的加权与可追溯恒等式） ------------------------------------------


def _factor_terms(**values: float) -> dict[str, FactorTerm]:
    """按因子名拼一层的 `breakdown` 叶节点。

    取值说明在加权里不参与，给个非空串即可 —— 但**不给空串**：`FactorTerm.note` 的契约是
    `min_length=1`（「取值说明」是理由卡的正文，不是可选项），造数若给空串，将来有人把
    契约收紧时这条用例会以「造数不合契约」的形态变红，指错方向。
    """
    return {
        factor: FactorTerm(value=value, note=f"{factor} 的取值说明")
        for factor, value in values.items()
    }


#: `17` §10.1 算例里的三个巷道分解（`station` 降级 ⇒ 每巷五项）。逐字取文档的 value，
#: 故下面 `SC-006` 那条用例的期望值也是文档的（0.79 / 0.58 / 0.43）—— 三方对照。
_DOC_BREAKDOWN: dict[str, dict[str, FactorTerm]] = {
    "01": _factor_terms(abc=1.00, cap=0.72, existing=0.60, batch=1.00, continuity=0.50),
    "02": _factor_terms(abc=1.00, cap=0.42, existing=0.20, batch=0.00, continuity=1.00),
    "21": _factor_terms(abc=0.67, cap=0.90, existing=0.00, batch=0.00, continuity=0.00),
}


def _recompute_identity(
    weights: Mapping[str, float],
    terms: Mapping[str, FactorTerm],
    available: Mapping[str, float],
) -> Decimal:
    """按 `17` §10.1 的恒等式**独立复算**一次：`Σ wᵢvᵢ ÷ Σ wᵢ`，全程十进制。

    用 `Decimal` 而不是 `float` 复算，是为了让这条断言真的独立：两边都用二进制浮点求和，
    末尾几位一致就成了同义反复 —— 而恒等式的意义正是「理由里的分数就是这些项算出来的」。
    """
    numerator = sum(
        Decimal(str(weights[factor])) * Decimal(str(terms[factor].value))
        for factor in available
    )
    denominator = sum(Decimal(str(weights[factor])) for factor in available)
    return (numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def test_sc_006_the_documented_example_reproduces_every_score() -> None:
    """`SC-006`：`17` §10.1 算例的三个总分逐项复现（0.79 / 0.58 / 0.43）。

    这条用例同时是**恒等式的复算**与**文档对照**：分解取文档的 value、权重取文档的示例、
    期望值取文档的 `scores`，三者都是文档给的（`station` 降级 ⇒ 分母 0.80）。任何一处口径
    写错（分母没重新归一化、权重读错、量化方式不同）都会在这里变红。
    """
    available = [f for f in WEIGHT_FACTORS if f != "station"]

    scores = score_aisles(
        weights=DEFAULT_WEIGHTS, breakdown=_DOC_BREAKDOWN, degraded={"station"}
    )

    assert scores == {"01": 0.79, "02": 0.58, "21": 0.43}
    for aisle, terms in _DOC_BREAKDOWN.items():
        assert Decimal(str(scores[aisle])) == _recompute_identity(
            DEFAULT_WEIGHTS, terms, available
        )


def test_the_traceability_identity_holds_for_every_aisle() -> None:
    """恒等式对**每一条**候选巷成立，且六因子齐全时分母为 1.0（`17` §10.1）。

    造一组各不相同、且**都不落在半分位上**的取值（文档点名要求），故期望值对量化方式
    不敏感 —— 量化本身另有专门的用例（`test_scores_are_decimal_half_up…`）。
    """
    weights = _weights(abc=0.30, cap=0.20, existing=0.20, station=0.10, batch=0.10, continuity=0.10)
    # 键序故意打乱（21 / 01 / 02）：下面那条键序断言才有内容 —— 否则「输出是升序」只是因为
    # 输入恰好是升序，实现里排没排都看不出来
    breakdown = {
        "21": _factor_terms(
            abc=0.33, cap=0.87, existing=0.05, station=0.55, batch=0.44, continuity=0.06
        ),
        "01": _factor_terms(
            abc=1.00, cap=0.63, existing=0.41, station=0.90, batch=0.17, continuity=0.29
        ),
        "02": _factor_terms(
            abc=0.67, cap=0.22, existing=0.13, station=0.30, batch=0.02, continuity=0.81
        ),
    }

    scores = score_aisles(weights=weights, breakdown=breakdown)

    assert list(scores) == ["01", "02", "21"]  # 键序 = aisle_no 升序（显式给出）
    for aisle, terms in breakdown.items():
        assert Decimal(str(scores[aisle])) == _recompute_identity(
            weights, terms, WEIGHT_FACTORS
        )


def test_sc_001_the_near_station_aisle_scores_highest_and_is_selected() -> None:
    """`SC-001`：近站台巷道得分最高并**入选**（`14` §3.4 步 2 / `17` §10.1 的 `aisles`）。

    两种形态各断一次：`station` 参与（六因子）与 `station` 降级（首期实际形态，
    `16` §394）—— A 类爆款优先占近站台这条结论不该因为一个因子缺席而翻过来。
    """
    full = {
        "01": _factor_terms(
            abc=1.00, cap=0.72, existing=0.60, station=0.90, batch=1.00, continuity=0.50
        ),
        "02": _factor_terms(
            abc=0.67, cap=0.42, existing=0.20, station=0.30, batch=0.00, continuity=1.00
        ),
    }
    degraded = {
        aisle: {f: term for f, term in terms.items() if f != "station"}
        for aisle, terms in full.items()
    }

    assert select_best(score_aisles(weights=DEFAULT_WEIGHTS, breakdown=full)) == ["01"]
    assert select_best(
        score_aisles(weights=DEFAULT_WEIGHTS, breakdown=degraded, degraded={"station"})
    ) == ["01"]


def test_sc_002_a_zero_weight_factor_contributes_nothing() -> None:
    """`SC-002`：权重为 0 的因子对总分**没有贡献**，**其余因子仍生效**（`14` §3.4 的加权和）。

    `20` §六「1. 评分正确性」的预期列是两半，缺一不可，故下面各断一条：

    | 断言 | 预期列里的哪半 |
    |---|---|
    | 只在 ABC 上不同的两条分解（1.00 / 0.00）得到**同一总分** | 「`W_ABC=0` ⇒ 该因子贡献 = 0」 |
    | 改动一个**权重非 0** 的因子（`existing`）⇒ 总分**随之改变** | 「其余因子仍生效」 |

    只断前一半是不够的：一个把六项取值全丢掉的实现（恒返回 0.00）也让前一半成立，而它在
    现场的表现是理由卡上六项全空、所有巷道同分、选道退化成「按 `aisle_no` 升序」。后一半
    就是冲这种实现来的 —— 它同样是 `SC-006` 与「档内选最高分」那类用例的前提。

    权重取 `W_ABC=0` 而非别的因子：预期列的输入列写的就是 `W_ABC`。这条守的是「因子取值
    进没进加权和」，不是「因子函数算得对不对」（后者在 `test_factors.py`）。
    """
    weights = _weights(abc=0.0)
    high_abc = {
        "01": _factor_terms(
            abc=1.00, cap=0.72, existing=0.60, station=0.90, batch=1.00, continuity=0.50
        )
    }
    low_abc = {
        "01": _factor_terms(
            abc=0.00, cap=0.72, existing=0.60, station=0.90, batch=1.00, continuity=0.50
        )
    }
    # 第三条只改 `existing`（`DEFAULT_WEIGHTS` 里它的权重非 0），其余五项与 `high_abc` 逐字相同。
    other_factor = {
        "01": _factor_terms(
            abc=1.00, cap=0.72, existing=0.20, station=0.90, batch=1.00, continuity=0.50
        )
    }

    nonzero = score_aisles(weights=weights, breakdown=high_abc)
    assert nonzero == score_aisles(weights=weights, breakdown=low_abc)  # 该因子贡献 = 0
    assert score_aisles(weights=weights, breakdown=other_factor) != nonzero  # 其余因子仍生效


def test_the_denominator_renormalizes_over_the_available_factors() -> None:
    """降级因子的权重**不进分母**：可用因子全取 1.00 时满分仍是 1.00（`17` §10.1）。

    若分母用全部六项权重，两个因子降级后满分只能到 0.70 —— 而「满分 1.0、跨轮次可比」
    正是 `17` §10.1 要的性质：一次降级不该让所有巷道看起来都「不够好」。
    """
    terms = _factor_terms(abc=1.00, cap=1.00, existing=1.00, continuity=1.00)  # station / batch 降级

    assert score_aisles(
        weights=DEFAULT_WEIGHTS, breakdown={"01": terms}, degraded={"station", "batch"}
    ) == {"01": 1.00}


@pytest.mark.parametrize(
    ("terms", "degraded"),
    [
        # 参与因子缺一项（有因子没给取值）
        (_factor_terms(abc=1.00, cap=0.50, existing=0.50, batch=0.50), {"station"}),
        # 降级因子混进来了（`station` 在 factor_degraded 里，却又出现在分解中）
        (
            _factor_terms(abc=1.00, cap=0.50, existing=0.50, station=0.50, batch=0.50, continuity=0.50),
            {"station"},
        ),
    ],
)
def test_a_breakdown_that_disagrees_with_the_available_factors_is_rejected(
    terms: dict[str, FactorTerm], degraded: set[str]
) -> None:
    """每条巷的分解键集必须恰为「六因子 − 降级因子」，**算分前**就拦。

    同一份不变式在 `schemas/reason.py` 的 `_contract_invariants` 里也有一份，两处都要有：
    只有契约那处，算出来的分数**是错的**（分母跟着少一项/多一项）而没人拦；只有这里，
    坏报文仍可能从别的组装路径写进 `payload_json`。两处的报错点不同（算分前 / 落库前），
    故这不是重复校验，是同一个不变式的两道门。
    """
    with pytest.raises(ValueError, match="分解键集"):
        score_aisles(
            weights=DEFAULT_WEIGHTS, breakdown={"01": terms}, degraded=degraded
        )


def test_available_weights_that_sum_to_zero_are_rejected() -> None:
    """可用因子的权重和为 0 ⇒ `ValueError`（不是 `ZeroDivisionError`）。

    六项都给 0 是配置能表达的（模型只要求六列存在），而 0 分母的报错若是
    `ZeroDivisionError: float division by zero`，读的人会以为引擎崩了 —— 实际是配置的
    问题，处置也不同（去配权重，不是重启）。
    """
    zero = {factor: 0.0 for factor in WEIGHT_FACTORS}

    with pytest.raises(ValueError, match="权重和为 0"):
        score_aisles(
            weights=zero,
            breakdown={"01": _factor_terms(abc=1.00, cap=1.00, existing=1.00,
                                           station=1.00, batch=1.00, continuity=1.00)},
        )


def test_scores_are_decimal_half_up_not_python_round() -> None:
    """两位小数走**十进制半值进位**（`17` §10.1 明令），不是 Python 内置 `round()`。

    构造一个**恰好落在半分位**的算例：权重 `abc=0.5 / cap=0.5`，取值 `0.75 / 0.40`
    ⇒ 0.575。内置 `round(0.575, 2)` 是 0.57（二进制表示略小于 0.575，且半值取偶），
    而业务口径要 0.58。第二条断言钉住「这个算例确实在半分位上」—— 否则哪天权重或取值
    被改动，这条用例会悄悄退化成恒真式（它原本守的正是这一位）。
    """
    weights = _weights(abc=0.5, cap=0.5, existing=0.0, station=0.0, batch=0.0, continuity=0.0)
    terms = _factor_terms(abc=0.75, cap=0.40, existing=0.0, station=0.0, batch=0.0, continuity=0.0)

    raw = 0.5 * 0.75 + 0.5 * 0.40
    assert round(raw, 2) == 0.57  # 前提：内置 round 的答案是另一个
    assert score_aisles(weights=weights, breakdown={"01": terms}) == {"01": 0.58}


def test_select_best_lists_all_aisles_tied_at_the_top_in_ascending_order() -> None:
    """并列同分 ⇒ **全列**，且按 `aisle_no` 升序（`17` §10.1 的 `aisles` 字段语义 / D16）。

    并列只在**量化后**的值上成立 —— 这正是「比较用量化后值」的用处：两条巷的原始加权和若
    只差在小数点后第六位，量化后同分即并列，不该让浮点末位决定谁进 `aisles`。
    （扣减落在 `aisle_no` 最小者那一条在分配器里，见 `17` §10.1 的字段语义。）
    """
    assert select_best({"01": 0.79, "02": 0.79, "21": 0.43}) == ["01", "02"]
    assert select_best({"01": 0.43, "02": 0.79, "21": 0.79}) == ["02", "21"]
    assert select_best({"01": 0.79}) == ["01"]


def test_select_best_rejects_an_empty_candidate_set() -> None:
    """可行集为空 ⇒ `ValueError`：那一步该走**降级链**（`14` §3.4 步 4），不是选道。

    静默返回 `[]` 会让「已经没有巷道可放」看起来像「选出来的巷道是空的」，而前者要下探
    降级链（`degradation.py`）、后者只是空列表 —— 两者在调用点长得一样。
    """
    with pytest.raises(ValueError, match="降级链"):
        select_best({})


def test_station_degrades_on_every_candidate_when_any_one_lacks_the_row() -> None:
    """`station` 的判据是**方案级**的：一条候选巷缺行 ⇒ 该因子在**全部**候选上一并降级。

    2.2 只落了「该巷有没有这一行」的单因子取值，聚合留到这里 —— 因为判据要看候选集，而
    候选集随容量扣减**逐单**变化，故本判据也是逐单求的（同一批里前后两张单的候选集不同，
    降级结论可以不同）。

    三态各断一次：表空（首期形态，措辞取 `17` §10.1 的示例）、部分导出（点名是哪条巷）、
    全都有行（不降级 ⇒ 该因子照常参与，分母里也有它的 0.20）。
    """
    assert station_degrade_reason(aisles=["01", "02"], station_weights={}) == "巷道-站台主数据未导出"

    partial = station_degrade_reason(aisles=["01", "02"], station_weights={"01": 0.9})
    assert partial is not None and "02" in partial and "01" not in partial

    assert station_degrade_reason(aisles=["01", "02"], station_weights={"01": 0.9, "02": 0.3}) is None
    # 非候选巷缺行不影响本单的结论（`station_weights` 里可能有多余的巷）
    assert station_degrade_reason(aisles=["01"], station_weights={"01": 0.9}) is None
