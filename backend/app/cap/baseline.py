"""cap 基线全量重算（`aggregate_aisle_caps` / `establish_baseline`）。

事实来源：16-数据衔接与 cap 自维护设计 §6.1（cap 四项口径）/ §6.2（基线建立）、A.4
          17-数据模型设计 §3.4（AisleCap）、§3.2（Snapshot 追加式）、§10.5（cap 快照 JSON）
          openspec/changes/data-import/design.md D5（cap_physical 口径 + cap_reserved =
          cap_physical × 40% 固定预留带，仅近站台巷道）
          spec `data-import`「cap 基线全量重算」（重算失败回滚该批并提示重导）

## 四条口径（唯一一处实现，`reserved.py` 只读不重算）

  1. **cap_physical** = 巷道内**去重库位格数**（主数据到位前 = 快照库位去重格数近似，D5）。
  2. **cap_total**    = cap_physical − 已占格数。
  3. **cap_reserved** = cap_physical × 40%（固定预留带，**仅近站台巷道非零**）。
  4. **cap_usable**   = max(cap_total − cap_reserved, 0)（非 A 类可用）。

## 已占格数的占位（本 change 的已知退化，D14 确认后单列修正）

「已占格数」本 change 以「有库存的库位去重格数」近似（design.md 风险表）。快照里每行
qty > 0（`InventoryItem` 的 `qty_positive` CHECK），故「有库存库位去重格数」恒等于
「库位去重格数」→ **cap_total 恒为 0**。这是占位不是结论：板-格换算（D14）确认后，
`aggregate_aisle_caps` 里的 `occupied_cells` 改为 `Σ to_occupied_cells(qty)`，cap_total
随之不再恒为 0。占位落在这里、不落在调用方，是为了让「改一处就能修正」成立。

## 建立基线的失败契约（无 `IMPORTED → FAILED` 回边）

`establish_baseline` 的入口守卫是 `assert_import_transition(IMPORTED, BASELINE)`。重算
失败（写快照 / 库存 / cap 时撞 DB 约束或业务校验）→ savepoint 整体回滚、`expire` 会话
回读数据库、**原样上抛**：状态机没有 `IMPORTED → FAILED` 的边（16 §3.1），会话停在
`IMPORTED`，由调用方（3.2 的执行编排）据此提示「重算异常，请修正后重导」。这与
`confirm.py` 的「执行失败回 `PLANNED`」不同 —— 那里有回边可退，这里没有可退的状态。

## 与 `increment.py` 的分工

全量重算（本模块）写 **AisleCap + Snapshot + InventoryItem**；事务内增量（`increment.py`）
只改 **InventoryItem**、靠台账同事务更新。两者互不调用，交集是「cap 口径」这条常量事实，
而非任何一行共享代码。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import ImportStatus
from app.core.errors import DomainError
from app.core.import_state import assert_import_transition
from app.models.base import utcnow
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

__all__ = [
    "DEFAULT_RESERVE_RATIO",
    "aggregate_aisle_caps",
    "establish_baseline",
    "next_snapshot_version",
    "recompute_snapshot_caps",
]

#: 近站台预留带比例（16 §6.1 / design.md D5；与 `Settings.reserve_ratio` 同值）。
#: 这里是**可传参的默认值**而非读配置：纯函数要能被单测直接喂比例，不引入配置依赖。
DEFAULT_RESERVE_RATIO: float = 0.40


def next_snapshot_version(session: Session, *, warehouse_id: str) -> int:
    """下一个快照版本号 = 当前最大 `version_no` + 1（无历史则从 1 起，17 §3.2）。

    `version_no` 按仓库单调递增，`Snapshot` 的唯一约束 `(warehouse_id, version_no)`
    是最终防线；WAL 单写者下「读 max + 1」不会并发撞号。
    """
    current = session.scalar(
        select(func.max(Snapshot.version_no)).where(Snapshot.warehouse_id == warehouse_id)
    )
    return 1 if current is None else current + 1


def _reserved_cells(cap_physical: int, ratio: float) -> int:
    """`cap_physical × ratio`，十进制半值进位到整数。

    与 `scoring.py` 同一约定：业务舍入走 `Decimal` + `ROUND_HALF_UP`，不是内置 `round()`
    （后者是银行家舍入，`round(2.5) == 2`，会把 40% 预留带在奇数格上少算一格）。
    """
    return int(
        (Decimal(cap_physical) * Decimal(str(ratio))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def aggregate_aisle_caps(
    items: Iterable[InventoryItem],
    *,
    warehouse_id: str,
    snapshot_id: int,
    reserve_ratio: float = DEFAULT_RESERVE_RATIO,
    is_near_station: Mapping[str, bool | None] | None = None,
) -> list[AisleCap]:
    """按巷道（库位号 `[:2]`）聚合库存行为 `AisleCap`（**纯函数，无 IO**）。

    `is_near_station` 是巷道号 → 是否近站台的字典，缺键即 `None`（未导出，不得当
    `False` 用，16 §3.4）。`cap_reserved` 只在 `is_near_station is True` 时非零；
    `None` / `False` / 缺键统一 reserved = 0，但 `is_near_station` 列保留原值（NULL 落
    NULL）—— 三态在列上不可折叠，`reserved.py` 靠它判断非 A 类可用性。

    返回按 `aisle_no` 升序排列（确定性：同样输入必得同样输出，CLAUDE.md §四）。
    """
    near = is_near_station or {}
    by_aisle: dict[str, set[str]] = {}
    for item in items:
        by_aisle.setdefault(item.location_code[:2], set()).add(item.location_code)

    caps: list[AisleCap] = []
    for aisle_no in sorted(by_aisle):
        locations = by_aisle[aisle_no]
        cap_physical = len(locations)
        # 已占格数占位：design.md 风险表「以有库存的库位去重格数近似」→ 与 cap_physical
        # 相等，cap_total 恒为 0。D14 确认板-格换算后，这里改 Σ to_occupied_cells(qty)。
        occupied_cells = len(locations)
        cap_total = cap_physical - occupied_cells
        cap_reserved = _reserved_cells(cap_physical, reserve_ratio) if near.get(aisle_no) is True else 0
        cap_usable = max(cap_total - cap_reserved, 0)
        caps.append(
            AisleCap(
                warehouse_id=warehouse_id,
                snapshot_id=snapshot_id,
                aisle_no=aisle_no,
                cap_physical=cap_physical,
                cap_total=cap_total,
                cap_reserved=cap_reserved,
                cap_usable=cap_usable,
                is_near_station=near.get(aisle_no),
            )
        )
    return caps


def _render_snapshot_version(snapshot_time) -> str:
    """`snapshot_version` = `snapshot_time` 的 `"%Y-%m-%dT%H:%M"` 渲染（D10 / 17 §10.5）。

    与 `reason.py` 的 `_SNAPSHOT_VERSION_PATTERN`、`allocate.py::_render_snapshot_version`
    同一格式 —— 这是报文层形状，不是 `Snapshot.version_no` 那个整数。
    """
    return snapshot_time.strftime("%Y-%m-%dT%H:%M")


def _cap_snapshot_json(snapshot: Snapshot, caps: list[AisleCap]) -> dict:
    """冻结 `cap_snapshot_json`（17 §10.5 的形状 + cap_physical 的 `physical` 键，D5）。

    `AisleCap` 行与这个 JSON 同事务写入，值必须一致（linkage.py：本列是「导入当时冻结
    的那一份视图」）。
    """
    return {
        "snapshot_version": _render_snapshot_version(snapshot.snapshot_time),
        "aisles": [
            {
                "aisle": cap.aisle_no,
                "physical": cap.cap_physical,
                "total": cap.cap_total,
                "reserved": cap.cap_reserved,
                "usable": cap.cap_usable,
                "near_station": cap.is_near_station,
            }
            for cap in caps
        ],
    }


def establish_baseline(
    session: Session,
    *,
    import_session: ImportSession,
    items: Iterable[InventoryItem],
    reserve_ratio: float = DEFAULT_RESERVE_RATIO,
    is_near_station: Mapping[str, bool | None] | None = None,
) -> Snapshot:
    """把一批 `IMPORTED` 会话的库存快照固化为新基线（`IMPORTED → BASELINE`，17 §3.2）。

    同一 savepoint 内原子完成：新 `Snapshot`（版本 max+1）→ 挂 `InventoryItem.snapshot_id`
    → 落 `AisleCap` → 回写会话三列（`status=BASELINE` / `snapshot_version_no` /
    `baselined_at`）→ 冻结 `cap_snapshot_json`。任一步失败 savepoint 整体回滚，不产生
    半成品基线（spec「重算异常回滚并提示重导」）。

    **入口守卫在 try 之外**（与 `confirm.py` 同构）：源状态非 `IMPORTED` 是「调用方过期」，
    直接 `StateConflict` 上抛、零写入，不进入 savepoint。重算失败（try 内）则回滚 +
    `expire` 会话回读数据库 + 原样上抛 —— 会话停在 `IMPORTED`（见模块 docstring 末节）。

    返回新 `Snapshot`（`version_no` 已定、`id` 已由 flush 生成）；不 `commit`，事务边界
    属于调用方。
    """
    # 入口守卫：IMPORTED → BASELINE 是状态机的唯一出边，非法源状态在此阻断、零写入。
    # **只判不写** —— 赋值必须留在 savepoint 内。若在此处改 `status`，`begin_nested()`
    # 的隐式 flush 会把它写进外层事务，失败回滚就撤不掉这个状态（本测试即栽在这里）。
    assert_import_transition(import_session.status, ImportStatus.BASELINE)
    version_no = next_snapshot_version(session, warehouse_id=import_session.warehouse_id)

    try:
        with session.begin_nested():
            snapshot = Snapshot(
                warehouse_id=import_session.warehouse_id,
                snapshot_time=import_session.data_time,
                version_no=version_no,
                import_session_id=import_session.id,
            )
            session.add(snapshot)
            session.flush()  # 先取 snapshot.id，供 items / caps 引用

            caps = aggregate_aisle_caps(
                items,
                warehouse_id=import_session.warehouse_id,
                snapshot_id=snapshot.id,
                reserve_ratio=reserve_ratio,
                is_near_station=is_near_station,
            )
            session.add_all(caps)

            for item in items:
                item.snapshot_id = snapshot.id
                session.add(item)

            snapshot.cap_snapshot_json = _cap_snapshot_json(snapshot, caps)

            import_session.status = ImportStatus.BASELINE
            import_session.snapshot_version_no = version_no
            import_session.baselined_at = utcnow()
            session.flush()
    except (DomainError, IntegrityError):
        # savepoint 已整体回滚（快照、库存行、cap 行、BASELINE 迁移都没了）。expire 让
        # 会话内存态回数据库读到 IMPORTED，再上抛 —— 无回边可退，失败交给调用方提示重导。
        session.expire(import_session)
        raise
    return snapshot


def recompute_snapshot_caps(
    session: Session,
    *,
    snapshot: Snapshot,
    reserve_ratio: float = DEFAULT_RESERVE_RATIO,
) -> list[AisleCap]:
    """对**已有快照**的 cap 就地重算（漂移校正，16 §6.4「以快照重算值为准」）。

    与 `establish_baseline` 的区别：后者「新建快照 + 建 cap」，本函数「同一快照已存在，
    把它的 `AisleCap` 按快照库位去重再算一遍」。用于校正被手工 / 程序改坏的 cap
    （漂移），**不产生新快照版本、不迁移会话状态** —— 快照是权威，重算只把派生值拉回
    权威口径。

    保留既有 `is_near_station` 三态（NULL 仍是 NULL）：那是巷道主数据属性，不随 cap
    数值重算而变 —— 只有 cap_physical/total/reserved/usable 由快照重算。

    旧 `AisleCap` 先 `delete` + `flush` 再写新行：新行与旧行同 `(warehouse_id,
    snapshot_id, aisle_no)`，SQLAlchemy 默认 INSERT 先于 DELETE 落库，会在唯一约束上
    撞自己。不 `commit`，事务边界属于调用方。
    """
    items = list(
        session.scalars(select(InventoryItem).where(InventoryItem.snapshot_id == snapshot.id))
    )
    near = {
        cap.aisle_no: cap.is_near_station
        for cap in session.scalars(select(AisleCap).where(AisleCap.snapshot_id == snapshot.id))
    }
    old_caps = list(
        session.scalars(select(AisleCap).where(AisleCap.snapshot_id == snapshot.id))
    )

    caps = aggregate_aisle_caps(
        items,
        warehouse_id=snapshot.warehouse_id,
        snapshot_id=snapshot.id,
        reserve_ratio=reserve_ratio,
        is_near_station=near,
    )

    for old in old_caps:
        session.delete(old)
    session.flush()  # 先清旧行，再写新行（避免唯一约束撞自己，见 docstring）。

    session.add_all(caps)
    snapshot.cap_snapshot_json = _cap_snapshot_json(snapshot, caps)
    session.flush()
    return caps
