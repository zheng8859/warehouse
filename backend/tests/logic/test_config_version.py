"""tasks 6.2 的逻辑测试：取「当前生效的配置版本」。

事实来源：17-数据模型设计 §七（配置实体带版本号 + 生效时间）
          design.md D1（**三分语义**之配置型：`version_no` + `effective_at`，
          当前版本 = `effective_at <= now` 中 `version_no` 最大者，**无指针列**）
          spec `data-model`「版本语义三分」

**为什么要有这个函数**：判断「现在用哪一版」是**纯函数**，不是查询。把它写成查询会
诱使实现加一列 `is_current` 来加速 —— 那一列必须与 `effective_at` 保持同步，而
「未来生效的版本」到点需要有人去翻牌（定时任务？还是每次读时算？），于是不可回放的
隐式状态就进来了。文档明确不要指针列，读时算。

**为什么按 `version_no` 而不是 `effective_at` 取最大**：D1 的原文是「`effective_at <= now`
中 `version_no` 最大者」。差额情形（版本 2 的生效时间**早于**版本 1）在真实数据里会出现
—— 补录一个更早生效的旧口径时就是如此 —— 而「当前用哪版」的答案仍是编号大的那版，
因为编号是**用户可见的版本序列**（17 §七「历史版本保留可回滚」），生效时间只决定
「什么时候开始能用」。用 `effective_at` 取最大会得到一个用户没选过的版本。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config_version import is_effective, pick_current_version
from app.models.base import Base
from app.models.configuration import CapacityConfig, WeightConfig

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 11, 8, 0)


@dataclass
class _Stub:
    """纯函数只依赖两个属性 —— 用桩对象断言它没有被绑死在 ORM 上。"""
    version_no: int
    effective_at: datetime


def _v(version_no: int, *, days: int) -> _Stub:
    return _Stub(version_no=version_no, effective_at=NOW + timedelta(days=days))


# ------------------------------------------------------------------ is_effective

def test_future_version_is_not_effective_yet() -> None:
    """生效时间在将来 → 现在不能用它（17 §七：配置**可预约生效**）。"""
    assert is_effective(_v(2, days=1), now=NOW) is False
    assert is_effective(_v(2, days=0), now=NOW) is True


def test_effective_at_is_inclusive() -> None:
    """边界取闭区间：`effective_at == now` 即已生效。

    这条是刻意的选择而非随意：另一侧（开区间）会让「预约在整点生效的版本」比
    用户预期的时刻晚一个瞬间生效，而窗口内没有第二次判断，很容易被当成 bug 报回来。
    """
    assert is_effective(_v(1, days=0), now=NOW) is True


# ------------------------------------------------------------------ pick_current_version

def test_picks_the_highest_version_among_effective() -> None:
    rows = [_v(1, days=-30), _v(2, days=-10), _v(3, days=-1)]
    assert pick_current_version(rows, now=NOW).version_no == 3  # type: ignore[union-attr]


def test_ignores_versions_scheduled_for_the_future() -> None:
    """预约生效的版本在到点之前不参与选择 —— 否则「提前配置好下月口径」会立刻改变
    现有评分，而没人按过任何按钮。"""
    rows = [_v(1, days=-30), _v(9, days=1)]
    assert pick_current_version(rows, now=NOW).version_no == 1  # type: ignore[union-attr]


def test_returns_none_when_nothing_is_effective() -> None:
    """全部未生效 → `None`，**不回退到最早那版**。

    回退是危险的自作主张：库里有配置行却没有生效版本，说明配置发布流程出了问题，
    调用方应当因此走「无配置」分支（阶段三的降级链），而不是拿到一个用户没启用的版本
    继续算分。同理，**没有指针列**可以问 —— 答案只能由这个函数给出。
    """
    assert pick_current_version([_v(1, days=5)], now=NOW) is None
    assert pick_current_version([], now=NOW) is None


def test_higher_version_wins_even_if_it_became_effective_earlier() -> None:
    """编号优先于生效时间（D1 的原文：「`effective_at <= now` 中 `version_no` 最大者」）。

    补录一条「其实上个月就该用的旧口径」时会出现这种数据。当前版本仍是编号大的那版
    —— 编号是用户看得见、能回滚的版本序列，生效时间只回答「什么时候开始可用」。
    """
    rows = [_v(1, days=-1), _v(2, days=-30)]
    assert pick_current_version(rows, now=NOW).version_no == 2  # type: ignore[union-attr]


def test_result_is_independent_of_input_order() -> None:
    """纯函数：同样输入必得同样输出（红线「同样输入必得同样输出」），与行的到达顺序无关
    —— 查询返回顺序不是契约。"""
    rows = [_v(1, days=-30), _v(2, days=-10), _v(3, days=-1)]
    assert pick_current_version(rows, now=NOW) is pick_current_version(list(reversed(rows)), now=NOW)


def test_does_not_mutate_the_rows() -> None:
    rows = [_v(1, days=-30), _v(2, days=-10)]
    pick_current_version(rows, now=NOW)
    assert [r.version_no for r in rows] == [1, 2]


def test_works_on_real_config_rows(session: Session) -> None:
    """桩对象之外，真表也走得通（本组两张版本化配置表共用同一函数）。"""
    session.add(WeightConfig(
        warehouse_id=WAREHOUSE, version_no=1, effective_at=NOW - timedelta(days=30),
        weight_abc=0.25, weight_cap=0.20, weight_existing=0.15,
        weight_station=0.20, weight_batch=0.10, weight_continuity=0.10,
    ))
    session.add(CapacityConfig(
        warehouse_id=WAREHOUSE, version_no=2, effective_at=NOW - timedelta(days=1),
        near_station_reserved_ratio=0.40, reserved_release_at=time(18, 0),
        concentration_n=5, same_material_cross_aisle_threshold=5,
        same_batch_cross_aisle_threshold=3, cap_drift_alert_threshold=0.01,
    ))
    session.flush()

    weights = session.execute(sa.select(WeightConfig)).scalars().all()
    capacities = session.execute(sa.select(CapacityConfig)).scalars().all()
    assert pick_current_version(weights, now=NOW).version_no == 1  # type: ignore[union-attr]
    assert pick_current_version(capacities, now=NOW).version_no == 2  # type: ignore[union-attr]


def test_no_current_version_pointer_column_exists() -> None:
    """D1：**不加指针列**。`is_current` / `current_version_no` 这类列一旦存在，
    「翻牌」就成了写入方必须记住的责任，而漏翻的后果是静默用错口径。"""
    for table_name in ("weight_configs", "capacity_configs"):
        columns = set(Base.metadata.tables[table_name].c.keys())
        assert not {"is_current", "current_version_no", "active_version_no"} & columns
