"""commit-msg 钩子：校验 Conventional Commits 格式。

事实来源：00-总体开发方案 §4.2

    <type>(<scope>): <subject>
    type : feat / fix / test / refactor / docs / chore / perf
    scope: model / auth / engine / job / import / cap / eval / ui / env / ai / golden-NNN

用法（由 pre-commit 调用）：python scripts/check_commit_msg.py <commit-msg 文件>
"""
from __future__ import annotations

import re
import sys

TYPES = ("feat", "fix", "test", "refactor", "docs", "chore", "perf")
#: env 覆盖环境搭建/脚手架等非子系统改动（00 §4.2 注）；ai 覆盖冷路径 AI 辅助（app/llm/，阶段五）。其余均为子系统。
FIXED_SCOPES = ("model", "auth", "engine", "job", "import", "cap", "eval", "ui", "env", "ai")

#: golden-NNN 是动态 scope（00 §4.2）。
_GOLDEN_SCOPE = re.compile(r"^golden-\d+$")

_PATTERN = re.compile(r"^(?P<type>[a-z]+)\((?P<scope>[a-z0-9\-]+)\): (?P<subject>.+)$")

#: 不需要遵守本规范的消息前缀。
_BYPASS_PREFIXES = ("Merge ", "Revert ", "fixup!", "squash!", "chore(release)")


def validate(message: str) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    subject_line = message.strip().splitlines()[0] if message.strip() else ""

    if not subject_line:
        return ["提交信息为空"]
    if subject_line.startswith(_BYPASS_PREFIXES):
        return []

    match = _PATTERN.match(subject_line)
    if not match:
        return [
            "不符合 Conventional Commits 格式：<type>(<scope>): <subject>",
            "  实际：%s" % subject_line,
            "  示例：feat(model): add AisleCap with pytest tests",
        ]

    problems: list[str] = []
    if match.group("type") not in TYPES:
        problems.append(
            "未知 type %r；允许：%s" % (match.group("type"), " / ".join(TYPES))
        )

    scope = match.group("scope")
    if scope not in FIXED_SCOPES and not _GOLDEN_SCOPE.match(scope):
        problems.append(
            "未知 scope %r；允许：%s 或 golden-NNN"
            % (scope, " / ".join(FIXED_SCOPES))
        )

    if len(match.group("subject").strip()) < 3:
        problems.append("subject 过短，请描述具体改动")

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python scripts/check_commit_msg.py <commit-msg 文件>", file=sys.stderr)
        return 2

    with open(argv[1], encoding="utf-8") as fh:
        message = fh.read()

    problems = validate(message)
    if problems:
        print("提交信息校验未通过：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
