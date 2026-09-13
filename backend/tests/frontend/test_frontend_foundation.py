"""前端基础层组件冒烟（31 号 · 基线核对 + 缺口补齐）。

无浏览器、无业务级 /qa —— 只用 pytest 静态断言核对三件事：
1. 设计令牌取值与 21 §3.3（=24 §3.1）逐键一致，派生冲突色已对齐权威值；
2. 角色菜单可见性矩阵与 13 §3.1 一致（7/4/6/8）；
3. 四态占位 + .node 流程状态卡的 class 齐备且引用正确令牌。

事实来源：31 号「先核对、后补缺」；断言值硬编码（D7 决策），与文档正本一致。
"""

import re
from pathlib import Path

# backend/tests/frontend/test_frontend_foundation.py
#   -> parents[0] = backend/tests/frontend
#   -> parents[1] = backend/tests
#   -> parents[2] = backend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
STYLES = (FRONTEND / "assets" / "styles.css").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "assets" / "app.js").read_text(encoding="utf-8")
LOGIN_HTML = (FRONTEND / "login.html").read_text(encoding="utf-8")

# 21 §3.3（=24 §3.1 原样内联）12 个色令牌权威值
AUTHORITATIVE_TOKENS = {
    "--bg": "#f5f7fa",
    "--panel": "#fff",
    "--ink": "#1f2d3d",
    "--muted": "#6b7a90",
    "--line": "#c9d4e0",
    "--blue": "#2f6fed",
    "--blue-soft": "#e8f0fe",
    "--amber": "#e0a000",
    "--amber-soft": "#fff7e0",
    "--green": "#1f9d55",
    "--red": "#d64545",
    "--grid": "#e4ecf5",
}


def _root_tokens() -> dict:
    """抽取 styles.css 里 :root 块的 token -> value 映射。"""
    m = re.search(r":root\s*\{(.*?)\}", STYLES, re.S)
    assert m, ":root 块缺失"
    return {
        name: val.strip()
        for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1))
    }


def _menu_sets() -> dict:
    """抽取 app.js 里 MENU 的 角色 -> data-tab 集合 映射。"""
    out = {}
    for role in ("warehouse_keeper", "planner", "supervisor", "admin"):
        m = re.search(rf"{role}:\s*\[([^\]]*)\]", APP_JS)
        assert m, f"app.js 缺少角色 {role} 的菜单映射"
        out[role] = re.findall(r"'(p\d)'", m.group(1))
    return out


# ---------- 1. 设计令牌 ----------

def test_root_tokens_match_authoritative_21_3_3():
    """12 个色令牌逐键等于 21 §3.3 权威值（tasks 1.1）。"""
    tokens = _root_tokens()
    for name, expected in AUTHORITATIVE_TOKENS.items():
        assert tokens.get(name) == expected, (
            f"{name} = {tokens.get(name)!r}，期望 {expected!r}（21 §3.3）"
        )


def test_derived_conflict_colors_aligned():
    """三处派生冲突色 + L2 描边 + .reason 列宽对齐权威值（tasks 1.2）。"""
    assert ".pill.ok{background:#e6f6ec;color:var(--green);}" in STYLES  # 21 §3.4
    assert ".btab th{text-align:left;background:#f3f8ff;color:var(--blue);" in STYLES  # 21 §7.6
    assert "box-shadow:0 4px 16px rgba(47,111,237,.4)" in STYLES  # .dock 主色蓝派生阴影
    assert ".chat .bar .qs.l2{border-color:#5b4b96;color:#5b4b96;}" in STYLES  # 24 §6.3
    assert ".reason .rl b{color:var(--blue);flex:0 0 64px;" in STYLES  # 22 §三


def test_old_conflict_token_values_absent_from_root():
    """旧柔和调令牌色值不得残留在 :root（偏差已纠正，而非并存）。"""
    body = re.search(r":root\s*\{(.*?)\}", STYLES, re.S).group(1)
    for stale in ("#5b8def", "#d9a73a", "#5cab7a", "#d46a6a"):
        assert stale not in body, f"旧令牌色值 {stale} 仍残留在 :root"


# ---------- 2. 角色菜单可见性 ----------

def test_menu_matrix_matches_13_3_1():
    """四角色可见集与 13 §3.1 逐行一致（tasks 2.1）。"""
    expected = {
        "warehouse_keeper": ["p1", "p2", "p3", "p4", "p5", "p6", "p8"],  # 7
        "planner": ["p1", "p2", "p6", "p8"],                              # 4
        "supervisor": ["p1", "p4", "p5", "p6", "p7", "p8"],              # 6
        "admin": ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"],      # 8
    }
    got = _menu_sets()
    counts = {role: len(tabs) for role, tabs in got.items()}
    assert counts == {"warehouse_keeper": 7, "planner": 4, "supervisor": 6, "admin": 8}, (
        f"菜单计数 {counts} 与 13 §3.1 的 7/4/6/8 不符"
    )
    for role, tabs in expected.items():
        assert got[role] == tabs, f"{role} 可见集 {got[role]} != {tabs}（13 §3.1）"


def test_role_filter_hooks_present():
    """getRole / applyMenuFilter 钩子存在且登录写入 sessionStorage（tasks 2.2）。"""
    assert "function getRole" in APP_JS
    assert "function applyMenuFilter" in APP_JS
    assert "applyMenuFilter();" in APP_JS
    assert "sessionStorage.setItem('role'" in APP_JS


def test_login_role_selector_has_four_roles():
    """登录页角色下拉覆盖 13 §一 四枚举（tasks 2.2）。"""
    for value in ("admin", "warehouse_keeper", "supervisor", "planner"):
        assert f'value="{value}"' in LOGIN_HTML, f"登录页缺角色选项 {value}"


# ---------- 3. 四态占位 ----------

def test_four_state_classes_present():
    """空/加载/成功/异常 四态 class 与转圈齐备（tasks 3.1）。"""
    for sel in (".state{", ".state.empty", ".state.loading", ".state.success",
                ".state.error", ".spin{", "@keyframes spin"):
        assert sel in STYLES, f"缺四态样式 {sel}"


def test_set_state_hook_present():
    """setState 触发钩子存在并挂到 window（tasks 3.2）。"""
    assert "function setState" in APP_JS
    assert "window.setState = setState" in APP_JS
    # 四态 class 切换逻辑存在
    assert "classList.remove('empty', 'loading', 'success', 'error')" in APP_JS
    assert "classList.add('state', state)" in APP_JS


# ---------- 4. .node 流程状态卡 ----------

def test_node_three_states_aligned():
    """entry 蓝 / exec 绿 / ledger 琥珀，引用正确令牌（tasks 4.1）。"""
    assert ".node.entry{background:var(--blue-soft);color:var(--blue);}" in STYLES
    assert ".node.exec{background:#e6f6ec;color:var(--green);}" in STYLES
    assert ".node.ledger{background:var(--amber-soft);color:var(--amber);}" in STYLES
    assert ".flow{" in STYLES
    assert ".flow .arrow" in STYLES
