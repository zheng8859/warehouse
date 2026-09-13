"""重复导入判定（**纯函数，无 IO**）。

事实来源：16-数据衔接与 cap 自维护 §11.5（重复导入判定）
          openspec/changes/data-import/design.md D1（重复导入判定与幂等）
          spec `data-import`「重复导入判定与幂等」

判重的依据是「数据时点 + 文件校验和」，两条规则：

1. **同数据时点 + 任一文件校验和相同 → 重复**：由 `find_duplicate` 判定，返回那个既有
   会话供编排层提示「该时点快照已导入，跳过 / 覆盖重算」。**绝不静默覆盖** —— 跳过与
   覆盖是两个显式动作（决策权在人，CLAUDE.md §四）。
2. **已 BASELINE 的会话再次提交 → 幂等返回**：由 `is_already_baselined` 判定。

两条规则分开成函数，因为它们发生在流水线的不同位置：判重在**上传/校验**时拿「进来的文件」
去比对**既有会话**；幂等在**执行导入**时看「本会话自己」的状态。合在一个函数里，两个
时点的入参形态会互相污染。

本模块**不得引入 IO**：判定「两份文件是不是同一个」只用到时点与校验和两个值，
与库里其它任何行无关。真正去库上 `SELECT` 既有会话的动作属于编排层（`session.py`），
它把查出来的会话喂进 `find_duplicate`，自己只做 IO。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from typing import Protocol, runtime_checkable

from app.core.enums import ImportStatus

__all__ = [
    "PriorImport",
    "extract_checksums",
    "find_duplicate",
    "is_already_baselined",
    "sha256_checksum",
]


def sha256_checksum(data: bytes) -> str:
    """文件内容级 SHA-256，返回小写 hex（64 位）。

    D1：校验和是**文件内容**级（非文件名、非大小）—— 同名不同内容的两个文件，或
    同内容改了名的两个文件，都必须被正确区分开。存 `files_json`，供 `find_duplicate` 判重。
    """
    return sha256(data).hexdigest()


@runtime_checkable
class PriorImport(Protocol):
    """判重需要读到的既有会话切片：数据时点 + 文件清单（含校验和）+ 状态。

    `ImportSession` 自带这三列，真模型天然满足；测试用轻量替身即可，不必连库。
    """

    data_time: datetime
    files_json: dict | list | None
    status: ImportStatus


def extract_checksums(files_json: dict | list | None) -> frozenset[str]:
    """从 `files_json` 提取全部文件校验和（16 §3.3 的文件清单）。

    只取 `checksum` 一项而非整份 JSON —— 判重依据是文件**内容**，不是文件名 / 大小。
    缺失、空、或条目里没有 `checksum` 键 → 一律不参与判重（返回空集），不因一个
    空字段阻断判重（那会反过来挡住正常导入）。
    """
    if not files_json:
        return frozenset()

    entries: Iterable[dict] = (files_json,) if isinstance(files_json, dict) else files_json

    checksums: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            value = entry.get("checksum")
            if isinstance(value, str) and value:
                checksums.add(value)
    return frozenset(checksums)


def find_duplicate(
    data_time: datetime,
    incoming_checksums: frozenset[str],
    prior: Iterable[PriorImport],
) -> PriorImport | None:
    """在既有会话里找「同数据时点 + 任一文件校验和相同」的那一个，找不到返回 `None`。

    三个判据缺一不可：

    1. **既有会话已 `BASELINE`**：只有建了基准的会话才算「已导入」。`DRAFT / DISCARDED /
       FAILED` 从没产生过基线，不该挡住重传 —— 一个取消掉的导入若也算重复，用户就
       永远无法再传那份文件。`IMPORTED` 是短暂中间态（下一步要么 `BASELINE` 要么
       回滚 `FAILED`），基线尚未成立，也不参与判重。
    2. **时点相等**：不同时点本就要各建一个基线版本，不算重复。
    3. **校验和集合有交集**（非整集合相等）：三份文件里只要有一份与既有导入的是同一
       文件内容，就该提示 —— 那份文件的基线已经存在，静默再导一遍会造出第二份同源基线。
    """
    if not incoming_checksums:
        return None

    for session in prior:
        if session.status is not ImportStatus.BASELINE:
            continue
        if session.data_time != data_time:
            continue
        if incoming_checksums & extract_checksums(session.files_json):
            return session
    return None


def is_already_baselined(status: ImportStatus) -> bool:
    """已建基准（`BASELINE`）的会话再次提交 → 幂等返回既有基线，不再重复建立。

    只认 `BASELINE`：`IMPORTED`（已分流未建基线）与 `VALIDATED` 都不是幂等点，仍需推进
    到下一里程碑；`DISCARDED` 是取消而非「已建基准」，重新提交应走新会话。
    """
    return status is ImportStatus.BASELINE
