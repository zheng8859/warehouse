"""会话凭据签发与校验的契约测试（tasks.md 7.2 的验证）。

事实来源：13-权限分级与访问控制系统 §7.1（有效期 8 小时）、§7.2（会话态字段，逐字）、
          §8.3（无服务端 Session / 紧急吊销）
          design.md D7（**自实现 HS256**：标准库 hmac + hashlib + base64 + json；
          必须用 `hmac.compare_digest`；解析时**先固定算法再验签**；载荷按 UTF-8 解码；
          `exp` 以服务端时钟为准）
          spec `auth`「无状态会话凭据」（8 小时有效 / 篡改被拒 / 不依赖服务端存储）

四例是 tasks 7.2 点名的：签发后校验通过 / 篡改载荷被拒 / 错误签名被拒 / 过期被拒。
另补的几例针对**自实现签名**特有的失效模式：

  - `alg=none`：不验签就接受。D7 把「不做算法协商」列为自实现的**理由**，所以要真测。
  - 算法混淆：头里写 `HS512`、签名按 HS256 算。解析时必须先固定算法再验签，
    否则「换一种算法」就成了绕过路径。
  - 段数不对 / 非法 base64 / 载荷不是 JSON —— 一律 `SessionInvalid`（→ 401），
    不得漏成 500。
  - `exp` 边界：**到期即失效**（08:00 签发 8 小时 → 16:00 起 401），
    对应 spec 的「超过 8 小时后失效」。

本文件不建库、不用 session：签发与校验是纯函数（`now` 可注入），
「同样输入必得同样输出」可以逐位断言。
"""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.security import (
    SESSION_CLAIMS,
    SessionInvalid,
    create_session_token,
    decode_session_token,
)

pytestmark = pytest.mark.logic

USER_ID = 7
ROLE = "warehouse_keeper"
STATUS = "active"
#: 08:00 登录（spec「凭据在有效期内可用」的场景起点）。
ISSUED_AT = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def _b64(raw: bytes) -> str:
    """base64url，**无填充**（与 JWT 一致）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_token(header: dict, payload: dict, secret: str, digest=hashlib.sha256) -> str:
    """手工造一份凭据，用于构造正常路径造不出来的形状。"""
    head = _b64(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{head}.{body}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, digest).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _payload_of(token: str) -> dict:
    part = token.split(".")[1]
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


# ------------------------------------------------------------------ 7.2 第一例：签发后校验通过

def test_issued_token_verifies() -> None:
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    claims = decode_session_token(token)

    assert claims["user_id"] == USER_ID
    assert claims["role"] == ROLE
    assert claims["status"] == STATUS
    assert claims["warehouse_id"] == settings.warehouse_code


def test_claims_match_doc_13_section_7_2_verbatim() -> None:
    """claims 集合 = 13 §7.2 逐字列的五个 + `warehouse_id`（必要时携带，首期固定）。

    多一个字段意味着「客户端能读到它」，少一个意味着某处要另存一次 —— 故逐字比对，
    不做「包含」断言。
    """
    assert set(SESSION_CLAIMS) == {"user_id", "role", "status", "iat", "exp", "warehouse_id"}
    assert set(_payload_of(create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT))) == set(
        SESSION_CLAIMS
    )


def test_token_is_three_base64url_segments_without_padding() -> None:
    """三段式 `header.payload.signature`，base64url、无填充。

    **无填充**是刻意的：`=` 在 URL / Cookie / 日志里都要转义，多一处编码就多一处
    对不齐的机会。无填充是 JWT 的既定写法，自实现没有理由偏离。
    """
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    parts = token.split(".")

    assert len(parts) == 3
    assert all(parts), "三段都不得为空"
    assert "=" not in token
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


def test_validity_is_eight_hours_and_expiry_is_exclusive() -> None:
    """有效期 = `settings.session_hours`（8 小时），**到期即失效**。

    边界取「`now >= exp` 即过期」：08:00 签发 → 16:00 起 401。
    另一侧（`now > exp` 才过期）会让凭据多活一个瞬间，而 spec 的场景是按小时对齐的
    （15:00 可用 / 16:01 失效），两种取法在场景上无差 —— 取严的那个，
    少一次「为什么刚好 16:00 还能用」的追问。
    """
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    exp = datetime.fromtimestamp(_payload_of(token)["exp"], tz=timezone.utc)

    assert exp - ISSUED_AT == timedelta(hours=settings.session_hours)
    assert decode_session_token(token, now=ISSUED_AT + timedelta(hours=7))["user_id"] == USER_ID
    assert decode_session_token(token, now=exp - timedelta(seconds=1))["user_id"] == USER_ID

    for moment in (exp, exp + timedelta(seconds=1), exp + timedelta(hours=1)):
        with pytest.raises(SessionInvalid):
            decode_session_token(token, now=moment)


def test_iat_exp_are_integer_epoch_seconds() -> None:
    """`iat` / `exp` 是整数秒（不是 ISO 串、不是浮点）。

    比较「到期了没有」用服务端时钟（D7），故载荷里存的必须是可直接比较的数值；
    存 ISO 串就要在每个校验点解析一次格式，格式写法的差异会变成「有的 token 校验得过、
    有的过不了」。
    """
    claims = _payload_of(create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT))

    assert isinstance(claims["iat"], int) and isinstance(claims["exp"], int)
    assert claims["iat"] == int(ISSUED_AT.timestamp())


def test_signing_is_deterministic_but_salted_by_clock() -> None:
    """同一时刻签发两次得到**同一份**凭据（确定性，红线「同样输入必得同样输出」）。

    注意这与口令哈希不同：口令哈希必须加盐（见 `test_security.py`），
    凭据的随机性来自 `iat`/`exp` 时钟，不需要额外随机数 —— 若这里引入了随机数，
    「同样输入必得同样输出」就在会话层破了一个口子。
    """
    assert create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT) == create_session_token(
        USER_ID, ROLE, STATUS, now=ISSUED_AT
    )


# ------------------------------------------------------------------ 第二例：篡改载荷被拒

def test_tampered_payload_is_rejected() -> None:
    """篡改载荷（改角色提权）→ 签名对不上 → 拒绝。

    这是凭据最值钱的一种篡改：把 `role` 改成 `admin`。签名覆盖的是**前两段的原文**，
    所以任何一处字节变动都会让验签失败 —— 前提是验签用的正是「原文」而不是「解析后的对象」，
    后者会因为 JSON 重排键序而给出错误的通过。
    """
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    head, body, sig = token.split(".")
    claims = _payload_of(token)
    claims["role"] = "admin"
    forged = f"{head}.{_b64(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}.{sig}"

    assert forged != token
    with pytest.raises(SessionInvalid):
        decode_session_token(forged, now=ISSUED_AT)


def test_tampered_header_is_rejected() -> None:
    """篡改头（改 `alg`）→ 拒绝。"""
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    _, body, sig = token.split(".")
    forged = f"{_b64(b'{\"alg\":\"none\",\"typ\":\"JWT\"}')}.{body}.{sig}"

    with pytest.raises(SessionInvalid):
        decode_session_token(forged, now=ISSUED_AT)


# ------------------------------------------------------------------ 第三例：错误签名被拒

def test_token_signed_with_another_secret_is_rejected() -> None:
    """用别的密钥签发的凭据被拒（改密钥 = 全体凭据作废，13 §8.3 的对称语义）。"""
    forged = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT).split(".")
    other = _make_token(
        {"alg": "HS256", "typ": "JWT"},
        _payload_of(".".join(forged)),
        secret="another-secret",
    )

    with pytest.raises(SessionInvalid):
        decode_session_token(other, now=ISSUED_AT)


def test_alg_none_is_rejected() -> None:
    """`alg=none` + 空签名被拒 —— D7 把「结构上排除 alg=none」列为自实现的理由，故要真测。

    通用 JWT 库需要同时支持多种算法（乃至 `none`）才能叫通用，而本产品只用一种：
    头部里的 `alg` 只被用来**核对**，不参与选择。
    """
    payload = {"user_id": USER_ID, "role": ROLE, "status": STATUS,
               "iat": int(ISSUED_AT.timestamp()), "exp": int(ISSUED_AT.timestamp()) + 3600,
               "warehouse_id": settings.warehouse_code}
    head = _b64(b'{"alg":"none","typ":"JWT"}')
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    for candidate in (f"{head}.{body}.", f"{head}.{body}", f"{head}.{body}.{_b64(b'')}"):
        with pytest.raises(SessionInvalid):
            decode_session_token(candidate, now=ISSUED_AT)


def test_algorithm_confusion_is_rejected() -> None:
    """算法混淆被拒：头里写 `HS512`、签名换成 SHA-512 算。

    D7 的要求是「解析时**先**固定算法再验签」—— 先读头再按头选算法，等于让攻击者
    挑选验签方式。这里用 HS512 而不是 `none`：它需要**真的知道密钥**才能算出正确签名，
    所以这一例考的是「算法协商被排除」，而不是「不验签」。
    """
    payload = _payload_of(create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT))
    forged = _make_token({"alg": "HS512", "typ": "JWT"}, payload, settings.jwt_secret,
                         digest=hashlib.sha512)

    with pytest.raises(SessionInvalid):
        decode_session_token(forged, now=ISSUED_AT)


# ------------------------------------------------------------------ 形状与结构异常

def test_malformed_tokens_are_rejected() -> None:
    """段数不对 / 非法 base64 / 载荷不是 JSON / 缺 claim → 一律 `SessionInvalid`（→ 401）。

    这些形状都不会由本系统签发，但它们都可以被伪造出来。要求是**同一个异常**：
    漏成 `binascii.Error` / `json.JSONDecodeError` / `KeyError` 就会变成 500，
    而 500 与 401 的差别是免费的探测信息。
    """
    token = create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT)
    head, body, sig = token.split(".")

    bad = [
        "",
        "   ",
        "not-a-token",
        f"{head}.{body}",                      # 两段
        f"{head}.{body}.{sig}.extra",          # 四段
        f"{head}.{body}.",                     # 空签名
        f"{head}.!!!not-base64!!!.{sig}",      # 非法 base64
        f"{head}.{_b64(b'not json')}.{sig}",   # 载荷不是 JSON
        f"{_b64(b'not json')}.{body}.{sig}",   # 头不是 JSON
        f"{head}.{_b64(b'[1,2,3]')}.{sig}",    # 载荷是数组而非对象
    ]
    for candidate in bad:
        with pytest.raises(SessionInvalid):
            decode_session_token(candidate, now=ISSUED_AT)


def test_missing_claim_is_rejected() -> None:
    """缺 claim（例如没有 `status`）→ 拒绝。

    签发是自实现的，但**校验不能假设签发方一定是自己** —— 若缺字段被放过，
    下游 `claims["status"]` 会以 `KeyError` 的形式炸在中间件里（500）。
    """
    claims = _payload_of(create_session_token(USER_ID, ROLE, STATUS, now=ISSUED_AT))
    for claim in SESSION_CLAIMS:
        partial = {k: v for k, v in claims.items() if k != claim}
        forged = _make_token({"alg": "HS256", "typ": "JWT"}, partial, settings.jwt_secret)
        with pytest.raises(SessionInvalid) as excinfo:
            decode_session_token(forged, now=ISSUED_AT)
        assert claim in str(excinfo.value), "报错须指名缺了哪个字段"


def test_non_string_token_is_rejected() -> None:
    """`None` / 非串入参（中间件会传 `request.headers.get(...)` 的结果）→ 拒绝而非抛类型错。"""
    for candidate in (None, b"bytes", 123):
        with pytest.raises(SessionInvalid):
            decode_session_token(candidate)  # type: ignore[arg-type]


# ------------------------------------------------------------------ D7 的结构性约束

def test_security_module_does_not_import_a_jwt_library() -> None:
    """D7：不得引入 `jwt` / `jose` —— 用 AST 读 import 清单，不做子串匹配。

    子串匹配会把 docstring 里解释「为什么不用 PyJWT」的那句话判成违规，
    于是只有删掉解释才能变绿 —— 正好把最有价值的一段挤掉（与
    `tests/models/test_migrations.py` 的 `create_all` 守卫同一处置）。
    """
    source = Path(__file__).resolve().parents[2] / "app" / "core" / "security.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    banned = [name for name in imported if name.split(".")[0] in {"jwt", "jose", "authlib"}]
    assert banned == [], f"security.py 引入了第三方 JWT 库：{banned}（D7 要求只用标准库）"


def test_signature_comparison_is_constant_time() -> None:
    """签名比较必须用 `hmac.compare_digest`（D7）。

    普通的 `==` 在第一个不同的字节处返回，比较耗时与「猜对了多少个前缀」相关
    —— 逐字节猜签名的攻击正是靠这个差异。这里查源码里确实出现了它，
    并禁止裸比较（`sig ==` / `== expected_sig`）。
    """
    source = Path(__file__).resolve().parents[2] / "app" / "core" / "security.py"
    text = source.read_text(encoding="utf-8")

    assert "hmac.compare_digest(" in text
    assert "== expected_signature" not in text
    assert "expected_signature ==" not in text
