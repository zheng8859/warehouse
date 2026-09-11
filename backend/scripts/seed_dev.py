"""开发种子数据：GTJ10036 单厂基础主数据与默认配置。

事实来源：openspec/changes/data-model-permission/tasks.md 1.5（入口）、§2.7 / §6（填充）
          17-数据模型设计 §2.1（巷道/库位示例）、§10.5（cap 快照示例的格数）、
          §10.1（6 因子权重示例）、§七（配置实体字段表）
          16-数据衔接与 cap 自维护 §6.1（巷道总格数来源）、§353~356（阈值默认值表）
          15-入库出库移库与后验流程设计 §8.2（四类可点击问句）、§8.3（参数化语句）
          PRD 8.3.1（真实样本：料号 / 品名 / 箱规板规 / 批号 / 生产日期）
          14-推荐引擎与评分流程设计 §2.2（近站台巷道 01/02）

## 填充进度

  - **已填**：主数据链 —— `Warehouse` / `Aisle` / `Location` / `Material` / `Batch`（任务 2.7）
  - **已填**：`WeightConfig` / `CapacityConfig` / `PromptTemplate`（任务 §6）——
    取值全部来自文档：6 因子权重取 17 §10.1 的示例（文档里唯一一组真实权重），
    容量与阈值取 16 §353~356 的默认值表，问句取 15 §8.2 的四条
  - **未填**：`AisleStation` —— 属**待业务方补充导出**的主数据（`16` §1.1），没有真实
    取值之前不编造距离权重：编出来的权重会让「站台就近」因子在开发环境"看起来能用"，
    而生产上该因子本该降级。空表是正确状态。
  - **未填**：衔接链（`ImportSession` / `Snapshot` / `InventoryItem` / `AisleCap` / `CapAlert`）
    —— 这五张表由**真实导入**产生（`16` §三），不是可种子的主数据。编一份快照会更糟：
    `cap_total` = 巷道总格数 − 已占格数，而「已占格数」要按「板-格」换算规则从数量折算
    （`16` A.4），该规则属 `CapacityConfig` —— 种出来的 cap 与库存明细必然互相矛盾，
    而矛盾的数据比空表更难排查（空表会让人去导入，矛盾的表会让人以为链路已经通了）。
  - **未填**：`FieldMappingConfig` —— 映射内容在 `16` 附录 A.1/A.2/A.3 的三张表里，
    而消费它的是导入管线（阶段四）。同理不种**半份**映射：缺列的映射会让导入校验
    按一条不存在的规则跑，比「缺映射配置」这个明确的错误更难发现。
    空表是正确状态（与 `AisleStation` 同一处置），已登记 tasks.md 9.4f。
  - **未填**：`ConversationContext` —— 会话上下文由真实对话产生，无种子可言。
  - **未填**：`Account` —— 种子**不预置口令**：预置口令会被抄进部署脚本，而它是全系统权限
    的入口；`assert_production_safe()`（配置层护栏）拦不住它，因为它是一行数据而不是一个配置项。
    注意**本阶段（§7）只实现了登录**，账号创建端点是路线图（13 §5.3，与 D9 同批），
    所以「空库跑完 `init_db.py` 之后怎么建第一个账号」目前**没有工具**——
    这是一处已登记的差口，见 tasks.md 9.4g③，不要在这里顺手补一个默认口令来"让它能跑"。

## 填充约定

  - 每组一个 `_seed_<group>(session, warehouse_id) -> list[str]`，返回可读的写入说明；
    `seed()` 按依赖链顺序调用它们 —— **父表先于子表**，否则 `foreign_keys=ON` 下必然
    报外键失败。
  - **幂等是硬要求**：`init_db.py` 可能被反复执行。按业务键先查后写，存在即跳过。
    重复运行不得报错，也不得产生重复行。
  - 只写 `settings.warehouse_code` 对应的单厂主数据与配置 —— 首期就是 `GTJ10036`，
    不要顺手造第二个厂。
  - 配置种子**只写第 1 版**（`version_no=1`），且只认自己那一版：生效时间用固定的
    `_CONFIG_EFFECTIVE_AT` 而不是 `utcnow()` —— 种子要可复现（红线「同样输入必得同样
    输出」），否则每次重建库得到的配置行都不同，按配置行做的比对与测试都不稳定。
  - 取值**只取文档里出现过的**：库位号 `010104` / `010105` / `050102` / `211202`、
    料号 `3001234`、品名 `PET500 茉莉柚茶`、箱规板规 `15入纸箱` / `102/板`、
    批号 `GJP2571221`、生产日期 `2026-09-05` 全部来自 `17` §2.1 与 PRD 8.3.1 的真实样本；
    巷道总格数取 `17` §10.5 cap 快照示例的 120 / 200。**不编造规模**。
  - **不填派生字段**：`Material.abc_class` 由成品清单聚合自动补录（`16` A.4），
    `Aisle.is_near_station` 的近站台判定同样源自巷道主数据导出（`16` §6.1）——
    种子里只写文档明确给出的部分，近站台按 `14` §2.2 的 01/02 标 True，其余 False。
"""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.configuration import (
    Capability,
    CapacityConfig,
    PromptTemplate,
    WeightConfig,
)
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

#: 6 因子权重：`17` §10.1「推荐理由」里的示例值，**文档里唯一一组真实权重**
#: （abc 0.25 / 巷道 cap 0.20 / 既有库位 0.15 / 站台就近 0.20 / 批次 0.10 / 连续性 0.10）。
#: 它是示例而非「出厂口径」—— 权重终归要由主管按实测调（15 §8.2 的第 ③ 条能力就是调它），
#: 但六项之和恰好为 1.00，作为一个起点是可用的；**不编造第二组**。
_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("weight_abc", 0.25),
    ("weight_cap", 0.20),
    ("weight_existing", 0.15),
    ("weight_station", 0.20),
    ("weight_batch", 0.10),
    ("weight_continuity", 0.10),
)

#: 首版配置的生效时间。文档没有「上线时刻」这个口径，故取一个**明显早于开发期**的固定值
#: 而不是 `utcnow()`：种子必须可复现（红线「同样输入必得同样输出」—— 每次重建库得到不同的
#: 生效时间，会让按配置行做的比对与测试都不稳定），且首版必须**已经生效**，
#: 否则 `app/core/config_version.pick_current_version` 取不到任何版本，阶段三会直接走降级。
_CONFIG_EFFECTIVE_AT = datetime(2026, 1, 1)

#: L2 可点击问句：`15` §8.2 的四条（各自映射四类冷路径能力，见 `10` §152）。
#: 第二条用 `15` §8.3 的参数化写法 `{物料}` —— §8.2 的表里给的是具体样本
#: 「为什么 PET600 跨了 11 个巷道？」，而 §8.3 明说带 `{}` 的才是模板、点击后弹物料选择器。
#: 第四条同理：§8.2 的样本写死了「茉莉柚茶」，参数化后即 `{物料}`。
_TEMPLATES: tuple[tuple[str, str, Capability, list[str] | None], ...] = (
    ("KPI_DIGEST", "帮我分析本周集中度下滑的原因", Capability.KPI_DIGEST, None),
    ("AISLE_WHY", "为什么 {物料} 跨了这么多巷道？",
     Capability.DEVIATION_ATTRIBUTION, ["物料"]),
    ("WEIGHT_TUNE", "权重怎么调能提升集中度？", Capability.WEIGHT_TUNING, None),
    ("RELOCATE_PLAN", "帮我生成 {物料} 的收拢方案，代价多大？",
     Capability.RELOCATE_PLAN, ["物料"]),
)


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


def _seed_weight_config(session: Session, warehouse_id: str) -> list[str]:
    """第 1 版权重配置（任务 6.1）。

    **幂等按业务键 `(warehouse_id, version_no)`** 判：本函数只拥有版本 1，
    将来由人调出来的版本 2 不归它管，重跑不会覆盖也不会新增。

    `changed_by_id` 留空 —— 种子不是「某个人」改的，与 `Account.created_by_id`
    的自举同一处置（详见 `app/models/configuration.py` 的字段注释）。
    """
    exists = session.execute(
        select(WeightConfig.id).where(
            WeightConfig.warehouse_id == warehouse_id, WeightConfig.version_no == 1
        )
    ).scalar_one_or_none()
    if exists is not None:
        return []

    session.add(
        WeightConfig(
            warehouse_id=warehouse_id,
            version_no=1,
            effective_at=_CONFIG_EFFECTIVE_AT,
            changed_by_id=None,
            **dict(_WEIGHTS),
        )
    )
    session.flush()
    return ["权重配置 v1（17 §10.1 示例权重）"]


def _seed_capacity_config(session: Session, warehouse_id: str) -> list[str]:
    """第 1 版容量与阈值配置（任务 6.3）。

    六项**逐个显式写出**，不依赖模型的 Python 默认值：种子的职责是让库里的每一行都能
    在文档里找到出处（`16` §353~356 的表），而默认值属于「没人配过时怎么跑」，
    两者是不同的承诺 —— 显式写出后，改模型默认值不会静默改掉已有环境的配置。
    """
    exists = session.execute(
        select(CapacityConfig.id).where(
            CapacityConfig.warehouse_id == warehouse_id, CapacityConfig.version_no == 1
        )
    ).scalar_one_or_none()
    if exists is not None:
        return []

    session.add(
        CapacityConfig(
            warehouse_id=warehouse_id,
            version_no=1,
            effective_at=_CONFIG_EFFECTIVE_AT,
            # 16 §353~356：近站台预留比例 40%
            near_station_reserved_ratio=0.40,
            # 16 §353~356：预留超时释放时点 = 当日 18:00（本地墙上时间，见模型注释）
            reserved_release_at=time(18, 0),
            # 16 §353~356 / 17 §七：加权集中度 N=5（统一验收指标「80% 拣货量 ≤N 巷道」）
            concentration_n=5,
            # 16 §353~356 / CLAUDE.md §十：同物料跨巷道 ≤5
            same_material_cross_aisle_threshold=5,
            # 16 §353~356 / CLAUDE.md §十：同批跨巷道 ≤3
            same_batch_cross_aisle_threshold=3,
            # 16 §353~356：「建议 > 1%」，故 0.01
            cap_drift_alert_threshold=0.01,
        )
    )
    session.flush()
    return ["容量与阈值配置 v1（16 §353~356 默认值）"]


def _seed_prompt_templates(session: Session, warehouse_id: str) -> list[str]:
    """内置四条 L2 可点击问句（任务 6.5）。

    **默认启用**（`enabled` 用模型默认的 True）：15 §8.5「新增一条问句即新增一条映射，
    无需改代码」—— 内置的四条若不启用，对话台会一条问句都点不到，而那是首期就该有的
    冷路径入口（`10` §166：① KPI 解读 / ② 偏离归因 建议首期即做）。
    """
    written: list[str] = []
    for template_id, question_text, capability, params in _TEMPLATES:
        exists = session.execute(
            select(PromptTemplate.id).where(
                PromptTemplate.warehouse_id == warehouse_id,
                PromptTemplate.template_id == template_id,
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue

        session.add(
            PromptTemplate(
                warehouse_id=warehouse_id,
                template_id=template_id,
                question_text=question_text,
                capability=capability,
                params_json=params,
            )
        )
        written.append(f"问句模板 {template_id}")
    session.flush()
    return written


#: 按依赖链顺序（父 → 子）。顺序错了会在 `foreign_keys=ON` 下报外键失败。
_GROUPS = (
    _seed_warehouse,
    _seed_aisles,
    _seed_locations,
    _seed_materials,
    _seed_batches,
    _seed_weight_config,
    _seed_capacity_config,
    _seed_prompt_templates,
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
