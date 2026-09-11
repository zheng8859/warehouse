"""测试库夹具的契约测试。

事实来源：openspec/changes/data-model-permission/design.md D12
验证：tasks 1.3 —— 「最小用例能取得 session，且 `PRAGMA foreign_keys` 读回为 1」

夹具本身也是代码，也需要被钉住。两条断言分别防两类静默失败：
  - `PRAGMA foreign_keys` 没生效 → 外键约束形同虚设，非法引用写进去也不报错
    （后面 2.6 / 3.4 的 FK 用例会全部假绿）
  - 会话不回滚 → 用例之间数据串味，单跑绿、全跑红
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.model


def test_session_fixture_is_usable(session: Session) -> None:
    """最小用例能取得会话并真的连到库上。"""
    assert session.execute(text("SELECT 1")).scalar() == 1


def test_foreign_keys_are_enabled(session: Session, foreign_keys_on: bool) -> None:
    """外键约束必须真的开着 —— SQLite 默认关闭，不显式打开则 FK 形同虚设。"""
    assert foreign_keys_on is True


def test_journal_mode_is_memory_not_wal(session: Session) -> None:
    """内存库的 journal_mode 是 `memory`，不是 `wal`。

    这条断言把「测试库不设 WAL」这个决定钉住：若有人照搬 `app/core/db.py` 的
    PRAGMA 组合，这里不会红但 WAL 请求本就是空操作 —— 真正要防的是有人
    据此以为「测试跑过了 WAL 路径」。WAL 行为只在真实库上有意义。
    """
    mode = session.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "memory"


def test_session_rolls_back_between_tests(session: Session) -> None:
    """本用例写入的数据不应漏给下一个用例。

    与 `test_session_rolls_back_between_tests` 的后半段成对：这里只写不读，
    由紧随其后的 `_verify_clean` 断言读不到。两个用例必须相邻且同文件。
    """
    session.execute(text("CREATE TABLE IF NOT EXISTS _rollback_probe (x INTEGER)"))
    session.execute(text("INSERT INTO _rollback_probe (x) VALUES (1)"))
    session.commit()


def test_rollback_probe_verify_clean(session: Session) -> None:
    """上一个用例的提交必须已被回滚。"""
    exists = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='_rollback_probe'")
    ).scalar()
    assert exists is None, "会话未回滚 —— 用例之间会互相污染"
