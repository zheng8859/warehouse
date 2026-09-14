"""Evals 运行入口 —— 三层评测金字塔的编排与门禁判定。

事实来源：30-Evals评测体系 §6；00-总体开发方案 §3.2/§3.3/§4.3；
          openspec/changes/evals/design.md D5/D8/D9

本脚本**不做测试逻辑**（design.md D5），只按 `--tier`/`--dimension` 组装 pytest 路径
与 marker，用 subprocess 调 `python -m pytest -c <backend>/pyproject.toml <evals 路径>
-m <marker> -q --tb=no`，解析末尾 `N passed / M failed` 汇总行得通过率。路径一律以
`Path(__file__)` 锚定，不受 cwd 影响（pre-push 钩子 cwd = 仓库根）。

门禁判定（00 §4.3 + baseline.json thresholds）：
    L1 通过率 ≥95% · L2 ≥90% · L3 达验收口径（零失败，无百分比门槛）
    劣化分流（00 §3.3）：>5% 阻断 / 3~5% 告警 / ≤3% 记录（绝对百分点差）
    P0 护栏（台账完整性 / 决策可追溯 / 出域>0）：任一失败 → 立即阻断，不靠百分比稀释

冷路径观测（design.md D8）：以 `observation` marker 独立统计，**不进任何通过率**。

退出码：0 全过 · 1 阈值未达标 · 2 P0 护栏阻断 · 3 劣化 >5% 阻断
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent
PYPROJECT = BACKEND_DIR / "pyproject.toml"
BASELINE_DEFAULT = EVALS_DIR / "baseline.json"

TIERS = ("l1", "l2", "l3", "all")

#: 各层的 pytest marker（`-m` 表达式）。L3 的通过率分母不含 observation（D8）；
#: `slow` 仅 `--run-slow` 时纳入。路径统一跑整个 `evals/`（marker 跨目录选择），
#: 避免 `l2_integration/test_eval_smoke.py` 里那一条 `@pytest.mark.l1` 冒烟被孤立。
TIER_MARKERS: dict[str, str] = {
    "l1": "l1",
    "l2": "l2",
    "l3": "l3 and not observation",
}

#: 各层通过线（CLAUDE.md §十 / 00 §3.2）。L3 无百分比门槛 → None（达口径 = 零失败）。
TIER_THRESHOLDS: dict[str, float | None] = {
    "l1": 0.95,
    "l2": 0.90,
    "l3": None,
}

TIER_DESC = {
    "l1": "单元层 · 5 维 × 24 场景（纯函数，确定性 100%），通过线 ≥95%",
    "l2": "集成层 · 4 维 × 20 场景（API 行为），通过线 ≥90%",
    "l3": "质量层 · 4 维 × 16 场景（验收口径 + 冷路径观测），达 PRD 验收口径",
}

#: 13 维度 → 测试文件（相对 evals/）。维度是 golden 的**可溯源轴**（20号 SC/CL/AC/TR），
#: 测试文件是实现轴，二者非严格 1:1；此处按「golden_ID 落在哪个文件」做 best-effort 映射。
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "评分正确性": ("l1_unit/test_l1_unit_scoring.py", "l1_unit/test_l1_unit_distance_fifo.py", "l1_unit/test_l1_unit_weights.py"),
    "作业闭环一致性": ("l2_integration/test_l2_job.py",),
    "KPI计量正确性": ("l2_integration/test_l2_kpi.py",),
    "配置合规与权限": ("l2_integration/test_l2_permission.py",),
    "安全隔离": ("l2_integration/test_l2_permission.py", "l1_unit/test_eval_utils.py"),
    "数据接入质量": ("l2_integration/test_l2_import.py",),
    "错误恢复降级": ("l1_unit/test_l1_unit_cap.py", "l1_unit/test_l1_unit_degrade.py"),
    "分配合规": ("l1_unit/test_l1_unit_cap.py", "l1_unit/test_l1_unit_degrade.py", "l2_integration/test_l2_job.py"),
    "指令可靠性": ("l2_integration/test_l2_instruction_reliability.py",),
    "集中度趋势": ("l3_quality/test_l3_concentration.py",),
    "移库有效性": ("l3_quality/test_l3_relocate.py",),
    "性能基线": ("l3_quality/test_l3_perf.py",),
    "KPI基线达标": ("l3_quality/test_l3_kpi_baseline.py",),
}

#: P0 护栏（00 §4.3 / 20号 Go/No-Go）：任一失败立即阻断，不靠百分比稀释。
#: 键为护栏名，值为对应 golden 场景 id（测试函数名含 `golden_NNN`，用 `-k` 选中）。
P0_GUARDS: dict[str, tuple[str, ...]] = {
    "台账完整性": ("golden_010", "golden_011"),
    "决策可追溯": ("golden_044",),
    "出域": ("golden_022",),
}

#: 劣化分流阈值（00 §3.3）：>5% 阻断，3~5% 告警，≤3% 记录（绝对百分点差）。
DEGRADE_WARN = 0.03
DEGRADE_BLOCK = 0.05

#: 退出码（见模块 docstring）。
EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_P0 = 2
EXIT_DEGRADE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="成品库位智能推荐 — Evals 运行入口")
    parser.add_argument("--tier", choices=TIERS, default="all", help="运行哪一层（l1/l2/l3/all）")
    parser.add_argument(
        "--dimension",
        choices=sorted(DIMENSIONS),
        default=None,
        help="只跑某一维度（13 维度之一，覆盖维度所落测试文件）",
    )
    # `%%` 是**必须的转义**（argparse 会 `help_string % params` 展开帮助文本），裸 `%`
    # 会在构造 parser 时就抛 `ValueError`，详见 tests/logic/test_run_evals.py 的回归说明。
    parser.add_argument("--compare", metavar="BASELINE_JSON", help="与基线对比，劣化 >5%% 判定失败")
    parser.add_argument("--save-baseline", action="store_true", help="把实测通过率回填 baseline.json（拒绝基线下调）")
    parser.add_argument("--run-slow", action="store_true", help="纳入 slow 慢测（性能基线，默认排除）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（stdout）")
    parser.add_argument("--list", action="store_true", help="只列出三层评测，不执行")
    return parser


# ---------------------------------------------------------------------------
# pytest 子进程
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    """强制子进程以 UTF-8 读写，避免 cp936 下 `text=True` 解码失败。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_pytest(paths: list[Path], *, marker: str | None = None, keyword: str | None = None) -> subprocess.CompletedProcess[str]:
    """调 pytest，返回 CompletedProcess（stdout 含 `-q` 汇总行）。"""
    # 注意：`-q` 不在此显式传 —— pyproject 的 `addopts` 已含 `-q`，再传一个会变成
    # `-q -q`（verbosity -2），pytest 会**吞掉末尾的 `N passed in Xs` 汇总行**，
    # 本脚本就无从解析通过率。`--tb=no` 显式覆盖 addopts 的 `--tb=short`。
    cmd = [sys.executable, "-m", "pytest", "-c", str(PYPROJECT)]
    cmd += [str(p) for p in paths]
    if marker:
        cmd += ["-m", marker]
    if keyword:
        cmd += ["-k", keyword]
    cmd += ["--tb=no", "-rf"]
    return subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )


_COUNT_PATTERNS = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "error": re.compile(r"(\d+) errors?"),
    "deselected": re.compile(r"(\d+) deselected"),
}
_GOLDEN_RE = re.compile(r"golden_\d{3}")


def _parse_counts(output: str) -> dict[str, int]:
    return {k: (int(m.group(1)) if (m := rx.search(output)) else 0) for k, rx in _COUNT_PATTERNS.items()}


def _summary_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line) and " in " in line:
            return line.strip()
    return ""


def _failed_golden_ids(output: str) -> set[str]:
    """从 `-rf` 的 `FAILED`/`ERROR` 行提取 golden 场景 id。"""
    ids: set[str] = set()
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            ids.update(_GOLDEN_RE.findall(line))
    return ids


def _pass_rate(counts: dict[str, int]) -> float:
    denom = counts["passed"] + counts["failed"] + counts["error"]
    return counts["passed"] / denom if denom else 0.0


# ---------------------------------------------------------------------------
# 选择器与基线
# ---------------------------------------------------------------------------


def _select_runs(args: argparse.Namespace) -> list[tuple[str, list[Path], str | None, str | None]]:
    """返回 (label, paths, marker, keyword)。keyword 用于 P0 护栏（`-k` 选 golden）。"""
    slow_suffix = "" if args.run_slow else " and not slow"

    if args.dimension:
        paths = [EVALS_DIR / rel for rel in DIMENSIONS[args.dimension]]
        return [(args.dimension, paths, "not observation" + slow_suffix, None)]

    tiers = tuple(TIER_MARKERS) if args.tier == "all" else (args.tier,)
    runs: list[tuple[str, list[Path], str | None, str | None]] = []
    for tier in tiers:
        marker = TIER_MARKERS[tier]
        if tier == "l3":
            marker += slow_suffix
        runs.append((tier, [EVALS_DIR], marker, None))
        if tier == "l3":
            runs.append(("observation", [EVALS_DIR], "observation", None))
    return runs


def _load_baseline(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save_baseline(path: str | Path, tier_rates: dict[str, float]) -> None:
    """回填 `tiers.*.pass_rate` + `baseline_captured`；拒绝基线下调。"""
    baseline_path = Path(path)
    baseline = _load_baseline(baseline_path)
    tiers = baseline.setdefault("tiers", {})
    for tier, rate in tier_rates.items():
        entry = tiers.setdefault(tier, {})
        existing = entry.get("pass_rate")
        if existing is not None and rate < existing:
            print(
                f"  拒绝基线下调 {tier}: 现有 {existing:.4f} > 实测 {rate:.4f}，保持现有基线。",
                file=sys.stderr,
            )
            continue
        entry["pass_rate"] = round(rate, 4)
    baseline["baseline_captured"] = datetime.now(timezone.utc).isoformat()
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 编排与判定
# ---------------------------------------------------------------------------


def _evaluate(args: argparse.Namespace) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    for label, paths, marker, keyword in _select_runs(args):
        proc = _run_pytest(paths, marker=marker, keyword=keyword)
        counts = _parse_counts(proc.stdout)
        results[label] = {
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errors": counts["error"],
            "deselected": counts["deselected"],
            "pass_rate": _pass_rate(counts),
            "summary": _summary_line(proc.stdout),
            "returncode": proc.returncode,
        }

    exit_code = EXIT_OK

    # 1) 阈值门禁（L1≥95% / L2≥90% / L3 零失败）；`--dimension` 是定向跑，门禁 = 零失败。
    tier_report: dict[str, dict[str, Any]] = {}
    gate_ok = True
    if args.dimension:
        r = results[args.dimension]
        ok = r["failed"] == 0 and r["errors"] == 0
        tier_report[args.dimension] = {**r, "threshold": None, "gate": ok}
        gate_ok = ok
    else:
        for tier in ("l1", "l2", "l3"):
            if tier not in results:
                continue
            r = results[tier]
            threshold = TIER_THRESHOLDS[tier]
            ok = (r["failed"] == 0 and r["errors"] == 0) if threshold is None else r["pass_rate"] >= threshold
            tier_report[tier] = {**r, "threshold": threshold, "gate": ok}
            if not ok:
                gate_ok = False
    if not gate_ok:
        exit_code = EXIT_THRESHOLD

    # 2) 劣化分流（仅 --compare；>5% 阻断 / 3~5% 告警 / ≤3% 记录）
    degradation: dict[str, dict[str, Any]] = {}
    degrade_blocked = False
    if args.compare:
        baseline = _load_baseline(args.compare)
        for tier in ("l1", "l2", "l3"):
            if tier not in results:
                continue
            base_rate = baseline.get("tiers", {}).get(tier, {}).get("pass_rate")
            if base_rate is None:
                continue  # 该层尚无基线 → 跳过对比
            delta = base_rate - results[tier]["pass_rate"]
            if delta > DEGRADE_BLOCK:
                status = "block"
                degrade_blocked = True
            elif delta > DEGRADE_WARN:
                status = "warn"
            else:
                status = "record"
            degradation[tier] = {
                "baseline": base_rate,
                "current": results[tier]["pass_rate"],
                "delta_pp": round(delta, 4),
                "status": status,
            }
    if degrade_blocked:
        exit_code = EXIT_DEGRADE

    # 3) P0 护栏（立即阻断；仅当 l2/all 纳入时跑，P0 场景落在 L2）
    p0: dict[str, bool] = {}
    ran_p0 = args.dimension is None and args.tier in ("l2", "all")
    if ran_p0:
        golden_ids = sorted({g for ids in P0_GUARDS.values() for g in ids})
        keyword = " or ".join(golden_ids)
        proc = _run_pytest([EVALS_DIR], keyword=keyword)
        failed_ids = _failed_golden_ids(proc.stdout)
        p0 = {guard: not any(g in failed_ids for g in ids) for guard, ids in P0_GUARDS.items()}
        if not all(p0.values()):
            exit_code = EXIT_P0

    report: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "run_slow": args.run_slow,
        "dimension": args.dimension,
        "results": results,
        "tiers": tier_report,
        "observation": results.get("observation"),
        "degradation": degradation,
        "p0": p0,
        "exit_code": exit_code,
    }
    return report


def _print_report(report: dict[str, Any]) -> None:
    if report["dimension"]:
        r = report["tiers"][report["dimension"]]
        verdict = "✓" if r["gate"] else "✗"
        print(f"维度 {report['dimension']}  {r['passed']} passed / {r['failed']} failed  {verdict}")
        print(f"结论：{'Go' if report['exit_code'] == EXIT_OK else 'No-Go'}（退出码 {report['exit_code']}）")
        return

    print("Evals 三层评测（30号 §3.1）")
    print("-" * 60)
    for tier in ("l1", "l2", "l3"):
        t = report["tiers"].get(tier)
        if not t:
            continue
        threshold = t["threshold"]
        verdict = "✓" if t["gate"] else "✗"
        if threshold is None:
            line = f"  {tier.upper()} 质量层  {t['passed']} passed / {t['failed']} failed  达验收口径  {verdict}"
        else:
            line = f"  {tier.upper()} {TIER_DESC[tier].split(' ·')[0]:<6}  {t['passed']} passed / {t['failed']} failed  通过率 {t['pass_rate']*100:5.1f}%  (≥{threshold*100:.0f}%)  {verdict}"
        print(line)
    obs = report.get("observation")
    if obs:
        print(f"  观测    冷路径观测  {obs['passed']} passed / {obs['failed']} failed  （独立统计，不进通过率）")

    if report["degradation"]:
        print("-" * 60)
        print("基线对比（劣化分流：≤3% 记录 / 3~5% 告警 / >5% 阻断）")
        for tier, d in report["degradation"].items():
            mark = {"record": "记录", "warn": "告警", "block": "阻断"}[d["status"]]
            print(f"  {tier.upper()}  基线 {d['baseline']*100:.1f}% → 实测 {d['current']*100:.1f}%  差 {d['delta_pp']*100:+.2f}pp  [{mark}]")

    if report["p0"]:
        print("-" * 60)
        marks = "  ".join(f"{g} {'✓' if ok else '✗'}" for g, ok in report["p0"].items())
        print(f"P0 护栏：{marks}")

    print("-" * 60)
    conclusion = "Go" if report["exit_code"] == EXIT_OK else "No-Go"
    print(f"结论：{conclusion}（退出码 {report['exit_code']}）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        print("Evals 三层评测金字塔（30号 §3.1）：")
        for tier in ("l1", "l2", "l3"):
            print("%-4s %s" % (tier, TIER_DESC[tier]))
        return EXIT_OK

    report = _evaluate(args)

    if args.save_baseline:
        if args.dimension:
            print(
                "  --save-baseline 与 --dimension 互斥：定向维度测不出整层通过率，跳过回填。",
                file=sys.stderr,
            )
        else:
            tier_rates = {
                t: report["tiers"][t]["pass_rate"]
                for t in ("l1", "l2", "l3")
                if t in report["tiers"]
            }
            baseline_path = args.compare if args.compare else BASELINE_DEFAULT
            _save_baseline(baseline_path, tier_rates)
            print(f"基线已回填：{Path(baseline_path)}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)

    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
