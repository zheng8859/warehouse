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


#: tasks.md 8.5 要求「新增未知 `/api/*` 路径的 401 断言」—— 单条随机串说服力不够：
#: 它只能证明"一个明显不存在的路径"被拦。下面这些是**探测者真正会试的形状**：
#: 已知路由的子路径、将来大概会有的路径、以及带尾斜杠 / 无尾斜杠两种写法。
#: 它们对**未认证**请求必须全部 401，一个 404 就足够把"哪里真的存在"指出来。
_UNKNOWN_API_PATHS: tuple[str, ...] = (
    "/api/definitely-not-a-route",
    "/api/auth/login/extra",  # 白名单路径的子路径 —— 白名单是精确匹配，不该被前缀放行
    "/api/auth",              # 白名单路径的父路径
    "/api/kpi/dashboard",     # 阶段四才有的看板端点（还没实现）
    "/api/ledger/write",      # 红线条目：这条路不该存在，更不该免认证
    "/api/",
    "/api",
)


@pytest.mark.parametrize("path", _UNKNOWN_API_PATHS)
def test_every_unknown_api_path_is_401(client: TestClient, path: str) -> None:
    """8.5：未知 `/api/*` 一律 401，**无一例外**。"""
    resp = client.get(path)

    assert resp.status_code == 401, f"{path} 返回了 {resp.status_code}"


def test_unknown_path_response_is_indistinguishable_from_a_known_one(client: TestClient) -> None:
    """两种情况的 401 **逐字节相同** —— 这才是"不区分原因"的落点。

    只断言 `status_code == 401` 挡不住「未知路径 401 + `{"error":"not_found"}`、
    已存在路径 401 + `{"error":"unauthenticated"}`」这类实现：状态码一致而响应体
    不一致，探测者照样能一条一条地问出哪些路由存在。所以这里比的是**整个响应体**，
    并且顺带钉住它的形状（`error` 与 `message` 两个键，中间件与登录端点同形 ——
    `test_auth.py` 另有一条守着登录端点那一份）。
    """
    unknown = client.get("/api/definitely-not-a-route")
    known = client.get("/api/health")  # 真实存在（19 附录B），但需认证

    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json()
    assert set(unknown.json()) == {"error", "message"}
    assert unknown.json()["error"] == "unauthenticated"


def test_whitelist_exact_matches_doc_13() -> None:
    """白名单常量与 13 §6.1 逐条一致。

    这条断言刻意贴住常量本身，而不是只走 HTTP：`/api/auth/login` 的响应取决于
    阶段二的实现，用 HTTP 断言它会把冒烟测试绑到尚未存在的代码上。
    """
    assert WHITELIST_EXACT == frozenset(
        {"/api/auth/login", "/health", "/docs", "/openapi.json"}
    )


def test_whitelist_matching_rules() -> None:
    """**只有**精确匹配；`/api/*` 其余路径一律不放行。

    曾经这里断言 `/docs/oauth2-redirect` 按 `/docs/` 前缀放行。那条前缀规则已删
    （理由见 `middleware.WHITELIST_EXACT` 的注释：`/docs` 页面不请求任何本站子路径，
    而白名单每多一条就多一个免认证入口）。13 §6.1 的清单是**四条**，
    所以从「四条」这一侧测 —— 前缀放行是一种很难被注意到的扩大。
    """
    assert _is_whitelisted("/health")
    assert _is_whitelisted("/docs")
    assert not _is_whitelisted("/docs/oauth2-redirect")  # 不做前缀放行
    assert not _is_whitelisted("/api/health")
    assert not _is_whitelisted("/healthz")  # 不做模糊匹配
    assert not _is_whitelisted("/api/auth/login/extra")  # 不做子路径放行
