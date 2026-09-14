"""evals/conftest.py 的冒烟测试：取到 4 角色 token、client、golden 目录、session。"""
from __future__ import annotations

import pytest

from app.core.enums import Role


@pytest.mark.l2
def test_eval_api_has_four_roles(eval_api):
    assert set(eval_api.tokens) == {Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR, Role.ADMIN}
    # 每个 token 都能通过认证中间件（/health 白名单外，用一个受保护端点验 401→200 的边界）
    for role in Role:
        resp = eval_api.client.get("/api/allocate/batch", headers=eval_api.headers(role))
        assert resp.status_code != 401, f"{role} token 应已认证"


@pytest.mark.l2
def test_golden_loaded_60_scenarios(golden):
    assert len(golden) == 60
    layers = {"baseline": 0, "boundary": 0, "regression": 0}
    for sample in golden.values():
        layers[sample["layer"]] += 1
    assert layers == {"baseline": 24, "boundary": 20, "regression": 16}


@pytest.mark.l1
def test_session_and_make_scenario(session):
    from tests.logic.conftest import make_scenario

    scenario = make_scenario(
        session,
        aisles=[],
        materials=[],
    )
    assert scenario.warehouse_id == "GTJ10036"
