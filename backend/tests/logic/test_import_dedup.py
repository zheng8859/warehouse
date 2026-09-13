"""重复导入判定的契约测试（tasks.md 1.3 的验证）。

事实来源：16 §11.5（重复导入判定）
          spec `data-import`「重复导入判定与幂等」的两个 Scenario
          openspec/changes/data-import/design.md D1

判重是**纯函数**：本文件不建库、不用 session，全部用例只调 `app/importer/dedup.py`，
用轻量替身表达既有会话。真 `ImportSession` + 真文件清单的端到端判重属任务 7.2 的
集成冒烟，这里钉的是「怎么算重复」这条规则本身。

**为什么值得穷举**：判重有三个独立判据（已建基准 / 时点相等 / 校验和相交），任一判据
漏掉或放宽，都会让「静默再导一遍」重新成为可能。把三条判据各自的边界单独钉住，
将来有人把「时点相等」改成「时点相近」、或把「已建基准」放宽成「任何非终态」，
红的是这里的用例，而不是生产上某天多出来的一条同源基线。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from app.core.enums import ImportStatus
from app.importer.dedup import (
    extract_checksums,
    find_duplicate,
    is_already_baselined,
    sha256_checksum,
)

pytestmark = pytest.mark.logic

NOW = datetime(2026, 9, 8, 0, 0)
LATER = datetime(2026, 9, 9, 0, 0)


@dataclass(frozen=True)
class _Prior:
    """既有会话的轻量替身（对应 `PriorImport` 协议的三列）。"""

    data_time: datetime
    files_json: dict | list | None
    status: ImportStatus


def _files(*checksums: str) -> list[dict]:
    return [{"filename": f"f{i}.xlsx", "checksum": c} for i, c in enumerate(checksums)]


# ------------------------------------------------------------------ 校验和

def test_sha256_is_deterministic_hex_of_known_vector() -> None:
    """SHA-256 空串是公开测试向量 —— 锁住实现没换哈希算法、没加盐。"""
    assert sha256_checksum(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_sha256_distinguishes_content() -> None:
    """同名不同内容 / 同内容不同名，都必须被正确区分。"""
    assert sha256_checksum(b"abc") != sha256_checksum(b"abd")
    assert sha256_checksum(b"same") == sha256_checksum(b"same")


# ------------------------------------------------------------------ extract_checksums

def test_extract_checksums_from_file_list() -> None:
    files = _files("a" * 64, "b" * 64)
    assert extract_checksums(files) == {"a" * 64, "b" * 64}


def test_extract_checksums_skips_missing_and_empty() -> None:
    """缺失 `checksum` 键、空串、非字典条目都不得参与判重，也不得让函数崩。"""
    files = [
        {"filename": "no_checksum.xlsx"},  # 缺键
        {"filename": "empty.xlsx", "checksum": ""},  # 空串
        "not_a_dict",  # 非字典
        {"filename": "ok.xlsx", "checksum": "c" * 64},
    ]
    assert extract_checksums(files) == {"c" * 64}


def test_extract_checksums_none_and_empty_are_empty() -> None:
    assert extract_checksums(None) == frozenset()
    assert extract_checksums([]) == frozenset()


# ------------------------------------------------------------------ find_duplicate

def test_same_time_same_checksum_is_duplicate() -> None:
    """spec Scenario「同时点同校验和判定重复」的判据本体。"""
    prior = _Prior(data_time=NOW, files_json=_files("x" * 64), status=ImportStatus.BASELINE)
    found = find_duplicate(NOW, frozenset({"x" * 64}), [prior])
    assert found is prior


def test_same_time_overlapping_checksum_is_duplicate() -> None:
    """三份文件里只要有一份内容相同就算重复 —— 判据是交集，不是整集合相等。"""
    prior = _Prior(
        data_time=NOW,
        files_json=_files("a" * 64, "b" * 64, "c" * 64),
        status=ImportStatus.BASELINE,
    )
    # 进来的只重复了其中一份（"b"）。
    assert find_duplicate(NOW, frozenset({"b" * 64}), [prior]) is prior


def test_same_time_no_overlap_is_not_duplicate() -> None:
    """同时点但文件内容全不同 → 合法的新导入（不同的文件）。"""
    prior = _Prior(data_time=NOW, files_json=_files("x" * 64), status=ImportStatus.BASELINE)
    assert find_duplicate(NOW, frozenset({"y" * 64}), [prior]) is None


def test_different_time_same_checksum_is_not_duplicate() -> None:
    """不同时点各建一个基线版本，文件内容相同也不是重复。"""
    prior = _Prior(data_time=NOW, files_json=_files("x" * 64), status=ImportStatus.BASELINE)
    assert find_duplicate(LATER, frozenset({"x" * 64}), [prior]) is None


@pytest.mark.parametrize(
    "status",
    [
        ImportStatus.DRAFT,
        ImportStatus.VALIDATING,
        ImportStatus.VALIDATED,
        ImportStatus.FAILED,
        ImportStatus.IMPORTING,
        ImportStatus.IMPORTED,
        ImportStatus.DISCARDED,
    ],
)
def test_non_baselined_session_is_not_a_duplicate(status: ImportStatus) -> None:
    """只有 `BASELINE` 才算「已导入」。取消 / 失败 / 中间态都不该挡住重传。

    这条最容易被「顺手放宽」成「任何非 DRAFT 都算」—— 那会让一个 `DISCARDED`
    （用户取消）的导入永久挡住同一份文件。
    """
    prior = _Prior(data_time=NOW, files_json=_files("x" * 64), status=status)
    assert find_duplicate(NOW, frozenset({"x" * 64}), [prior]) is None


def test_empty_incoming_checksums_is_not_duplicate() -> None:
    """进来的校验和为空（文件清单缺失）→ 判不了重，放行（不因空字段阻断）。"""
    prior = _Prior(data_time=NOW, files_json=_files("x" * 64), status=ImportStatus.BASELINE)
    assert find_duplicate(NOW, frozenset(), [prior]) is None


def test_no_prior_sessions_is_not_duplicate() -> None:
    assert find_duplicate(NOW, frozenset({"x" * 64}), []) is None


# ------------------------------------------------------------------ is_already_baselined

def test_is_already_baselined_only_true_for_baseline() -> None:
    """spec Scenario「已建基准会话幂等返回」的判据：只有 BASELINE 幂等。"""
    assert is_already_baselined(ImportStatus.BASELINE) is True
    for status in ImportStatus:
        if status is ImportStatus.BASELINE:
            continue
        assert is_already_baselined(status) is False, status
