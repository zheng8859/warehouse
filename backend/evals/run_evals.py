"""Evals 运行入口。

事实来源：20-评测体系设计；00-总体开发方案 §3.2 / §3.3 / §4.3

三层金字塔（13 维 / 60 场景）：
    L1 单元   5 维 × 24 场景   通过线 ≥95%        每次构建（pre-commit）
    L2 集成   4 维 × 20 场景   通过线 ≥90%        每个工具组完成
    L3 质量   4 维 × 16 场景   达 PRD 验收口径    每日收工 / push 前 / 试点评测

场景编号：SC-001~006 评分正确性 / CL-001~006 作业闭环一致性 /
          AC-001~005 分配合规 / TR-001~005 集中度趋势

劣化处理（00 §3.3）：≤3% 记录；3~5% 走 /investigate 定位 golden 样本；
>5% 定位 bad commit 并 revert 或修复。

⚠️ 骨架阶段（阶段一）本脚本**尚未实现任何评测**，因此默认以退出码 3 失败，
   避免"钩子跑了但什么都没检查"这种静默通过。阶段六（文档 30）实现后，
   再把 .pre-commit-config.yaml 里的 evals-baseline 钩子取消注释。
"""
from __future__ import annotations

import argparse
import sys

TIERS = ("l1", "l2", "l3", "all")

#: 阶段六实现；此处登记以便 --list 时能看全貌。
PLANNED_TIERS = {
    "l1": "5 维 × 24 场景（评分正确性 / 作业闭环 / KPI 计量 / 配置合规 / 安全隔离），通过线 ≥95%",
    "l2": "4 维 × 20 场景（数据接入质量 / 错误恢复降级 / 分配合规 / 指令可靠性），通过线 ≥90%",
    "l3": "4 维 × 16 场景（集中度趋势 / 移库有效性 / 性能基线 / KPI 基线达标），达 PRD 验收口径",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="成品库位智能推荐 — Evals 运行入口")
    parser.add_argument("--tier", choices=TIERS, default="all", help="运行哪一层")
    # `%%` 是**必须的转义**，不是笔误：argparse 会用 `help_string % params` 展开帮助文本
    # （`_expand_help`，为的是替换 `%(default)s` 这类占位符），于是裸 `%` 后面跟着中文
    # 会被当成格式说明符 → `ValueError: unsupported format character`。
    # 而它发生在**构造 parser 时**，也就是每一次调用（含 `--list`）都在 argparse 里崩成
    # 退出码 1 + traceback，永远走不到本文件那段「以退出码 3 失败」的设计。
    # 回归由 tests/logic/test_run_evals.py 守着。
    parser.add_argument("--compare", metavar="BASELINE_JSON", help="与基线对比，劣化 >5%% 判定失败")
    parser.add_argument("--list", action="store_true", help="只列出计划中的评测层，不执行")
    parser.add_argument(
        "--allow-skeleton",
        action="store_true",
        help="骨架阶段允许空跑（仅用于验证接线，不作为质量证据）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        for tier, desc in PLANNED_TIERS.items():
            print("%-4s %s" % (tier, desc))
        return 0

    print("Evals 尚未实现（阶段六 / 文档 30）—— 本次未运行任何评测。", file=sys.stderr)
    print("计划中的三层：", file=sys.stderr)
    for tier, desc in PLANNED_TIERS.items():
        print("  %-4s %s" % (tier, desc), file=sys.stderr)

    if args.allow_skeleton:
        print(
            "\n--allow-skeleton 已指定：空跑通过。此结果**不构成**质量证据，"
            "不得用于 push 门禁的放行依据。",
            file=sys.stderr,
        )
        return 0

    return 3


if __name__ == "__main__":
    raise SystemExit(main())
