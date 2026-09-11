"""乐观锁校验辅助。

事实来源：19-系统架构与部署视图 §3.3（SQLite 单写；单进程约束）
          17-数据模型设计 §六（并发控制）
          openspec/changes/data-model-permission/design.md D1（版本语义三分）
          openspec/changes/data-model-permission/specs/data-model/spec.md
            「乐观锁并发守卫」

## 为什么需要它，而不是靠数据库

SQLite 在 WAL 下是「多读单写」：写与写之间靠文件锁串行，**读与写之间不串行**。
于是经典的丢失更新在这里照样发生：两端都读到 `lock_version = 3`，各自改完提交，
后提交的把那一次写入静默覆盖掉 —— 文件锁拦不住，因为它只保证「两个写不并发」，
不保证「写之前没人动过」。

`busy_timeout` 同理：它解决的是写冲突时的等待，不是陈旧读。

本项目的口径对这一点尤其敏感：红线「`EXECUTED` 后不允许重复写台账」正是靠
状态机守卫 + 乐观锁双保险。若乐观锁退化成静默覆盖，两端同时确认同一单会写出
**两条台账** —— 而台账只有一套，这是不可恢复的数据损伤。

## 用法

    # 请求里带上用户读到的版本号（前端表单隐藏字段）
    bump_lock_version(order, expected=form.lock_version)
    # 不匹配 → StateConflict；调用方交给异常处理器渲染成 409「请重新读取」
    # 匹配   → lock_version 推进一格

读侧不需要辅助：`lock_version` 对用户不可见（D1），它只出现在
「读出来 → 原样带回去 → 提交」这条回环里，不参与任何业务判断。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.errors import StateConflict

__all__ = [
    "LOCK_VERSION_ATTR",
    "SupportsLockVersion",
    "assert_lock_version",
    "bump_lock_version",
]

#: 列名常量，不散落成字面量 —— 它被 D1 钉死，改名要走设计文档。
LOCK_VERSION_ATTR = "lock_version"

#: D1 明确禁止与乐观锁混用的列名。给出它们是为了把报错指向真因：
#: 传错列不会「碰巧能用」，但报错若不指名，排查要绕一圈。
_BUSINESS_VERSION_ATTRS = ("version", "version_no")


@runtime_checkable
class SupportsLockVersion(Protocol):
    """可被乐观锁保护的实例。

    刻意用 Protocol 而非抽象基类：`lock_version` 是**列**，不是能力实现，
    实体只需带上这一列即可（`JobOrder` / `ImportSession`，见 D1 的表格）。
    """

    lock_version: int


def _reject_wrong_version_column(instance: Any) -> None:
    """把「传了业务版本列」与「这个实体本来就不需要乐观锁」区分开。

    两种情况都不该静默通过，但报错必须指向不同的动作：前者是调用方传错了对象，
    后者是有人给一个不该有乐观锁的实体加了守卫。
    """
    cls = type(instance).__name__
    present = [n for n in _BUSINESS_VERSION_ATTRS if hasattr(instance, n)]
    if present:
        raise TypeError(
            f"{cls} 没有 {LOCK_VERSION_ATTR}，但有 {present[0]} —— "
            "D1 要求乐观锁与业务版本分列：version_no 是配置型业务版本（对用户可见），"
            "不能当乐观锁用。两列语义不同，共用必然导致「把业务版本当并发版本读」。"
        )
    raise TypeError(
        f"{cls} 缺少 {LOCK_VERSION_ATTR} 属性，无法做乐观锁校验。"
        f"若该实体不在 D1 的乐观锁名单内（JobOrder / ImportSession），"
        "它本来就不需要这个守卫。"
    )


def _require_non_negative_int(name: str, value: Any) -> None:
    """版本号必须是非负整数。

    `bool` 要单独挡掉：`True == 1`，一个误传的开关会与 `lock_version=1` 撞上，
    于是守卫在最该拦住的地方放行。`str` 也要挡 —— HTTP 查询串/表单来的值天生是
    字符串，若放任比较，`"3" != 3` 会以「版本冲突」的面目报出来，把调用方的
    类型错误伪装成并发冲突。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是非负整数，实际是 {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} 不能为负，实际是 {value}")


def assert_lock_version(instance: Any, expected: int) -> None:
    """校验实例当前的 `lock_version` 与期望一致，不一致即抛 `StateConflict`。

    `expected` 是调用方**读的时候**看到的版本号。**只读不写** —— 推进版本请用
    `bump_lock_version`，避免出现「校验过了但忘了加一」，那会让下一次冲突
    永远检测不到。
    """
    if not hasattr(instance, LOCK_VERSION_ATTR):
        _reject_wrong_version_column(instance)
    _require_non_negative_int("expected", expected)

    actual = getattr(instance, LOCK_VERSION_ATTR)
    _require_non_negative_int(f"{type(instance).__name__}.{LOCK_VERSION_ATTR}", actual)

    if actual != expected:
        raise StateConflict(
            "数据已被他处修改，请重新读取后再提交",
            detail={
                "entity": type(instance).__name__,
                "expected": expected,
                "actual": actual,
            },
        )


def bump_lock_version(instance: Any, expected: int) -> int:
    """校验通过后把 `lock_version` 推进一格，返回新值。

    推进后的值由**本次事务提交时**生效。本函数不提交 —— 事务边界属于调用方，
    这样「cap 与台账同事务」才可能成立（见 `app/core/db.py` 的模块 docstring）。

    推进量固定为 1（不是 `actual + 1`）：校验已保证 `actual == expected`，
    二者在成功路径上等价，但写成 `expected + 1` 时，万一将来有人放宽校验，
    推进就不会悄悄依赖一个未经校验的读数。
    """
    assert_lock_version(instance, expected)
    new_version = expected + 1
    setattr(instance, LOCK_VERSION_ATTR, new_version)
    return new_version
