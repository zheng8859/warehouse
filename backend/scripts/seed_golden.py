"""把 Golden 数据集 JSON 种入 SQLite 镜像库。

事实来源：30-Evals评测体系 §6.1 / §7；落地路径见 openspec/changes/evals/design.md D6。

真相唯一来源是 ``evals/golden/*.json``（13 维度 + schema.json），本脚本只做：
读 JSON → 轻量校验 → 种入 ``evals/golden_dataset.db``（表 ``golden_samples`` + ``eval_runs``）。
幂等：每次运行先清空 ``golden_samples`` 再全量重种，``eval_runs`` 保留（运行记录不随 seed 清空）。
``.db`` 是生成物（gitignore），不提交。

用法：
    python scripts/seed_golden.py [--check]
    --check  只校验 JSON 与数量，不写库
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = BACKEND_ROOT / "evals" / "golden"
DB_PATH = BACKEND_ROOT / "evals" / "golden_dataset.db"

SCHEMA_FILE = "schema.json"

# 30号 §1.1 三层计数
EXPECTED_LAYERS = {"baseline": 24, "boundary": 20, "regression": 16}
VALID_LAYERS = set(EXPECTED_LAYERS)
VALID_EVAL_TYPES = {"l1", "l2", "l3"}
VALID_ASSERTIONS = {"exact", "semantic", "threshold"}
VALID_DIMENSIONS = {
    "评分正确性", "作业闭环一致性", "KPI计量正确性", "配置合规与权限", "安全隔离",
    "数据接入质量", "错误恢复降级", "分配合规", "指令可靠性",
    "集中度趋势", "移库有效性", "性能基线", "KPI基线达标",
}
REQUIRED_KEYS = {
    "id", "layer", "dimension", "eval_type", "input", "expected",
    "assertion", "tolerance", "tags", "source",
}


def load_samples() -> list[dict]:
    """读 golden 目录下除 schema.json 外的所有 JSON，合并为样本列表。"""
    files = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != SCHEMA_FILE)
    if not files:
        raise SystemExit(f"未找到 Golden JSON：{GOLDEN_DIR}")

    samples: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"JSON 解析失败 {path.name}: {exc}")
        if not isinstance(data, list):
            raise SystemExit(f"{path.name} 顶层应为数组，实际 {type(data).__name__}")
        for sample in data:
            sample["_file"] = path.name
            samples.append(sample)
    return samples


def validate(samples: list[dict]) -> None:
    """轻量校验：必填键、id 格式、枚举值、回归样本字段、三层计数。"""
    ids: set[str] = set()
    layer_counts: dict[str, int] = {}
    errors: list[str] = []

    for sample in samples:
        sid = sample.get("id", "<missing>")
        where = f"{sample.get('_file', '?')}/{sid}"

        missing = REQUIRED_KEYS - set(sample)
        if missing:
            errors.append(f"{where} 缺字段 {sorted(missing)}")

        if sid in ids:
            errors.append(f"{where} id 重复")
        ids.add(sid)

        if not isinstance(sid, str) or not (sid.startswith("golden_") and sid[7:].isdigit() and len(sid) == 10):
            errors.append(f"{where} id 需为 golden_NNN（如 golden_001）")

        layer = sample.get("layer")
        if layer not in VALID_LAYERS:
            errors.append(f"{where} layer 非法: {layer!r}")
        else:
            layer_counts[layer] = layer_counts.get(layer, 0) + 1

        if sample.get("eval_type") not in VALID_EVAL_TYPES:
            errors.append(f"{where} eval_type 非法: {sample.get('eval_type')!r}")
        if sample.get("assertion") not in VALID_ASSERTIONS:
            errors.append(f"{where} assertion 非法: {sample.get('assertion')!r}")
        if sample.get("dimension") not in VALID_DIMENSIONS:
            errors.append(f"{where} dimension 非法: {sample.get('dimension')!r}")
        if not isinstance(sample.get("tags"), list):
            errors.append(f"{where} tags 应为数组")

        # 回归样本额外字段（30号 §1.2）
        if layer == "regression":
            if "baseline_value" not in sample or "comparison" not in sample:
                errors.append(f"{where} 回归样本缺 baseline_value/comparison")
            if sample.get("comparison") not in (None, "trend_better", "no_regression"):
                errors.append(f"{where} comparison 非法: {sample.get('comparison')!r}")

    for layer, expected in EXPECTED_LAYERS.items():
        actual = layer_counts.get(layer, 0)
        if actual != expected:
            errors.append(f"layer={layer} 期望 {expected} 个，实际 {actual} 个")

    total = len(samples)
    if total != 60:
        errors.append(f"样本总数期望 60，实际 {total}")

    if errors:
        for err in errors:
            print(f"  ✗ {err}", file=sys.stderr)
        raise SystemExit(f"Golden 校验失败：{len(errors)} 处错误")

    print(f"✓ Golden 校验通过：{total} 场景，"
          f"基线 {layer_counts.get('baseline', 0)} / 边界 {layer_counts.get('boundary', 0)} / 回归 {layer_counts.get('regression', 0)}")


def _row(sample: dict) -> tuple:
    return (
        sample["id"], sample["layer"], sample["dimension"], sample["eval_type"],
        sample["input"], sample["expected"], sample["assertion"],
        json.dumps(sample["tolerance"], ensure_ascii=False),
        json.dumps(sample["tags"], ensure_ascii=False),
        sample["source"],
        sample.get("baseline_value"),
        sample.get("comparison"),
    )


def init_db(samples: list[dict]) -> None:
    """建表 + 清空 golden_samples + 全量重种。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS golden_samples (
                id             TEXT PRIMARY KEY,
                layer          TEXT NOT NULL,
                dimension      TEXT NOT NULL,
                eval_type      TEXT NOT NULL,
                input          TEXT,
                expected       TEXT,
                assertion      TEXT NOT NULL,
                tolerance_json TEXT,
                tags_json      TEXT,
                source         TEXT,
                baseline_value REAL,
                comparison     TEXT
            );
            CREATE TABLE IF NOT EXISTS eval_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tier              TEXT NOT NULL,
                pass_rate         REAL,
                total             INTEGER,
                passed            INTEGER,
                run_at            TEXT NOT NULL,
                baseline_compared INTEGER DEFAULT 0,
                degradation       REAL,
                verdict           TEXT
            );
            """
        )
        conn.execute("DELETE FROM golden_samples")
        conn.executemany(
            """
            INSERT INTO golden_samples
                (id, layer, dimension, eval_type, input, expected, assertion,
                 tolerance_json, tags_json, source, baseline_value, comparison)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_row(s) for s in samples],
        )
        conn.commit()
    finally:
        conn.close()

    count = query_count()
    print(f"✓ 种入 SQLite：{DB_PATH.relative_to(BACKEND_ROOT)}，golden_samples = {count} 行")


def query_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，无法输出 ✓ 等字符；统一重配为 UTF-8（见 pre-commit 的 PYTHONUTF8 说明）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="种入 Golden 数据集到 SQLite 镜像库")
    parser.add_argument("--check", action="store_true", help="只校验 JSON 与数量，不写库")
    args = parser.parse_args(argv)

    samples = load_samples()
    validate(samples)
    if args.check:
        print("（--check 模式，未写库）")
        return 0

    init_db(samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
