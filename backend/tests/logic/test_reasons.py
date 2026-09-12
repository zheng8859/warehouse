"""`reasons.py` 的理由体组装（7.1）与跨巷道预测的契约面（7.2 / 7.3 / 7.4）。

事实来源：`17` §10.1（推荐理由的字段与可追溯恒等式）、§10.7（响应侧字段与预测跨巷道的定义式）、
          §4.2（`payload_json` 与两列的关系）
          `14` §3.4 步 5（**回溯**预测跨巷道）、§3.5（降级链与 A 类告警）、§3.1（队列输入）
          `18` §1.3（同物料跨巷道 ≤5 —— 此处是它的**预演**：口径相同、时点不同）
          `20` §六 `AC-004`（引擎输出跨巷道数 ≤5）
          `openspec/.../spec.md` 的「推荐理由的可追溯契约」「因子级降级与两种降级不得混用」
          `design.md` D2（显式遍历序 / 量化比较 —— 集合并集只取 `len`）、D4（扣减不落库）、
          D8（两种降级不共用字段）、D9（三条不变量）、D16（`aisles` 单条）、D19（降级文案）
          `tasks.md` 7.1 / 7.2 / 7.3 / 7.4（本用例的任务书）

## 与 `test_reason_schema.py` 的分工

契约本身（字段、必填、校验器）由 1.1 的 `test_reason_schema.py` 钉住。本文件验的是**组装**：
从一次**真实的**批量分配产出理由体 —— 于是「分配器的证据满足对外契约」也一并被验了，
而那正是 8.x 端点要依赖的性质（手搓一份字典满足不了它）。

## 三条不变量各自钉住什么

1. **降级因子不出现在分解里** —— 漏了它，操作员会看到一个值为空的因子行，误以为它参与了打分。
2. **`scores` 可由取值复算** —— 复算取的是**理由体自己**给的权重与取值（十进制半值进位），
   故「理由里的分数不是这些项算出来的」当场显形；造数让 `station` 降级，分母不是 1.0 ——
   六因子齐全时归一化那一步写没写都看不出差别。
3. **列与 JSON 同名字段一致** —— 引擎侧能验的是「两列的同源事实只有一处」（`stop_tier` →
   `tier_stop_reason`），且它与**因子级**降级互不侵入（7.4 用「两种降级同时发生」的算例钉住）。
   列本身的写入属 8.2，那里从 `payload.degraded` / `payload.degrade_reason` 取值 ——
   「同一次写入的投影」（D9 第 3 条）在**代码**上成立，而不只是一句约定。

## 为什么 7.2 的用例按 `material_code` 取预测

预测是**整批回溯**的产物（`14` §3.4 步 5）：同料号的多张单拿到的是同一个数。它的天然键
因此是料号而非单据号 —— 按料号取数时，「两张单各算各的」这种偏差无从发生。
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.enums import AbcClass
from app.engine.allocator import AllocationItem, allocate_batch, predict_cross_aisles
from app.engine.factors import load_snapshot_index, load_station_weights
from app.engine.reasons import build_reason_payload
from app.engine.scoring import load_aisle_state, load_weights
from app.models.configuration import WEIGHT_FACTORS
from app.schemas.reason import ReasonPayload
from tests.logic.conftest import (
    DEFAULT_WEIGHTS,
    AisleSpec,
    InventorySpec,
    MaterialSpec,
    make_scenario,
)

pytestmark = pytest.mark.logic

#: 现场墙上时间（D17）：早于默认的 18:00 释放钟点。
NOW = datetime(2026, 9, 8, 9, 0)

#: `17` §10.1 的字段集 —— 理由体的**全部**键。写成一份闭合的集合，是因为 7.3 要断言
#: 「不含同批跨巷道键」：只查某一个键在不在，测不出「换了个名字塞进来」。
REASON_FIELDS = frozenset(
    {
        "job_id",
        "aisles",
        "factors",
        "scores",
        "breakdown",
        "priority",
        "factor_degraded",
        "degraded",
        "degrade_reason",
        "predicted_cross_aisle",
    }
)


def _item(
    job_order_id: str,
    material_code: str,
    qty: int,
    *,
    abc_class: AbcClass | None = None,
    batch_no: str | None = None,
) -> AllocationItem:
    """一条队列项。`abc_class` 缺省为 `None`（**不是** C 类）—— 兜底顺序是 `abc_factor`
    的口径，由用例显式给（与 `test_allocator.py` 同一个 `_item`）。"""
    return AllocationItem(
        job_order_id=job_order_id,
        material_code=material_code,
        qty=qty,
        order_abc_class=abc_class,
        order_batch_no=batch_no,
    )


def _assemble(session: Session, scenario, items) -> tuple:
    """走一遍端点（8.1）将走的次序：取数 → 分配 → **整批回溯**预测 → 逐单组装。

    返回 `(batch, predictions, payloads)`。四个 `load_*` 照跑（与 `test_allocator.py`
    同一个理由：分配器的入参**就是**它们的产物，绕过它们测的是一份只有本文件才知道的形状）；
    阈值取**配置行**而不是 `Settings` 的引导值 —— 现场改的是前者（7.2）。
    """
    assert scenario.snapshot is not None
    assert scenario.capacity_config is not None
    weights = load_weights(session, warehouse_id=scenario.warehouse_id, now=NOW)
    index = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)
    batch = allocate_batch(
        items=items,
        weights=weights,
        state=load_aisle_state(
            session,
            warehouse_id=scenario.warehouse_id,
            snapshot_id=scenario.snapshot.id,
        ),
        snapshot=index,
        station_weights=load_station_weights(
            session, warehouse_id=scenario.warehouse_id
        ),
        is_near_station={
            aisle_no: row.is_near_station for aisle_no, row in scenario.aisles.items()
        },
        release_at=scenario.capacity_config.reserved_release_at,
        now=NOW,
    )
    predictions = predict_cross_aisles(
        batch,
        snapshot=index,
        threshold=scenario.capacity_config.same_material_cross_aisle_threshold,
    )
    payloads = [
        build_reason_payload(
            outcome,
            weights=weights,
            predicted_cross_aisle=predictions[outcome.item.material_code],
        )
        for outcome in batch.outcomes
    ]
    return batch, predictions, payloads


# --- 7.1 理由体组装 -----------------------------------------------------------


def test_the_payload_carries_the_whole_reason_contract(session: Session) -> None:
    """一次真实分配的证据被**完整**搬进理由体，并满足 `ReasonPayload` 的契约校验。

    「通过校验」不是一句客套：`ReasonPayload` 的 `_contract_invariants` 会当场拦掉「六因子
    缺一」「`breakdown` 与 `scores` 键集不等」「`aisles` 不在候选集里」「降级缺原因」等形态。
    故这条用例同时说明两件事：组装正确，且**分配器交出来的证据本身就满足对外契约**。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01",
                cap_total=80,
                cap_reserved=20,
                is_near_station=True,
                station_weight=0.9,
            ),
            AisleSpec(aisle_no="02", cap_total=80, is_near_station=True, station_weight=0.3),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(
                location_code="010104", material_code="M1", batch_no="GJP2571221", qty=6
            )
        ],
    )
    batch, _, (payload,) = _assemble(
        session,
        scenario,
        # 批号给全：`batch` 因子在 `order_batch_no is None` 时**降级**（「无从比较，非
        # 「无同批」」），而这一条要的正是六因子齐全的形态（测试 2 反过来验降级）。
        [_item("PO-01", "M1", 5, abc_class=AbcClass.A, batch_no="GJP2571221")],
    )

    (outcome,) = batch.outcomes
    assert payload.job_id == "PO-01"
    assert payload.aisles == list(outcome.aisles) == ["01"]
    assert payload.scores == dict(outcome.scores)
    assert set(payload.scores) == {"01", "02"}  # 候选巷道集 = 停档的成员（D16）
    assert payload.factors == dict(DEFAULT_WEIGHTS)  # 六项权重 = 打分用的那一份
    assert set(payload.breakdown) == set(payload.scores)
    for aisle, terms in payload.breakdown.items():
        assert set(terms) == set(WEIGHT_FACTORS), aisle
        assert all(term.note for term in terms.values()), aisle  # 取值说明齐备
    assert payload.priority == outcome.priority
    assert payload.factor_degraded == {}
    assert payload.degraded is False and payload.degrade_reason is None

    # 预测的数值口径归 7.2；这里只确认它**在**理由体里、且与配置行同源（§10.7 随方案留存）。
    pred = payload.predicted_cross_aisle
    assert pred.material == 1  # 既有 {01} ∪ 本次 {01}
    assert pred.threshold == scenario.capacity_config.same_material_cross_aisle_threshold == 5

    # 落库的那一份就是 `model_dump` 的产物，键集封闭（多一个键立刻变红 —— 7.3 的前提）。
    written = payload.model_dump(mode="json")
    assert set(written) == REASON_FIELDS

    # 组装本身是纯的：同一份证据两次组装逐字相同（D2 第二条 —— 理由体不得含会话序 / 时钟）。
    again = build_reason_payload(
        outcome, weights=DEFAULT_WEIGHTS, predicted_cross_aisle=pred
    )
    assert again.model_dump(mode="json") == written


def test_a_degraded_factor_is_absent_from_every_decomposition(session: Session) -> None:
    """不变量 1：数据缺失 ⇒ 该因子不参与评分、**不出现在分解里**，但权重里仍有它。

    造数让 `station` 降级（首期形态：巷道-站台主数据整表未导出，`16` §394 / `A.4`）——
    即 `spec` 的「站台主数据未导出时因子降级」。

    权重**仍是六项**：`17` §10.1 的算例里 `station` 降级后 `factors` 仍有它的 `0.20`
    （分母 `0.80` 就是把它刨掉的结果）。契约给全、参与与否由 `factor_degraded` 表达 ——
    前端要显示哪几行是前端的事（§10.1 末节的「后端不裁剪」）。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec(aisle_no="01", cap_total=80, is_near_station=True)],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
    )
    _, _, (payload,) = _assemble(
        session,
        scenario,
        [_item("PO-01", "M1", 5, abc_class=AbcClass.A, batch_no="GJP1")],
    )

    assert payload.factor_degraded == {"station": "巷道-站台主数据未导出"}
    assert set(payload.factors) == set(WEIGHT_FACTORS)
    assert set(payload.breakdown["01"]) == set(WEIGHT_FACTORS) - {"station"}
    # 因子级降级**不是**方案级降级：这一单停在了近站台，没走降级链（D8 的最简形态）。
    assert payload.degraded is False and payload.degrade_reason is None


def test_the_scores_can_be_recomputed_from_the_payload(session: Session) -> None:
    """不变量 2：`scores[巷] = Σ(wᵢ·vᵢ) ÷ Σwᵢ`，两位小数**十进制半值进位**（`17` §10.1）。

    复算取的是**理由体自己**的权重与取值：分母没重新归一化、取值来自别处、量化方式不同，
    三种偏差都会在这里变红。用 `Decimal` 而不是 `float` 复算，是为了让断言真的独立 ——
    两边都用二进制浮点求和，末几位一致就成了同义反复，而这条恒等式的意义正是
    「理由里的分数就是这些项算出来的」（复算的人手里只有这一份 JSON）。

    造数让 `station` 降级（分母 `0.80`）：六因子齐全时分母恰好是 `1.0`，归一化那一步写没写
    都看不出差别，而它正是「有因子降级时满分仍为 1.0、跨轮次可比」的落点。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=80, cap_reserved=20, is_near_station=True),
            AisleSpec(aisle_no="02", cap_total=80, is_near_station=True),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(location_code="020205", material_code="M1", batch_no="GJP1", qty=4)
        ],
    )
    _, _, (payload,) = _assemble(
        session,
        scenario,
        [_item("PO-01", "M1", 5, abc_class=AbcClass.A, batch_no="GJP1")],
    )

    # 参与评分的因子：按 `WEIGHT_FACTORS` 的次序取（D2 第 1 条 —— 求和序显式给出）。
    scored = [factor for factor in WEIGHT_FACTORS if factor in payload.breakdown["01"]]
    denominator = sum(Decimal(str(payload.factors[factor])) for factor in scored)
    assert denominator != Decimal(1), "前提：这一例真的重新归一化了（否则本用例恒真）"

    for aisle, terms in payload.breakdown.items():
        numerator = sum(
            Decimal(str(payload.factors[factor])) * Decimal(str(terms[factor].value))
            for factor in scored
        )
        expected = (numerator / denominator).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert Decimal(str(payload.scores[aisle])) == expected, aisle


# --- 7.2 预测跨巷道（`14` §3.4 步 5） ------------------------------------------


def _cross_aisle_scenario(session: Session, *, threshold: int | None = None):
    """既有 2 巷道（`01` / `02`）+ 一条能放的远巷道（`07`）—— `spec` 的场景与 `AC-004` 共用。

    `01` / `02` 各只有 1 格、塞不下本单（5 板）⇒ 不可行；`07` 有 100 格 ⇒ 本单落在 `07`。
    既有库存在 `01` / `02`（库位号 6 位文本，前导 0 不得丢）⇒ `| {01,02} ∪ {07} | = 3`。
    """
    capacity = (
        {} if threshold is None else {"same_material_cross_aisle_threshold": threshold}
    )
    return make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=1, is_near_station=True, station_weight=0.9),
            AisleSpec(aisle_no="02", cap_total=1, is_near_station=True, station_weight=0.3),
            AisleSpec(aisle_no="07", cap_total=100, is_near_station=False, station_weight=0.3),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(location_code="010104", material_code="M1", batch_no="GJP1", qty=6),
            InventorySpec(location_code="020205", material_code="M1", batch_no="GJP1", qty=4),
        ],
        capacity=capacity,
    )


def test_ac_004_the_new_aisle_adds_to_the_existing_two(session: Session) -> None:
    """7.2 / `AC-004`：既有 2 巷道 + 本次分到 1 条新巷道 ⇒ `material = 3`，未超阈。

    即 `spec` 的「预测跨巷道按同物料口径」那条场景，也是 `20` §六 `AC-004`
    （「引擎输出跨巷道数 ≤5」）的**预演**形态：落位后实测属后验（F8），此处算的是
    「若照此落位，跨巷道数会变成几」—— 口径相同、时点不同。
    """
    scenario = _cross_aisle_scenario(session)
    batch, _, (payload,) = _assemble(
        session, scenario, [_item("PO-01", "M1", 5, abc_class=AbcClass.A)]
    )

    assert batch.outcomes[0].aisles == ("07",)  # 前提：新分的是 07，不在既有集里
    pred = payload.predicted_cross_aisle
    assert pred.material == 3
    assert pred.threshold == 5
    assert pred.exceeded is False
    assert pred.material <= pred.threshold  # `AC-004` 的「≤5」


def test_the_threshold_comes_from_the_config_row(session: Session) -> None:
    """阈值读 `CapacityConfig.same_material_cross_aisle_threshold`，不是写死的 5。

    `16` §353~356 把它列为默认值表的一项 ⇒ 它是**可改的口径**。写死 5 的表现是：现场把它
    调成 2 之后不再触发超阈，而没有任何地方会报错 —— 于是「预知集中度」只报好消息。
    """
    scenario = _cross_aisle_scenario(session, threshold=2)
    _, _, (payload,) = _assemble(
        session, scenario, [_item("PO-01", "M1", 5, abc_class=AbcClass.A)]
    )

    pred = payload.predicted_cross_aisle
    assert pred.material == 3  # 同一个数：阈值不参与计数
    assert pred.threshold == 2
    assert pred.exceeded is True


def test_the_prediction_unions_every_aisle_the_batch_gave_that_material(
    session: Session,
) -> None:
    """预测是**整批回溯**的（`14` §3.4 步 5）：同料号的多张单合起来算一次。

    造数让同料号的两张单分到两条巷（第一张占满 `01`，第二张去 `02`）⇒ 跨巷道数 2。
    逐单算的实现会各报 1 —— 而它看起来同样「正常」：「本次分配给该物料的巷道」这句话的
    主体是**料号**，不是单据。两张单拿到同一个数，正是这条口径的对外形态。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=10, is_near_station=True, station_weight=0.9),
            AisleSpec(aisle_no="02", cap_total=100, is_near_station=True, station_weight=0.3),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
    )
    batch, _, payloads = _assemble(
        session,
        scenario,
        [
            _item("PO-1", "M1", 10, abc_class=AbcClass.A),  # 占满 01
            _item("PO-2", "M1", 5, abc_class=AbcClass.A),  # 01 已满 ⇒ 去 02
        ],
    )

    assert [outcome.aisles for outcome in batch.outcomes] == [("01",), ("02",)]
    assert [payload.predicted_cross_aisle.material for payload in payloads] == [2, 2]


def test_the_prediction_covers_exactly_the_materials_that_got_plans(
    session: Session,
) -> None:
    """键集 = **出了方案的**料号：四级走尽的单没有方案，预测也就无处可挂（D19 的出口）。

    多一个键会让人以为那张单有方案（下钻时才发现没有）；少一个键则让 `plans` 里的某一条
    取不到预测 —— 两种都是「按料号取数」这条约定的失效形态。故这里直接取映射的键集来断言
    （其余用例经过 `payloads` 取数，看不出多出来的键）。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec(aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9)],
        materials=[
            MaterialSpec(material_code="MBIG", abc_class="A"),
            MaterialSpec(material_code="MOK", abc_class="C"),
        ],
    )
    batch, predictions, payloads = _assemble(
        session,
        scenario,
        [
            _item("PO-BIG", "MBIG", 10, abc_class=AbcClass.A),  # 5 板的巷塞不下 10 板
            _item("PO-OK", "MOK", 2, abc_class=AbcClass.C),
        ],
    )

    assert [failure.job_order_id for failure in batch.failures] == ["PO-BIG"]
    assert {payload.job_id for payload in payloads} == {"PO-OK"}
    assert set(predictions) == {"MOK"}


def test_the_aisle_of_a_location_is_the_text_slice_not_a_number(session: Session) -> None:
    """巷道的取法是 `location_code[:2]` 的**文本**切片（`CLAUDE.md` §七 / `17` §3.3）。

    造数：库位号 `010104` 落在巷道 `01`，本单也落在 `01` ⇒ 跨巷道数 1。任何「数值化」的
    错法都给 2：`location_code[:3]` = `"010"`、`int("010104")` = `10104`、取成整数 `1`
    与巷道号 `"01"` 不相等 —— 三者都把同一条巷数成两条。前导 0 一丢，多出来的是
    **不存在的巷道**，而 1 与 2 都长得像正常的数。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=100, is_near_station=True, station_weight=0.9)
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(location_code="010104", material_code="M1", batch_no="GJP1", qty=3)
        ],
    )
    batch, _, (payload,) = _assemble(
        session, scenario, [_item("PO-01", "M1", 5, abc_class=AbcClass.A)]
    )

    assert batch.outcomes[0].aisles == ("01",)
    assert payload.predicted_cross_aisle.material == 1


# --- 7.3 同批跨巷道不预测 ------------------------------------------------------


def test_the_payload_never_carries_a_same_batch_prediction(session: Session) -> None:
    """`payload_json` 只给**同物料**口径，不含同批跨巷道键（本期的契约范围，`17` §10.7）。

    不给同批口径**不是时点上做不到** —— 批号在分配时刻已知、同批分布本可聚合 —— 而是本期
    契约的**范围**；同批跨巷道留待落位后验（`15` §1.3）。故「少一个键」是刻意的，
    要由用例守住，免得哪天被顺手补上。

    造数让两个口径**给出不同的数**，这条用例才有内容：同批（`GJP1`）的两张单分到 `01` / `02`
    ⇒ 同批口径是 2；而该料号在 `03` 还有既有库存 ⇒ 同物料口径是 3。断言给出的数是 3，
    且键集与字段集都**封闭**（不是「查某个键在不在」—— 换个名字塞进来要照样变红）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=10, is_near_station=True, station_weight=0.9),
            AisleSpec(aisle_no="02", cap_total=100, is_near_station=True, station_weight=0.3),
            AisleSpec(aisle_no="03", cap_total=None),  # 主数据有、这一版快照没有它
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(location_code="030304", material_code="M1", batch_no="GJP0", qty=2)
        ],
    )
    batch, _, payloads = _assemble(
        session,
        scenario,
        [
            _item("PO-1", "M1", 10, abc_class=AbcClass.A, batch_no="GJP1"),
            _item("PO-2", "M1", 5, abc_class=AbcClass.A, batch_no="GJP1"),
        ],
    )

    assert [outcome.aisles for outcome in batch.outcomes] == [("01",), ("02",)]
    written = payloads[1].model_dump(mode="json")
    assert set(written) == REASON_FIELDS
    assert set(written["predicted_cross_aisle"]) == {"material", "threshold", "exceeded"}
    # 同物料口径（同批口径会是 2 —— 那正是本期不给的那个数）
    assert written["predicted_cross_aisle"]["material"] == 3


# --- 7.4 两种降级分开承载（D8） -------------------------------------------------


def test_the_two_degradation_channels_do_not_cross(session: Session) -> None:
    """一份方案**同时**容量不足与数据缺失时，两个字段各说各的、内容不交叉（D8）。

    造数两件事一起发生：近站台 `01` 只有 5 格（塞不下 10 板 ⇒ 走降级链到远巷道），
    而巷道-站台主数据整表未导出（⇒ `station` 因子级降级）。

    三条断言各守一处失效：
    ① 方案级原因只说**容量/档位**，不许混进「主数据未导出」——混进去会让操作员去补数据，
       而真问题是近站台塞不下；
    ② 因子级原因只说**数据缺失**，不许混进容量口径 —— 混进去会让「补导出」与「腾容量」
       两件事在理由里长得一样（`tier_stop_reason` 的 docstring 正是为此把两者分开说）；
    ③ **序列化**之后两个字段仍各就各位（落库的就是这一份），且方案级降级带得住原因
       （`RecommendationPlan` 的 `CHECK(degrade_reason_required)` 与 D9 第 3 条）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            # 两条巷都**不给** `station_weight`：`station` 因子降级的判据是「该巷没有
            # `AisleStation` 行」，给了权重就没有降级可验了（首期形态：整表未导出）。
            AisleSpec(aisle_no="01", cap_total=5, is_near_station=True),
            AisleSpec(aisle_no="07", cap_total=100, is_near_station=False),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
    )
    _, _, (payload,) = _assemble(
        session,
        scenario,
        # 批号给全，否则 `batch` 也要降级 —— 而这一条要的恰恰是「**只有** `station`
        # 因子在缺数据」的清晰形态（多降一个因子，就没有哪条断言能说清是哪一种降级）。
        [_item("PO-01", "M1", 10, abc_class=AbcClass.A, batch_no="GJP1")],
    )

    # ① 方案级：容量不足走了降级链
    assert payload.degraded is True
    assert payload.degrade_reason is not None
    assert "停在「远巷道」档" in payload.degrade_reason
    assert "近站台无可行容量" in payload.degrade_reason
    assert "未导出" not in payload.degrade_reason
    assert "主数据" not in payload.degrade_reason

    # ② 因子级：数据缺失使该因子不参与评分
    assert set(payload.factor_degraded) == {"station"}
    factor_reason = payload.factor_degraded["station"]
    assert "未导出" in factor_reason
    assert "容量" not in factor_reason and "档" not in factor_reason
    # 两句原因互不为对方的内容 —— 「内容不得互换」的最短形态
    assert factor_reason != payload.degrade_reason
    assert factor_reason not in payload.degrade_reason
    assert payload.degrade_reason not in factor_reason

    # ③ 落库的那一份同名字段与列源一致（D9 第 3 条）
    written = payload.model_dump(mode="json")
    assert written["degraded"] is True
    assert written["degrade_reason"] == payload.degrade_reason
    assert written["factor_degraded"] == payload.factor_degraded
    assert set(payload.breakdown["07"]) == set(WEIGHT_FACTORS) - {"station"}
