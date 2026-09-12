"""`allocator.py` 的用例：贪心主循环（6.1）、「扣减不落库 · 两次调用逐项相同」（6.2）、
四类输入全空的端到端形态（9.3）与快照缺席时的引擎行为（9.5）。

事实来源：`14` §3.4（步 1 可行集 → 步 2 选最高分 → 步 3 扣减 → 步 4 空则降级 → 步 5 回溯）、
          §3.5（四级链与降级不静默）、§3.2（队列序 = 谁先挑稀缺容量）
          `16` §394 / `A.4`（首期四类输入全空的预期形态；ABC 与站台主数据缺失时的降级口径）
          `17` §3.4（`AisleCap` 三列）、§10.1（六因子与权重的取数）、§10.7
          `openspec/.../spec.md` 的「批量竞争分配」「四级降级链与降级不静默」
          `design.md` D2（全序 / 量化比较 / 时钟注入）、D4（内存扣减 · 不落库）、
          D5（分档）、D6（板-格换算收在一个函数里）、D7（三判据）、D9（理由体的三条不变量）、
          D10 补记 ②（无快照：引擎不阻断、端点闸住 —— 两层各管一半）、D16（单条 `aisles`）、
          D18（队列优先级）、D19（降级文案与四级走尽的出口）
          `20` §六 `SC-005`（既有同物料落位 ⇒ 得分更高，链路上的那一半）、
          `AC-002`（容量不足触发降级链）、`AC-005`（降级链末端仍无容 ⇒ 提示人工干预）
          `tasks.md` 1.4（注入式时钟在链路上的那一半）/ 6.1 / 6.2 / 9.3 / 9.5 / 10.1 / 10.2
          （本用例的任务书）

## 9.3 与 9.5 是同一类用例的两半

两条都在问「一类输入缺席时引擎走哪条通道」：9.3 是**四类全空**（cap / 库存 / ABC / 站台）
⇒ 逐单四级走尽 + 各自降级；9.5 只让**库存索引**缺席 ⇒ 照常分配 + 三个库存因子降级。两条都
不抛异常、不静默落位，也都在 docstring 里写明「库里造什么、引擎收到什么」的差异 —— 库层面的
「无快照」必然连带没有 cap 行（`AisleCap.snapshot_id` 是非空外键），那个形态属 9.3 的第一类。

## 本文件验的是「循环」，不是「算术」

六因子、加权归一化、分档解析各有自己的用例（`test_factors.py` / `test_scoring.py` /
`test_degradation.py`）。本文件只回答循环层面的五个问题：**谁先挑**（队列序）、**挑哪条**
（档内最高分）、**挑完怎么记账**（内存扣减对后续项生效）、**挑不到怎么办**（逐档下探 →
四级走尽失败）、**这笔账有没有留下痕迹**（批结束后三张表逐列未变，6.2）。故造数都挑
「分差明显」的巷，不去碰恰好落在半值上的边界（那是 3.3 的事）。

**一处有意的例外**：`test_sc_005_…` 断的是「因子值 → 总分 → 落位」这条链的**贯通**
（`20` §六的 `SC-005` 预期列只有把两半合起来才成立）。它放在本文件是因为另外两处都够不着：
`test_scoring.py` 的 `score_aisles` 收**手写的分解**，在那儿断言「有库存的巷得分更高」是
同义反复；`test_factors.py` 只有因子值、没有选道。例外仅此一条，且它断言的主体仍是
「**挑哪条**」——因子取值只是证据。

## 6.2 的两条用例互为前提

「三表未变」若无扣减发生就是恒真式；「两次调用相同」若无降级与失败在内，也只覆盖了
最顺的那条路径。故前者先断言**扣减确实让后续单分不出去**，后者造的批里同时有
降级告警、四级走尽失败与正常方案 —— 两者都指名道姓地要求「结论里看得见那次扣减」。

## 为什么用例走真实的取数函数

`load_aisle_state` / `load_snapshot_index` / `load_station_weights` / `load_weights` 都在
本文件里真跑一遍：分配器的入参**就是**这四个的产物，绕过它们直接手搓 `AisleState` 之类的
对象，测的是一份只有本文件才知道的形状 —— 而那正是接口错位最容易藏进去的地方
（例如 `is_near_station` 取自主数据行还是快照行、`reserved_release_at` 读的是不是当期配置）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import AbcClass, AccountStatus, LedgerType, Role
from app.engine.allocator import AllocationItem, allocate_batch, to_occupied_cells
from app.engine.degradation import DEGRADATION_TIER_LABELS
from app.engine.factors import (
    abc_factor,
    existing_factor,
    load_snapshot_index,
    load_station_weights,
    station_factor,
)
from app.engine.scoring import load_aisle_state, load_weights, station_degrade_reason
from app.models.identity import Account
from app.models.job import Ledger
from app.models.linkage import AisleCap, Snapshot
from tests.logic.conftest import (
    DEFAULT_WAREHOUSE_ID,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
    make_scenario,
)

pytestmark = pytest.mark.logic

#: 现场墙上时间：早于 `reserved_release_at`（默认 18:00），故非 A 类**不**共享预留池。
NOW = datetime(2026, 9, 8, 9, 0)

#: 「不给这个键」的哨兵 —— 与显式的 `None` 分得开（见 `_allocate`）。
_FROM_SCENARIO = object()


def _item(
    job_order_id: str,
    material_code: str,
    qty: int,
    *,
    abc_class: AbcClass | None = None,
    material_abc_class: AbcClass | None = None,
    outbound_qty: int | None = 0,
    batch_no: str | None = None,
) -> AllocationItem:
    """一条队列项。两个 ABC 参数缺省为 `None`（**不是** C 类）：夹具里不替用例决定
    「单据侧还是物料侧出 ABC」——那正是 `abc_factor` 的口径，由用例显式给。

    `material_abc_class` 单列一个参数（与 `abc_class` 分开），因为两者在库里各有一列；
    9.3 传的是**从物料行上读到的那一列**（该列为 NULL），而不是一个字面 `None`。
    """
    return AllocationItem(
        job_order_id=job_order_id,
        material_code=material_code,
        qty=qty,
        order_abc_class=abc_class,
        material_abc_class=material_abc_class,
        outbound_qty=outbound_qty,
        order_batch_no=batch_no,
    )


def _allocate(
    session: Session,
    scenario,
    items,
    *,
    now: datetime = NOW,
    snapshot_id: object = _FROM_SCENARIO,
):
    """按**真实取数**装配分配器的入参。四个 `load_*` 与分配器的入参一一对应。

    `snapshot_id` 不给 = 库存索引取自造数那一版（绝大多数用例）；**显式给 `None`** =
    引擎拿到一个**缺席**的库存索引（9.5 的形态，`design.md` D10 ② 点名的入口）。
    哨兵而不是把默认值写成 `None`：后者会让「忘了给」与「故意不给」长得一样，而这两件事
    在 9.5 那里正好是同一句断言的两面。
    """
    assert scenario.snapshot is not None
    assert scenario.capacity_config is not None
    index_id = scenario.snapshot.id if snapshot_id is _FROM_SCENARIO else snapshot_id
    return allocate_batch(
        items=items,
        weights=load_weights(session, warehouse_id=scenario.warehouse_id, now=now),
        state=load_aisle_state(
            session, warehouse_id=scenario.warehouse_id, snapshot_id=scenario.snapshot.id
        ),
        snapshot=load_snapshot_index(session, snapshot_id=index_id),
        station_weights=load_station_weights(
            session, warehouse_id=scenario.warehouse_id
        ),
        is_near_station={
            aisle_no: row.is_near_station
            for aisle_no, row in scenario.aisles.items()
        },
        release_at=scenario.capacity_config.reserved_release_at,
        now=now,
    )


# --- 6.1 贪心主循环 ---------------------------------------------------------


def test_the_highest_scoring_feasible_aisle_is_selected(session: Session) -> None:
    """步 1/步 2：**同一档内**比总分，选得分最高的那条（`14` §3.4）。

    造数让两条巷各赢一头：`01` 距离近（`station` 0.9）但预留池大（`cap` 0.50），`02` 反过来
    （`station` 0.3 / `cap` 1.00）。谁入选只能由加权和决定 —— 若实现里按「哪条更空」选道，
    这条用例就会挂，而那种实现在真实数据下的表现是「近站台永远填不满，A 类爆款被挤到远巷道」。

    两条巷必须**同档**才谈得上比总分：档 0 有可行巷道时，档 2 的巷根本不参与比较（链的
    语义就是先近后远，见模块 docstring），故这里给的是两条近站台巷道。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01",
                cap_total=80,
                cap_reserved=40,
                is_near_station=True,
                station_weight=0.9,
            ),
            AisleSpec(
                aisle_no="02",
                cap_total=80,
                is_near_station=True,
                station_weight=0.3,
            ),
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(
        session,
        scenario,
        [_item("PO-01", "M1", 10, abc_class=AbcClass.C)],
    )

    (outcome,) = result.outcomes
    assert result.failures == ()
    assert outcome.stop_tier.index == 0  # 前提：两候选同档
    assert set(outcome.scores) == {"01", "02"}
    assert outcome.scores["01"] > outcome.scores["02"]  # 入选的那条分更高
    assert outcome.aisles == ("01",)
    # 没有降级：档 0 就停下了。
    assert outcome.degraded is False
    assert outcome.degrade_reason is None
    assert outcome.alert is None


def test_sc_005_an_aisle_holding_the_material_wins_on_its_score(session: Session) -> None:
    """`SC-005`：既有库位存在同物料历史落位 ⇒ 该巷得分更高、**落位落在它身上**
    （`20` §六「1. 评分正确性」的预期列）。

    预期列是两半，本条守的是后半：因子值（`test_factors.py` 的
    `test_existing_stock_lifts_the_score_of_its_own_aisle`）与「选道不同」之间隔着**权重、
    分母归一化、档内比较**三处口径 —— 任一处把因子值丢掉，因子层那半照样全绿，而现场拿到
    的是「推荐到另一条没有同物料历史的巷」，理由卡上却每一项都写着有值。

    ## 造数为什么把库存放在 `02`（而不是 `01`）

    两条巷做成**除库存外处处相同**：同档（都近站台）、同 `station_weight`、同 `cap_total`。
    于是「谁赢」只剩库存这一项可解释。而库存放在**编号大**的那条上，是因为期望值与
    「**什么都不算**」的退化实现所给出的答案**相反**：一个把因子值丢掉的实现会退化成
    「按 `aisle_no` 升序挑」⇒ 选 `01`，本条随即变红。若把库存放在 `01`，同一份退化实现
    会**碰巧**选中 `01`，用例是绿的而问题照旧。

    ## 两个驱动因子都断一次

    `breakdown` 里指向 `02` 的是两项：`existing`（同物料已占 4 板 ⇒ 严格更高）与
    `continuity`（同物料**仅**在 `02` ⇒ 1.00，而 `01` 是 0.00）。断言取值而不是只断言
    「总分更高」，是为了让失败信息能直接指出是哪一项丢了 —— 否则「总分不相等」这条
    在六个因子里的任一处理解错时都长得一样。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec("01", cap_total=100, is_near_station=True, station_weight=0.5),
            AisleSpec("02", cap_total=100, is_near_station=True, station_weight=0.5),
        ],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[InventorySpec("020205", "M1", "GJP1", qty=4)],
    )
    result = _allocate(
        session,
        scenario,
        [_item("PO-01", "M1", 5, material_abc_class=AbcClass.A, batch_no="GJP1")],
    )

    (outcome,) = result.outcomes

    # 三个前提：同档、两条都在候选集里、六因子齐全（没有降级因子把某一项从分母里摘走）。
    assert result.failures == ()
    assert outcome.stop_tier.index == 0
    assert set(outcome.scores) == {"01", "02"}
    assert set(outcome.factor_degraded) == set()

    # 既有的那 4 板在 `02`：两个被它驱动的因子都指向 `02`。
    assert (
        outcome.breakdown["02"]["existing"].value
        > outcome.breakdown["01"]["existing"].value
    )
    assert outcome.breakdown["02"]["continuity"].value == 1.00  # 同物料现仅在此巷道
    assert outcome.breakdown["01"]["continuity"].value == 0.00  # 同物料不在该巷道

    # 因而（预期列的后半）：得分更高、且**落位落在这一条巷上**。
    assert outcome.scores["02"] > outcome.scores["01"]
    assert outcome.aisles == ("02",)


def test_the_deduction_applies_to_the_next_queue_item(session: Session) -> None:
    """步 3：扣减记在**内存**里，对同批后续项生效（`14` §3.4 / D4）。

    任务书的场景：`01` 只剩 10 板 —— 先来的单占满它，后面那张要 5 板的单**不再把它算作
    可行巷道**。这是「同一批内竞争同一份容量」的全部内容：没有扣减，两张单会同时拿到
    `01`，而现场只有一份 10 板的空位。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01",
                cap_total=10,
                is_near_station=True,
                station_weight=0.9,
            ),
            AisleSpec(
                aisle_no="02",
                cap_total=100,
                is_near_station=False,
                station_weight=0.3,
            ),
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-01", "M1", 10, abc_class=AbcClass.A),  # 队首先挑：拿满 01
            _item("PO-02", "M2", 5, abc_class=AbcClass.C),  # 01 已无余额 ⇒ 只能去 02
        ],
    )

    first, second = result.outcomes
    assert first.item.job_order_id == "PO-01"
    assert first.aisles == ("01",)
    assert second.item.job_order_id == "PO-02"
    assert second.aisles == ("02",)
    # 关键断言：`01` 根本不在第二单的评分范围里（不可行 ⇒ 不进候选），
    # 而不是「进了候选但分低」——后者会让 `breakdown` 里出现一条塞不下的巷道。
    assert set(second.scores) == {"02"}


def test_the_cap_factor_reads_the_snapshot_not_the_in_batch_deduction(
    session: Session,
) -> None:
    """`cap` 因子的分子取**快照**的可用量，不减本批已占（D16 的公式逐字落地）。

    这是刻意的一处不对称：扣减只进**可行集判据**（`feasible_aisles` 的 `consumed`），不进
    **因子取值**。理由是 `17` §10.1 的那条不变式 —— 「`scores` 可由取值复算」（7.1 的验证
    条件之一）：复算的人手里只有快照与这一单，没有当时的 `consumed`。若因子值随竞争过程
    变化，落库的理由就成了只有当事进程算得出的东西。

    故本条断言的是「先来的单占走 10 格之后，后来那张单看到的 `cap` 仍是快照的
    `可用 100 / 100 板`」，同时它仍**因扣减而可能不可行** —— 两件事各管一段。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01",
                cap_total=100,
                is_near_station=True,
                station_weight=0.9,
            ),
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-01", "M1", 10, abc_class=AbcClass.A),
            _item("PO-02", "M2", 5, abc_class=AbcClass.A),
        ],
    )

    first, second = result.outcomes
    assert first.aisles == ("01",) and second.aisles == ("01",)
    assert second.breakdown["01"]["cap"].note == "可用 100 / 100 板"


def test_one_aisle_ties_with_another_so_both_are_listed_and_the_smallest_takes_the_deduction(
    session: Session,
) -> None:
    """并列同分 ⇒ `aisles` **全列**（升序），扣减落在 `aisle_no` 最小者（D16）。

    两件事都要验，因为它们的失效形态不同：只列一条会**丢掉一个并列的推荐**（`17` §10.1
    的字段语义是「本次选中的推荐巷道集」）；把扣减摊到每一条上会让容量被重复扣（两次
    `5 板` 的占用其实是同一份 5 板）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="02", cap_total=5, is_near_station=True, station_weight=0.9
            ),
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-01", "M1", 5, abc_class=AbcClass.A),
            _item("PO-02", "M2", 5, abc_class=AbcClass.C),
        ],
    )

    first, second = result.outcomes
    assert first.scores["01"] == first.scores["02"]  # 前提：确实并列
    assert first.aisles == ("01", "02")
    # 扣减只落最小者：`01` 满、`02` 仍空 ⇒ 第二单只能去 `02`。
    assert second.aisles == ("02",)


def test_the_batch_processes_the_queue_in_priority_order(session: Session) -> None:
    """步 0 的次序来自 `priority.order_queue`（`14` §3.2「谁先挑稀缺容量」）。

    分配器**不自己排一遍**：出参顺序就是落库的 `plans` 顺序，两处各排一次的话，
    「A 类先挑」在容量结果里成立、在报文顺序里却可能被另一套规则覆盖。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=100, is_near_station=True, station_weight=0.9
            )
        ],
        materials=[
            MaterialSpec(material_code="MA"),
            MaterialSpec(material_code="MC"),
        ],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-C", "MC", 1, abc_class=AbcClass.C),
            _item("PO-A", "MA", 1, abc_class=AbcClass.A),
        ],
    )

    assert [outcome.item.job_order_id for outcome in result.outcomes] == ["PO-A", "PO-C"]


def test_an_empty_queue_yields_an_empty_result(session: Session) -> None:
    """空队列 ⇒ 空结果（`17` §10.7 的「空数组是明确的空集」）。

    不是异常、也不是「全量」：本阶段没有「不填就提交全部待分配单」这种隐含默认。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=10, is_near_station=True)
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(session, scenario, [])

    assert result.outcomes == ()
    assert result.failures == ()
    assert result.alerts == ()


# --- 逐档下探与四级走尽（`14` §3.4 步 4 / §3.5） ------------------------------


def test_a_full_preferred_tier_descends_to_the_far_tier_and_says_why(
    session: Session,
) -> None:
    """档 0 塞不下 ⇒ 下探到档 2，并记下降级原因（`14` §3.4 步 4 / §3.5）。

    降级**不是异常**（`CLAUDE.md` 第四节）：该单照常出方案、照常落 `aisles`，只是
    `degraded=true` 且原因写清「哪几档为什么没停下」——`14` §3.5 要求每一步都记录原因
    并展示给操作员，所以「停在远巷道」与「近站台为什么没停」缺一不可。

    **本条即 `20` §六 `AC-002`**（「容量不足触发降级链 ⇒ 降级到次优巷道集，不崩溃、不丢弃」）
    的第一种成因，四条断言逐一对应：容量不足 = `01` 只有 5 格（造数本身即触发条件）、
    降级到次优巷道集 = `aisles == ("07",)` 且 `stop_tier.label == "远巷道"`、不崩溃 = 走到断言
    即无异常、不丢弃 = `failures == ()` 而该单在 `outcomes` 里。**首期「次优巷道集」就是档 2**：
    档 1（次近巷道）与档 3（溢出区）是**空集但占位**，判据留待业务口径（D5），故下探今天
    必然从档 0 一步落到档 2。第二种成因见下一条。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="07", cap_total=100, is_near_station=False, station_weight=0.3
            ),
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(
        session, scenario, [_item("PO-01", "M1", 10, abc_class=AbcClass.A)]
    )

    (outcome,) = result.outcomes
    assert result.failures == ()
    assert outcome.aisles == ("07",)
    assert outcome.degraded is True
    assert outcome.stop_tier.label == "远巷道"
    assert "停在「远巷道」档" in outcome.degrade_reason
    # 档 0 有候选（`01` 在主数据与快照里都有）却塞不下 ⇒ 是容量问题，不是数据问题。
    assert "近站台无可行容量" in outcome.degrade_reason


def test_in_batch_exhaustion_descends_and_records_the_same_degradation(
    session: Session,
) -> None:
    """`AC-002` 的第二种成因：容量是被**同一批的前一张单**吃掉的，不是一开始就不够。

    与上一条同一档位、同一结局（下探到档 2、`degraded=true`、写明原因），差别只在**成因**：
    上一条是「这一版快照里近站台就只有 5 格」，本条是「快照给的 10 格被先来的单占满了」。
    两种成因在理由里的措辞**相同**（都是「近站台无可行容量」）—— 这是有意的：理由回答的是
    「为什么没停在近站台」，不是「谁的锅」；两者处置不同（前者要改配置或移库，后者是正常的
    批内竞争），但那是看板的语境，不属于单条推荐理由（D19 的口径：理由要能直接给操作员读）。

    本条补的是 `test_the_deduction_applies_to_the_next_queue_item` **没断言的那一半**：
    那条只断言第二单落到 `02`、且 `01` 不在它的候选集里 —— **没有断言这次下探被记录下来了**。
    若下探只影响 `aisles` 而不置 `degraded` / `degrade_reason`，那条用例照样全绿，而操作员
    看到的是「系统推荐了远巷道」却读不到任何原因 —— 「降级不静默」（`CLAUDE.md` §四）要拦的
    正是这个。同一份事实因此需要两条用例：一条守「选对了巷道」，一条守「说了为什么」。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=10, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="02", cap_total=100, is_near_station=False, station_weight=0.3
            ),
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-01", "M1", 10, abc_class=AbcClass.A),  # 队首先挑：拿满 01
            _item("PO-02", "M2", 5, abc_class=AbcClass.C),  # 01 已无余额 ⇒ 只能去 02
        ],
    )

    # 按单号索引而不是按下标取：`outcomes` 是**队列序**（优先级降序），与提交序不是同一个序，
    # 按下标取会错位到「断言写对了、但看的是另一张单」这种不报错的形态（同 8.1 的处置）。
    by_id = {outcome.item.job_order_id: outcome for outcome in result.outcomes}
    assert set(by_id) == {"PO-01", "PO-02"}  # 不丢弃：两张单都出了方案
    assert result.failures == ()  # 不崩溃：没有一张单走到四级走尽

    first, second = by_id["PO-01"], by_id["PO-02"]
    assert first.stop_tier.index == 0 and first.degraded is False, (
        "先来的单停在近站台，不该被记成降级 —— 否则「降级」这个词就不再表示"
        "「没抢到近站台」，「某单降级了」这句告警级信号会被稀释成背景噪音"
    )
    assert second.aisles == ("02",)
    assert second.stop_tier.label == "远巷道"
    assert second.degraded is True
    assert "停在「远巷道」档" in second.degrade_reason
    assert "近站台无可行容量" in second.degrade_reason
    # C 类被迫降级**不告警**（告警限 A 类，`14` §3.5）—— 见 test_a_non_a_class_...
    assert second.alert is None


def test_an_a_class_order_forced_to_the_far_tier_alarms_with_the_shortfall(
    session: Session,
) -> None:
    """A 类被迫降到远巷道 ⇒ 告警，且缺口量是**差额**（`14` §3.5 / D19）。

    近站台只剩 5 板、本单要 10 板 ⇒ 缺口 5 板（不是 10）。报整单会把「还差一点」说成
    「完全没有」，而主管据此决定是否移库腾挪。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="07", cap_total=100, is_near_station=False, station_weight=0.3
            ),
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(
        session, scenario, [_item("PO-01", "M1", 10, abc_class=AbcClass.A)]
    )

    (alert,) = result.alerts
    assert alert.job_order_id == "PO-01"
    assert alert.aisle == "07"  # 降级后实际落到的那条（与 plans 对齐）
    assert "近站台缺口 5 板，建议移库腾挪" in alert.message
    assert "容量不足" in alert.message  # 成因：有近站台巷道、只是塞不下


def test_a_non_a_class_order_descending_does_not_alarm(session: Session) -> None:
    """非 A 类降级**不告警**（`14` §3.5 只管 A 类爆款）。

    放宽这一条会让每条 B/C 单都告警，告警就不再是告警 —— 这与 5.2 的 `is_alarming`
    是同一个判据，这里验的是「主循环确实按它筛了」。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="07", cap_total=100, is_near_station=False, station_weight=0.3
            ),
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(
        session, scenario, [_item("PO-01", "M1", 10, abc_class=AbcClass.B)]
    )

    (outcome,) = result.outcomes
    assert outcome.degraded is True
    assert result.alerts == ()


def test_exhausting_every_tier_fails_only_that_order_and_the_rest_still_get_plans(
    session: Session,
) -> None:
    """四级走尽 ⇒ **该单**失败；同批其余单照常出方案（`14` §3.4 步 4 / `15` §11.1）。

    失败的边界是**单**不是**批**：一张单分不出去不该让整批 500，也不该让它被静默塞进
    某条塞不下的巷道（AC-005 的「不静默落位到非法巷道」）。故它进 `failures` 而不是
    `outcomes`，且**不占容量** —— 后续那张 2 板的单照常拿到 `01`。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            )
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
    )
    result = _allocate(
        session,
        scenario,
        [
            _item("PO-BIG", "M1", 10, abc_class=AbcClass.A),  # 5 板的巷塞不下 10 板
            _item("PO-SMALL", "M2", 2, abc_class=AbcClass.C),
        ],
    )

    assert [outcome.item.job_order_id for outcome in result.outcomes] == ["PO-SMALL"]
    (failure,) = result.failures
    assert failure.job_order_id == "PO-BIG"
    assert "人工介入" in failure.message
    # 失败单不进 outcomes ⇒ 也就不会出现在 `plans` 里、不回写批次号（8.2 的状态迁移据此）。
    assert "PO-BIG" not in {outcome.item.job_order_id for outcome in result.outcomes}


def test_an_order_whose_every_candidate_has_no_near_station_flag_still_allocates(
    session: Session,
) -> None:
    """`is_near_station` 整列未导出（首期形态）⇒ 全进档 2，**照常分配**、只标降级。

    这是「降级不静默」与「不静默收缩候选集」两条的合流：未导出的巷**仍可达**（不是
    不可行），只是不享近站台优先（`app/models/master_data.py` 第 3 条：NULL 不得当
    `False` 用）—— 丢掉它们会让首期整批分不出去。
    """
    scenario = make_scenario(
        session,
        aisles=[AisleSpec(aisle_no="01", cap_total=100, is_near_station=None)],
        materials=[MaterialSpec(material_code="M1")],
    )
    result = _allocate(
        session, scenario, [_item("PO-01", "M1", 10, abc_class=AbcClass.A)]
    )

    (outcome,) = result.outcomes
    assert outcome.aisles == ("01",)
    assert outcome.degraded is True
    assert outcome.stop_tier.label == "远巷道"
    assert "未导出" in outcome.degrade_reason


# --- 时钟注入（`design.md` D2 第 3 条 / 1.4） --------------------------------


def test_the_release_clock_is_the_injected_now_not_a_clock_read_inside(
    session: Session,
) -> None:
    """同一批、同一张单，**只换 `now`**：09:00 分不出去，18:30 落进 `01`。

    这是注入式时钟在**链路上**的那一半（静态面「引擎代码里不许取钟」在
    `test_engine_package.py`）。两种错误形态都在这条用例下变红：把 `now` 收下却从不使用的
    分配器 ⇒ 两次结果相同；自己 `datetime.now()` 的分配器 ⇒ 两次结果随**跑用例的墙上
    时间**变（白天跑绿、夜里跑红，是最难查的一类间歇失败）。

    ## 造数为什么是「B 类 70 板 + 预留 20 板」

    近站台巷道 `01`：`cap_total = 80`、`cap_reserved = 20` ⇒ 释放前 B 类只拿得到
    `cap_usable` = 60 板，70 板塞不下（四级走尽 ⇒ 该单失败）；过了 `reserved_release_at`
    （夹具默认 18:00）预留池释放给 B/C（`14` §3.5）⇒ 可用回到 80 板，同一张单随即落 `01`。
    可动的差额只有 20 板，故 `qty` 必须落在 `(60, 80]` 这个窗口里 —— 取 70 让两侧都留出
    余量，不落在任何边界上（边界属 `test_reserved.py` 的闭区间用例）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                "01",
                cap_total=80,
                cap_reserved=20,
                is_near_station=True,
                station_weight=0.9,
            )
        ],
        materials=[MaterialSpec(material_code="M1")],
    )
    items = [_item("PO-01", "M1", 70, abc_class=AbcClass.B)]

    before = _allocate(session, scenario, items)  # NOW = 当日 09:00，早于 18:00
    after = _allocate(session, scenario, items, now=NOW.replace(hour=18, minute=30))

    # 释放前：可用 60 < 70 ⇒ 四级走尽，该单失败并提示人工介入（不静默落位）
    assert before.outcomes == ()
    (failure,) = before.failures
    assert failure.job_order_id == "PO-01"
    assert "人工介入" in failure.message

    # 释放后：同一张单、同一份快照 —— 落进近站台巷道，且「可用」读到的就是释放后的 80
    (outcome,) = after.outcomes
    assert after.failures == ()
    assert outcome.aisles == ("01",)
    assert outcome.degraded is False
    # `cap` 因子读的是「本单落位**前**这条巷还有多空」（`17` §10.1 的「可用 58 / 80 板」
    # 同一读法）：释放后 B 类拿得到全部 80 ⇒ 80 / 80。取值本身在释放前那一次是算不上
    # 的（该单当时分不出去），故这里断的是 `note` 里的那个 80 —— 它正是「预留池已释放」
    # 的可见证据；`value` 只是它的比值。
    assert outcome.breakdown["01"]["cap"].note == "可用 80 / 80 板"
    assert outcome.breakdown["01"]["cap"].value == pytest.approx(1.0)


# --- 板-格换算（D6） ---------------------------------------------------------


def test_the_cell_conversion_is_the_identity_in_this_phase() -> None:
    """`to_occupied_cells` 本阶段取**恒等**（D6）：一单一位 = 一格 = 一板。

    恒等不是占位符，而是唯一与全部文档算例自洽的读法（`14` §2.2 用板作巷道容量、
    §3.5 的告警文案也是板）。这里同时钉住「材料参数被收进签名」——换算法则到齐时
    只改这一个函数体，而**调用方已经把它传进来了**，不必回头改每一处调用。
    """
    assert to_occupied_cells(7) == 7
    assert to_occupied_cells(7, cartons_per_pallet=12) == 7  # 规则未定 ⇒ 参数不影响结果


# --- 6.2 扣减不落库 · 两次调用逐项相同（D4 + D2） ------------------------------


def _fingerprint(session: Session, model: type) -> tuple[tuple, ...]:
    """一张表**全部映射列**的取值快照（按 `repr` 排序，故与行序无关）。

    取全部列而不是挑几列：漏掉一列就是给「悄悄改了这一列」留出一个绿的口子，而本用例
    要断言的正是**没有任何一列被写**。排序键用 `repr` 而不是元组本身 —— 元组比较会逐位
    比较，遇到可空列上 `None` 与 `str` 同处一位时抛 `TypeError`，那会把一条断言变成
    一条用例错误。
    """
    columns = [attr.key for attr in sa.inspect(model).mapper.column_attrs]
    rows = session.execute(sa.select(*[getattr(model, name) for name in columns])).all()
    return tuple(sorted((tuple(row) for row in rows), key=repr))


def test_the_in_batch_deduction_is_never_written_to_the_database(
    session: Session,
) -> None:
    """整批结束后 `AisleCap` / `Snapshot` / `Ledger` 三表**行数与内容均未变**（D4）。

    `AisleCap` 是这一版快照的**冻结**事实（`17` §3.4）：分配产出的是**建议**，写库要等
    二次确认卡通过（`CLAUDE.md` §四「未确认不产生台账」）。故本批已占的格数只活在
    `allocate_batch` 内部那个 `consumed` 字典里，批一结束就没了 —— 同一入参再跑一遍得到
    的是同一份建议，而不是「第二遍算出来的容量比第一遍少」。

    **先断言扣减确实发生了**：只判「表没变」的话，一个压根没跑起来、或把每条单都塞进零格
    巷道的实现也照样绿。这里的形态是「先来的单占满 `01` ⇒ 后来那张塞不下而失败」，故
    「表未变」与「结论里看得见那次扣减」两件事同时成立。

    `Ledger` 特意**先插一行**：三张表里它是唯一会被别的流程写的那张（引擎自动写台账，
    `ledger.write` 不向任何角色开放手动入口），空表的 `0 == 0` 是恒真式，钉不住东西。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=10, is_near_station=True, station_weight=0.9
            )
        ],
        materials=[MaterialSpec(material_code="M1"), MaterialSpec(material_code="M2")],
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code="M1", qty=10),
            JobOrderSpec(order_no="PO-02", material_code="M2", qty=10),
        ],
    )
    # 台账的两个 NOT NULL 外键（`operator_id` / `job_order_id`）都要有真行：SQLite 开了
    # `foreign_keys=ON`，填占位整数会撞完整性错误而不是被当成「随便造一行」。
    operator = Account(
        warehouse_id=scenario.warehouse_id,
        username="gtj_keeper",
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(operator)
    session.flush()
    session.add(
        Ledger(
            warehouse_id=scenario.warehouse_id,
            job_order_id=scenario.job_orders[0].id,
            ledger_type=LedgerType.INBOUND,
            order_no="PO-01",
            material_code="M1",
            batch_no="GJP2571221",
            qty=10,
            operator_id=operator.id,
            executed_at=NOW,  # 审计钟（D17）：台账写入即固化，本用例不读它
            # 入库台账无源库位、有目标库位（`_LEDGER_LOCATION_CHECK` 的矩阵），
            # 库位号按 6 位文本（前导 0 不得丢）。
            target_location_code="010104",
        )
    )
    session.flush()

    before = {
        model: _fingerprint(session, model) for model in (AisleCap, Snapshot, Ledger)
    }
    assert before[Ledger] != ()  # 前提：台账里真的有东西可比（否则下面的断言恒真）

    result = _allocate(
        session,
        scenario,
        [
            _item("PO-01", "M1", 10, abc_class=AbcClass.A),  # 占满 01
            _item("PO-02", "M2", 10, abc_class=AbcClass.C),  # 01 已无余额 ⇒ 四级走尽
        ],
    )

    # 前提：扣减确实生效（否则「表未变」是恒真式）。
    (outcome,) = result.outcomes
    assert outcome.aisles == ("01",)
    (failure,) = result.failures
    assert failure.job_order_id == "PO-02"

    for model in (AisleCap, Snapshot, Ledger):
        assert _fingerprint(session, model) == before[model], (
            f"{model.__tablename__} 被本批写入了 —— 分配只出建议，扣减只活在内存里（D4）"
        )
    # 表未变，但**内存里的扣减确实发生了**：两个断言合起来才是 D4 的全文
    # （扣减生效于本批、不落库、批结束即丢）。
    assert scenario.aisle_caps["01"].cap_total == 10  # 快照事实没被改写


def test_two_identical_calls_produce_identical_output(session: Session) -> None:
    """同一入参两次调用输出**逐项相同**（D2「同样输入必得同样输出」/ D4）。

    第二遍不是「接着第一遍的余量算」，而是从同一份只读快照**重算**（D4 的扣减只活在当次
    调用的 `consumed` 里）—— 若实现把已占格数写回了 `AisleCap`，第二遍的可行集就会比第一遍
    小，而那种偏差在单次调用里完全看不出来。

    **逐项比而不是整对象比**：整对象比对失败时只告诉你「不一样」，而漂移可能落在
    `scores` 的小数末位、`breakdown` 的某条 `note`、或某个降级标记上 —— 三处成因不同
    （量化方式 / 取值来源 / 分档），逐项断言失败时能直接指向是哪一个。

    造的批里同时有**降级告警**（A 类被迫去远巷道）、**四级走尽失败**（200 板塞不进任何一条）
    与**正常方案**：只比一条最顺的路径，比不出降级文案与失败文案里的时钟、集合序有没有漂。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(
                aisle_no="01", cap_total=5, is_near_station=True, station_weight=0.9
            ),
            AisleSpec(
                aisle_no="07", cap_total=100, is_near_station=False, station_weight=0.3
            ),
        ],
        materials=[
            MaterialSpec(material_code="MA"),
            MaterialSpec(material_code="MB"),
            MaterialSpec(material_code="MBIG"),
        ],
    )
    items = [
        _item("PO-A", "MA", 10, abc_class=AbcClass.A),  # 近站台塞不下 ⇒ 降级 + 告警
        _item("PO-BIG", "MBIG", 200, abc_class=AbcClass.A),  # 到哪儿都塞不下 ⇒ 失败
        _item("PO-B", "MB", 3, abc_class=AbcClass.C),  # 正常拿近站台
    ]

    first = _allocate(session, scenario, items)
    second = _allocate(session, scenario, items)

    # 前提：这一批确实三种形态都走到了（否则比的是三条同样的顺路单）。
    assert [outcome.item.job_order_id for outcome in first.outcomes] == ["PO-A", "PO-B"]
    assert [failure.job_order_id for failure in first.failures] == ["PO-BIG"]
    assert len(first.alerts) == 1

    assert len(first.outcomes) == len(second.outcomes)
    for left, right in zip(first.outcomes, second.outcomes, strict=True):
        where = left.item.job_order_id
        assert left.aisles == right.aisles, where
        assert left.scores == right.scores, where
        assert left.breakdown == right.breakdown, where
        assert left.priority == right.priority, where
        assert left.factor_degraded == right.factor_degraded, where
        assert left.stop_tier == right.stop_tier, where
        # 降级标记三件套：方案级降级、原因文案、A 类告警。
        assert left.degraded == right.degraded, where
        assert left.degrade_reason == right.degrade_reason, where
        assert left.alert == right.alert, where
    assert first.failures == second.failures
    assert first.alerts == second.alerts


# --- 9.3 四类输入全空（阶段三现场的真实形态） -----------------------------------


def test_the_all_empty_input_shape_fails_every_order_and_degrades_each_missing_input(
    session: Session,
) -> None:
    """四类输入全空 ⇒ 逐单四级走尽失败 + 逐项降级，**不抛异常、不静默落位**（`16` §394）。

    这是阶段三**唯一能在现场跑通的形态**（`16` §394 / `A.4`：导入管线属阶段四，四类输入
    —— cap 基线 / 既有库位分布 / ABC 分类 / 巷道-站台主数据 —— 在现场全为空）。它与 9.1
    （缺权重 ⇒ **整批阻断**）分属两条通道：缺权重时连「该按什么比例算总分」都不知道，
    **没有可降级的目标**；缺这四类时分配照跑，每一处缺席都留下一条可读的痕迹。

    四类缺席**不落在同一条通道上**，这是本用例要固定下来的事实（`design.md` D8 的三条
    通道 + D19）：

    | 缺的输入 | 通道 | 本用例的断言 |
    |---|---|---|
    | `AisleCap`（cap 基线） | **逐单失败**（D19） | 每张单都在 `failures`；`outcomes` / `alerts` 为空 |
    | `Material.abc_class` | **因子级降级** | `abc_factor` 返回 `degraded` |
    | `AisleStation`（巷道-站台主数据） | **因子级降级**（方案级） | `station_degrade_reason` 给出「未导出」 |
    | `InventoryItem`（既有库位分布） | **不是降级**：合法空档案 | `existing` 取 `0.00` 且不降级 |

    **为什么 cap 缺失落在失败通道而不是降级通道**：`AisleCap` 是候选集的**构成条件**
    （`AisleState` 的两半取交集），没有 cap 行的巷道连候选都不是 —— 因子层没有它可取的值，
    四级链也就没有一档停得下来。降级的前提是「有值但不参与」，这里根本没有值（与 `design.md`
    D3 那句「缺权重时没有可降级的目标」同构）。

    **为什么缺库存**不**算降级**：空档案是合法事实 —— 该料号在库里没有库存，三个读库存的
    因子里 `existing` / `continuity` 取 `0.00`（`InventoryProfile` 的 docstring：降级 =
    数据没到位，`0.00` = 数据到了且为零）。把它记成降级会让首期**每一张**单都带一条永不
    消失的理由，理由就不再是信号。

    造数：`cap_total=None`（**不建** `AisleCap` 行，与「有该行、容量为零」严格不同）、无
    `inventory`、`MaterialSpec` 不给 `abc_class`、`AisleSpec` 不给 `station_weight`。
    快照行**在**：`16` §11.6 的三类文件独立导入，cap / 库存缺失不等于没有快照 —— 「无快照」
    那一形态由 9.5 验，且端点在进引擎之前就闸住了（D10 补记 ② 的两层分工）。

    失败文案里能读到什么：四档**都走过**（不是漏判）且成因是「无候选巷道」（数据问题 ⇒
    补数据）而不是「无可行容量」（容量问题 ⇒ 腾容量）—— 两种成因的处置相反（D19）。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec(aisle_no="01", cap_total=None, is_near_station=None),
            AisleSpec(aisle_no="02", cap_total=None, is_near_station=None),
        ],
        materials=[
            MaterialSpec(material_code="M1"),
            MaterialSpec(material_code="M2"),
        ],
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code="M1", qty=10, batch_no="PB-01"),
            JobOrderSpec(order_no="PO-02", material_code="M2", qty=5, batch_no="PB-02"),
        ],
    )
    # ABC 从**物料行上读**（该列为 NULL），不写字面 `None`：后者与「用例忘了给」同形。
    # 批号照给：它由系统在入库单建立时生成（`factors.py` 第 3 条），不属于这四类输入，
    # 让它取「有值但无同批」的 0.00，免得这份造数里多出一条与 9.3 无关的降级。
    items = [
        _item(
            "PO-01",
            "M1",
            10,
            material_abc_class=scenario.materials["M1"].abc_class,
            batch_no="PB-01",
        ),
        _item(
            "PO-02",
            "M2",
            5,
            material_abc_class=scenario.materials["M2"].abc_class,
            batch_no="PB-02",
        ),
    ]

    # 走到这一行本身即「不抛异常」：四级走尽是一条**结果**，不是异常。
    result = _allocate(session, scenario, items)

    # ① 逐单失败 = 不静默落位。`AllocationFailure` 没有巷道字段，`outcomes` 为空 ⇒ 报文
    #    的 `plans` 里没有这两张单（AC-005 的「不静默落位到非法巷道」在结构上成立）。
    #    按单号取而不是按下标取：`failures` 是队列序，与提交序不是同一个序。
    assert result.outcomes == ()
    assert result.alerts == ()
    assert sorted(failure.job_order_id for failure in result.failures) == [
        "PO-01",
        "PO-02",
    ]
    for failure in result.failures:
        message = failure.message
        assert "四级降级链走尽" in message
        for label in DEGRADATION_TIER_LABELS:  # 四档都走过 ⇒ 不是漏判
            assert f"{label}无候选巷道" in message
        assert "无可行容量" not in message  # 成因是「没有这类巷」而不是「塞不下」
        assert "PENDING" in message  # 停留 PENDING ⇒ 可重试
        assert "plans" in message  # 不进 plans ⇒ 不静默落位
        assert "人工介入" in message

    # ② 缺 ABC ⇒ `abc` 因子降级（`16` §394：「ABC 因子降级不参与评分」）。
    abc = abc_factor(
        order_abc_class=None, material_abc_class=scenario.materials["M1"].abc_class
    )
    assert abc.is_degraded is True
    assert "ABC 分类未导入" in abc.degrade_reason

    # ③ 缺巷道-站台主数据 ⇒ `station` 因子降级，措辞与 `17` §10.1 的示例一致（`16` §394：
    #    该情形的处置与 ABC 缺失「一致」）。判据的粒度是**方案级**（一条候选巷缺行 ⇒
    #    全部候选一并降级），故问的不是 `station_factor` 而是 `station_degrade_reason`。
    station_weights = load_station_weights(session, warehouse_id=scenario.warehouse_id)
    assert station_weights == {}
    assert station_degrade_reason(aisles=["01", "02"], station_weights=station_weights) == (
        "巷道-站台主数据未导出"
    )
    assert station_factor(distance_weight=station_weights.get("01")).is_degraded is True

    # ④ 缺库存**不**降级：空档案取 0.00（三个读库存因子里 `existing` 是其中一个）。
    assert scenario.snapshot is not None
    snapshot = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)
    profile = snapshot.profile("M1")
    assert snapshot.snapshot_present is True
    assert profile.cross_aisle_count == 0
    existing = existing_factor(profile=profile, aisle="01", order_cells=10)
    assert existing.is_degraded is False
    assert existing.term is not None
    assert existing.term.value == 0.0


# --- 9.5 快照缺席：引擎不阻断（与端点的 409 是两层） ---------------------------


def test_an_absent_snapshot_degrades_the_three_inventory_factors_and_still_allocates(
    session: Session,
) -> None:
    """引擎拿到一个**缺席**的库存索引 ⇒ 三个库存因子降级、该单照常出方案（`design.md` D10 ②）。

    出库侧的红线是「快照缺失或过期 ⇒ 阻断」（`CLAUDE.md` §四），**入库不搬这一条**：入库的
    批量分配在缺快照时照常出方案、每一处缺席都留下可读的痕迹（`14` §3.5 / `19` 的四类空输入）。
    本用例钉的就是这一半 —— 它与端点那道 409 关口**并存**（8.x 的
    `test_a_missing_snapshot_blocks_the_batch` 是那一半），不是二选一。

    ## 「无 `Snapshot` 行」在库层面做不到「照常完成」，故本节验的是**引擎的入参缺席**

    这不是口径松动，是数据模型的硬约束：`AisleCap.snapshot_id` 是**非空外键**
    （`app/models/linkage.py`），**库层面一版快照都没有 ⇒ 也一定没有 cap 行** ⇒ 候选集为空
    ⇒ 每张单都四级走尽。那个形态归 9.3（它正是「四类输入全空」里的第一类），端点上则由 8.x
    的 409 闸住。所以 9.5 能在引擎层验、且**只能在引擎层验**的那句话是：**喂给它一个缺席的
    库存索引，它照常分配、只在理由里标降级**。入口是 `design.md` D10 ② 点名的那个调用
    （`load_snapshot_index(session, snapshot_id=None)` ⇒ `SnapshotIndex.absent`），本用例真的
    走它（`_allocate(..., snapshot_id=None)`），不手搓索引对象。

    造数因此保留**快照行 + cap 行**（cap 是候选集的构成条件，拿掉它测的就成了 9.3），只让
    索引那一项缺席 —— 这两个入参在引擎签名里本就是**两项**（`state` 与 `snapshot`），用例把
    它们的差异摆出来，而不是让一个「库里什么都没有」的场景去掩盖它。

    ## 四条断言各钉的是哪件事

    | 断言 | 钉的是 |
    |---|---|
    | `failures == ()`、单子有 `outcome` | **不阻断**：没有异常、没有失败（出库口径没被搬过来） |
    | `aisles == ("01",)`（档 0 首选） | 缺席的索引**不改变选道**：降级是「这个因子不参与」，不是「降级链下探」 |
    | `degraded is False` / `degrade_reason is None` | **方案级**没有降级 —— 两条通道不混用（D8） |
    | `factor_degraded` 恰为三个库存因子、文案含「无库存快照」 | **因子级**降级被记下且只有它（「降级不静默」） |

    加一条：分解的键集里**没有**降级因子（D9 第 1 条）—— 若它们留在分解里，操作员会看到
    三个值为空的因子，以为它们参与了打分。

    **渲染那一半不在这里验**：`payload_json.factor_degraded` 由 `reasons.build_reason_payload`
    **逐字取用** `outcome.factor_degraded`（7.1 的测试已钉），本节验的是它的唯一来源。
    """
    scenario = make_scenario(
        session,
        aisles=[
            AisleSpec("01", cap_total=10, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec("MOK", abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code="MOK",
                qty=10,
                abc_class="A",
                batch_no="B26090801",
            )
        ],
    )
    result = _allocate(
        session,
        scenario,
        [_item("PO-01", "MOK", 10, material_abc_class=AbcClass.A, batch_no="B26090801")],
        snapshot_id=None,
    )

    # ① 不阻断：出了方案，且没有任何单进失败名单。
    (outcome,) = result.outcomes
    assert result.failures == ()
    assert result.alerts == ()
    assert outcome.item.job_order_id == "PO-01"

    # ② 选道不受影响：缺口只在库存那一项，容量与站台都齐备 ⇒ 首选档照旧。
    assert outcome.aisles == ("01",)
    assert outcome.stop_tier.label == "近站台"

    # ③ 方案级降级**没有**被触发（D8：两条通道不共用字段）。
    assert outcome.degraded is False
    assert outcome.degrade_reason is None

    # ④ 因子级降级恰好是三个读库存的因子，且写明「为什么没有值」。
    assert set(outcome.factor_degraded) == {"existing", "batch", "continuity"}
    for reason in outcome.factor_degraded.values():
        assert "无库存快照" in reason

    # ⑤ 降级因子不进分解（D9 第 1 条）：参与评分的恰为另外三个。
    assert set(outcome.breakdown["01"]) == {"abc", "cap", "station"}
    assert set(outcome.scores) == {"01"}
