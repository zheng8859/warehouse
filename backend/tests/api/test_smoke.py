"""阶段一装配冒烟测试。

事实来源：
  19-系统架构与部署视图 §2.2（五层架构）、附录B（路由）
  13-权限分级与访问控制系统 §6.1（认证白名单）

阶段一的交付物是**能装配、能启动的骨架**，所以这里只断言三件当时就该成立的事：

  1. 9 个路由模块全部挂载（装配完整性，防止后续改动静默漏挂）
  2. 认证中间件真的在链上（不是"配置了但没生效"）
  3. 白名单逐条生效 —— 特别是 `/api/health` 需认证这条，它对应 13 与 19
     之间已标记的文档不一致（见 app/api/middleware.py 模块 docstring）

**刻意不碰数据库**：本文件不起 SQLite、不建表。阶段一的端点没有一条读库，
冒烟测试若因为库没建而红，红的就是错误的东西。库相关的测试随阶段二的模型落地。

L1 单元，须 < 5 秒（00-总体开发方案 §4.3 pre-commit 门禁）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import WHITELIST_EXACT, _is_whitelisted
from app.main import ROUTE_MODULES, app, create_app

pytestmark = pytest.mark.api


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ------------------------------------------------------------------ 装配完整性


def test_app_imports_and_is_fastapi() -> None:
    """模块级 `app` 可导入 —— 这是 `uvicorn app.main:app` 能起来的充分条件。"""
    assert isinstance(app, FastAPI)


def test_create_app_is_idempotent() -> None:
    """create_app() 可重复调用。

    测试会反复装配应用；若装配过程把状态写进模块级全局，第二次就与第一次不同。
    这里只断言能重复装配且路由数一致，不比对对象标识。
    """
    assert len(create_app().routes) == len(create_app().routes)


def test_all_route_modules_are_mounted() -> None:
    """ROUTE_MODULES 里每个模块的每条路由都在 app 上。"""
    mounted = {(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", ()) or ())))
               for r in app.routes}

    for module in ROUTE_MODULES:
        for route in module.router.routes:
            key = (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
            assert key in mounted, f"{module.__name__} 的 {route.path} 未挂载到 app"


# ------------------------------------------------------------------ 白名单与中间件


def test_health_is_public(client: TestClient) -> None:
    """`/health` 免认证 —— 13 §6.1 白名单内的探活端点。"""
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["warehouse"] == "GTJ10036"  # 首期单厂试点（config.warehouse_code）


def test_docs_and_openapi_are_public(client: TestClient) -> None:
    """文档路径在 13 §6.1 白名单内，必须在中间件之前放行。"""
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_api_health_requires_auth(client: TestClient) -> None:
    """`/api/health` **需**认证。

    19 附录B 定义了该端点，13 §6.1 的白名单却没有它 —— 按 13 字面实现，
    即探活请用 `/health`。本测试把该决策钉住：若有人"顺手"把它加进白名单，
    这里会红，改动就得先回答文档不一致的问题。
    """
    assert client.get("/api/health").status_code == 401


def test_unknown_api_path_is_401_not_404(client: TestClient) -> None:
    """不存在的 `/api/*` 返回 401 而非 404。

    中间件守卫整个 `/api/*` 前缀，凭据不合格一律 401 且不区分原因
    （middleware.py："避免成为探测面"）。404 会泄露"该路径不存在"这一信息。
    """
    assert client.get("/api/definitely-not-a-route").status_code == 401


def test_whitelist_exact_matches_doc_13() -> None:
    """白名单常量与 13 §6.1 逐条一致。

    这条断言刻意贴住常量本身，而不是只走 HTTP：`/api/auth/login` 的响应取决于
    阶段二的实现，用 HTTP 断言它会把冒烟测试绑到尚未存在的代码上。
    """
    assert WHITELIST_EXACT == frozenset(
        {"/api/auth/login", "/health", "/docs", "/openapi.json"}
    )


def test_whitelist_matching_rules() -> None:
    """精确匹配 + `/docs/` 前缀放行；`/api/*` 其余路径不放行。"""
    assert _is_whitelisted("/health")
    assert _is_whitelisted("/docs/oauth2-redirect")  # 子资源按前缀放行
    assert not _is_whitelisted("/api/health")
    assert not _is_whitelisted("/healthz")  # 不做模糊匹配
