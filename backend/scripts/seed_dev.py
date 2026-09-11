"""开发种子数据：GTJ10036 单厂基础主数据与默认配置。

事实来源：openspec/changes/data-model-permission/tasks.md 1.5（入口）、§2.7 / §6（填充）
          17-数据模型设计 §2.1（巷道/库位示例）、§10.5（cap 快照示例的格数）
          16-数据衔接与 cap 自维护 §6.1（巷道总格数来源）
          PRD 8.3.1（真实样本：料号 / 品名 / 箱规板规 / 批号 / 生产日期）
          14-推荐引擎与评分流程设计 §2.2（近站台巷道 01/02）

## 填充进度

  - **已填**：主数据链 —— `Warehouse` / `Aisle` / `Location` / `Material` / `Batch`（任务 2.7）
  - **未填**：`AisleStation` —— 属**待业务方补充导出**的主数据（`16` §1.1），没有真实
    取值之前不编造距离权重：编出来的权重会让「站台就近」因子在开发环境"看起来能用"，
    而生产上该因子本该降级。空表是正确状态。
  - **未填**：衔接链（`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert`）
    —— 这五张表由**真实导入**产生（`16` §三），不是可种子的主数据。编一份快照会更糟：
    `cap_total` = 巷道总格数 − 已占格数，而「已占格数」要按「板-格」换算规则从数量折算
    （`16` A.4），该规则属 `CapacityConfig`（任务 §6，实体尚未建模）—— 种出来的 cap
    与库存明细必然互相矛盾，而矛盾的数据比空表更难排查（空表会让人去导入，
    矛盾的表会让人以为链路已经通了）。
  - **未填**：`WeightConfig` / `CapacityConfig`（任务 §6）—— 实体尚未建模

## 填充约定

  - 每组一个 `_seed_<group>(session, warehouse_id) -> list[str]`，返回可读的写入说明；
    `seed()` 按依赖链顺序调用它们 —— **父表先于子表**，否则 `foreign_keys=ON` 下必然
    报外键失败。
  - **幂等是硬要求**：`init_db.py` 可能被反复执行。按业务键先查后写，存在即跳过。
    重复运行不得报错，也不得产生重复行。
  - 只写 `settings.warehouse_code` 对应的单厂主数据 —— 首期就是 `GTJ10036`，
    不要顺手造第二个厂。
  - 取值**只取文档里出现过的**：库位号 `010104` / `010105` / `050102` / `211202`、
    料号 `3001234`、品名 `PET500 茉莉柚茶`、箱规板规 `15入纸箱` / `102/板`、
    批号 `GJP2571221`、生产日期 `2026-09-05` 全部来自 `17` §2.1 与 PRD 8.3.1 的真实样本；
    巷道总格数取 `17` §10.5 cap 快照示例的 120 / 200。**不编造规模**。
  - **不填派生字段**：`Material.abc_class` 由成品清单聚合自动补录（`16` A.4），
    `Aisle.is_near_station` 的近站台判定同样源自巷道主数据导出（`16` §6.1）——
    种子里只写文档明确给出的部分，近站台按 `14` §2.2 的 01/02 标 True，其余 False。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.master_data import Aisle, Batch, Location, Material, Warehouse

#: 近站台巷道：`14` §2.2「近站台巷道（01/02）合计剩 100 板」。
_NEAR_STATION_AISLES = {"01", "02"}

#: 巷道号 → 总格数。取自 `17` §10.5 cap 快照示例（01 为 120、21 为 200）；
#: 示例未覆盖的巷道沿用同量级数值，**仅为开发环境可跑**，不是真实物理格数。
_AISLES: tuple[tuple[str, int], ...] = (
    ("01", 120),
    ("02", 120),
    ("05", 100),
    ("21", 200),
    ("22", 200),
)

#: 库位号：文档出现过的四个（`17` §2.1、`16` §4.2）。
_LOCATIONS: tuple[str, ...] = ("010104", "010105", "050102", "211202")

#: 料号 / 品名 / 箱规 / 板规：PRD 8.3.1 的真实样本「PET500茉莉柚茶15入纸箱 102/板」。
_MATERIALS: tuple[tuple[str, str, int, int], ...] = (
    ("3001234", "PET500 茉莉柚茶", 15, 102),
)

#: 批号 / 生产日期：同一份样本（`16` 附录 A.1 / A.2 的示例取值）。
_BATCHES: tuple[tuple[str, date], ...] = (("GJP2571221", date(2026, 9, 5)),)


def _seed_warehouse(session: Session, warehouse_id: str) -> list[str]:
    """仓库行。首期单厂一行，`warehouse_id` 即仓库号（`17` §2.1）。"""
    exists = session.execute(
        select(Warehouse.id).where(Warehouse.warehouse_id == warehouse_id)
    ).scalar_one_or_none()
    if exists is not None:
        return []

    session.add(
        Warehouse(
            warehouse_id=warehouse_id,
            name="广州顶津成品库（开发种子）",
            # 工厂编码留空 = 与 warehouse_id 同值，见 master_data.py 的字段注释。
            plant_code=None,
        )
    )
    session.flush()
    return [f"仓库 {warehouse_id}"]


def _seed_aisles(session: Session, warehouse_id: str) -> list[str]:
    """巷道行。父表：仓库（本表不建外键，但依赖链顺序仍照此排）。"""
    written: list[str] = []
    for aisle_no, total_cells in _AISLES:
        exists = session.execute(
            select(Aisle.id).where(
                Aisle.warehouse_id == warehouse_id, Aisle.aisle_no == aisle_no
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        session.add(
            Aisle(
                warehouse_id=warehouse_id,
                aisle_no=aisle_no,
                total_cells=total_cells,
                is_near_station=aisle_no in _NEAR_STATION_AISLES,
            )
        )
        written.append(f"巷道 {aisle_no}")
    session.flush()
    return written


def _seed_locations(session: Session, warehouse_id: str) -> list[str]:
    """库位行。三个两位段由库位号切出 —— 库位的 CHECK 要求它们逐位相符。"""
    written: list[str] = []
    for location_code in _LOCATIONS:
        exists = session.execute(
            select(Location.id).where(
                Location.warehouse_id == warehouse_id,
                Location.location_code == location_code,
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        session.add(
            Location(
                warehouse_id=warehouse_id,
                location_code=location_code,
                aisle_no=location_code[:2],
                layer_column_no=location_code[2:4],
                cell_no=location_code[4:6],
                status=None,
            )
        )
        written.append(f"库位 {location_code}")
    session.flush()
    return written


def _seed_materials(session: Session, warehouse_id: str) -> list[str]:
    """物料行。`abc_class` 刻意留空 —— 它是成品清单导入触发的派生字段（`16` A.4）。"""
    written: list[str] = []
    for material_code, material_name, per_carton, per_pallet in _MATERIALS:
        exists = session.execute(
            select(Material.id).where(
                Material.warehouse_id == warehouse_id,
                Material.material_code == material_code,
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        session.add(
            Material(
                warehouse_id=warehouse_id,
                material_code=material_code,
                material_name=material_name,
                units_per_carton=per_carton,
                cartons_per_pallet=per_pallet,
            )
        )
        written.append(f"物料 {material_code}")
    session.flush()
    return written


def _seed_batches(session: Session, warehouse_id: str) -> list[str]:
    """批次行。**必须在物料之后**：`material_id` 是外键，父行不存在会被拒。"""
    written: list[str] = []
    for batch_no, production_date in _BATCHES:
        exists = session.execute(
            select(Batch.id).where(
                Batch.warehouse_id == warehouse_id, Batch.batch_no == batch_no
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        # 种子只有 `_MATERIALS` 里的那个料号，批号挂在它下面 —— 样本里批号本就属于该料号。
        material_id = session.execute(
            select(Material.id).where(
                Material.warehouse_id == warehouse_id,
                Material.material_code == _MATERIALS[0][0],
            )
        ).scalar_one()
        session.add(
            Batch(
                warehouse_id=warehouse_id,
                batch_no=batch_no,
                material_id=material_id,
                production_date=production_date,
                status=None,
            )
        )
        written.append(f"批次 {batch_no}")
    session.flush()
    return written


#: 按依赖链顺序（父 → 子）。顺序错了会在 `foreign_keys=ON` 下报外键失败。
_GROUPS = (
    _seed_warehouse,
    _seed_aisles,
    _seed_locations,
    _seed_materials,
    _seed_batches,
)


def seed(session: Session) -> list[str]:
    """写入开发种子，返回已写入分组的说明。

    返回空列表表示没有可种子的实体 —— 全部已存在（幂等），或尚未建模。
    不提交事务：由调用方决定边界（`init_db.py` 在全部组写完后一次 `commit`）。
    """
    warehouse_id = settings.warehouse_code
    written: list[str] = []
    for group in _GROUPS:
        written.extend(group(session, warehouse_id))
    return written
