"""`DomainError` 体系的渲染契约。

事实来源：13-权限分级与访问控制系统 §六（三层检查）、§8.3（紧急吊销）
          spec `auth`「凭据问题一律 401」
          app/api/middleware.py 与 app/core/errors.py 的模块 docstring

## 为什么值得单独几条断言

401 有**两个**产生方：中间件（在 `app.exception_handler` **之外** —— 它包着整个应用，
拿不到处理器那条路径，只能自己构造响应）与登录端点（抛 `DomainError`，走处理器）。
前端拦截器按 `error` 分流（401 → 跳登录页），所以两份响应体必须逐字一致，
否则它得为同一个 401 写两个分支。

「两处各自写对」是这次代码评审挑出的问题：两处原本各手写一份 dict，谁改了
`detail` 的处置都会让形状漂移，而漂移的那一侧不会有任何测试变红。
故渲染收进 `errors.error_body`，两处都调它 —— 本文件把这条**结构上的同形**
钉住，而不是断言「当前这两份恰好相同」（那种断言在漂移发生的那一刻仍然是绿的）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.api.middleware import _unauthorized
from app.core.errors import Unauthenticated, ValidationBlocked, error_body

pytestmark = pytest.mark.logic

MIDDLEWARE_SOURCE = Path(__file__).resolve().parents[2] / "app" / "api" / "middleware.py"


# ------------------------------------------------------------------ 渲染规则

def test_error_body_carries_the_code_and_the_message() -> None:
    """基本形状：`error` 取自异常类、`message` 取自实例。"""
    assert error_body(Unauthenticated("账号不可用，请联系管理员")) == {
        "error": "unauthenticated",
        "message": "账号不可用，请联系管理员",
    }


def test_error_body_includes_detail_only_when_it_is_not_none() -> None:
    """`detail` 仅在**不是 None** 时出现 —— 判定是 `is not None`，不是「真值」。

    401 不带 `detail`、403/409 带（前端据此分流），这个分法只有一处定义。
    这里刻意把 `0` / `""` / `{}` 这些**假值**也钉成「会出现」：写成
    `if exc.detail:` 时它们会被静默丢掉 —— 而一个 `detail={}` 的 403 与一个
    没有 detail 的 403，在前端看来是两种不同的响应。
    """
    assert "detail" not in error_body(Unauthenticated("x"))
    assert "detail" not in error_body(ValidationBlocked("x", detail=None))

    for falsy in (0, "", {}, [], False):
        assert error_body(ValidationBlocked("x", detail=falsy))["detail"] == falsy


def test_both_401_producers_render_identically() -> None:
    """中间件的 401（自己构造响应）与端点的 401（处理器渲染）**逐字一致**。

    这是 `auth` spec「凭据问题一律 401」在**形状**上的那一半：状态码与响应体
    都取自同一处 —— 状态码取自 `Unauthenticated.http_status`，响应体取自
    `error_body`。改类上的常量会同时改掉两处，这正是想要的效果。
    """
    message = "账号不可用，请联系管理员"

    response = _unauthorized(message)

    assert response.status_code == Unauthenticated(message).http_status == 401
    assert json.loads(response.body) == error_body(Unauthenticated(message))
    assert set(json.loads(response.body)) == {"error", "message"}


# ------------------------------------------------------------------ 结构守卫

def test_middleware_does_not_hand_write_the_401_body() -> None:
    """AST 查 `middleware.py` 的 `_unauthorized`：它必须**调** `error_body`，
    且体内不得出现字面量 `"unauthenticated"`。

    断言「两处当前相同」挡不住下次漂移 —— 手写回 dict 的那一刻两处仍然相同，
    直到有人给其中一处加字段。所以查的是结构：状态码与响应体都从异常类来。

    用 AST 而不是子串匹配：子串会把注释或 docstring 里解释「为什么不再手写」的
    那句话判成违规，于是只有删掉解释才能变绿 —— 正好把最有价值的一段挤掉
    （与 `test_token.py` 的 JWT 库守卫、`test_migrations.py` 的 create_all 守卫同一处置）。
    顺带查完整模块：`"unauthenticated"` 这个字面量在 middleware.py 里**一处都不该有**。
    """
    tree = ast.parse(MIDDLEWARE_SOURCE.read_text(encoding="utf-8"), filename=str(MIDDLEWARE_SOURCE))

    unauthorized = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_unauthorized"
    )
    calls = {
        node.func.id
        for node in ast.walk(unauthorized)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "error_body" in calls, "_unauthorized 必须调 errors.error_body 渲染，不得自己拼 dict"

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "unauthenticated" not in literals, (
        "middleware.py 里出现了字面量 'unauthenticated' —— 它只应来自 Unauthenticated.code"
    )
