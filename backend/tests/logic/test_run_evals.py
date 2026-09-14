"""run_evals 的真实门禁断言（阶段六，任务 8.3）。

事实来源：openspec/changes/evals/tasks.md 8.3；design.md D5/D8/D9；
          00-总体开发方案 §3.3（劣化分流）/ §4.3（门禁）。

本文件**替代**阶段一的「exit-3 骨架契约」——run_evals 已实现真实评测，`main([])` 不再返回 3。
测的是**门禁判定逻辑**，不真跑 pytest：`_run_pytest`（subprocess）被 monkeypatch 成返回固定
stdout 的 `FakeProc`，据此断言三层闸门（阈值 / 劣化 / P0）的退出码与报告字段。
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

import evals.run_evals as re

pytestmark = pytest.mark.logic


class FakeProc:
    """伪造 subprocess.CompletedProcess，只带 `_run_pytest` 消费的两个字段。"""

    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _args(**kw) -> Namespace:
    defaults = dict(
        tier="l1", dimension=None, compare=None, save_baseline=False,
        run_slow=False, json=False, list=False,
    )
    defaults.update(kw)
    return Namespace(**defaults)


# ---------------------------------------------------------------------------
# 入口契约（parser / --list / 帮助文本）
# ---------------------------------------------------------------------------


def test_parser_constructs() -> None:
    assert re.build_parser() is not None


def test_compare_help_survives_percent_expansion() -> None:
    """`--compare` 帮助文本里保留 `5%` 字样（`%%` 转义后展开剩一个 `%`）。"""
    actions = [a for a in re.build_parser()._actions if "--compare" in a.option_strings]
    assert len(actions) == 1
    assert "5%" in actions[0].help


def test_tier_choices_match_the_pyramid_levels() -> None:
    assert re.TIERS == ("l1", "l2", "l3", "all")


def test_list_returns_zero_and_lists_every_tier(capsys: pytest.CaptureFixture[str]) -> None:
    assert re.main(["--list"]) == 0
    out = capsys.readouterr().out
    for tier in ("l1", "l2", "l3"):
        assert tier in out


# ---------------------------------------------------------------------------
# 门禁判定（monkeypatch _run_pytest，不真跑 pytest）
# ---------------------------------------------------------------------------


def test_threshold_gate_l1_below_95_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 通过率 <95% → 阈值门禁失败，退出码 1。"""
    monkeypatch.setattr(
        re, "_run_pytest",
        lambda *a, **k: FakeProc("25 passed, 5 failed in 0.1s", returncode=1),
    )
    report = re._evaluate(_args(tier="l1"))
    assert report["tiers"]["l1"]["pass_rate"] == pytest.approx(25 / 30)
    assert report["tiers"]["l1"]["gate"] is False
    assert report["exit_code"] == re.EXIT_THRESHOLD


def test_l3_gate_is_zero_failure_not_percentage(monkeypatch: pytest.MonkeyPatch) -> None:
    """L3 无百分比门槛（threshold=None）：任何失败都不过闸。"""
    monkeypatch.setattr(
        re, "_run_pytest",
        lambda *a, **k: FakeProc("16 passed, 1 failed in 0.1s", returncode=1),
    )
    report = re._evaluate(_args(tier="l3"))
    assert report["tiers"]["l3"]["threshold"] is None
    assert report["tiers"]["l3"]["gate"] is False
    assert report["exit_code"] == re.EXIT_THRESHOLD


def test_degradation_gt_5pct_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """实测通过率较基线下降 >5pp → 阻断，退出码 3。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"tiers": {"l1": {"pass_rate": 0.95}}}), encoding="utf-8"
    )
    # 24/30 = 0.80，较基线 0.95 下降 15pp。
    monkeypatch.setattr(
        re, "_run_pytest",
        lambda *a, **k: FakeProc("24 passed, 6 failed in 0.1s", returncode=1),
    )
    report = re._evaluate(_args(tier="l1", compare=str(baseline)))
    assert report["degradation"]["l1"]["status"] == "block"
    assert report["degradation"]["l1"]["delta_pp"] == pytest.approx(0.15)
    assert report["exit_code"] == re.EXIT_DEGRADE


def test_degradation_within_3pct_records(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """下降 ≤3pp → 记录，不阻断。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"tiers": {"l1": {"pass_rate": 0.95}}}), encoding="utf-8"
    )
    # 29/30 = 0.9667，较基线 0.95 反而改善 → record。
    monkeypatch.setattr(
        re, "_run_pytest",
        lambda *a, **k: FakeProc("29 passed, 1 failed in 0.1s", returncode=1),
    )
    report = re._evaluate(_args(tier="l1", compare=str(baseline)))
    assert report["degradation"]["l1"]["status"] == "record"
    assert report["exit_code"] != re.EXIT_DEGRADE


def test_p0_guard_failure_blocks_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0 护栏（出域=0 golden_022）失败 → 立即阻断，退出码 2，不靠百分比稀释。"""

    def fake(paths, *, marker=None, keyword=None):
        if keyword:  # P0 那一次调用（-k 选 golden）
            return FakeProc(
                "FAILED evals/l2_integration/test_l2_permission.py::test_golden_022_x - AssertionError\n"
                "2 passed, 1 failed in 0.1s",
                returncode=1,
            )
        return FakeProc("26 passed in 0.1s", returncode=0)

    monkeypatch.setattr(re, "_run_pytest", fake)
    report = re._evaluate(_args(tier="l2"))
    assert report["p0"]["出域"] is False
    assert report["p0"]["决策可追溯"] is False  # 同一条 golden_022 也守可追溯
    assert report["p0"]["台账完整性"] is True
    assert report["exit_code"] == re.EXIT_P0


def test_all_pass_goes_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """全过 → 退出码 0（Go）。"""

    def fake(paths, *, marker=None, keyword=None):
        if marker == "observation":
            return FakeProc("1 passed in 0.1s")
        if keyword:  # P0
            return FakeProc("3 passed in 0.1s")
        return FakeProc("31 passed in 0.1s")

    monkeypatch.setattr(re, "_run_pytest", fake)
    report = re._evaluate(_args(tier="l2"))
    assert report["exit_code"] == re.EXIT_OK


# ---------------------------------------------------------------------------
# 基线回填（拒绝基线下调）
# ---------------------------------------------------------------------------


def test_save_baseline_backfills_and_refuses_downgrade(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"tiers": {"l1": {"pass_rate": None}}}), encoding="utf-8")

    re._save_baseline(b, {"l1": 1.0})
    d = json.loads(b.read_text(encoding="utf-8"))
    assert d["tiers"]["l1"]["pass_rate"] == 1.0
    assert d["baseline_captured"] is not None

    # 拒绝基线下调：实测 0.8 < 现有 1.0 → 保持 1.0
    re._save_baseline(b, {"l1": 0.8})
    d2 = json.loads(b.read_text(encoding="utf-8"))
    assert d2["tiers"]["l1"]["pass_rate"] == 1.0
    assert "拒绝基线下调" in capsys.readouterr().err
