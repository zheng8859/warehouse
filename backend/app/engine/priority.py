"""队列优先级排序：未来 N 天出库量（归一化）+ ABC 等级分 + 既有集中度增益，三项加权求和。

事实来源：`14` §3.2（排序键的三项加法 + 「ABC 只分三档、**同档内不排序**」）、
          §6.2（缺数据 ⇒ 排序退化为仅按 ABC，**标注降级排序**）
          `17` §10.1（`priority` 的示例取值与字段语义 —— 三项分量 + `score` 的域）
          `openspec/changes/recommendation-engine/design.md` D2（全序与定点比较）、
          D14（待确认默认值：三项等权 + 未来 N 天）、D18（三项的归一化口径、降级作用域）
          `tasks.md` 4.1（三项加法）、4.2（排序降级）、4.3（全序兜底）

本文件按任务书分三段落地，**三段已落齐**：**三项取数 + 加权求和**（4.1）→ **排序降级**
（4.2，缺出库量 ⇒ 整批退化为仅按 ABC）→ **全序兜底**（4.3，`(priority 降序, job_order_id 升序)`）。

## 三项的「归一化」指每一格都落在 `[0,1]`，不是把总分缩到 `[0,1]`

`14` §3.2 的式子里，三项各自带「(归一化)」的标注 —— 归一化的是**分量**，式子是它们的
加权和。这样三项才可加：`23` 的既有库存、`ABC` 的等级分、出库量的箱数若直接相加，就是在
加不同单位（`17` §10.1 已把每个归一化值背后的分子 / 分母写进 `note`，供人复核）。

## 为什么 `score` 不做两位数量化（与 `scores` 有意不同）

`17` §10.1 的两位小数 + 十进制半值进位那一套只写给 `scores`（那是**同一次分配内**可比、
可复算的分值口径）。把 `priority` 也量化到两位，会让「同档内按出库量降序」在归一化差
< 0.005 的两单上**并成一档**，落到 4.3 的 `job_order_id` 兜底 —— 而用出库量把同档拆开
正是 `14` §3.2 要它做的事（`1.42` 那条示例值的订正也记在这里：示例值按三项等权为 `0.70`，
它是**展示**到两位的结果，原值是按 `[0,1]` 三项加权不该超过 `1` 的）。量化在这里是丢信息。

## 为什么权重按「权重和」归一化

业务方给的系数很可能是 `(5, 3, 2)` 这类**相对值**而不是和为 `1` 的绝对值（`14` §3.2 只给
符号 `w1 / w2 / w3`）。按权重和缩放是纯线性的：**不改次序**，却让 `score` 的域恒为 `[0,1]`、
让不同系数下的分值仍可比 —— 与 `scoring.score_aisles` 的分母重新归一化是同一手法
（`17` §10.1 的可追溯恒等式也在那里）。系数到齐后只改 `DEFAULT_PRIORITY_WEIGHTS` 一处。

## 为什么 `abc` 分量不直接复用 `abc_factor` 的返回值

两侧对「**没有 ABC**」的处置不同，不能互换返回值：评分侧在那里**降级**（该因子不进分母，
写进 `factor_degraded`），排序侧必须给出一个数（`PriorityTerms.abc` 是必填的 `[0,1]`），
而「整批的数据缺失」走的是**另一条**通道（`priority.degraded`，4.2）。

但等级的**表**必须复用（`ABC_GRADE` / `ABC_GRADE_MAX`）：`factors.py` 的模块 docstring 已
点名两处同套分数（`17` §10.1 的示例里两处同为 `A → 1.00`），各写一份就会分叉 —— 分叉的
后果是同一张单在「谁先挑」与「靠近站台」两件事上得到不同的 ABC 档。

## 为什么降级只换一组系数，不另开一条算式

`14` §6.2 的退化是「排序退化为仅按 ABC」—— 本文件把这一句落成**系数的另一种取值**
（`ABC_ONLY_WEIGHTS = (0, 1, 0)`），仍走 `score_priority` 的同一段算术。这样 `17` §10.1
的字段语义（`score` 是三项的加权和）在降级态下依然成立，不必为它单开一条分支；反过来说，
降级后 `score` 恒等于 `terms.abc` 是一眼可复核的。

## 为什么降级的作用域是**整批**，而不是出事的那几条

一单缺数据就只降它自己，会造出**两种刻度混排**的队列：有数据的按三项排（域 `[0,1]`）、
没数据的只按 ABC（域也是 `[0,1]`，但含义完全不同），两者相加比较没有意义 —— 出来的序
既不是三项序也不是 ABC 序。故任一队列项缺数据 ⇒ 本批**全部**队列项按同一条降级规则算，
每条都带 `degraded` 与同一个 `degrade_reason`。这与 `station` 因子「一条候选巷缺行 ⇒
全候选一并降级」（D16）是同一条论证，也正因如此 `ABC_ONLY_WEIGHTS` 是**批级**的常量、
不随队列项变。

**ABC 未派生**（单据侧与物料侧都空）走同一条路（D18 的实现期补记）：`abc_term` 对未知档
返回 `0.00`，若只把出事的那几条计零，同批又会一半按真实等级、一半按 `0.00` 排 —— 同样是
混刻度。故任一队列项缺 ABC ⇒ 该分量在**全部**队列项上计 `0.00`，权重取余下两项
（`NO_ABC_WEIGHTS`，按权重和重新归一化 —— 与 `score_aisles` 丢弃降级因子的手法一致）。
代价是这一项对本批无信息，而那正是「数据没到」的真相；`degraded` 与原因把它说出来，
免得 `0.00` 被当成一个算出来的值。

`terms` 记的是**事实**（这一项的数据有没有、是多少），`score` 记的是**本批实际用的排序键**：
两者在部分缺失时故意不一致（把已有的真实值抹成 `0.00` 去凑一个自洽的等式，会掩盖「这份
数据其实有」，比不一致更糟），差异由 `degrade_reason` 解释 —— 那正是留给复核者的入口。

## 为什么兜底要一路兜到单据号（而不是靠稳定排序）

`(priority 降序, job_order_id 升序)` 是一条**全序**：任何两条队列项都有确定的先后。少了后
一项，同分项的次序就由**入参序**（= 数据库行序）决定 —— 稳定排序看着「稳定」，实际是把一个
未定义的遍历序（D2 要显式给出的正是它）当成了排序依据：两次分配若取数的行序不同，推荐序
就变，而现场只会看到「推荐怎么变了」却查不出原因（「同样输入必得同样输出」）。

单号按**文本**升序比较：单号是文本（`17` §10.1 的示例形态 `JOB-20260908-001` 是零填充的，
文本序即数值序），解析成数字会在非数字单号上抛错或静默给出**另一**种序。降级批同样走这条
兜底 —— `14` §6.2 的降级只换排序键，不放松「顺序确定」。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.enums import AbcClass
from app.engine.factors import ABC_GRADE, ABC_GRADE_MAX
from app.schemas.reason import PriorityPayload, PriorityTerms

__all__ = [
    "ABC_ONLY_WEIGHTS",
    "DEFAULT_OUTBOUND_WINDOW_DAYS",
    "DEFAULT_PRIORITY_WEIGHTS",
    "NO_ABC_WEIGHTS",
    "QueueItem",
    "RankedQueueItem",
    "abc_term",
    "build_priorities",
    "existing_gain_term",
    "order_queue",
    "outbound_term",
    "score_priority",
]

#: 三项系数的默认值（`design.md` D14「一处常量元组」）：**等权**。
#: 三项权重的取值 `14` §3.2 只给符号、未给数（属业务口径待定），故此处取等权；
#: 系数一经业务方确定，改这一处即可（阶段四的落点是配置列，届时它随配置版本取数，
#: 与 `WeightConfig` 一样的「版本 + 生效时间」形态）。
DEFAULT_PRIORITY_WEIGHTS: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)

#: **排序降级**后的系数（`14` §6.2「退化为仅按 ABC」）：出库量与集中度增益一并退出排序。
#:
#: 为什么集中度增益也跟着退（而不是「ABC + 出库量两项」）：`14` §6.2 说的退化对象是
#: **排序键**，不是「丢掉缺的那一项」。缺出库量的场景（成品清单·历史流水未导入）里，
#: 现场能站得住的判据只剩 ABC 档 —— 而「进不进既有巷道」是**收拢**目标用的第二判据，
#: 让散料凭它插到 A 类之前，与「同档内不排序」的补充说明直接冲突。
ABC_ONLY_WEIGHTS: tuple[float, float, float] = (0.0, 1.0, 0.0)

#: **ABC 未派生**时的系数（D18 的实现期补记）：该分量在全部队列项上计 `0.00`、权重退出
#: 归一化，余下两项按权重和重新归一化（`score_aisles` 丢弃降级因子的同一手法）。
#: 写成 `(1/3, 0, 1/3)` 而不是 `(0.5, 0, 0.5)`：相对值，`score_priority` 会按权重和归一化。
NO_ABC_WEIGHTS: tuple[float, float, float] = (1 / 3, 0.0, 1 / 3)

#: 「未来 **N** 天交货单出库量」的那个 **N** —— **3 天**（`design.md` D14 表格第 1 行；
#: 正本口径记在 `14` §3.2：业务方 2026-09-12 确认，按**自然日**计）。
#:
#: **N 只在这里定义一次**：它决定「排序主依据」把哪些交货单算进来，从而决定**谁先挑近站台** ——
#: 也正因如此，`14` §3.2 的式子、§3.1 的因子输入表、§6.2 的降级文案、§227 的验收条目与 PRD
#: US-04 都只写符号 `N`，取值只此一处。它是业务口径而非算法常数，故落成**可替换的单点**
#: （常量 → 配置列，与 `WeightConfig` 同样的「版本 + 生效时间」形态）；改这一处即改全链。
#:
#: **本阶段它不参与计算**：阶段三没有真实数据（导入管线属阶段四），聚合好的 `outbound_qty`
#: 由调用方给出，本常量此处只作**唯一定义点**存在。它与 `QueueItem.outbound_qty` 是同一条链的
#: 两端：取数窗口定不出来 ⇒ 聚合给不出数 ⇒ 传 `None` ⇒ 4.2 的整批降级（退化为仅按 ABC 排序并
#: 标注原因）。**降级表达的是「这份数据没拿到」，与 N 取多少无关** —— 故 N 从「留空」变为 3 天
#: 不改动降级链：那条路仍由调用侧传不传得出数决定。
#:
#: 仍待确认的默认值（三项权重系数、换算恒等、分档阈值与溢出区）的集中清单与 `TODO` 在
#: `app/engine/__init__.py` 末尾 —— 放在那里是为了 `grep` 一次就得到**全量**欠账，而不是在
#: 四个模块的注释里各翻一遍。
DEFAULT_OUTBOUND_WINDOW_DAYS: int = 3


@dataclass(frozen=True)
class QueueItem:
    """排一条队列项所需的**事实** —— 取数在调用侧（D1：本模块不读会话、不读快照）。

    字段就是 `14` §3.2 三项各自的原始量，外加单据号（出参按它索引、4.3 用它兜底定序）。
    三项取数为什么这么切：`abc_term` 的入参形态与 `factors.abc_factor` 一致，出库量与既有
    巷道数则分别是**已经聚合好的**一个整数 —— 聚合（按料号汇总交货单、数快照里的巷道）
    属数据衔接层，本模块只做算术，才能不依赖会话就测得动。

    `outbound_qty` 的 `None` 与 `0` **严格不同**：`0` 是「该料未来 N 天没有交货单」（数据在、
    值为零，这一项对序无信息），`None` 是「这份数据没拿到」（成品清单·历史流水未导入，
    触发 4.2 的整批降级）。把两者混成一个 `0` 会让降级永不触发 —— 而不标降级的「仅按
    ABC 序」在现场看起来与「有数据但都很小」一模一样。
    """

    job_order_id: str
    #: 单据侧与物料侧的 ABC（`abc_term` 的同一对入参：单据优先、物料兜底，`16` A.4）。
    order_abc_class: AbcClass | None
    material_abc_class: AbcClass | None
    #: 未来 N 天交货单出库量。`None` ⇒ 数据缺失（触发整批降级）。N 的唯一定义点是本模块的
    #: `DEFAULT_OUTBOUND_WINDOW_DAYS`（现为 3 天，`design.md` D14 第 1 行 / `14` §3.2）；
    #: 聚合这一步属数据衔接层（阶段四），故本文件只收聚合好的整数，文案沿用 `14` §6.2 的术语。
    outbound_qty: int | None
    #: 该物料在**这一版快照**里板数 > 0 的巷道数（`factors.InventoryProfile.cross_aisle_count`，
    #: 「分配前」的既有值）。空档案取 `0` —— 与 `existing` 因子的「无既有库存」同源，不是降级。
    existing_aisle_count: int


def build_priorities(items: Sequence[QueueItem]) -> dict[str, PriorityPayload]:
    """本批全部队列项的优先级（含排序降级），键 = `job_order_id`。

    降级的判据与作用域见模块 docstring 与 D18：任一队列项缺出库量 ⇒ 整批退化为仅按 ABC；
    任一队列项缺 ABC ⇒ 该项分量在整批计 `0.00`。两种情形都**不中断**（降级不是异常，
    `CLAUDE.md` 第四节）、每条队列项都带 `degraded=True` 与同一个原因、队列项一条不少。

    出参是**表**而不是列表：调用侧（分配器）按单据号取优先级，且同一批里可能多家物料共用
    一条队列项之外的上下文；顺序不在这里定（4.3 的 `(priority 降序, job_order_id 升序)`）。
    重复的单据号当场报错 —— 静默覆盖会让**一张单从所有方案里消失**，而队列看着完好。
    """
    if not items:
        return {}
    duplicated = _duplicated_ids(items)
    if duplicated:
        raise ValueError(
            f"队列里出现重复的作业单号 {duplicated} —— 出参按单据号索引，静默覆盖会让"
            "其中一张单从所有方案里消失，而队列本身看不出问题"
        )

    missing_outbound = [item.job_order_id for item in items if item.outbound_qty is None]
    missing_abc = [
        item.job_order_id
        for item in items
        if item.order_abc_class is None and item.material_abc_class is None
    ]
    if missing_outbound:
        weights = ABC_ONLY_WEIGHTS
    elif missing_abc:
        weights = NO_ABC_WEIGHTS
    else:
        weights = DEFAULT_PRIORITY_WEIGHTS
    degraded = bool(missing_outbound or missing_abc)
    reason = _degrade_reason(
        total=len(items), missing_outbound=missing_outbound, missing_abc=missing_abc
    ) if degraded else None

    # 分母只在有数据的那些项上取；全批皆缺时 `batch_max = 0`（`outbound_term` 对它返回 0.00，
    # 而那几条队列项本就走 `None` 分支，不会用到这个分母）。
    batch_max = max(
        (item.outbound_qty for item in items if item.outbound_qty is not None), default=0
    )
    abc_zeroed = bool(missing_abc)
    payloads: dict[str, PriorityPayload] = {}
    for item in items:
        terms = PriorityTerms(
            outbound_qty=0.00
            if item.outbound_qty is None
            else outbound_term(qty=item.outbound_qty, batch_max=batch_max),
            abc=0.00
            if abc_zeroed
            else abc_term(
                order_abc_class=item.order_abc_class,
                material_abc_class=item.material_abc_class,
            ),
            existing_gain=existing_gain_term(
                existing_aisle_count=item.existing_aisle_count
            ),
        )
        payloads[item.job_order_id] = PriorityPayload(
            score=score_priority(terms, weights=weights),
            terms=terms,
            degraded=degraded,
            degrade_reason=reason,
        )
    return payloads


@dataclass(frozen=True)
class RankedQueueItem:
    """队列里的一条：**事实**（`QueueItem`）+ 本批算出的**优先级**（`PriorityPayload`）。

    成对返回的理由：分配器遍历队列时两样都要 —— 队列项告诉它这一单是什么，优先级既定次序、
    又是要写进推荐理由的那份证据（`17` §10.1 的 `priority`）。若让调用侧分两次取（先排序、
    再单独算理由用），两处的分就有了各自漂移的余地，而 `score` 是个像模像样的浮点数，
    错配了没人会发现。
    """

    item: QueueItem
    priority: PriorityPayload


def order_queue(items: Sequence[QueueItem]) -> list[RankedQueueItem]:
    """队列的**全序**：`(priority 降序, job_order_id 升序)`（D2 第 1 条 / 4.3）。

    这是「谁先挑稀缺容量」唯一的一处次序定义 —— `14` §3.2 的排序键由它落地，分配器（6.x）
    按它遍历，不再自己排一遍。降级批（4.2）走同一条兜底：降级只换排序键，不放松「顺序确定」。
    """
    priorities = build_priorities(items)
    return sorted(
        (
            RankedQueueItem(item=item, priority=priorities[item.job_order_id])
            for item in items
        ),
        key=lambda ranked: (-ranked.priority.score, ranked.item.job_order_id),
    )


def _duplicated_ids(items: Sequence[QueueItem]) -> list[str]:
    """出现两次以上的单据号（去重、保持首次出现的次序），空列表表示无重复。"""
    seen: set[str] = set()
    duplicated: list[str] = []
    for item in items:
        if item.job_order_id in seen and item.job_order_id not in duplicated:
            duplicated.append(item.job_order_id)
        seen.add(item.job_order_id)
    return duplicated


def _degrade_reason(
    *, total: int, missing_outbound: list[str], missing_abc: list[str]
) -> str:
    """降级原因（`17` §10.1 的 `priority.degrade_reason`）。

    两条要求同时压在文案上：**说得清**（`14` §6.2「标注降级排序」）与**能被用来判断去补
    哪份数据**（`factors.py` 的降级文案标准）。故原因里点名缺的是哪一份数据（成品清单·
    历史流水）与影响面（几条 / 共几条），并把「分配照常进行」写出来 —— 操作员看见
    `degraded` 时的第一反应会是「这个批次是不是废了」。

    全缺与部分缺分开措辞：全缺意味着**整份数据没来**（补导入即可），部分缺则是本批数据
    不全（要去看那几条队列项），两者的处置不同。
    """
    parts: list[str] = []
    if missing_outbound:
        if len(missing_outbound) == total:
            parts.append(
                "本批队列缺未来 N 天交货单出库量（成品清单·历史流水未导入）"
            )
        else:
            parts.append(
                f"本批 {len(missing_outbound)}/{total} 条队列项缺未来 N 天交货单出库量"
            )
        parts.append("排序退化为仅按 ABC 等级")
    if missing_abc:
        if len(missing_abc) == total:
            parts.append("本批队列的 ABC 等级未派生（成品清单未聚合）")
        else:
            parts.append(f"本批 {len(missing_abc)}/{total} 条队列项的 ABC 等级未派生")
        parts.append("该分量在全部队列项上计 0.00，本批无法分辨 A/B/C")
    return "；".join(parts) + "。分配照常进行，不中断"


def abc_term(
    *, order_abc_class: AbcClass | None, material_abc_class: AbcClass | None
) -> float:
    """`abc` 分量 = 等级分 ÷ 3（A = `1.00` / B = `0.67` / C = `0.33`）。

    入参形态与 `factors.abc_factor` 一致、分辨顺序也一致（**单据侧优先、物料侧兜底**，
    `16` A.4）：单据上的 ABC 是入队时抄下来的，物料主数据上的是成品清单聚合的结果；
    两者不一致时以单据为准 —— 它是这一单的当次事实。

    **两侧皆空 ⇒ `0.00`**（`design.md` D18），与评分侧的「降级」有意不同：排序侧要一个数，
    而 `0.00` 取的是**保守侧** —— 未知档不排到 C 类之前。它与 C 类（`1/3`）仍分得开，
    故「ABC 未派生」不会在理由里长得像「C 类」（`Material.abc_class` 的注释：视为未知，
    不得当 C 类静默放行）；「未知不得占用预留池」那条另由 D7 的 ABC 分支把守，两处各管一段。

    入参是**枚举成员**（DB 侧的 `enum_column` 给的就是成员）。枚举外的取值会在这里
    `KeyError` —— 不静默、也不在本模块再写一份「枚举不得增删」的判据（那份在 `factors.py`）。
    """
    source = order_abc_class if order_abc_class is not None else material_abc_class
    if source is None:
        return 0.0
    return ABC_GRADE[source] / ABC_GRADE_MAX


def outbound_term(*, qty: int, batch_max: int) -> float:
    """`outbound_qty` 分量 = 本单该物料的未来 N 天出库量 ÷ **本批队列中该量的最大值**。

    分母取本批最大值（`design.md` D18）：`14` §3.2 没给分母、也没给 N（N 在 D14 登记），
    而**队列内最大值是唯一不需要编造常数的分母**（D5 的同一条原则 —— 编造阈值比留空更糟）。
    同一批共享一个分母 ⇒ 「同档内按出库量降序」的序不变；跨批不可比 —— 这与 `priority`
    的用途一致：它是**当日队列内**的「谁先挑稀缺容量」。

    **`0` 是合法取值**（该料在 N 天内没有交货单）：「数据在、值全为 0」与「没有这份数据」
    不是一回事 —— 后者走 4.2 的排序降级，前者只是这一项对序无信息（`batch_max = 0` 即
    全批皆 0 的形态，返回 `0.00` 而不是除零）。

    两个越界情形当场报错，因为它们都**不会被下游发现**：负数会得到一个负分量（把这张单
    压到队尾，而队尾正是「最后拿容量」的位置），`qty > batch_max` 说明分母不是本批最大值
    （那会让比值越出 `[0,1]`，契约在 `PriorityTerms` 那一层才拦得住）。
    """
    if qty < 0 or batch_max < 0:
        raise ValueError(
            f"出库量不得为负（收到 {qty} ／本批最大值 {batch_max}）——负数会算出一个负分量，"
            "把这张单静默压到队尾；`16` §171 把「数量 > 0」列为导入期必须阻断的口径异常"
        )
    if batch_max == 0:
        return 0.0
    if qty > batch_max:
        raise ValueError(
            f"出库量 {qty} 大于本批最大值 {batch_max} —— 分母必须是**本批队列中该量的"
            "最大值**（非本批最大值的分母会让比值越出 [0,1]，而已归一化是契约）"
        )
    return qty / batch_max


def existing_gain_term(*, existing_aisle_count: int) -> float:
    """`existing_gain` 分量 = 该物料在快照里已有占用巷道 ⇒ `1.00`，否则 `0.00`（二值）。

    口径见 `design.md` D18：`14` §3.2 的「并回既有巷道**可降**跨巷道数的幅度」，能归一化的
    读法只有这两种 —— **能并回**（本单落进既有巷道，该物料的跨巷道数不因本单增加）/ 不能。
    比「能并回」更细的读法都要编造口径：`1/(k+1)` 对 k **递减**（越散的物料越先挑，与
    「收拢」恰好反向 —— `continuity` 因子用的是 `1/跨巷道数`，那是**评分**侧的刻度）；
    `k/(k+1)` 则是又一个未定的口径。形态与 `batch` 因子的二值同类。

    `existing_aisle_count` 由取数侧给：`factors.InventoryProfile.cross_aisle_count`
    （= 该料号在**这一版快照**里板数 > 0 的巷道数，「分配前」的既有值）—— 空档案（该料
    在库里还没有库存）取 `0.00`，与 `existing` 因子的「无既有库存」同源，不是降级。
    """
    if existing_aisle_count < 0:
        raise ValueError(
            f"既有占用巷道数不得为负（收到 {existing_aisle_count}）——它按 `len()` 取得，"
            "出现负数只能是取数侧的缺陷，不该静默当成 0.00"
        )
    return 1.0 if existing_aisle_count > 0 else 0.0


def score_priority(
    terms: PriorityTerms, *, weights: tuple[float, float, float] = DEFAULT_PRIORITY_WEIGHTS
) -> float:
    """三项加权求和 ÷ 权重和（`14` §3.2；分母的理由见模块 docstring）。

    入参是已经归一化好的 `PriorityTerms`（契约层已把三项各自钉在 `[0,1]`）—— 本函数**不**
    重新取数、也不夹取：越界是取数侧的缺陷，在契约层当场失败比在这里被抹平好。

    求和的**位置对应**按 `weights` 的分量声明，不按 `terms` 的字段序：`terms` 是带名字的
    Pydantic 模型，字段序不是契约（改字段序不该改分），而系数的位置是 —— 它由
    `DEFAULT_PRIORITY_WEIGHTS` 的元组序钉住，并与 `17` §10.1 的 `terms` 列举序一致。

    越界系数（项数不对 / 含负数 / 和为零）当场报错：三种都是**配置**缺陷（阶段四从配置列来），
    而它们算出来的分仍是个像模像样的浮点数 —— 除了这里没有第二处会发现。「排序降级」不在此处
    （4.2，且作用于整批），本函数对「三项齐全」的队列项一视同仁。
    """
    if len(weights) != 3:
        raise ValueError(
            f"权重必须恰为三项（收到 {len(weights)} 项）——依次是未来 N 天出库量、"
            "ABC 等级分、既有集中度增益的系数"
        )
    if any(weight < 0 for weight in weights):
        raise ValueError(
            f"权重不得为负（收到 {weights}）——负系数会把「分高者先挑」翻过来，"
            "而它算出来的分仍是个像模像样的浮点数"
        )
    total = sum(weights)
    if total <= 0:
        raise ValueError(
            f"三项权重之和必须 > 0（收到 {weights}）——三项全零时无从归一化，"
            "而它意味着这条队列的序没有任何依据"
        )
    outbound_w, abc_w, gain_w = weights
    weighted = (
        outbound_w * terms.outbound_qty
        + abc_w * terms.abc
        + gain_w * terms.existing_gain
    )
    return weighted / total
