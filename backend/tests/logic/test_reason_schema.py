"""推荐理由与批量分配报文的契约用例（`17` §10.1 / §10.7）。

事实来源：`17` §10.1（推荐理由的字段表 + 可追溯恒等式 + 两种降级不得混用）
          `17` §10.7（批量分配的请求 / 响应字段表 + 预测跨巷道的口径）
          `openspec/changes/recommendation-engine/design.md` D9（组装边界的三条不变量）、
            D16（`aisles` 取单条、六因子的归一化口径）

**这里测的是契约的「形状」，不是引擎的「算法」** —— 算法在 `test_factors.py` /
`test_scoring.py` 等处。分开的理由：形状错了会让每一处调用方都跟着错，而它比算法便宜得多，
值得先钉死（本用例是 tasks.md 1.1 的 RED 侧）。

几条断言不是洁癖，各自对应一条已写进文档的要求：

- **`factors` 恒为六项**（`17` §10.1「六项权重…不得增删」）—— 含降级因子时也照给六项，
  因为「哪些因子参与」由 `breakdown` / `factor_degraded` 表达，不由权重字典增删表达。
  §10.1 的算例自证这一点：`station` 降级了，`factors` 里仍有它的 0.20（分母 0.80 就是把它刨掉的結果）。
- **降级因子不得出现在 `breakdown` 里**（spec「每因子取值说明齐备」的 THEN 半句）。
- **`degraded` ⇒ `degrade_reason` 非空**（「降级不静默」）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.models.configuration import WEIGHT_FACTORS
from app.schemas.reason import (
    BatchAllocateRequest,
    BatchAllocateResponse,
    FactorTerm,
    ReasonPayload,
)

pytestmark = pytest.mark.logic


def _reason() -> dict:
    """`17` §10.1 的示例载荷（逐字搬运，仅把 `aisles` 按 D16 的裁决收成单条）。

    刻意用文档的原始示例而不是自造一个：契约用例的价值在于「文档给的形状能通过」，
    自造载荷会让用例与文档悄悄分叉（示例里 `station` 就是降级因子，这一点很重要）。
    """
    return {
        "job_id": "JOB-20260908-001",
        "aisles": ["01"],
        "factors": {
            "abc": 0.25,
            "cap": 0.20,
            "existing": 0.15,
            "station": 0.20,
            "batch": 0.10,
            "continuity": 0.10,
        },
        "scores": {"01": 0.79, "02": 0.58, "21": 0.43},
        "breakdown": {
            "01": {
                "abc": {"value": 1.00, "note": "A 类（3/3）"},
                "cap": {"value": 0.72, "note": "可用 58 / 80 板"},
                "existing": {"value": 0.60, "note": "既有 6 板集中于此"},
                "batch": {"value": 1.00, "note": "同批 GJP2571221 已在此巷道"},
                "continuity": {"value": 0.50, "note": "同物料现跨 2 个巷道"},
            },
            "02": {
                "abc": {"value": 1.00, "note": "A 类（3/3）"},
                "cap": {"value": 0.42, "note": "可用 34 / 80 板"},
                "existing": {"value": 0.20, "note": "既有 2 板"},
                "batch": {"value": 0.00, "note": "无同批"},
                "continuity": {"value": 1.00, "note": "同物料现仅在此巷道"},
            },
            "21": {
                "abc": {"value": 0.67, "note": "B 类（2/3）"},
                "cap": {"value": 0.90, "note": "可用 180 / 200 板"},
                "existing": {"value": 0.00, "note": "无既有库存"},
                "batch": {"value": 0.00, "note": "无同批"},
                "continuity": {"value": 0.00, "note": "同物料不在该巷道"},
            },
        },
        "priority": {
            "score": 0.70,
            "terms": {"outbound_qty": 0.80, "abc": 1.00, "existing_gain": 0.30},
            "degraded": False,
            "degrade_reason": None,
        },
        "factor_degraded": {"station": "巷道-站台主数据未导出"},
        "degraded": False,
        "degrade_reason": None,
        "predicted_cross_aisle": {"material": 3, "threshold": 5, "exceeded": False},
    }


# --- 六因子与权重 -------------------------------------------------------------------


def test_factors_carry_exactly_the_six_weight_factors() -> None:
    """「六因子键齐全」：与 `configuration.WEIGHT_FACTORS` 逐项相等（防两处各写一份而漂移）。"""
    payload = ReasonPayload.model_validate(_reason())
    assert set(payload.factors) == set(WEIGHT_FACTORS)
    assert len(payload.factors) == 6


def test_missing_factor_is_rejected() -> None:
    raw = _reason()
    del raw["factors"]["continuity"]
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_seventh_factor_is_rejected() -> None:
    """「不得引入第 7 个因子」—— 多给一项就要被挡在边界上。"""
    raw = _reason()
    raw["factors"]["zone"] = 0.05
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_degraded_factor_stays_in_factors_but_leaves_breakdown() -> None:
    """`station` 降级 ⇒ 权重字典里仍有它，因子分解里没有它（`17` §10.1 的算例正是此形态）。"""
    payload = ReasonPayload.model_validate(_reason())
    assert "station" in payload.factors
    assert set(payload.factor_degraded) == {"station"}
    for terms in payload.breakdown.values():
        assert "station" not in terms


# --- breakdown 的二层结构与不变量 ----------------------------------------------------


def test_breakdown_is_aisle_by_factor_two_levels() -> None:
    payload = ReasonPayload.model_validate(_reason())
    assert set(payload.breakdown) == set(payload.scores)
    for aisle, terms in payload.breakdown.items():
        assert terms, aisle
        for name, term in terms.items():
            assert isinstance(term, FactorTerm)
            assert 0.0 <= term.value <= 1.0
            assert term.note.strip(), f"{aisle}/{name} 缺少取值说明"


def test_breakdown_factor_keys_must_match_available_factors() -> None:
    """每个参与评分的因子都要有取值与说明；降级因子反而**不得**出现。"""
    raw = _reason()
    # 缺一个参与因子
    del raw["breakdown"]["01"]["batch"]
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)

    # 降级因子混进分解里
    raw = _reason()
    raw["breakdown"]["01"]["station"] = {"value": 0.90, "note": "站台 A"}
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_breakdown_keys_must_match_scores_keys() -> None:
    raw = _reason()
    raw["scores"]["99"] = 0.10
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_aisles_must_be_a_subset_of_candidate_aisles() -> None:
    """`aisles` = 候选巷道集里选中的那条（D16）—— 选中一个不在候选集里的巷道是不可能的。"""
    raw = _reason()
    raw["aisles"] = ["99"]
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)

    raw = _reason()
    raw["aisles"] = []
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_factor_value_out_of_unit_interval_is_rejected() -> None:
    raw = _reason()
    raw["breakdown"]["01"]["cap"] = {"value": 1.20, "note": "可用 96 / 80 板"}
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


# --- 两种降级分开承载 ---------------------------------------------------------------


def test_degrade_requires_reason() -> None:
    """「降级不静默」在报文层也要成立（库层那条 CHECK 只覆盖方案级）。"""
    raw = _reason()
    raw["degraded"] = True
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)

    raw = _reason()
    raw["degraded"] = True
    raw["degrade_reason"] = "近站台与次近巷道均无可行容量，下探至远巷道"
    assert ReasonPayload.model_validate(raw).degraded is True


def test_priority_degrade_requires_reason() -> None:
    """排序降级是**第三处**降级标记，同样不得静默（`17` §10.1 表下注）。"""
    raw = _reason()
    raw["priority"] = {**raw["priority"], "degraded": True, "degrade_reason": None}
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


def test_factor_degraded_keys_must_be_known_factors() -> None:
    raw = _reason()
    raw["factor_degraded"] = {"zone": "库区主数据未导出"}
    with pytest.raises(ValidationError):
        ReasonPayload.model_validate(raw)


# --- 请求 / 响应（`17` §10.7） ------------------------------------------------------


def test_request_requires_explicit_job_order_ids() -> None:
    """「不设『空即全量』的隐含默认」：字段**必填**，但空数组是明确的空集。"""
    with pytest.raises(ValidationError):
        BatchAllocateRequest.model_validate({"warehouse_id": "GTJ10036"})

    request = BatchAllocateRequest.model_validate(
        {"warehouse_id": "GTJ10036", "job_order_ids": []}
    )
    assert request.job_order_ids == []
    assert request.snapshot_version is None


def test_request_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        BatchAllocateRequest.model_validate(
            {"warehouse_id": "GTJ10036", "job_order_id": ["1"]}
        )


def test_response_shape_and_degraded_alerts() -> None:
    response = BatchAllocateResponse.model_validate(
        {
            "bulk_batch_no": "BAT-20260908-01",
            "snapshot_version": "2026-09-08T00:00",
            "plans": [
                {
                    "job_order_id": "1",
                    "order_no": "PO-20260908001",
                    "material_code": "3001234",
                    "material_name": "冰红茶 500ml×15",
                    "abc_class": "A",
                    "qty": 12,
                    "aisles": ["01"],
                    "predicted_cross_aisle": {"material": 2, "threshold": 5, "exceeded": False},
                    "priority": 0.70,
                    "plan_id": 10231,
                }
            ],
            "degraded_alerts": [
                {
                    "job_order_id": "3",
                    "aisle": "21",
                    "message": "近站台缺口 12 板，建议移库腾挪",
                }
            ],
        }
    )
    assert response.plans[0].abc_class == "A"
    assert response.plans[0].material_name == "冰红茶 500ml×15"
    assert response.degraded_alerts[0].aisle == "21"


def test_degraded_alert_requires_all_three_places() -> None:
    """告警的三处（单 / 巷道 / 文案）缺一不可 —— 缺单则无法追责，缺巷道则无法定位。"""
    base = {
        "bulk_batch_no": "BAT-20260908-01",
        "snapshot_version": "2026-09-08T00:00",
        "plans": [],
    }
    for missing in ("job_order_id", "aisle", "message"):
        alert = {
            "job_order_id": "3",
            "aisle": "21",
            "message": "近站台缺口 12 板，建议移库腾挪",
        }
        del alert[missing]
        with pytest.raises(ValidationError):
            BatchAllocateResponse.model_validate({**base, "degraded_alerts": [alert]})

    # 反向：三处齐备时通过
    response = BatchAllocateResponse.model_validate(
        {
            **base,
            "degraded_alerts": [
                {"job_order_id": "3", "aisle": "21", "message": "近站台缺口 12 板，建议移库腾挪"}
            ],
        }
    )
    assert len(response.degraded_alerts) == 1


def test_nullable_job_order_fields_stay_nullable() -> None:
    """`material_name` / `abc_class` 在模型侧可空（ABC 未导入时单据已经可以入队），报文照给 null。"""
    raw = {
        "bulk_batch_no": "BAT-20260908-01",
        "snapshot_version": "2026-09-08T00:00",
        "plans": [
            {
                "job_order_id": "1",
                "order_no": "PO-20260908001",
                "material_code": "3001234",
                "material_name": None,
                "abc_class": None,
                "qty": 12,
                "aisles": ["01"],
                "predicted_cross_aisle": {"material": 1, "threshold": 5, "exceeded": False},
                "priority": 0.50,
                "plan_id": 1,
            }
        ],
        "degraded_alerts": [],
    }
    assert BatchAllocateResponse.model_validate(raw).plans[0].abc_class is None


# --- OpenAPI 可生成 ----------------------------------------------------------------


def test_dtos_render_into_openapi() -> None:
    """tasks.md 1.1 的「`/openapi.json` 能生成该模型」。

    这里用一个**只挂这两个模型**的探针 app，而不是 `create_app()` —— 端点在 task 8.1
    才落地，拿真实 app 断言会让本用例在 8.1 之前一直红，把 pre-commit 的 L1 门禁拖住。
    探针验证的正是这条性质本身：模型能被 FastAPI 渲染成 OpenAPI 组件（字段注解、
    嵌套模型、字面量类型有一样不合规就会在这里炸）。8.1 再在真实 app 上补一道断言。
    """
    probe = FastAPI()

    @probe.post("/probe", response_model=BatchAllocateResponse)
    def _probe(payload: BatchAllocateRequest) -> BatchAllocateResponse:  # pragma: no cover
        raise NotImplementedError

    schemas = probe.openapi()["components"]["schemas"]
    for name in ("BatchAllocateRequest", "BatchAllocateResponse"):
        assert name in schemas
    # ReasonPayload 未出现在任何一个端点的签名里，故不进 components —— 这是 OpenAPI 的
    # 正常行为，不是缺陷；它可生成由下面的往返用例负责。
    assert ReasonPayload.model_json_schema()["title"] == "ReasonPayload"


def test_reason_payload_is_json_serializable_and_round_trips() -> None:
    """理由体要落 `RecommendationPlan.payload_json`（JSON 列）—— 往返后必须逐项相等。"""
    payload = ReasonPayload.model_validate(_reason())
    again = ReasonPayload.model_validate_json(payload.model_dump_json())
    assert payload == again
