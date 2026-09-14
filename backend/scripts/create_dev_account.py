"""开发期账号自举：本地建一个 ACTIVE 账号，供登录与联调。

事实来源：tasks.md 9.4g③（「空库跑完 init_db.py 后怎么建第一个账号」的已登记差口）、
          13-权限分级与访问控制系统 §5.1（管理员开通 + 显式激活）、
          app/core/security.py（bcrypt 直接哈希，不用 passlib，见 CLAUDE.md §五）

**这是开发期替代工具，不是账号创建端点**：13 §5.3 的账号开通端点是路线图（与 D9 同批）。
在它落地前，本脚本让「空库 → 能登录」成为可能，且**不把口令抄进任何会被提交的文件**。

用法（backend/ 下）：
    python scripts/create_dev_account.py <username> <password> <role>

role ∈ warehouse_keeper / planner / supervisor / admin（13 §一，lowercase 枚举值）。
创建后 status=ACTIVE（开发期直接可用；生产走「开通 → 激活」两步，见 13 §5.1）。
口令只从命令行读、只落 bcrypt 哈希 —— 与 seed_dev.py「种子不预置口令」同一红线。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 独立脚本运行时 sys.path[0] 是 scripts/ 而非 backend/，`import app` 会失败；
# 把 backend 根加进去，与 pytest 的 rootdir 行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.models  # noqa: F401  注册全部模型，保证 Base.metadata 完整
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.enums import AccountStatus, Role
from app.core.security import hash_password
from app.models.base import Base
from app.models.identity import Account


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "用法: python scripts/create_dev_account.py <username> <password> <role>",
            file=sys.stderr,
        )
        print("role ∈ " + " / ".join(role.value for role in Role), file=sys.stderr)
        return 2

    username, password, role_raw = argv[1], argv[2], argv[3]
    try:
        role = Role(role_raw)
    except ValueError:
        print(
            f"未知角色 {role_raw!r}；允许：{' / '.join(r.value for r in Role)}",
            file=sys.stderr,
        )
        return 2

    Base.metadata.create_all(engine)

    with SessionLocal() as session:
        existing = session.scalars(
            select(Account).where(Account.username == username)
        ).first()
        if existing is not None:
            print(
                f"账号 {username} 已存在（role={existing.role.value}，"
                f"status={existing.status.value}）—— 未改动"
            )
            return 0

        account = Account(
            warehouse_id=settings.warehouse_code,
            username=username,
            password_hash=hash_password(password),
            role=role,
            status=AccountStatus.ACTIVE,
        )
        session.add(account)
        session.commit()
        print(
            f"已创建账号 {username}（role={role.value}，status=active，"
            f"warehouse={settings.warehouse_code}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
