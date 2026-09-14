"""L2 指令可靠性（4 道）：确认卡 / 写意图零台账 / AI Notice / 决策可追溯。

对应 golden `golden_041`（写操作未确认不执行）、`golden_042`（写意图路由未确认零台账）、
`golden_043`（AI 建议琥珀色免责标注，不渲染为系统结论）、`golden_044`（决策可追溯：
每巷 × 每因子取值可见，P0）。

四者同属 CLAUDE.md §四「决策权在人 / 冷路径三条红线」：系统只给建议与理由，绝不静默
改写落位；AI 输出必须带「AI 建议，仅供参考，需人工核实，不自动执行」标注，不渲染成
系统结论。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.core.enums import JobType
from app.schemas.llm import AI_NOTICE, DualProductResponse
from tests.logic.conftest import (
    DEFAULT_WEIGHTS,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
)

pytestmark = pytest.mark.l2

WAREHOUSE = "GTJ10036"
ALLOCATE_URL = "/api/allocate/batch"

MATERIAL = "M1"


def _allocate(client, headers, job_order_ids: list[str]):
    return client.post(
        ALLOCATE_URL,
        json={"warehouse_id": WAREHOUSE, "job_order_ids": job_order_ids},
        headers=headers,
    )


def _seed_inbound(eval_api):
    """一张 A 类入库单 + 两条候选巷道（01 近站台 / 02 非近站台），分配结果恒落 01。"""
    scenario = eval_api.seed(
        aisles=[
            AisleSpec("01", cap_total=100, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec(MATERIAL, abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=10,
                abc_class="A", batch_no="B26090801", job_type=JobType.INBOUND,
            ),
        ],
    )
    return str(scenario.job_orders[0].id)


def test_golden_041_allocate_is_advice_not_execution(eval_api):
    """golden_041：写操作未确认不执行 —— allocate 只出落位建议，零台账、零后验。"""
    order_id = _seed_inbound(eval_api)

    resp = _allocate(eval_api.client, eval_api.headers(), [order_id])

    assert resp.status_code == 200
    (plan,) = resp.json()["plans"]
    assert plan["aisles"] == ["01"]
    # 决策权在人：方案须逐单确认，未确认不落位 —— 不写台账、不写后验。
    assert eval_api.ledgers() == ()
    assert eval_api.verifications() == ()


def test_golden_042_write_intent_zero_ledger_until_confirm(eval_api, monkeypatch):
    """golden_042：L2 写意图路由只出建议，未确认零台账、零作业单。"""
    monkeypatch.setattr(settings, "cold_path_enabled", True)
    eval_api.seed(
        aisles=[AisleSpec(aisle_no=f"{i:02d}", cap_total=100) for i in range(1, 9)],
        materials=[MaterialSpec(material_code=MATERIAL, abc_class="A")],
        inventory=[
            InventorySpec(
                location_code=f"{i:02d}0101", material_code=MATERIAL, batch_no="B1", qty=60 - i
            )
            for i in range(1, 9)
        ],
    )

    resp = eval_api.client.post(
        "/api/conversation/message",
        json={
            "warehouse_id": WAREHOUSE,
            "intent": "RELOCATE_PROPOSE",
            "slots": {"material_code": MATERIAL},
        },
        headers=eval_api.headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "RELOCATE_PROPOSE"
    assert body["write_intent"] is True
    # 红线 2：写意图只回建议，不产生 Ledger / JobOrder（确认后才落地）。
    assert eval_api.ledgers() == ()
    assert eval_api.orders() == ()


def test_golden_043_ai_notice_amber_not_system_conclusion():
    """golden_043：AI 建议渲染琥珀色免责标注（AI Notice），不渲染为系统结论。"""
    response = DualProductResponse(
        rule={"metrics": {}},
        ai="集中度 80% 落在 3 巷",
        ai_generated=True,
        degraded_reason=None,
    )
    dumped = response.model_dump()
    # 非空叙事强制注入免责标注（琥珀色「AI 建议」）。
    assert dumped["ai"].startswith(AI_NOTICE)
    assert "仅供参考" in dumped["ai"] and "不自动执行" in dumped["ai"]
    # rule 是规则算的系统结论，不注入标注。
    assert "AI 建议" not in json.dumps(dumped["rule"], ensure_ascii=False)


def test_golden_044_decision_traceable_per_aisle_per_factor(eval_api):
    """golden_044（P0）：决策可追溯 —— 推荐理由给到每巷 × 每因子取值。"""
    order_id = _seed_inbound(eval_api)

    resp = _allocate(eval_api.client, eval_api.headers(), [order_id])
    (plan,) = resp.json()["plans"]

    reason = eval_api.client.get(
        f"/api/plan/{plan['plan_id']}", params={"warehouse_id": WAREHOUSE}, headers=eval_api.headers()
    ).json()

    # 六因子权重齐备，且候选巷道集 = 每巷分解的键集（每条候选巷都有取值）。
    assert reason["factors"] == dict(DEFAULT_WEIGHTS)
    assert set(reason["breakdown"]) == set(reason["scores"])
    assert set(reason["breakdown"]) >= set(plan["aisles"])
    # 每巷 × 每因子取值可见（无降级时恰为六因子，取值非空）。
    for aisle, terms in reason["breakdown"].items():
        assert set(terms) == set(DEFAULT_WEIGHTS), f"巷道 {aisle} 的分解键集必须恰为六因子"
        assert all(t["value"] is not None for t in terms.values())
