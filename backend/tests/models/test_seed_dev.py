"""开发种子的契约测试。

事实来源：openspec/changes/data-model-permission/tasks.md 1.5（建库入口）、§2.7（主数据种子）
          scripts/seed_dev.py 的「填充约定」（幂等、父表先于子表、只种单厂）

种子的两条硬要求都是**会静默失效**的那一类，故钉成测试：

  - **幂等**：`init_db.py` 可能被反复执行。不幂等的种子第一次跑没事，第二次
    要么报唯一约束、要么写重复行 —— 而那时人已经在做别的事，故障现场离改动很远。
  - **父表先于子表**：顺序错了在 `foreign_keys=ON` 下必定报外键失败。它不会
    "偶尔"出错，但只有在真的种了子表（批次）之后才会暴露。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.master_data import (
    Aisle,
    AisleStation,
    Batch,
    Location,
    Material,
    Warehouse,
)
from scripts.seed_dev import seed

pytestmark = pytest.mark.model


def _count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_writes_master_data(session: Session) -> None:
    """首次执行写入主数据链各表，且仓库号取自 `settings.warehouse_code`。"""
    written = seed(session)

    assert written, "首次执行应有写入（否则 init_db 的种子是空操作）"
    assert _count(session, Warehouse) == 1
    assert _count(session, Aisle) == 5
    assert _count(session, Location) == 4
    assert _count(session, Material) == 1
    assert _count(session, Batch) == 1

    code = session.execute(select(Warehouse.warehouse_id)).scalar_one()
    assert code == settings.warehouse_code == "GTJ10036"


def test_seed_is_idempotent(session: Session) -> None:
    """重复执行不得报错，也不得产生重复行 —— 第二次应无事可做。"""
    seed(session)
    second = seed(session)

    assert second == [], "第二次执行应跳过全部已存在记录"
    assert _count(session, Warehouse) == 1
    assert _count(session, Aisle) == 5
    assert _count(session, Location) == 4
    assert _count(session, Material) == 1
    assert _count(session, Batch) == 1


def test_seed_fills_batch_material_link(session: Session) -> None:
    """批次的外键必须指向真实物料行 —— 父表先于子表，顺序错了这里会炸。"""
    seed(session)

    material_id = session.execute(select(Material.id)).scalar_one()
    linked = session.execute(select(Batch.material_id)).scalar_one()
    assert linked == material_id


def test_seed_leaves_derived_fields_empty(session: Session) -> None:
    """派生字段不种：`abc_class` 由成品清单导入补录（`16` A.4）。

    种子里塞一个编出来的 ABC 会让开发环境"看起来能用"，而真实链路里该列
    本就要等成品清单导入 —— 掩盖掉的正是阶段四要验的那段。
    """
    seed(session)

    assert session.execute(select(Material.abc_class)).scalar_one() is None


def test_seed_does_not_invent_aisle_station(session: Session) -> None:
    """巷道-站台主数据**待业务方补充导出**（`16` §1.1）—— 种子不得编造。

    编出距离权重，会让「站台就近」因子在开发环境生效、生产上却降级，
    两个环境的推荐结果不可比。空表是正确状态。
    """
    seed(session)
    assert _count(session, AisleStation) == 0
