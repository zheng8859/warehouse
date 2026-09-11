"""会话凭据（HS256）签发/校验与密码哈希。

事实来源：13-权限分级与访问控制系统 §7 / §8
          openspec/changes/data-model-permission/design.md D6（密码）、D7（会话）

会话 claims（13 §7.2，逐字）：`user_id`、`role`、`status`、`iat`、`exp`，
必要时携带 `warehouse_id`（当前单厂固定 GTJ10036）。集合封闭 —— 见 `SESSION_CLAIMS`。

有效期 8 小时（一个班次，13 §7.1），超时重新登录。
**无 refresh token、无服务端黑名单**：凭据过期前始终有效；紧急吊销 = 管理员置
`status=disabled`，下次请求校验即失败（13 §8.3）。因此校验方必须回查账号当前
状态，不能只信 token 里的 `status` 快照（见 app/api/middleware.py 的 TODO）。

三类开发阶段决策（13 号未规定，均记录在 design.md）：

1. **密码哈希用 `bcrypt` 库，不用 passlib**（D6）。本机 passlib 1.7.4 + bcrypt 5.x +
   Python 3.14 下，`passlib.hash.bcrypt.hash()` 直接抛 `ValueError: password cannot be
   longer than 72 bytes`（passlib 的后端探测触发了 bcrypt 5.x 的新校验）。
2. **密码长度强制 ≤72 字节**。bcrypt 只取前 72 字节，超长必须报错而非静默截断，
   否则「密码 abc…(超长)」与「abc…(被截断)」会互相通过验证。按**字节**判而非字符：
   `汉` × 24 = 72 字节可过、× 25 = 75 字节必须报错。
3. **会话凭据用标准库自实现 HS256**（D7），不引第三方 JWT 库：
   - 本产品只用一种算法，而通用库必须支持多种（含 `none`）才叫通用。
     自实现时头部里的 `alg` 只被**核对**、不参与选择 —— 结构上不存在
     `alg=none` 与算法混淆这两条路径。
   - 解析流程固定：**先定算法 → 再验签 → 最后才解释载荷**。
   - 签名比较用 `hmac.compare_digest`（普通 `==` 的耗时与「猜对多少前缀」相关）。
   - claims 集合封闭且逐字校验：**校验方不假设签发方一定是自己** —— 缺字段若被放过，
     下游会以 `KeyError` 的形式炸成 500，而 500 与 401 的差别本身就是探测面。

`create_session_token` 在**同一时刻**签发两次得到同一份凭据（确定性，红线「同样输入
必得同样输出」）。随机性来自 `iat`/`exp` 时钟，与口令哈希的随机盐是两回事。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from app.core.config import settings

#: bcrypt 的硬上限。超长必须显式失败，不得静默截断。
BCRYPT_MAX_PASSWORD_BYTES = 72

#: 会话态字段集合（13 §7.2 逐字列的五个 + `warehouse_id`）。**封闭**：
#: 签发时只写这些，校验时逐字要求全在。多一个字段意味着客户端多读到一份数据，
#: 少一个意味着某处要另存一次。
SESSION_CLAIMS: tuple[str, ...] = ("user_id", "role", "status", "iat", "exp", "warehouse_id")

#: 头部只核对不选择 —— 这个值写死，不从 settings 读（见 D7：不做算法协商）。
_EXPECTED_HEADER = {"alg": "HS256", "typ": "JWT"}

#: 自实现 HS256 用的摘要算法。与 `_EXPECTED_HEADER["alg"]` 是同一件事的两处写法，
#: 改算法要同时改这两行与 `settings.jwt_algorithm`。
_DIGEST = hashlib.sha256


class SessionInvalid(Exception):
    """凭据缺失、过期、篡改或结构不合法 —— 上层统一转 HTTP 401。

    **只有这一个异常**：`binascii.Error` / `json.JSONDecodeError` / `KeyError` 漏上去
    都会变成 500，而「500 还是 401」对探测者是有用信息。
    """


# --------------------------------------------------------------------- 密码
def hash_password(plain: str) -> str:
    """生成密码哈希。超长时抛 ValueError（不截断）。"""
    raw = plain.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            "密码超过 %d 字节，bcrypt 无法完整处理；请在接口层限制密码长度"
            % BCRYPT_MAX_PASSWORD_BYTES
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt(settings.bcrypt_cost)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。输入异常一律返回 False，不向上抛（避免成为探测面）。"""
    try:
        raw = plain.encode("utf-8")
        if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------- 编解码
def _b64url_encode(raw: bytes) -> str:
    """base64url、**无填充**。`=` 在 URL / Cookie / 日志里都要额外转义，徒增对不齐的机会。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(part: str) -> bytes:
    """还原无填充的 base64url 段；非法字符即 `SessionInvalid`。"""
    if not isinstance(part, str) or not part:
        raise SessionInvalid("会话凭据段落为空")
    padded = part + "=" * (-len(part) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise SessionInvalid("会话凭据段落不是合法的 base64url") from exc


def _sign(signing_input: bytes) -> bytes:
    return hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, _DIGEST).digest()


def _as_utc(moment: datetime | None, *, field: str) -> datetime:
    """取服务端时钟，并要求带时区（D7：`exp` 以服务端时钟为准）。

    朴素时间戳会按**本机时区**解释（`timestamp()` 的语义），于是同一份代码在
    开发机与服务器上给出不同的 `exp` —— 这是那种「本地一直好用」的故障。
    故这里直接拒绝，而不是替调用方补一个猜测的时区。
    """
    if moment is None:
        return datetime.now(timezone.utc)
    if not isinstance(moment, datetime):
        raise ValueError("%s 必须是 datetime，收到 %r" % (field, type(moment).__name__))
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("%s 必须是带时区的 datetime（朴素时间会按本机时区解释）" % field)
    return moment


# --------------------------------------------------------------------- 会话
def create_session_token(
    user_id: str | int,
    role: str,
    status: str,
    warehouse_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """签发会话凭据。claims 与 13 §7.2 一一对应，集合封闭。"""
    issued_at = _as_utc(now, field="now")
    payload: dict[str, Any] = {
        "user_id": user_id,
        "role": role,
        "status": status,
        # 整数秒：比较「到期了没有」用服务端时钟，故载荷里存可直接比较的数值。
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(hours=settings.session_hours)).timestamp()),
        "warehouse_id": warehouse_id or settings.warehouse_code,
    }
    head = _b64url_encode(json.dumps(_EXPECTED_HEADER, separators=(",", ":")).encode("utf-8"))
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{head}.{body}".encode("ascii")
    return f"{head}.{body}.{_b64url_encode(_sign(signing_input))}"


def decode_session_token(token: str, now: datetime | None = None) -> dict[str, Any]:
    """校验并解析会话凭据。任何问题都抛 `SessionInvalid`。

    顺序是刻意的：**结构 → 算法 → 签名 → 载荷语义**。
    签名验证的是**前两段的原文**，不是「解析后的对象」—— 后者会因 JSON 键序重排
    而给出错误的通过。
    """
    if not isinstance(token, str) or not token.strip():
        raise SessionInvalid("缺少会话凭据")

    parts = token.split(".")
    if len(parts) != 3:
        raise SessionInvalid("会话凭据应为三段（header.payload.signature）")
    head_b64, body_b64, sig_b64 = parts
    if not sig_b64:
        raise SessionInvalid("会话凭据缺少签名段")

    # 先定算法、再验签：先读头再按头选算法，等于让攻击者挑选验签方式。
    # 头部里的 alg 只被核对（上面 `_EXPECTED_HEADER` 写死），不参与选择。
    try:
        header = json.loads(_b64url_decode(head_b64).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionInvalid("会话凭据头部不是合法的 JSON") from exc
    if not isinstance(header, dict) or header.get("alg") != _EXPECTED_HEADER["alg"]:
        raise SessionInvalid("会话凭据的签名算法不受支持（仅 %s）" % _EXPECTED_HEADER["alg"])

    expected = _sign(f"{head_b64}.{body_b64}".encode("ascii"))
    if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
        raise SessionInvalid("会话凭据签名不匹配")

    # 签名之后才解释载荷。
    try:
        claims = json.loads(_b64url_decode(body_b64).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionInvalid("会话凭据载荷不是合法的 JSON") from exc
    if not isinstance(claims, dict):
        raise SessionInvalid("会话凭据载荷应为 JSON 对象")

    missing = [claim for claim in SESSION_CLAIMS if claim not in claims]
    if missing:
        raise SessionInvalid("会话凭据缺少必要字段: %s" % ", ".join(missing))

    moment = _as_utc(now, field="now")
    if moment.timestamp() >= claims["exp"]:
        raise SessionInvalid("会话已过期，请重新登录")
    return claims
