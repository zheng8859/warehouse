"""开发种子的契约测试。

事实来源：openspec/changes/data-model-permission/tasks.md 1.5（建库入口）、§2.7（主数据种子）、
          §6（配置种子：权重 / 容量阈值 / 问句模板）
          scripts/seed_dev.py 的「填充约定」（幂等、父表先于子表、只种单厂、不编造规模）
          17-数据模型设计 §10.1（6 因子权重示例）、16 §353~356（阈值默认值表）、
          15 §8.2 / §8.3（四条可点击问句）

种子的两条硬要求都是**会静默失效**的那一类，故钉成测试：

  - **幂等**：`init_db.py` 可能被反复执行。不幂等的种子第一次跑没事，第二次
    要么报唯一约束、要么写重复行 —— 而那时人已经在做别的事，故障现场离改动很远。
  - **父表先于子表**：顺序错了在 `foreign_keys=ON` 下必定报外键失败。它不会
    "偶尔"出错，但只有在真的种了子表（批次）之后才会暴露。

配置种子另有一类风险：**编造取值**。权重与阈值是推荐引擎的输入，种一个编出来的数
会让「因子生效」在开发环境看起来成立 —— 故配置项的断言逐条对着文档取值写，
而不是只断言「有一行」。
"""
from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.configuration import (
    Capability,
    CapacityConfig,
    ConversationContext,
    FieldMappingConfig,
    PromptTemplate,
    WeightConfig,
)
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


def test_seed_writes_the_documented_weights(session: Session) -> None:
    """第 1 版权重 = `17` §10.1 的示例值，逐项比对（任务的验证点：两版本可共存）。

    六项之和恰为 1.00，但**种子的职责只是把文档那组值放进去**，不替文档保证归一
    （模型层也没有「和为 1」的 CHECK，见 tasks.md 9.4f）。
    """
    seed(session)

    row = session.execute(select(WeightConfig)).scalar_one()
    assert row.version_no == 1
    assert row.weight_abc == 0.25
    assert row.weight_cap == 0.20
    assert row.weight_existing == 0.15
    assert row.weight_station == 0.20
    assert row.weight_batch == 0.10
    assert row.weight_continuity == 0.10
    assert sum((row.weight_abc, row.weight_cap, row.weight_existing,
                row.weight_station, row.weight_batch, row.weight_continuity)) == pytest.approx(1.0)
    assert row.changed_by_id is None, "首版权重由种子写入，没有变更人（不编造 system 账号）"


def test_seed_writes_the_documented_capacity_defaults(session: Session) -> None:
    """第 1 版容量与阈值 = `16` §353~356 的表值。"""
    seed(session)

    row = session.execute(select(CapacityConfig)).scalar_one()
    assert row.version_no == 1
    assert row.near_station_reserved_ratio == 0.40
    assert row.reserved_release_at == time(18, 0)
    assert row.concentration_n == 5
    assert row.same_material_cross_aisle_threshold == 5
    assert row.same_batch_cross_aisle_threshold == 3
    assert row.cap_drift_alert_threshold == 0.01


def test_seed_writes_the_four_clickable_questions(session: Session) -> None:
    """四条 L2 问句取自 `15` §8.2，各自映射一类冷路径能力（`10` §152），且默认启用。

    第二条与第四条用 §8.3 的参数化写法（§8.2 的表里给的是具体样本，§8.3 明说带
    `{}` 的才是模板）—— 参数名进 `params_json`，供选择器取值。
    """
    seed(session)

    rows = session.execute(select(PromptTemplate)).scalars().all()
    assert {r.template_id for r in rows} == {
        "KPI_DIGEST", "AISLE_WHY", "WEIGHT_TUNE", "RELOCATE_PLAN"
    }
    assert {r.capability for r in rows} == {
        Capability.KPI_DIGEST, Capability.DEVIATION_ATTRIBUTION,
        Capability.WEIGHT_TUNING, Capability.RELOCATE_PLAN,
    }
    assert all(r.enabled for r in rows), "新增即生效（15 §8.5），内置问句不得默认关闭"

    parameterised = {r.template_id: r for r in rows if r.params_json is not None}
    assert set(parameterised) == {"AISLE_WHY", "RELOCATE_PLAN"}
    assert all("{物料}" in r.question_text for r in parameterised.values())


def test_seed_config_is_idempotent(session: Session) -> None:
    """配置种子的幂等按 `(warehouse_id, version_no)` / `template_id` 判 —— 重跑不新增第 2 版。

    如果按「表里有没有行」判，重跑会跳过；如果按 `created_at` 之类别的东西判，
    重跑就会造出一个**没人调过的版本 2**，而版本号是给人看的回滚序列
    （17 §七），凭空多一版会让主管以为自己错过了什么。
    """
    seed(session)
    assert seed(session) == []

    assert _count(session, WeightConfig) == 1
    assert _count(session, CapacityConfig) == 1
    assert _count(session, PromptTemplate) == 4


def test_seed_does_not_invent_field_mappings(session: Session) -> None:
    """字段映射**不种**：内容在 `16` 附录 A.1/A.2/A.3，消费它的是阶段四的导入管线。

    半份映射比空表更坏 —— 空表会让导入报「缺映射配置」，而缺列的映射会让校验
    按一条不存在的规则跑，两者发现的时机差了整整一个阶段。登记在 tasks.md 9.4f。
    """
    seed(session)

    assert _count(session, FieldMappingConfig) == 0
    assert _count(session, ConversationContext) == 0, "会话上下文由真实对话产生"
