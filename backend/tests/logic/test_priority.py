"""`priority.py` 的用例：本文件当前覆盖**三项加法**（4.1）与**排序降级**（4.2）。

事实来源：`14` §3.2（优先级排序键：三项加法 + 「ABC 只分三档、同档内不排序」）
          `17` §10.1（`priority` 的示例取值与字段语义 —— 三项加法分量 + `score` 的域）
          `openspec/changes/recommendation-engine/specs/.../spec.md` 的
          「队列优先级排序」需求与「高优先级先挑」场景
          `openspec/changes/recommendation-engine/design.md` D2（全序与定点比较）、
          D14（待确认默认值：三项等权 + 未来 N 天）、D18（三项的归一化口径与降级作用域）
          `tasks.md` 4.1（本用例的任务书）

4.2（排序降级）与 4.3（全序兜底）的用例按任务书**续写在本文件** —— 三者共用「一条队列项
算一个分」的上下文，断言也常落在同一批造数上。

## 这里验的是「三项怎么算成一个分」，不是「谁先挑」

`14` §3.2 的落点是**资源仲裁**（谁先挑稀缺容量），而本文件的纯算术部分只到 `score` 为止；
「先挑」的那一半在分配器（6.x）里 —— 本文件用**分数的大小关系**断言序，不去碰容量。
这也正是 D1 的模块切分要的效果：`priority.py` 不 import `scoring` / `allocator`，
它的可测性就不依赖后者。

## 不写 DB 用例

本段是**纯函数**：入参是三项的原始量与权重，不读会话、不读快照。取数（哪条队列项、
ABC 取单据侧还是物料侧、出库量怎么聚合）属调用侧的活，`abc_term` 的入参形态与
`factors.abc_factor` 一致正是为此。
"""
from __future__ import annotations

import pytest

from app.core.enums import AbcClass
from app.engine.factors import abc_factor
from app.engine.priority import (
    DEFAULT_PRIORITY_WEIGHTS,
    QueueItem,
    abc_term,
    build_priorities,
    existing_gain_term,
    order_queue,
    outbound_term,
    score_priority,
)
from app.schemas.reason import PriorityTerms

pytestmark = pytest.mark.logic


#: `17` §10.1 示例里那条队列项的三项分量（订正后的示例本身）。
#: `existing_gain = 0.30` **不是**本阶段任何口径能算出来的值（D18 已记：该值反推不出），
#: 故此处直接构造 —— 本用例验的是「三项怎么加权求和」，三项各自怎么取数由下面的用例分头验。
_DOC_TERMS = PriorityTerms(outbound_qty=0.80, abc=1.00, existing_gain=0.30)


def _terms(**overrides: float) -> PriorityTerms:
    """一份「三项齐全」的基线队列项，便于逐个替换其中一项。"""
    base = {"outbound_qty": 0.50, "abc": 0.50, "existing_gain": 0.50}
    return PriorityTerms(**{**base, **overrides})


def test_the_documented_example_reproduces_the_documented_score() -> None:
    """`17` §10.1 的示例：三项 `(0.80, 1.00, 0.30)`、三项等权 ⇒ `score = 0.70`。

    这条用例把三处事实来源钉在一起：`14` §3.2 的式子（三项加权和）、`17` §10.1 的示例
    数值、D14 的等权系数。示例原写 `1.42`，与任何「权重合 = 1」的系数都不自洽
    （`1.42 > 1` 不可能由 `[0,1]` 的三项加权而得），已按铁律先改正本为 `0.70`。
    """
    assert score_priority(_DOC_TERMS) == pytest.approx(0.70)


def test_abc_term_uses_the_same_grade_table_as_the_scoring_layer() -> None:
    """`abc` 分量与 `abc` **因子**是同一套分数 —— 两处各写一份就会分叉。

    `factors.py` 的模块 docstring 已点名这一条（`17` §10.1 示例里两处同为 `A → 1.00`）。
    断言「同值」而不是「同为 0.67」：等级的**表**与归一化分母只有一处（`ABC_GRADE` /
    `ABC_GRADE_MAX`），本用例是那条不变式的守门人。
    """
    for abc_class in AbcClass:
        assert abc_term(order_abc_class=abc_class, material_abc_class=None) == pytest.approx(
            abc_factor(order_abc_class=abc_class, material_abc_class=None).term.value
        )


def test_abc_term_takes_the_order_side_first_and_falls_back_to_the_material() -> None:
    """单据侧优先、物料侧兜底 —— 与 `abc_factor` 的分辨顺序一致（`16` A.4）。

    单据上的 ABC 是入队时抄下来的，物料主数据上的是成品清单聚合的结果；两者不一致时
    以**单据**为准（它是这一单的当次事实）。两处若各有一套顺序，同一个料的同一张单会在
    「谁先挑」与「靠近站台」两件事上得到不同的 ABC 档。
    """
    assert abc_term(
        order_abc_class=AbcClass.C, material_abc_class=AbcClass.A
    ) == pytest.approx(abc_term(order_abc_class=AbcClass.C, material_abc_class=None))


def test_abc_term_is_zero_when_neither_side_has_a_class() -> None:
    """两侧皆空 ⇒ 分量计 `0.00`，**不是**降级。

    与评分侧有意不同（`abc_factor` 在那里返回降级、不进分母）：排序侧的分量必须是一个数，
    而队列的「数据缺失 ⇒ 排序降级」是**另一条**通道（`priority.degraded`，4.2，且是整批
    的）。`0.00` 取保守侧 —— 未知档不排到 C 类之前（`Material.abc_class` 的注释：
    「视为未知，不得当 C 类静默放行」；预留池那条红线由 D7 的 ABC 分支把守，两处各管一段）。
    """
    assert abc_term(order_abc_class=None, material_abc_class=None) == 0.00
    # C 类是 1/3，不是 0 —— 「未知 ≠ C 类」这条区分必须留在取值上，否则两者在理由里同形。
    assert abc_term(order_abc_class=AbcClass.C, material_abc_class=None) > 0.00


def test_outbound_term_normalizes_against_the_batch_maximum() -> None:
    """出库量按**本批队列的最大值**归一化（D18）：同批共享分母 ⇒ 序不变，最大值取 `1.00`。"""
    assert outbound_term(qty=40, batch_max=40) == pytest.approx(1.00)
    assert outbound_term(qty=20, batch_max=40) == pytest.approx(0.50)
    # 0 是**合法取值**（该料在 N 天内没有交货单），与「没有这份数据」不是一回事。
    assert outbound_term(qty=0, batch_max=40) == 0.00


def test_an_all_zero_batch_normalizes_to_zero_without_blowing_up() -> None:
    """全批出库量都是 0（成品清单导入了，但这批料 N 天内都没有交货单）⇒ 三项里这项无信息。

    分母为 0 不该抛：数据**在**、只是值全为 0，与「数据缺失 ⇒ 排序降级」（4.2）是两回事。
    这条也把「`batch_max = 0` 时的返回值」钉死 —— 否则调用点会各自决定，而其中一种写法
    就是 0 除。
    """
    assert outbound_term(qty=0, batch_max=0) == 0.00


@pytest.mark.parametrize(("qty", "batch_max"), [(-1, 10), (5, -10)])
def test_a_negative_quantity_is_rejected(qty: int, batch_max: int) -> None:
    """负数出库量是数据缺陷（`16` §171 的「数量 > 0」一族），不得静默变成负分量。

    静默的后果不是报错而是**排序反向**：负分量会把这张单压到队尾，而队尾正是「最后拿容量」
    甚至「拿不到容量」的位置 —— 现场看到的是一条无人察觉的错序。
    """
    with pytest.raises(ValueError, match="出库量"):
        outbound_term(qty=qty, batch_max=batch_max)


def test_existing_gain_term_is_binary() -> None:
    """既有集中度增益只分「能并回既有巷道」与「不能」（D18）。

    二值而不是比例：`14` §3.2 说的「并回既有巷道**可降**跨巷道数的幅度」，能归一化的读法
    只有这两种 —— 能并回（跨巷道数不因本单增加）/ 不能。`1/(k+1)` 那种读法对 k **递减**
    （越散的物料越先挑），与「收拢」恰好反向。
    """
    assert existing_gain_term(existing_aisle_count=0) == 0.00
    assert existing_gain_term(existing_aisle_count=1) == 1.00
    assert existing_gain_term(existing_aisle_count=3) == 1.00


def test_orders_in_the_same_class_are_ordered_by_outbound_quantity() -> None:
    """`14` §3.2 的关键补充：「ABC 只分三档，**同档内不排序**」⇒ 用出库量把同档拆开。

    这是 4.1 任务书的头一条验证：三条同为 A 类、既有分布也相同的单，谁未来 N 天出库量大
    谁的分高。三单同批 ⇒ 共享 `batch_max`，故这里的分母取三者的最大量。
    """
    quantities = {"PO-01": 80, "PO-02": 40, "PO-03": 20}
    batch_max = max(quantities.values())
    scores = {
        order_no: score_priority(
            _terms(
                outbound_qty=outbound_term(qty=qty, batch_max=batch_max),
                abc=abc_term(order_abc_class=AbcClass.A, material_abc_class=None),
                existing_gain=existing_gain_term(existing_aisle_count=0),
            )
        )
        for order_no, qty in quantities.items()
    }
    assert scores["PO-01"] > scores["PO-02"] > scores["PO-03"]


def test_an_a_class_bestseller_outranks_a_c_class_slow_mover() -> None:
    """4.1 任务书的后一条验证，spec 的「高优先级先挑」场景：

    A 类爆款（出库量高）的分必须高于 C 类慢流转品（出库量低）—— 即使后者的**料已经在库里**
    （`existing_gain = 1.00`）而前者是新料。ABC 与出库量两项合起来的权重盖过集中度增益一项。
    """
    bestseller = _terms(
        outbound_qty=outbound_term(qty=100, batch_max=100),
        abc=abc_term(order_abc_class=AbcClass.A, material_abc_class=None),
        existing_gain=existing_gain_term(existing_aisle_count=0),
    )
    slow_mover = _terms(
        outbound_qty=outbound_term(qty=10, batch_max=100),
        abc=abc_term(order_abc_class=AbcClass.C, material_abc_class=None),
        existing_gain=existing_gain_term(existing_aisle_count=3),
    )
    assert score_priority(bestseller) > score_priority(slow_mover)


def test_a_zero_weight_component_contributes_nothing() -> None:
    """权重取 `0` 的项不进分 —— 顺带钉住三项与三个系数的**位置对应**。

    位置错配（把 `abc` 的权重乘到出库量上）不会报错，只会静默改序；这条用例让它在
    「某一项为 0 权重」时现形。
    """
    terms = _terms(outbound_qty=1.00, abc=0.00, existing_gain=0.00)
    assert score_priority(terms, weights=(0.0, 1.0, 0.0)) == pytest.approx(0.00)
    assert score_priority(terms, weights=(1.0, 0.0, 0.0)) == pytest.approx(1.00)


def test_the_score_is_renormalized_by_the_weight_sum() -> None:
    """系数按**相对值**用：等比例放大不改分（与 `scores` 的分母重新归一化同一手法）。

    业务方给的很可能是 `(5, 3, 2)` 这类相对系数而不是和为 1 的绝对值。按权重和归一化，
    `score` 的域就恒为 `[0,1]`（D18），也让 `17` §10.1 的示例值在任何系数下都可比。
    """
    assert score_priority(_terms(), weights=(1.0, 1.0, 1.0)) == pytest.approx(
        score_priority(_terms(), weights=(2.0, 2.0, 2.0))
    )
    assert score_priority(_terms(), weights=(5.0, 3.0, 2.0)) == pytest.approx(
        score_priority(_terms(), weights=(2.5, 1.5, 1.0))
    )


def test_the_default_weights_are_the_equal_weight_default() -> None:
    """D14 登记的默认系数：三项等权。系数到齐后**只改这一处**（一个常量元组）。"""
    assert DEFAULT_PRIORITY_WEIGHTS == (1 / 3, 1 / 3, 1 / 3)


@pytest.mark.parametrize(
    "weights",
    [
        (1 / 3, 1 / 3),  # 少一项
        (1 / 3, 1 / 3, 1 / 3, 1 / 3),  # 多一项
        (0.0, 0.0, 0.0),  # 三项全零：无从归一化
        (1.0, -1.0, 1.0),  # 负系数：分不再是「越大越优先」
    ],
)
def test_weights_outside_the_contract_are_rejected(
    weights: tuple[float, ...],
) -> None:
    """系数是**配置**（阶段四会从配置列来），越界不能静默。

    四种形态都不是理论情形：少/多一项是位置错配的前兆；全零会让归一化除零；负系数会
    把「分高者先挑」翻过来 —— 而它算出来的分仍是个像模像样的浮点数，除了在这里报出来，
    没有第二处会发现。
    """
    with pytest.raises(ValueError, match="权重"):
        score_priority(_terms(), weights=weights)


# --- 4.2 排序降级（缺未来 N 天出库量） -------------------------------------


def _queue_item(
    job_order_id: str,
    *,
    abc_class: AbcClass | None = AbcClass.A,
    outbound_qty: int | None = 10,
    existing_aisle_count: int = 0,
    material_abc_class: AbcClass | None = None,
) -> QueueItem:
    """一条队列项：默认「ABC 在、出库量在」，便于只替换出事的那一格。"""
    return QueueItem(
        job_order_id=job_order_id,
        order_abc_class=abc_class,
        material_abc_class=material_abc_class,
        outbound_qty=outbound_qty,
        existing_aisle_count=existing_aisle_count,
    )


def test_a_batch_without_outbound_data_degrades_to_abc_only_without_interrupting() -> None:
    """`14` §6.2：无交货单 ⇒ 排序退化为仅按 ABC，**标注降级排序**、不中断批量分配。

    本阶段（导入管线属阶段四）现场就是这一态：没有任何一份数据能给出「未来 N 天出库量」。
    三件事必须同时成立 —— ① 每条队列项都有 `degraded=True` 与非空 `degrade_reason`
    （「不静默」）；② 序仍由 ABC 决定（同档并列，这正是 4.3 兜底要接的形态）；③ 队列项
    一条不少（不中断 —— 降级不是异常，`CLAUDE.md` 第四节）。
    """
    items = [
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=None),
        _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=None),
        _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=None),
    ]
    priorities = build_priorities(items)

    assert set(priorities) == {"PO-01", "PO-02", "PO-03"}
    for payload in priorities.values():
        assert payload.degraded is True
        assert payload.degrade_reason  # 非空
    # 退化为仅按 ABC：A 档两条并列（同档内不排序），且都高于 C 档。
    assert priorities["PO-01"].score == pytest.approx(priorities["PO-03"].score)
    assert priorities["PO-01"].score > priorities["PO-02"].score
    # 原因要能被操作员用来判断**去补哪份数据**（`factors.py` 的降级文案标准）。
    assert "出库量" in priorities["PO-01"].degrade_reason


def test_the_degraded_score_is_the_abc_term_itself() -> None:
    """降级后的 `score` 恒等于 `abc` 分量 —— 退化是**系数**换了一组，不是另开一条算式。"""
    priorities = build_priorities(
        [
            _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=None, existing_aisle_count=5),
            _queue_item("PO-02", abc_class=AbcClass.B, outbound_qty=None),
        ]
    )
    for job_order_id, payload in priorities.items():
        assert payload.score == pytest.approx(payload.terms.abc)
    # 集中度增益与出库量都退出排序（否则「散料先挑」会把收拢的目标反过来）。
    assert priorities["PO-01"].terms.abc == pytest.approx(1.00)
    assert priorities["PO-02"].terms.abc == pytest.approx(2 / 3)


def test_one_missing_item_degrades_the_whole_batch_not_just_that_item() -> None:
    """降级的作用域是**整批**（D18）：一单缺 ⇒ 全队列退化为仅按 ABC。

    只降级出事那一单会造出**两种刻度混排**的队列：有数据的按三项（域 `[0,1]`）、没数据的按
    ABC（域 `[0,1]`），两者不可比 —— 混排出来的序没有意义。这与 `station` 因子「一条候选巷
    缺行 ⇒ 全候选一并降级」（D16）是同一条论证。故此处让 PO-02 的出库量高达 1000：若它
    仍按三项算，分会把它顶到队首；整批降级后它只按 ABC（C 档）垫底。
    """
    priorities = build_priorities(
        [
            _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=10),
            _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=None),
            _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=1000),
        ]
    )
    for payload in priorities.values():
        assert payload.degraded is True
        assert payload.degrade_reason
    # 全批同一个原因：降级说的是**队列的尺度**，不是某一条队列项的毛病。
    assert len({payload.degrade_reason for payload in priorities.values()}) == 1
    assert priorities["PO-01"].score > priorities["PO-02"].score
    assert priorities["PO-01"].score == pytest.approx(priorities["PO-03"].score)


def test_the_terms_keep_the_facts_even_when_the_score_ignores_them() -> None:
    """`terms` 记**事实**、`score` 记**本批实际用的排序键** —— 两者在部分缺失时故意不一致。

    有出库量的那几条仍写自己的归一化值（把真实值抹成 `0.00` 会掩盖「这份数据其实有」，
    比不一致更糟）；缺的那几条写 `0.00`（「没有这份数据」为真）。差异由 `degrade_reason`
    解释，这也正是「降级不静默」要留给复核者的入口。
    """
    priorities = build_priorities(
        [
            _queue_item("PO-01", abc_class=AbcClass.B, outbound_qty=40),
            _queue_item("PO-02", abc_class=AbcClass.B, outbound_qty=None),
        ]
    )
    assert priorities["PO-01"].terms.outbound_qty == pytest.approx(1.00)  # 本批最大
    assert priorities["PO-02"].terms.outbound_qty == 0.00
    # 分仍只按 ABC（同档 ⇒ 同分），出库量的事实不掺进排序。
    assert priorities["PO-01"].score == pytest.approx(priorities["PO-02"].score)


def test_a_partial_missing_reason_names_how_many_items_are_affected() -> None:
    """部分缺失与全缺的文案要分得开：前者是本批数据不全，后者是这份数据整份没来。"""
    partial = build_priorities(
        [
            _queue_item("PO-01", outbound_qty=10),
            _queue_item("PO-02", outbound_qty=None),
        ]
    )
    assert "1/2" in partial["PO-01"].degrade_reason
    complete = build_priorities([_queue_item("PO-03", outbound_qty=None)])
    assert "1/2" not in complete["PO-03"].degrade_reason


def test_a_queue_with_all_data_present_is_not_degraded() -> None:
    """四项齐全 ⇒ 不降级、无原因 —— 降级标记不能变成永远亮着的灯。"""
    priorities = build_priorities(
        [
            _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
            _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=100),
        ]
    )
    for payload in priorities.values():
        assert payload.degraded is False
        assert payload.degrade_reason is None
    # 三项都在用：C 类那张单靠出库量与集中度也追不上 A 类（此处同量同增益，只差 ABC）。
    assert priorities["PO-01"].score > priorities["PO-02"].score


def test_an_unknown_abc_zeroes_that_component_on_every_item_and_says_so() -> None:
    """ABC 未派生（`Material.abc_class` 为空、单据侧也空）⇒ 该分量在**全部**队列项上计 `0.00`。

    `14` §6.2 只写了缺出库量那一条，本形态是**实现期补的口径**（D18 已记）：`abc_term`
    对未知档返回 `0.00`，若只把出事的那几条计零，同批里就会一半按真实等级、一半按 `0.00`
    排 —— 又是混刻度。整批计零则刻度统一，代价是 ABC 这一项对本批无信息，而这正是
    「数据没到」的真相。`degraded` 与原因必须说出来（「不静默」），否则 `0.00` 会被当成
    一个算出来的值。
    """
    items = [
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=50),
        _queue_item("PO-02", abc_class=None, outbound_qty=50, material_abc_class=None),
    ]
    priorities = build_priorities(items)

    both = [priorities["PO-01"], priorities["PO-02"]]
    assert all(payload.degraded is True for payload in both)
    assert all("ABC" in payload.degrade_reason for payload in both)
    # A 类那一单的 abc 分量也被计成 0.00 —— 不是它自己没有等级，是本批不可比。
    assert all(payload.terms.abc == 0.00 for payload in both)
    # 余下两项按权重和重新归一化 ⇒ 同出库量、同集中度 ⇒ 同分（落到 4.3 的兜底）。
    assert priorities["PO-01"].score == pytest.approx(priorities["PO-02"].score)


def test_a_missing_abc_leaves_a_fallback_to_the_material_side_undisturbed() -> None:
    """单据侧为空但**物料侧有** ⇒ 不是「未派生」（`abc_term` 的兜底顺序），不触发降级。"""
    items = [
        _queue_item(
            "PO-01",
            abc_class=None,
            material_abc_class=AbcClass.A,
            outbound_qty=10,
            existing_aisle_count=1,
        )
    ]
    payload = build_priorities(items)["PO-01"]
    assert payload.degraded is False
    assert payload.terms.abc == pytest.approx(1.00)


def test_an_empty_queue_yields_no_priorities() -> None:
    """空队列不是降级场景：没有队列项就没有「排序降级」可言，返回空表、不报错。"""
    assert build_priorities([]) == {}


def test_duplicate_job_order_ids_are_rejected() -> None:
    """同一 `job_order_id` 出现两次 ⇒ 当场报错。

    出参按单据号索引，静默覆盖会让**一张单从所有方案里消失**（分配器按它取优先级），
    而队列本身看着完好 —— 这是本模块最不该静默的一种缺陷。
    """
    with pytest.raises(ValueError, match="重复"):
        build_priorities(
            [_queue_item("PO-01", outbound_qty=10), _queue_item("PO-01", outbound_qty=20)]
        )


# --- 4.3 全序兜底（同分不同单的次序确定） -----------------------------------


def _ids(items: list[QueueItem]) -> list[str]:
    return [ranked.item.job_order_id for ranked in order_queue(items)]


def test_the_queue_is_ordered_by_priority_descending() -> None:
    """`14` §3.2 的排序键：**分高者先挑** —— 4.1 的分在这里第一次变成次序。"""
    items = [
        _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=10),
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
        _queue_item("PO-03", abc_class=AbcClass.B, outbound_qty=50),
    ]
    assert _ids(items) == ["PO-01", "PO-03", "PO-02"]


def test_three_tied_items_are_ordered_by_job_order_id_ascending() -> None:
    """同分 ⇒ 单据号**升序**兜底（D2 的全序；`17` §10.7 的 `plans` 也按这个序列）。

    没有这一层兜底，同分三单的相对次序取决于数据库的返回序 —— 而那正是「同样输入必得
    同样输出」要禁掉的东西（`CLAUDE.md` 第四节）：同一批数据两次分配可能给出不同的序，
    现场只会看到「推荐怎么变了」而查不出原因。
    """
    items = [
        _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=100),
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
        _queue_item("PO-02", abc_class=AbcClass.A, outbound_qty=100),
    ]
    assert _ids(items) == ["PO-01", "PO-02", "PO-03"]
    # 顺带确认这三条**确实**同分 —— 否则上面的断言证明的不是「同分兜底」而是「按分排序」。
    scores = {payload.score for payload in build_priorities(items).values()}
    assert len(scores) == 1


def test_the_input_order_does_not_leak_into_the_queue_order() -> None:
    """入参的排列不得影响出参序 —— 这是「全序」与「稳定排序」的分界。

    稳定排序会把**入参序**（= 数据库行序）保留成同分项的次序，而那是一个**未定义的遍历序**
    （D2 要显式给出的正是它）。两条同为 A 类的单在两种入参排列下必须给出同一串序。
    """
    forward = [
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
        _queue_item("PO-02", abc_class=AbcClass.B, outbound_qty=100),
        _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=100),
    ]
    assert _ids(forward) == _ids(list(reversed(forward))) == ["PO-01", "PO-03", "PO-02"]


def test_two_consecutive_allocations_of_the_same_queue_give_the_same_order() -> None:
    """任务书的验证：**连续两次分配**（重取一遍数据、构造新对象）队列序逐项相同。

    第二次用的是**新建但相等**的队列项对象（不是同一批引用）—— 现场两次分配的数据来自两次
    查询，若序依赖对象身份（`id()`、集合序、dict 序）或任何一次性的状态，这条用例会现形。
    """
    def fresh() -> list[QueueItem]:
        return [
            _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=100),
            _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
            _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=100),
            _queue_item("PO-04", abc_class=AbcClass.C, outbound_qty=100),
        ]

    first = _ids(fresh())
    second = _ids(fresh())
    # 两条 A 类同分 ⇒ 按单号升序（`PO-01` 在前），与它们在入参里的先后无关。
    assert first == second == ["PO-01", "PO-03", "PO-02", "PO-04"]


def test_each_queue_entry_carries_the_priority_computed_for_it() -> None:
    """出参成对返回，且分与序出自**同一次**计算 —— 「按一份算的序、拿另一份的分写理由」不可能。

    分配器遍历时两样都要：队列项告诉它这一单是什么，优先级既定次序又是写进推荐理由的那份
    证据（`17` §10.1 的 `priority`）。若让调用侧分两次取（先排序、再单独算理由），两处的
    分就有了各自漂移的余地，而 `.score` 是个像模像样的浮点数，没人会发现错配。
    """
    items = [
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=100),
        _queue_item("PO-02", abc_class=AbcClass.C, outbound_qty=10),
    ]
    ranked = order_queue(items)
    expected = build_priorities(items)
    for entry in ranked:
        assert entry.priority == expected[entry.item.job_order_id]
    # 序与分自洽：把出参按（分降序, 单号升序）重排，得到的还是它自己。
    assert [entry.item.job_order_id for entry in ranked] == [
        entry.item.job_order_id
        for entry in sorted(ranked, key=lambda e: (-e.priority.score, e.item.job_order_id))
    ]


def test_a_degraded_batch_still_gets_a_total_order() -> None:
    """降级批（仅按 ABC）同样是全序：A 档内部按单号，其后才是 B 档 —— 不中断、也不并列。

    `14` §6.2 的降级只换排序键，不放松「顺序确定」这条要求；此处的两条 A 类同分，正靠
    4.3 的兜底才有一条可复现的序（4.2 的用例已断言它们**同分**）。
    """
    items = [
        _queue_item("PO-02", abc_class=AbcClass.B, outbound_qty=None),
        _queue_item("PO-01", abc_class=AbcClass.A, outbound_qty=None),
        _queue_item("PO-03", abc_class=AbcClass.A, outbound_qty=None),
    ]
    assert _ids(items) == ["PO-01", "PO-03", "PO-02"]


def test_an_empty_queue_orders_to_nothing() -> None:
    """空队列 ⇒ 空序（与 `build_priorities` 的空表同一处置），不报错。"""
    assert order_queue([]) == []
