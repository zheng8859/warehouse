"""eval_utils 门面的同源一致性测试：门面输出必须与业务函数一致，不漂移。

事实来源：30号 §6.5；design.md D4（收编既有函数，不重复实现）。
本文件只断言「门面 = 业务」，不重测业务函数本身的正确性（那是 tests/ 的职责）。
"""
from __future__ import annotations

import pytest

from app.engine import degradation as biz_degradation
from app.llm import redact as biz_redact
from app.services import kpi as biz_kpi
from evals import eval_utils


@pytest.mark.l1
def test_weighted_concentration_same_source():
    items = [("01", 100), ("02", 50), ("03", 30), ("04", 20)]
    assert eval_utils.weighted_concentration(items) == biz_kpi.weighted_concentration(items)
    assert eval_utils.weighted_concentration(items, threshold=0.7) == biz_kpi.weighted_concentration(items, threshold=0.7)


@pytest.mark.l1
def test_cross_aisle_counts_match_mean_numerator():
    items = [("M1", "01"), ("M1", "01"), ("M1", "02"), ("M2", "03"), ("M2", "03")]
    counts = eval_utils.cross_aisle(items)
    # 逐物料去重计数：M1 占 01/02 两条巷，M2 占 03 一条巷
    assert counts == {"M1": 2, "M2": 1}
    # 均值 = (2 + 1) / 2 个物料 = 1.5，与业务同源
    assert eval_utils.cross_aisle_mean(items) == biz_kpi.same_material_cross_aisle_mean(items) == 1.5


@pytest.mark.l1
def test_normalize_weights_sums_to_one_and_drops_degraded():
    weights = {"near": 0.5, "cap": 0.3, "existing": 0.2}
    norm = eval_utils.normalize_weights(weights)
    assert sum(norm.values()) == pytest.approx(1.0)
    # 降级因子不进分母，其余重新归一化
    norm_degraded = eval_utils.normalize_weights(weights, degraded=("existing",))
    assert set(norm_degraded) == {"near", "cap"}
    assert sum(norm_degraded.values()) == pytest.approx(1.0)


@pytest.mark.l1
def test_normalize_weights_zero_contributes_zero_and_all_zero_raises():
    # 权重 = 0 的因子贡献为 0（30号 §6.5 权重=0 时贡献=0）
    norm = eval_utils.normalize_weights({"a": 1.0, "b": 0.0})
    assert norm["b"] == 0.0
    assert norm["a"] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        eval_utils.normalize_weights({"a": 0.0, "b": 0.0})


@pytest.mark.l1
def test_select_degrade_same_source():
    aisles = ["01", "02", "03"]
    flags = {"01": True, "02": False, "03": None}
    facade = eval_utils.select_degrade(aisles, is_near_station=flags)
    biz = biz_degradation.resolve_tiers(aisles, is_near_station=flags)
    assert facade == biz
    assert [t.label for t in facade.tiers] == ["近站台", "次近巷道", "远巷道", "溢出区"]


@pytest.mark.l1
def test_assert_no_pii_strips_forbidden_and_passes_clean():
    payload = {
        "material_code": "M1",
        "order_no": "O-1",          # 禁出：订单号
        "operator_name": "张三",     # 禁出：操作员姓名
        "metrics": {"order_no": "nested"},  # 嵌套禁出字段也要剥
    }
    out = eval_utils.assert_no_pii(payload)
    assert "order_no" not in out
    assert "operator_name" not in out
    assert out["metrics"] == {}
    # 干净 payload 不抛错、白名单保留
    clean = eval_utils.assert_no_pii({"material_code": "M1", "qty": 3})
    assert clean == {"material_code": "M1", "qty": 3}
    # 与业务 redact 同源
    assert out == biz_redact.redact(payload)
