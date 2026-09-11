"""JWT 签发/校验与密码哈希。

事实来源：13-权限分级与访问控制系统 §7 / §8

会话 claims（13 §7.2，逐字）：`user_id`、`role`、`status`、`iat`、`exp`，
必要时携带 `warehouse_id`（当前单厂固定 GTJ10036）。

有效期 8 小时（一个班次，13 §7.1），超时重新登录。
**无 refresh token、无服务端黑名单**：凭据过期前始终有效；紧急吊销 = 管理员置
`status=disabled`，下次请求校验即失败（13 §8.3）。因此校验方必须回查账号当前
状态，不能只信 token 里的 `status` 快照（见 app/api/middleware.py 的 TODO）。

密码哈希：**不使用 passlib**。本机 passlib 1.7.4 + bcrypt 5.x + Python 3.14 下，
`passlib.hash.bcrypt.hash()` 直接抛 `ValueError: password cannot be longer than
72 bytes`（passlib 的后端探测触发了 bcrypt 5.x 的新校验）。直接用 bcrypt 库。

13 号未规定密码哈希算法、密码策略、签名算法 —— 以下为开发阶段决策：
  - 哈希：bcrypt（默认 cost 12）
  - 签名：HS256（对称密钥 + 单应用内网部署足够）
  - 密码长度：**强制 ≤72 字节**。bcrypt 只取前 72 字节，超长必须报错而非静默截断，
    否则「密码 abc…(超长)」与「abc…(被截断)」会互相通过验证。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

#: bcrypt 的硬上限。超长必须显式失败，不得静默截断。
BCRYPT_MAX_PASSWORD_BYTES = 72

_SESSION_CLAIMS = ("user_id", "role", "status", "iat", "exp", "warehouse_id")


# --------------------------------------------------------------------- 密码
def hash_password(plain: str) -> str:
    """生成密码哈希。超长时抛 ValueError（不截断）。"""
    raw = plain.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            "密码超过 %d 字节，bcrypt 无法完整处理；请在接口层限制密码长度"
            % BCRYPT_MAX_PASSWORD_BYTES
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。输入异常一律返回 False，不向上抛（避免成为探测面）。"""
    try:
        raw = plain.encode("utf-8")
        if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------- 会话
def create_session_token(
    user_id: str,
    role: str,
    status: str,
    warehouse_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """签发会话凭据。claims 与 13 §7.2 一一对应。"""
    issued_at = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "user_id": user_id,
        "role": role,
        "status": status,
        "iat": issued_at,
        "exp": issued_at + timedelta(hours=settings.session_hours),
        "warehouse_id": warehouse_id or settings.warehouse_code,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class SessionInvalid(Exception):
    """凭据缺失、过期、篡改或结构不合法 —— 上层统一转 HTTP 401。"""


def decode_session_token(token: str) -> dict[str, Any]:
    """校验并解析会话凭据。任何问题都抛 SessionInvalid。"""
    if not token:
        raise SessionInvalid("缺少会话凭据")
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionInvalid("会话已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise SessionInvalid("会话凭据无效") from exc

    missing = [c for c in _SESSION_CLAIMS if c not in payload]
    if missing:
        raise SessionInvalid("会话凭据缺少必要字段: %s" % ", ".join(missing))
    return payload
