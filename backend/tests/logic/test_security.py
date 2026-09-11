"""口令哈希的契约测试（tasks.md 7.1 的验证）。

事实来源：13-权限分级与访问控制系统 §7.1（密码策略：初始密码管理员线下发放，首次登录强制改密）
          spec `auth`「初始密码与首次强制改密」（密码必须以不可逆哈希存储）
          design.md D6（直接用 `bcrypt`，不用 passlib）、Risks（bcrypt 对 >72 字节抛错）

三例是 tasks 7.1 点名的：正确口令通过 / 错误口令不通过 / 超长口令给出明确错误。
另补的几例都对应**会静默失效**的失败模式：

  - **加盐**：两次哈希同一口令得到不同串。相同就说明用了无盐/固定盐 —— 一张彩虹表
    可以同时解掉全系统的口令。
  - **多字节按字节算**：`汉` × 24 = 72 字节可过、× 25 = 75 字节必须报错。
    按**字符**数判会放行 75 字节的输入，而 bcrypt 只取前 72 字节 ——
    于是「口令 A」与「口令 A + 一个汉字」互相通过验证。
  - **畸形哈希不抛异常**：库里存了半截/非 bcrypt 的串时，校验函数返回 `False`
    而不是向上抛 —— 抛出去的异常会变成 500，而 500 与 401 的差别本身就是探测面。

**成本因子在测试里降到 4**：默认 12 每次约 0.28s，本文件与 `test_auth.py` 合计几十次
调用会把整包推出 pre-commit 的 L1 门禁（<5s）。用例关心的是「通过 / 不通过」的语义，
与成本因子无关；生产侧由 `settings.assert_production_safe` 挡住低于 12 的取值。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.security import BCRYPT_MAX_PASSWORD_BYTES, hash_password, verify_password

pytestmark = pytest.mark.logic

#: 22 §2.1 的登录表单样本（文档只给「用户名 + 密码」两栏，具体口令是合成值）。
PLAIN = "Gtj@2026#init"


@pytest.fixture(autouse=True)
def _cheap_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bcrypt_cost", 4)


def test_correct_password_verifies() -> None:
    """正确口令通过 —— spec `auth`「首次登录强制改密」的前提。"""
    assert verify_password(PLAIN, hash_password(PLAIN)) is True


def test_wrong_password_does_not_verify() -> None:
    """错误口令不通过。"""
    hashed = hash_password(PLAIN)
    assert verify_password("Gtj@2026#ini", hashed) is False   # 少一个字符
    assert verify_password(PLAIN + "x", hashed) is False      # 多一个字符
    assert verify_password("", hashed) is False


def test_over_long_password_raises_a_clear_error() -> None:
    """超长口令给出**明确错误**而非 500（tasks 7.1 的第三例，D6 的 Risks）。

    bcrypt 5.x 自己会抛 `ValueError: password cannot be longer than 72 bytes`，
    但那句话不指向修法。这里要求的是一个说明**该在接口层限制长度**的错误 ——
    调用方据此返回 422 而不是把它漏成 500。
    """
    too_long = "a" * (BCRYPT_MAX_PASSWORD_BYTES + 1)

    with pytest.raises(ValueError) as excinfo:
        hash_password(too_long)

    assert "72" in str(excinfo.value)


def test_over_long_password_fails_verification_instead_of_raising() -> None:
    """校验侧不抛：超长输入返回 `False`。

    登录端点的语义是「凭据不合格 → 401」，把异常漏上去会变成 500 —— 而
    「500 与 401 的差别」本身就告诉攻击者「这个口令长度特殊」。
    """
    hashed = hash_password(PLAIN)
    assert verify_password("a" * (BCRYPT_MAX_PASSWORD_BYTES + 1), hashed) is False


def test_limit_is_counted_in_bytes_not_characters() -> None:
    """长度上限按**字节**算（72 字节 = 24 个三字节汉字）。

    按字符数判会放行 75 字节的输入，而 bcrypt 只取前 72 字节 ——
    于是「24 个汉字 + 1 个汉字」与「24 个汉字」互相通过验证。
    """
    exactly_72 = "汉" * 24
    assert len(exactly_72.encode("utf-8")) == BCRYPT_MAX_PASSWORD_BYTES
    hash_password(exactly_72)  # 不抛

    with pytest.raises(ValueError):
        hash_password("汉" * 25)


def test_hash_is_salted() -> None:
    """同一口令两次哈希不同（`bcrypt` 自带随机盐）。

    相同即说明用了固定盐或无盐，一张彩虹表就能同时解掉全系统的口令。
    """
    assert hash_password(PLAIN) != hash_password(PLAIN)


def test_hash_is_not_reversible_looking_text() -> None:
    """库里存的是 bcrypt 形态的哈希（`$2b$` 前缀），不含明文。"""
    hashed = hash_password(PLAIN)

    assert hashed.startswith("$2b$")
    assert PLAIN not in hashed


def test_malformed_hash_returns_false() -> None:
    """库里的哈希畸形（手工改过的行、别的算法写的行）→ 校验返回 `False`，不抛。

    这条不只是健壮性：如果它抛，那么「这一行坏了」会以 500 的形式暴露出来，
    而 500 与 401 的差异足以让攻击者区分「账号存在但数据坏了」与「凭据不对」。
    """
    for broken in ("", "not-a-hash", "$2b$12$too-short", "$argon2id$v=19$m=1,t=1,p=1$x$y"):
        assert verify_password(PLAIN, broken) is False


def test_non_ascii_password_round_trips() -> None:
    """非 ASCII 口令（现场可能用中文）按 UTF-8 编码后哈希与校验。"""
    chinese = "康饮台塑立体库"
    assert verify_password(chinese, hash_password(chinese)) is True
    assert verify_password("康饮台塑立体库1", hash_password(chinese)) is False
