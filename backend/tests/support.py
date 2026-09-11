"""跨层共用的测试**事实**。

`tests/` 是包（有 `__init__.py`），`pyproject.toml` 又把 `backend/` 放进了 `pythonpath`，
所以这里的东西用 `from tests.support import ...` 取用。

## 为什么单独一层

判据只有一条：**同一个事实被两个层各自写了一遍**。分开写时它们会各自漂移，而两处都是绿的
—— 直到某一层漏掉一个刚补上的取值，或者两边的注释对同一个取值给出两种解释。

**只放事实，不放夹具。** 夹具留在 `conftest.py`：每层要的库与会话本就不同（`test_auth.py`
的模块 docstring 讲了它为什么自带一套），没有「同一个事实」可言，硬合反而制造耦合。
"""
from __future__ import annotations

#: 签名合法、但 `user_id` **类型**不对的取值。两处消费，同一个事实：
#:
#:   - `tests/logic/test_token.py` —— `decode_session_token` 的类型校验必须拒掉它们
#:   - `tests/api/test_auth.py` —— 中间件这条**端到端**路径上，它们必须是 401 而不是 500
#:
#: 各值的落点（这两条结论此前在两个文件里各写了一遍，现收在此处）：
#:
#:   - `{}` / `[1, 2]` → `session.get(Account, …)` 抛 `InvalidRequestError`（500）
#:   - `"7"` → SQLite 按等值比较**能查到**，但线上格式是整型
#:   - `None` → 主键查询恒不中
#:   - `True` → bool 是 int 的子类，会去查主键 **1**
#:
#: 本系统绝不会签发这些取值（`create_session_token` 的参数已标 `int`），造得出来的只有
#: 改过载荷或跨版本的一方 —— 而「签名对」只证明内容没被第三方改过，不证明内容合法。
BAD_USER_IDS: tuple[object, ...] = ({}, [1, 2], "7", None, True)
