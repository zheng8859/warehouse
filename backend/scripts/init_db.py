"""建库入口：`alembic upgrade head` + 开发种子。

事实来源：openspec/changes/data-model-permission/design.md D8
          openspec/changes/data-model-permission/tasks.md 1.5

本脚本**只做两件事**，因为它只能是这两件事：

  1. 把库升到 `head` —— 用 Alembic，不用 `create_all`
  2. 调用 `scripts/seed_dev.py` 写开发种子

为什么不能有 `create_all` 分支（D8）：建库路径一旦有两条，必然漂移 ——
`create_all` 建出的库没有 `alembic_version` 记录，后续迁移会在它上面从头 ALTER，
报出「表已存在」或更糟的「静默跳列」。而 `autogenerate` 会把漂移当成待处理的
diff，产出一堆假变更。所以真实库**唯一**的建表入口是迁移。

用法（cwd 不限，脚本自己把 backend/ 放进 sys.path）：

    python scripts/init_db.py              # 迁移 + 种子
    python scripts/init_db.py --no-seed    # 只迁移
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本脚本在 backend/scripts/ 下；把 backend/ 提前放进 sys.path，
# 这样无论从哪个 cwd 调起，`import app.*` 与 `import scripts.*` 都能解析。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from scripts.seed_dev import seed  # noqa: E402

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def run_migrations() -> None:
    """把库升到 head。

    库文件不存在时 SQLite 会自行创建，不必先 `touch`；但父目录必须存在，
    否则 sqlite3 报 "unable to open database file"（默认 `./data/` 由 .gitkeep 占位）。
    """
    cfg = Config(str(ALEMBIC_INI))
    # alembic.ini 里 `prepend_sys_path` 已钉到 %(here)s，这里再兜一层：
    # 本脚本可能被人以任意 cwd 调起，而 env.py 的 `import app.models` 失败时
    # 报的是 ModuleNotFoundError，与「库连不上」很难区分。
    cfg.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建库 + 开发种子")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="只跑迁移，不写开发种子",
    )
    args = parser.parse_args(argv)

    print(f"[init_db] 数据库：{settings.database_url}")
    run_migrations()
    print("[init_db] 迁移完成（upgrade head）")

    if args.no_seed:
        print("[init_db] 按 --no-seed 跳过种子")
        return 0

    if settings.is_prod:
        # 种子是开发数据（GTJ10036 示例巷道与库位），写进生产库是事故。
        # 这里不做静默跳过 —— 打印原因，让运维能判断是否真的调错了环境。
        print("[init_db] 当前 environment=prod，跳过开发种子（迁移已执行）")
        return 0

    session = SessionLocal()
    try:
        written = seed(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if written:
        for line in written:
            print(f"[init_db] 种子：{line}")
    else:
        print("[init_db] 种子：无可写入的分组（见 scripts/seed_dev.py 的填充约定）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
