"""ImportSession 状态机（**纯函数，无 IO**）。

事实来源：16-数据衔接与 cap 自维护设计 §3.1（状态机图）
          openspec/changes/data-import/specs/data-model/spec.md「ImportSession 状态机」

与 `app/core/state_machine.py`（作业单状态机）同构，只是枚举换成 `ImportStatus`。
分开成两个模块而非合并：两套状态机的图互不相干，合并只会让「作业单的 15 条边」
与「导入会话的 10 条边」在同一张表里互相干扰，改错一处伤及另一处。

## 三条读这张表时要知道的事

1. **`FAILED → DRAFT` 是唯一的回边**：校验失败后修正文件重新校验，必须退回 `DRAFT`
   再走 `DRAFT → VALIDATING`，不存在 `FAILED → VALIDATING` 的捷径 —— 那会绕过
   「时点标注 + 文件重新上传」这个入口，让旧文件被静默重检。
2. **两个终态**：`BASELINE`（建基准完成）与 `DISCARDED`（用户取消）没有出边。
   `FAILED` 不是终态：它可回 `DRAFT` 重新校验。
3. **`IMPORTING → FAILED`（写入异常/回滚）与 `VALIDATING → FAILED`（校验不过）同落
   `FAILED`，但回退路径相同**：`FAILED → DRAFT`。两者在状态机上看不出差别，差别在
   回执（`receipt_json` 里 FAILED 的成因明细），不在迁移图。

本模块**不得引入任何 IO 依赖**。判定「两个状态之间有没有边」只用到两个枚举值，
与那一行的其它字段无关 —— 与作业单状态机同一理由（见 `state_machine.py` 的模块 docstring）。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.core.enums import ImportStatus
from app.core.errors import StateConflict

__all__ = [
    "LEGAL_IMPORT_TRANSITIONS",
    "IMPORT_TERMINAL_STATUSES",
    "assert_import_transition",
    "can_import_transition",
]

#: 当前状态 → 允许迁移到的状态集。内容即 16 §3.1 的图，10 条边。
#: 外层 `MappingProxyType` + 内层 `frozenset` 都是**只读**：这张表是判定基准，
#: 任何一处的「临时放宽」都必须改到本文件，而不是在调用点改一个副本。
LEGAL_IMPORT_TRANSITIONS: Final[Mapping[ImportStatus, frozenset[ImportStatus]]] = MappingProxyType(
    {
        ImportStatus.DRAFT: frozenset(
            {
                ImportStatus.VALIDATING,  # 点击「开始校验」
                ImportStatus.DISCARDED,   # 用户取消
            }
        ),
        ImportStatus.VALIDATING: frozenset(
            {
                ImportStatus.VALIDATED,   # 时点有效且至少一类文件通过（部分失败隔离）
                ImportStatus.FAILED,      # 时点缺失/未来 · 或全部文件失败
            }
        ),
        ImportStatus.VALIDATED: frozenset(
            {
                ImportStatus.IMPORTING,   # 点击「执行导入」
                ImportStatus.DISCARDED,   # 用户取消
            }
        ),
        ImportStatus.FAILED: frozenset(
            {
                ImportStatus.DRAFT,       # 修正文件后重新校验（唯一的回边）
            }
        ),
        ImportStatus.IMPORTING: frozenset(
            {
                ImportStatus.IMPORTED,    # 写入与分流成功
                ImportStatus.FAILED,      # 写入异常 / 回滚
            }
        ),
        ImportStatus.IMPORTED: frozenset(
            {
                ImportStatus.BASELINE,    # 快照重算 cap 基线完成
            }
        ),
        ImportStatus.BASELINE: frozenset(),   # 终态
        ImportStatus.DISCARDED: frozenset(),  # 终态
    }
)

#: 没有出边的状态（16 §3.1 的结束节点）：`BASELINE` 与 `DISCARDED`。
IMPORT_TERMINAL_STATUSES: Final[frozenset[ImportStatus]] = frozenset(
    status for status, targets in LEGAL_IMPORT_TRANSITIONS.items() if not targets
)


def _require_import_status(name: str, value: object) -> ImportStatus:
    """把非 `ImportStatus` 的入参挡在类型错上，**不要**让它退化成「非法迁移」。

    理由与 `state_machine._require_status` 逐字相同：`ImportStatus` 是 `str` 枚举，
    `ImportStatus.DRAFT == "DRAFT"` 为真，但查表走 `hash`（成员名），类型错误会被
    伪装成状态冲突。**类型错了就说类型错了。**
    """
    if not isinstance(value, ImportStatus):
        raise TypeError(
            f"{name} 必须是 ImportStatus 成员，实际是 {type(value).__name__}：{value!r}。"
            "请在入口（请求解析 / 导入映射）就转换，不要在状态机里比较裸字符串。"
        )
    return value


def can_import_transition(current: ImportStatus, target: ImportStatus) -> bool:
    """判断 `current → target` 是否是 16 §3.1 列出的迁移。不抛异常。"""
    current = _require_import_status("current", current)
    target = _require_import_status("target", target)
    return target in LEGAL_IMPORT_TRANSITIONS[current]


def assert_import_transition(current: ImportStatus, target: ImportStatus) -> ImportStatus:
    """守卫式判定：合法则返回 `target`，非法则抛 `StateConflict`（409）。

    返回 `target` 是为了让调用点能写成 `session.status = assert_import_transition(session.status, X)`
    —— 判定与赋值之间没有缝隙。不提交、不落库：事务边界属于调用方。
    """
    current = _require_import_status("current", current)
    target = _require_import_status("target", target)

    if target not in LEGAL_IMPORT_TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in LEGAL_IMPORT_TRANSITIONS[current]))
        raise StateConflict(
            f"导入会话不能从 {current.value} 迁移到 {target.value}；"
            f"{current.value} 只能迁移到：{allowed or '（终态，无后续迁移）'}",
            detail={"current": current.value, "target": target.value},
        )
    return target
