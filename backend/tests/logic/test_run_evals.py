"""Evals 入口的**骨架契约**测试（阶段一交付物，阶段二验收时发现失修）。

事实来源：CLAUDE.md §二（`python evals/run_evals.py --list`）、§十一（「现在跑刻意以退出码
          3 失败」）、00-总体开发方案 §3.2 / §4.3
          backend/.pre-commit-config.yaml 第 59~68 行（pre-push 的 evals 钩子待阶段六启用）

## 为什么阶段二会来测阶段六的文件

9.3 要求为完成标准 #9（`run_evals --tier l1`）出具**书面豁免记录**，豁免理由是「阶段六实现」。
写这条记录时先跑了一遍，发现脚本**根本没走到**那个设计好的「退出码 3」——
`--compare` 的帮助文本里有一个裸 `%`（`劣化 >5% 判定失败`），argparse 用
`help_string % params` 展开帮助时会把它当格式说明符，抛
`ValueError: unsupported format character`。而它发生在**构造 parser 时**，于是
**每一次**调用（含 `--list`）都在 argparse 里崩成退出码 1 + traceback。

后果不是「少了个功能」，而是两处文档说的都不成立：`--list` 不能列、退出码不是 3。
骨架的价值全在「它明确地失败」——崩在参数解析上，与「刻意以退出码 3 失败」是
两件不同的事，而后者才是有意的设计。修法是转义成 `%%`（一处字符）。

## 这里测的是**入口契约**，不是评测内容

断言只有三类：能构造、三种调用各自的退出码、`--list` 真的列出了三层。
**不测任何评测行为** —— 阶段六实现后，`main([])` 就不再返回 3，届时本文件删掉或改写成
真实 eval 的门禁断言。现在它的职责是把「退出码 3 + `--list` 可用」这个**阶段一契约**
钉住，防止它在下一次无人注意时再次失修。
"""
from __future__ import annotations

import pytest

from evals.run_evals import PLANNED_TIERS, TIERS, build_parser, main

pytestmark = pytest.mark.logic


def test_parser_constructs() -> None:
    """构造 parser 不抛异常 —— 就是这一条抓到了那次故障。

    argparse 的 `%` 展开在 `add_argument` 时就执行，所以裸 `%` 是**构造期**错误：
    单独调 `build_parser()` 就能复现，不必真跑评测。
    """
    assert build_parser() is not None


def test_compare_help_survives_percent_expansion() -> None:
    """`--compare` 的帮助文本里保留 `5%` 字样。

    转义的语义必须与原文一致：写成 `%%` 是让 argparse 展开后**剩下**一个 `%`，
    而不是把那个百分号删掉（删掉的话「劣化 >5% 判定失败」变成了别的意思）。
    """
    parser = build_parser()
    actions = [a for a in parser._actions if "--compare" in a.option_strings]

    assert len(actions) == 1
    assert "5%" in actions[0].help


def test_tier_choices_match_the_three_pyramid_levels() -> None:
    """`--tier` 的取值是三层金字塔 + `all`（20 号 / 00 §3.2）。"""
    assert TIERS == ("l1", "l2", "l3", "all")


def test_list_returns_zero_and_lists_every_planned_tier(capsys: pytest.CaptureFixture[str]) -> None:
    """`--list` 返回 0 并列出三层 —— CLAUDE.md §二把它当命令列着。

    断言的是「三层各自被打印」，不是打印格式：目的是保证这条路能走到 `main` 里那段
    Print 循环，而不是只保证退出码是 0。
    """
    assert main(["--list"]) == 0

    out = capsys.readouterr().out
    for tier in PLANNED_TIERS:
        assert tier in out


def test_unimplemented_run_exits_three(capsys: pytest.CaptureFixture[str]) -> None:
    """默认（或指定任一层）**以退出码 3 失败** —— 刻意不静默通过。

    3 而不是 0 或 1：0 会被门禁当成「评测通过」，1 与真实报错难以区分（阶段六的钩子
    要按退出码分流）。这条是 9.3 豁免记录里「脚本按设计明确失败」的**唯一**证据。
    """
    assert main([]) == 3
    capsys.readouterr()

    assert main(["--tier", "l1"]) == 3
    capsys.readouterr()

    assert main(["--tier", "all"]) == 3


def test_allow_skeleton_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """`--allow-skeleton` 空跑通过 —— 仅用于验证接线，不作为质量证据。"""
    assert main(["--allow-skeleton"]) == 0

    err = capsys.readouterr().err
    assert "不构成" in err, "必须写明该结果不能当质量证据"


def test_skeleton_run_prints_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """失败路径的输出走 **stderr**：stdout 若被脚本管道消费，不能混进"结果"里。

    `--list` 走 stdout（那是给人看的结果），未实现的失败走 stderr ——
    这条把两者的分流钉住。
    """
    main([])
    captured = capsys.readouterr()

    assert "阶段六" in captured.err
    assert captured.out == ""
