"""出境脱敏管线（正向白名单）的契约测试（tasks.md 2.1 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §五（脱敏与数据出域）
          openspec/changes/ai-assist/design.md D5（正向白名单，10 字段）
          spec `ai-assist`「脱敏白名单出站」

红线：核心链路不出域 —— 出境 JSON 仅含白名单字段，禁出 `order_no` + 操作员姓名 +
操作员备注；客户名 / 价格 / 供应商 / 配方 / 真实产能从来不出。正向白名单比黑名单
剥离更安全（新字段默认不出境），所以测试不只是「禁出字段没了」，还要「名单之外的
任何字段都不放行」。
"""
from __future__ import annotations

import pytest

from app.llm.redact import (
    ALLOWED_OUTBOUND_FIELDS,
    FORBIDDEN_OUTBOUND_FIELDS,
    redact,
)

pytestmark = pytest.mark.logic


# ------------------------------------------------------------------ 白名单 / 禁出名单本身

def test_allowlist_is_exactly_the_ten_documented_fields() -> None:
    """D5 的 10 类字段逐项在列，且不多不少（多了 = 名单失守，少了 = 能力缺字段）。"""
    assert ALLOWED_OUTBOUND_FIELDS == {
        "material_code",   # 料号
        "material_name",   # 品名
        "batch_no",        # 批号
        "aisle_no",        # 巷道
        "location_code",   # 库位
        "qty",             # 数量
        "plates",          # 板数
        "metrics",         # 聚合指标（同物料跨巷道均值/加权集中度/采纳率）
        "scores",          # 6 因子分值
        "cap_cells",       # cap 格数
    }


def test_forbidden_list_covers_all_documented_bans() -> None:
    """D5 禁出字段逐项在列：order_no / 操作员姓名 / 操作员备注 + 从来不出五类。"""
    assert FORBIDDEN_OUTBOUND_FIELDS == {
        "order_no",        # 订单号（去标识化）
        "operator_name",   # 操作员姓名（去标识化）
        "operator_note",   # 操作员备注（v1 自由文本不出境）
        "customer_name",   # 客户名（从来不出）
        "price",           # 价格（从来不出）
        "supplier",        # 供应商（从来不出）
        "recipe",          # 配方（从来不出）
        "true_capacity",   # 真实产能（从来不出）
    }


def test_no_identifier_is_both_allowed_and_forbidden() -> None:
    """两份名单不相交 —— 否则同一字段既放行又拦截，语义自相矛盾。"""
    assert ALLOWED_OUTBOUND_FIELDS.isdisjoint(FORBIDDEN_OUTBOUND_FIELDS)


# ------------------------------------------------------------------ 正向白名单：新字段默认不出境

def test_redact_keeps_only_allowlisted_fields() -> None:
    """不在白名单里的键（哪怕是看似无害的新字段）一律丢弃 —— 正向枚举的核心语义。"""
    payload = {
        "material_code": "3001234",
        "material_name": "康师傅冰红茶",
        "batch_no": "B20260901",
        "aisle_no": "02",
        "location_code": "020101",
        "qty": 120,
        "plates": 12,
        "metrics": {"concentration": 3.4, "adoption_rate": 0.72},
        "scores": {"abc": 0.5, "cap": 0.3},
        "cap_cells": 200,
        # 名单之外：新字段默认不出境。
        "order_no": "PO-2026-001",          # 禁出
        "operator_name": "张三",             # 禁出
        "operator_note": "担心排队",          # 禁出
        "secret_extra_field": "不该出境",     # 从未登记的白名单外字段
    }

    result = redact(payload)

    assert set(result) == ALLOWED_OUTBOUND_FIELDS
    assert "order_no" not in result
    assert "operator_name" not in result
    assert "operator_note" not in result
    assert "secret_extra_field" not in result


def test_redact_passes_allowed_values_through_untouched() -> None:
    """白名单键的值（规则侧算好的数字/字符串/聚合对象）原样透传，不被改写。"""
    metrics = {"cross_aisle_avg": 2.1, "concentration": 3.4, "adoption_rate": 0.72}
    scores = {"abc": 0.50, "cap": 0.30, "existing": 0.20}

    result = redact({"material_code": "3001234", "metrics": metrics, "scores": scores})

    assert result["metrics"] == metrics
    assert result["scores"] == scores
    assert result["material_code"] == "3001234"


# ------------------------------------------------------------------ 双保险：禁出字段在任意层级被剥除

def test_redact_strips_forbidden_fields_nested_inside_allowed_values() -> None:
    """防敏感键被塞进「聚合指标」「6 因子分值」这类透传对象的嵌套值里。"""
    payload = {
        "material_code": "3001234",
        "scores": {
            "abc": 0.5,
            "cap": 0.3,
            "order_no": "PO-2026-002",       # 藏在因子分值里也必须出不去
        },
        "metrics": {
            "concentration": 3.4,
            "operator_name": "李四",          # 藏在聚合指标里也必须出不去
        },
    }

    result = redact(payload)

    assert result["scores"] == {"abc": 0.5, "cap": 0.3}
    assert result["metrics"] == {"concentration": 3.4}


def test_redact_strips_forbidden_fields_inside_lists() -> None:
    """列表（如多候选巷道）里的禁出字段同样剥除。"""
    payload = {
        "material_code": "3001234",
        "scores": [
            {"aisle_no": "02", "cap_cells": 200},
            {"aisle_no": "03", "cap_cells": 180, "order_no": "PO-2026-003"},
        ],
    }

    result = redact(payload)

    assert result["scores"][0] == {"aisle_no": "02", "cap_cells": 200}
    assert result["scores"][1] == {"aisle_no": "03", "cap_cells": 180}


# ------------------------------------------------------------------ 不污染入参

def test_redact_returns_a_new_dict_and_does_not_mutate_input() -> None:
    """脱敏是纯函数：出站副本被过滤，调用方手里的原始 payload 不动。"""
    payload = {
        "material_code": "3001234",
        "order_no": "PO-2026-004",
        "scores": {"abc": 0.5, "operator_name": "王五"},
    }

    result = redact(payload)

    assert result is not payload
    assert payload == {
        "material_code": "3001234",
        "order_no": "PO-2026-004",
        "scores": {"abc": 0.5, "operator_name": "王五"},
    }
