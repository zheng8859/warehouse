"""建库路径的守卫测试。

事实来源：openspec/changes/data-model-permission/design.md D8
          openspec/changes/data-model-permission/tasks.md 1.5（验证：源码内不出现 create_all）

D8：真实库**唯一**的建表入口是 `alembic upgrade head`。这条不变量靠「没人写
第二个入口」维持 —— 而人会忘，所以钉成测试。

**断言走 AST 而非子串匹配**。`scripts/init_db.py` 的 docstring 里正当地提到了
`create_all` 这个词（解释它为什么被禁），子串匹配会把这些解释性文字判成违规，
于是把注释删掉才能让测试变绿 —— 正好把最有价值的那段"为什么"挤掉。要禁的是
**调用**，不是这个字符串本身。

后续测试落点（阶段二内，见 tasks）：2.7 / 3.5 / 4.x / 5.x / 6.x 各自的
「`autogenerate` 产生空 diff」断言也属于本文件 —— 它们同样在守 D8。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.model

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = BACKEND_DIR / "scripts"

#: 真实库的建表入口白名单 —— 只有迁移。
GUARDED_SOURCES = (SCRIPTS_DIR / "init_db.py", SCRIPTS_DIR / "seed_dev.py")


def _create_all_calls(path: Path) -> list[int]:
    """返回源码中 `*.create_all(...)` 调用的行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "create_all":
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("path", GUARDED_SOURCES, ids=lambda p: p.name)
def test_build_scripts_never_call_create_all(path: Path) -> None:
    """建库脚本不得调用 `create_all`（D8：两条建库路径必然漂移）。

    漂移的具体后果：`create_all` 建出的库没有 `alembic_version` 记录，后续迁移
    会在它上面从头 ALTER，于是要么报「表已存在」，要么更糟 —— 静默跳列。
    """
    assert path.exists(), f"守卫目标缺失：{path}"
    assert _create_all_calls(path) == [], (
        f"{path.name} 出现了 create_all 调用；真实库必须只经 alembic 迁移建表（D8）"
    )


def test_guard_would_actually_catch_a_violation(tmp_path: Path) -> None:
    """守卫自检：若检测逻辑失效（例如 AST 遍历写错），上面的断言会静默变成空跑。

    用一个必然违规的样本证明检测器真的会报 —— 否则「通过」可能只是没检出。
    """
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from app.models.base import Base\n\n"
        "def build(engine):\n"
        "    Base.metadata.create_all(engine)\n",
        encoding="utf-8",
    )
    assert _create_all_calls(sample) == [4]
