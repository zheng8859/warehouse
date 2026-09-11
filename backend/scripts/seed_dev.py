"""开发种子数据：GTJ10036 单厂基础主数据与默认配置。

事实来源：openspec/changes/data-model-permission/tasks.md 1.5（入口）
          16-数据衔接与 cap 自维护 §10.6（CapacityConfig 默认阈值）
          14-推荐引擎与评分流程设计 §3.3（WeightConfig 六因子权重 / 预留比例）

**当前为空实现，这是依赖顺序而非遗漏**：主数据链实体（`Warehouse` / `Aisle` /
`AisleStation` / `Location` / `Material` / `Batch`）由任务 2.1–2.6 建模，领域配置
实体（`WeightConfig` / `CapacityConfig`）由 §6 建模。实体不存在时无从种子。

## 填充约定（写给 §2.7 与 §6 的实现者）

  - 每组一个 `_seed_<group>(session, warehouse_id) -> list[str]`，返回可读的
    写入说明；`seed()` 负责按依赖链顺序调用它们 —— **父表先于子表**，
    否则 `foreign_keys=ON` 下必然报外键失败。
  - **幂等是硬要求**：`init_db.py` 可能被反复执行。按业务键先查后写，
    存在即跳过。重复运行不得报错，也不得产生重复行。
  - 只写 `settings.warehouse_code` 对应的单厂主数据 —— 首期就是 `GTJ10036`，
    不要顺手造第二个厂。
  - 种子是**开发数据**：`Warehouse` / `Aisle` / `Location` 的格数按 `26` 附录的
    示例规模给，不需要与真实立体库一致。
  - 配置类实体（`WeightConfig` / `CapacityConfig`）带 `version_no` + `effective_at`，
    种子写的是 **v1**；首次落库取值从 `app/core/config.py` 的 `settings` 引导，
    之后以数据库中的版本为准（见 config.py 模块 docstring）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def seed(session: Session) -> list[str]:
    """写入开发种子，返回已写入分组的说明。

    返回空列表表示没有可种子的实体 —— 见模块 docstring 的填充约定。
    """
    written: list[str] = []
    return written
