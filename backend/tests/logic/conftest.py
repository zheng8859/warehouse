"""阶段三（推荐引擎）逻辑测试的造数夹具：`make_scenario()` 与四个输入规格。

事实来源：`openspec/changes/recommendation-engine/design.md` 开篇「本阶段无真实数据 ⇒
          造数单测」（四类输入 = cap 基线 / 既有库位分布 / ABC 分类 / 巷道-站台主数据）、
          `16` §394 / `A.4`、`17` §2.1~§2.3（巷道 / 物料）、§3.1~§3.4（导入会话 / 快照 /
          库存 / cap）、§4.1（作业单）、§七（两类配置）、§10.1（默认权重取示例值）
          `tasks.md` 1.3（本夹具的任务书）、9.3（四类输入全空的端到端形态）

库与会话复用 `tests/conftest.py` 的 `session` 夹具（内存 SQLite、每用例整体回滚、
`isolation_level = None` + 显式 BEGIN）—— 本文件**不另起一套**：那两处 PRAGMA 与
事务边界的写法都有成因（见那个文件的 docstring），复制一份等于把成因也复制成两份。

## 为什么是「函数 + 规格对象」，不是「夹具 + 裸字典」

- **函数而不是夹具**：`make_scenario(session, …)` 收的是会话本身。同一份造数因此既能给
  `tests/logic/` 用，也能给 `tests/api/` 用 —— 后者的会话来自 `dependency_overrides`
  而不是 `tests/conftest.py` 的 `session` 夹具，绑死在一个夹具上就复用不了。
- **规格对象而不是裸字典**：一个场景最多要摆四类输入、每类 3~8 个字段。裸字典的可读性
  只在调用点成立 —— `{"cap_total": 80}` 与 `{"cap_usable": 80}` 长得一样，而后者会让
  cap 因子静默按 0 算（`cap_usable` 根本不是构造参数）。规格对象把字段名钉在一次定义上。

## 三条自我约束

1. **不做任何自动推导。** 不给 `JobOrderSpec` 自动补 `Material` 行、不按 `qty` 反推 cap。
   本阶段的头号验收形态就是「四类输入缺失时如何降级」（`design.md` 开篇 / `tasks.md` 9.3），
   自动补齐会把「缺料号主数据」的用例悄悄变成「有料号主数据」—— 而它照样是绿的。
2. **配置参数的 `None` 一律是「不建这一行」**，不是「用默认值」。取默认值由**省略**该参数
   表达（`weights` 的默认值即 `17` §10.1 的示例权重）。这样 2.3 的「无生效版本 ⇒ 阻断」
   与正常路径共用同一个入口，不必另写一个「只建一半」的函数。
3. **同一用例内造第二个场景时，只造「快照级」的行。** 每类行都有仓库内唯一键，但层级不同：
   `Material` / `Aisle` / `WeightConfig` / `CapacityConfig` 是**仓库级**的（同仓一份），
   `Snapshot` / `AisleCap` / `InventoryItem` 挂在**快照**上（每版一份）。想再补一份快照时，
   仓库级的那些要显式传空（`materials=()`、`weights=None`、`capacity=None`）—— 默认值会
   再造一套同键的行：轻则撞唯一键，重则留下两条同版本号的配置，让「当前生效版本」的选取
   变成一件看插入顺序的事（后者更糟：它不报错）。多快照用例的写法见 `test_factors.py`
   的快照隔离那条。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import AbcClass, JobStatus, JobType
from app.models.configuration import WEIGHT_FACTORS, CapacityConfig, WeightConfig
from app.models.job import JobOrder
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot
from app.models.master_data import Aisle, AisleStation, Material

#: 首期单厂（`16` A.1「仓库号」直映射 `warehouse_id`；`17` §11 数据隔离）。
DEFAULT_WAREHOUSE_ID = "GTJ10036"

#: 快照时点：取 `17` §10.7 示例里的 `2026-09-08T00:00`。用同一个值而不是另挑一个，
#: 是因为报文层的 `snapshot_version` 正是它的 `"%Y-%m-%dT%H:%M"` 渲染 ——
#: 造数与报文对照时，中间没有一道「换算」需要解释。
DEFAULT_SNAPSHOT_TIME = datetime(2026, 9, 8, 0, 0)

#: 配置行的生效时间：远早于任何会被传进引擎的 `now`，故默认造数下「当前生效版本」恒存在。
#: 挑 2020 而不是「比快照早一天」：本夹具不假设用例把 `now` 定在哪天。
DEFAULT_CONFIG_EFFECTIVE_AT = datetime(2020, 1, 1)

#: 六项权重的默认值 = `17` §10.1 的示例（和为 1.00）。用文档的示例值而不是自拟一组：
#: 示例值有出处可对照，自拟值只是本文件里的一个约定。
#: 用 `zip(..., strict=True)` 与 `WEIGHT_FACTORS` 对齐 —— 因子增删时这里**当场**报错，
#: 而不是少一项、等到某条用例算出个偏低的分数才被发现。
DEFAULT_WEIGHTS: Mapping[str, float] = dict(
    zip(WEIGHT_FACTORS, (0.25, 0.20, 0.15, 0.20, 0.10, 0.10), strict=True)
)

#: `capacity` 参数的默认值：**建一行、全用模型默认值**（40% / 18:00 / N=5 / ≤5 / ≤3 / 1%，`16` §353~356）。
#: 与 `weights=None` 的「不建行」相对：容量配置的每一项在模型上都有默认值，故「建一行」
#: 不需要任何参数，而「不建」必须由调用方显式说出来。
DEFAULT_CAPACITY: Mapping[str, Any] = {}


@dataclass(frozen=True)
class AisleSpec:
    """一条巷道的造数规格：主数据行 +（可选）cap 行 +（可选）站台主数据行。

    三者放在一个规格里而不是三个参数，是因为它们**逐条对应同一个 `aisle_no`**：
    分开传就得靠调用方保证三个集合的键一致，而键不一致的表现是「某巷道没有 cap」
    这种**看起来合法**的场景 —— 它会静默改变候选集，用例却仍是绿的。
    """

    aisle_no: str

    #: `None` ⇒ **不建 `AisleCap` 行**（巷道主数据存在、但没有 cap 基线）。
    #: 与 `0` 不是一回事：`0` 是「有这一行、容量为零」，`None` 是「没有这一行」。
    #: 3.2 的三判据里「cap 足够」在这两种形态下走的是不同的分支，造数必须能分别表达；
    #: 9.3 的「无 `AisleCap`」也正是后者。
    cap_total: int | None = 0

    #: 近站台预留池的分子（`14` §3.3：`cap_reserved = cap_total × 预留比例`）。
    #: 这里直接给绝对值而不是比例 —— 比例是 `CapacityConfig` 的口径，两份口径并存
    #: 会让「用例里的 cap_reserved 与配置对不上」变成一件说不清谁对的事。
    cap_reserved: int = 0

    #: `None` = 未导出（`17` §2.1 的「可空 = 权威值未到位」，不得当 `False` 用）。
    is_near_station: bool | None = None

    #: 给了就建 `AisleStation` 行 —— 它是 `station` 因子参与评分的**唯一**前提（2.2）。
    #: 不给即「巷道-站台主数据缺失」，六因子里的 station 走因子级降级。
    station_weight: float | None = None
    station_code: str = "站台 A"


def _as_enum(value: Any | None, enum_cls: type) -> Any | None:
    """把规格里的枚举取值归成枚举成员，`None` 原样返回。

    **这不是类型洁癖，是「造数行要长得像库里读回的行」。** 送进 ORM 的值原样留在
    实例上（`flush()` 不回读，`expire_on_commit=False` 也不作废），所以不做这一步时，
    用例拿到的 `abc_class` 是它自己传的那个 `"A"` 还是 `AbcClass.A`，取决于它传的是
    字符串还是成员 —— 同一份夹具，两种形态，而 `==` 恰好两种都能过。
    引擎从库里取数据时拿到的一律是枚举成员，夹具就该让用例在同一个形态上写断言。
    """
    return None if value is None else enum_cls(value)


@dataclass(frozen=True)
class MaterialSpec:
    """物料主数据（`17` §2.3）。

    `abc_class` 收 `str` 也收枚举：该列是 `sa.Enum(..., validate_strings=True)`（`base.py`），
    两种写法都会落到同一个取值上，而用例里写 `"A"` 比 `AbcClass.A` 少一层噪音。
    收进来的字符串由 `_as_enum` 归成枚举成员 —— 见那个函数的 docstring。
    """

    material_code: str
    abc_class: AbcClass | str | None = None
    material_name: str | None = None
    units_per_carton: int | None = None
    cartons_per_pallet: int | None = None


@dataclass(frozen=True)
class InventorySpec:
    """一行库存分布（`17` §3.3）。

    库位号按 6 位文本给（`010104`）—— 前导 0 不得丢，`16` §4.2 与 CLAUDE.md §七 都钉过；
    巷道由 `location_code[:2]` 派生，故库位号写错时错的不是一行库存，而是整条巷道因子。
    """

    location_code: str
    material_code: str
    batch_no: str
    qty: int
    material_name: str | None = None
    item_status: str = "合格"
    production_date: date | None = None


@dataclass(frozen=True)
class JobOrderSpec:
    """一条待分配作业单（`17` §4.1）。默认即入库、`PENDING` —— 阶段三只有入库队列。

    `abc_class` 与 `MaterialSpec.abc_class` **分开两个字段**：两者在库里各有一列，
    取值可以不同（单据上的 ABC 是入队时抄下来的、物料主数据上的会随成品清单重算），
    abc 因子读哪一个由 2.1 定 —— 在夹具里替那个决定预先合并，就把问题藏起来了。

    三个枚举字段（`job_type` / `abc_class` / `status`）收字符串，进库前由 `_as_enum`
    归一 —— 理由见那个函数。
    """

    order_no: str
    material_code: str
    qty: int
    #: 文本（`16` A 的行唯一键「单据号码 + 行号」，示例 `10`）—— 存整数会丢 `0010`。
    line_no: str = "10"
    job_type: JobType | str = JobType.INBOUND
    abc_class: AbcClass | str | None = None
    material_name: str | None = None
    batch_no: str | None = None
    status: JobStatus | str = JobStatus.PENDING


@dataclass(frozen=True)
class Scenario:
    """一次 `make_scenario()` 造出来的全部行。

    字段名一律**用表名/类名的蛇形**（`aisle_caps` / `job_orders`），不另起一套短名：
    用例里 `scenario.aisle_caps["01"]` 能直接对回 `AisleCap` 表，少一层「这个字段是哪个表」。
    映射的键是业务键（巷道号 / 料号），值是 ORM 行 —— 用例两头都要用得到。
    """

    warehouse_id: str
    snapshot: Snapshot | None
    aisles: dict[str, Aisle]
    aisle_caps: dict[str, AisleCap]
    stations: dict[str, AisleStation]
    materials: dict[str, Material]
    inventory: tuple[InventoryItem, ...]
    job_orders: tuple[JobOrder, ...]
    weight_configs: tuple[WeightConfig, ...]
    capacity_config: CapacityConfig | None


def make_scenario(
    session: Session,
    *,
    warehouse_id: str = DEFAULT_WAREHOUSE_ID,
    aisles: Sequence[AisleSpec] = (),
    materials: Sequence[MaterialSpec] = (),
    inventory: Sequence[InventorySpec] = (),
    job_orders: Sequence[JobOrderSpec] = (),
    weights: Mapping[str, float] | Sequence[Mapping[str, float]] | None = DEFAULT_WEIGHTS,
    capacity: Mapping[str, Any] | None = DEFAULT_CAPACITY,
    snapshot_time: datetime | None = DEFAULT_SNAPSHOT_TIME,
    snapshot_version_no: int = 1,
) -> Scenario:
    """按依赖链顺序（会话 → 快照 → 巷道 → 物料 → 库存 → 单据 → 配置）造一段场景。

    只 `flush()` **不 `commit()`**：本夹具的会话由 `tests/conftest.py` 接管事务
    （外层真事务 + `create_savepoint`），用例自己 commit 时提交的是保存点。
    这里替用例 commit 会在 9.4 的「整批回滚」用例里帮倒忙 —— 那条用例要断言的正是
    「回滚之后库里没有东西」，而夹具先把一半提交了就无从断言。

    参数口径（`None` 的两种含义都在 docstring 里，不在类型里）：

    - `weights`：给一个映射 = 建 1 版；给一串 = 逐版建（`version_no` 从 1 起递增），
      用于 2.3 的「两版并存取新版且旧版仍可回滚」；**给 `None` = 不建任何权重行**，
      即 D3 的「无生效版本 ⇒ 整批阻断」。
    - `capacity`：省略 = 建一行、全用模型默认值；给映射 = 建一行并覆盖列举的字段；
      **给 `None` = 不建容量配置行**。
    - `snapshot_time`：省略 = 建快照（时点为 `17` §10.7 的示例值）；
      **给 `None` = 不建快照**（9.5 的「快照缺失」形态）。快照是 cap 行与库存行的
      必填外键，故这两类非空时不允许 `None` —— 与其让用例撞一条 `NOT NULL` 的
      完整性错误，不如在这里把「你造的这个场景自相矛盾」说清楚。
    """
    has_cap_rows = any(spec.cap_total is not None for spec in aisles)
    if snapshot_time is None and (has_cap_rows or inventory):
        raise ValueError(
            "cap 行与库存行都挂在快照上（snapshot_id 是必填外键）——"
            "要造 aisles（其中 cap_total 非 None）或 inventory，就得给 snapshot_time；"
            "若本意是「无快照」形态（tasks.md 9.5），请把这两项一并去掉"
        )

    snapshot: Snapshot | None = None
    if snapshot_time is not None:
        # 会话号与批次号由「时点 + 快照版本」拼出：确定性、可读，且同一用例内造两个
        # 快照版本时不会撞上 session_no 的仓库内唯一约束（不给计数器，是为了不留
        # 一个「上一次造到几号」的隐式状态 —— 那会让调用顺序影响造数结果）。
        import_session = ImportSession(
            warehouse_id=warehouse_id,
            session_no=f"IMP-{snapshot_time:%Y%m%d}-{snapshot_version_no:02d}",
            import_batch_no=f"BAT-{snapshot_time:%Y%m%d}-{snapshot_version_no:02d}",
            data_time=snapshot_time,
        )
        session.add(import_session)
        session.flush()

        snapshot = Snapshot(
            warehouse_id=warehouse_id,
            snapshot_time=snapshot_time,
            version_no=snapshot_version_no,
            import_session_id=import_session.id,
        )
        session.add(snapshot)
        session.flush()

    aisle_rows: dict[str, Aisle] = {}
    cap_rows: dict[str, AisleCap] = {}
    station_rows: dict[str, AisleStation] = {}
    for aisle_spec in aisles:
        aisle_rows[aisle_spec.aisle_no] = Aisle(
            warehouse_id=warehouse_id,
            aisle_no=aisle_spec.aisle_no,
            is_near_station=aisle_spec.is_near_station,
        )
        if aisle_spec.cap_total is not None:
            cap_rows[aisle_spec.aisle_no] = AisleCap(
                warehouse_id=warehouse_id,
                snapshot_id=snapshot.id,
                aisle_no=aisle_spec.aisle_no,
                cap_total=aisle_spec.cap_total,
                cap_reserved=aisle_spec.cap_reserved,
                # 口径与 `17` §3.4 逐字一致（cap_total − cap_reserved）。写在这里而不是
                # 从规格对象上取，是为了让造数只接受「绝对值」一种输入 —— 见规格字段注释。
                cap_usable=aisle_spec.cap_total - aisle_spec.cap_reserved,
                is_near_station=aisle_spec.is_near_station,
            )
        if aisle_spec.station_weight is not None:
            station_rows[aisle_spec.aisle_no] = AisleStation(
                warehouse_id=warehouse_id,
                aisle_no=aisle_spec.aisle_no,
                station_code=aisle_spec.station_code,
                distance_weight=aisle_spec.station_weight,
            )
    session.add_all([*aisle_rows.values(), *cap_rows.values(), *station_rows.values()])
    session.flush()

    material_rows = {
        spec.material_code: Material(
            warehouse_id=warehouse_id,
            material_code=spec.material_code,
            material_name=spec.material_name,
            abc_class=_as_enum(spec.abc_class, AbcClass),
            units_per_carton=spec.units_per_carton,
            cartons_per_pallet=spec.cartons_per_pallet,
        )
        for spec in materials
    }
    session.add_all(material_rows.values())

    inventory_rows = [
        InventoryItem(
            warehouse_id=warehouse_id,
            snapshot_id=snapshot.id,
            location_code=spec.location_code,
            material_code=spec.material_code,
            material_name=spec.material_name,
            batch_no=spec.batch_no,
            production_date=spec.production_date,
            item_status=spec.item_status,
            qty=spec.qty,
            snapshot_time=snapshot_time,
        )
        for spec in inventory
    ]
    session.add_all(inventory_rows)

    job_order_rows = [
        JobOrder(
            warehouse_id=warehouse_id,
            order_no=spec.order_no,
            line_no=spec.line_no,
            job_type=_as_enum(spec.job_type, JobType),
            material_code=spec.material_code,
            material_name=spec.material_name,
            qty=spec.qty,
            abc_class=_as_enum(spec.abc_class, AbcClass),
            batch_no=spec.batch_no,
            status=_as_enum(spec.status, JobStatus),
        )
        for spec in job_orders
    ]
    session.add_all(job_order_rows)

    weight_specs = [weights] if isinstance(weights, Mapping) else list(weights or ())
    weight_rows: list[WeightConfig] = []
    for version_no, weight_spec in enumerate(weight_specs, start=1):
        if set(weight_spec) != set(WEIGHT_FACTORS):
            raise ValueError(
                f"第 {version_no} 版权重的键集必须恰为六因子（"
                f"缺 {sorted(set(WEIGHT_FACTORS) - set(weight_spec))}、"
                f"多 {sorted(set(weight_spec) - set(WEIGHT_FACTORS))}）—— "
                "不给「缺哪项就少算哪项」留余地：那会让权重和不等于 1 而无人察觉"
            )
        weight_rows.append(
            WeightConfig(
                warehouse_id=warehouse_id,
                version_no=version_no,
                effective_at=DEFAULT_CONFIG_EFFECTIVE_AT,
                **{f"weight_{name}": weight_spec[name] for name in WEIGHT_FACTORS},
            )
        )
    session.add_all(weight_rows)

    capacity_config: CapacityConfig | None = None
    if capacity is not None:
        capacity_config = CapacityConfig(
            warehouse_id=warehouse_id,
            version_no=1,
            effective_at=DEFAULT_CONFIG_EFFECTIVE_AT,
            **capacity,
        )
        session.add(capacity_config)

    session.flush()

    return Scenario(
        warehouse_id=warehouse_id,
        snapshot=snapshot,
        aisles=aisle_rows,
        aisle_caps=cap_rows,
        stations=station_rows,
        materials=material_rows,
        inventory=tuple(inventory_rows),
        job_orders=tuple(job_order_rows),
        weight_configs=tuple(weight_rows),
        capacity_config=capacity_config,
    )
