"""登录端点与认证中间件的契约测试（tasks.md 7.4 ~ 7.7 的验证）。

事实来源：13-权限分级与访问控制系统 §5.1（状态流转）、§5.2（账号字段）、§6.1（白名单）、
          §7（认证体系）、§8（会话与登出）
          22-前端规格与设计系统 §2.1（登录页：失败提示文案「账号或密码错误」、成功跳转）
          spec `auth`（六条 Requirement 的场景）
          openspec/changes/data-model-permission/tasks.md 7.4 ~ 7.7
          `26` 完成标准 #4（白名单放行 / 有效凭据通过 / 过期凭据 401 / 权限拒绝路径）

## 为什么本文件自带一个应用与一个库，而不是用 conftest 的夹具

`tests/conftest.py` 的 `session` 夹具把用例包在一层**外层事务**里（结束时整体回滚）。
本文件测的是中间件：它在请求进入路由之前**自己开一个会话**去回查账号状态（7.6），
那个会话不在用例的外层事务里 —— 而内存库是 `StaticPool`（全进程**一条**连接），
于是「外层已经 BEGIN 了，中间件又要 BEGIN」必然撞车。

所以这里给每个用例起一套**独立的**「引擎 + 工厂 + 应用」：内存库（D12）、
`create_all` 建表（测试库瞬时的，不引入迁移，D8 只管真实库）、
把工厂挂到 `app.state.session_factory` —— 中间件与 `deps.get_db` 读的是**同一个属性**，
一次覆盖两处，不会出现「路由看到了测试库、中间件看到了开发库」这种半覆盖。

**不使用 `tmp_path` 文件库**：文件库是 design.md Risks 为「cap 与台账同事务写入」
（阶段四）开的方子。本文件不需要真实事务语义 —— 需要的是「没有外层事务」，
而这由夹具自己建工厂就已满足。

## 密码哈希的成本因子

与 `tests/logic/test_security.py` 同一处置：autouse 夹具把 `settings.bcrypt_cost`
降到 4。本文件有二十来次哈希与校验，默认 12（每次约 0.28s）会把整包推出
pre-commit 的 L1 门禁（<5s）。用例断言的是「通过 / 不通过」的语义，与成本因子无关。
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表依赖）
from app.api.middleware import COOKIE_NAME
from app.core.config import settings
from app.core.enums import AccountStatus, Role
from app.core.errors import PermissionDenied
from app.core.security import create_session_token, decode_session_token, hash_password
from app.main import create_app
from app.models.base import Base
from app.models.identity import Account

#: 22 §2.1 的登录表单样本（文档只给「用户名 + 密码」两栏，具体口令是合成值）。
PASSWORD = "Gtj@2026#init"
USERNAME = "gtj_keeper"
#: 登录失败的一行提示（22 §2.1：「登录失败红色提示（红字『账号或密码错误』）」）。
FAILURE_MESSAGE = "账号或密码错误"


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """与 conftest 同一处置：SQLite 默认关闭外键，不显式打开则 FK 形同虚设。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


@dataclass
class Api:
    """一个用例的全套家当：可被覆盖的应用、它背后的库、以及几个动作。"""

    client: TestClient
    factory: sessionmaker

    # ------------------------------------------------------------ 造数
    def create_account(
        self,
        username: str = USERNAME,
        password: str = PASSWORD,
        role: Role = Role.WAREHOUSE_KEEPER,
        status: AccountStatus = AccountStatus.ACTIVE,
        initial_password_changed: bool = False,
    ) -> int:
        """建一个账号并返回 id。默认是「已激活 + 初始密码未改」，即登录页的正常起点。"""
        with self.factory() as session:
            account = Account(
                warehouse_id=settings.warehouse_code,
                username=username,
                password_hash=hash_password(password),
                role=role,
                status=status,
                initial_password_changed=initial_password_changed,
            )
            session.add(account)
            session.commit()
            return account.id

    def set_status(self, account_id: int, status: AccountStatus) -> None:
        """管理员动作的替身：把状态改掉，模拟「停用」（13 §8.3 的紧急吊销）。"""
        with self.factory() as session:
            account = session.get(Account, account_id)
            assert account is not None
            account.status = status
            session.commit()

    def reload(self, account_id: int) -> Account:
        """从库里重读一行（用**新**会话，避免读到身份映射里的旧对象）。"""
        with self.factory() as session:
            account = session.get(Account, account_id)
            assert account is not None
            session.expunge(account)
            return account

    # ------------------------------------------------------------ 动作
    def login(self, username: str = USERNAME, password: str = PASSWORD):
        return self.client.post("/api/auth/login", json={"username": username, "password": password})

    def token_of(self, response) -> str:
        return response.json()["access_token"]

    def auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def login_token(self, **kwargs) -> str:
        return self.token_of(self.login(**kwargs))


def _create_accounts_table(engine) -> None:
    """只建 `accounts` 一张表。

    本文件只碰账号（登录 + 状态回查），而 `create_all` 会把 23 张实体表全建一遍 ——
    每个用例一套库，那点开销乘三十次就够把整包推向 5 秒的门禁线。
    `accounts` 的唯一外键指向它自己（`created_by_id`），故这张表可以单独建。
    """
    Base.metadata.create_all(engine, tables=[Account.__table__])


@pytest.fixture(autouse=True)
def _cheap_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bcrypt_cost", 4)


@pytest.fixture
def api() -> Iterator[Api]:
    """独立的「引擎 + 工厂 + 应用」三件套（见模块 docstring）。"""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    _create_accounts_table(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    application = create_app()
    # 中间件与 deps.get_db 都读这个属性 —— 一处覆盖，两处生效。
    application.state.session_factory = factory

    try:
        yield Api(client=TestClient(application), factory=factory)
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 夹具自检

def test_the_test_app_really_uses_the_test_database(api: Api) -> None:
    """夹具自检：确认 `app.state.session_factory` 真的被换掉了。

    这一条防的是**最坏的一种绿**：覆盖没生效时，中间件会去读 `app.core.db.SessionLocal`
    —— 那是**真实开发库**（`data/warehouse.db`）。届时「登录成功」这条断言仍可能通过
    （开发库里恰好有账号），而用例已经不再测被测对象，还会往开发库里写 `last_login_at`。
    """
    from app.core.db import SessionLocal

    with api.factory() as session:
        assert session.get_bind().url.database in (None, ":memory:"), (
            "测试工厂指向的不是内存库 —— 覆盖没生效"
        )
    assert SessionLocal is not api.factory


# ------------------------------------------------------------------ 7.4 登录端点

def test_login_needs_no_credentials(api: Api) -> None:
    """spec 场景「登录端点免认证」：未携带任何凭据也能登录成功（白名单 13 §6.1）。"""
    api.create_account()

    response = api.login()

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_returns_the_session_claims(api: Api) -> None:
    """返回的凭据就是 13 §7.2 的那份 claims —— 签发的内容与断言逐项对上。"""
    account_id = api.create_account(role=Role.SUPERVISOR)

    claims = decode_session_token(api.login_token())

    assert claims["user_id"] == account_id
    assert claims["role"] == "supervisor"
    assert claims["status"] == "active"
    assert claims["warehouse_id"] == settings.warehouse_code
    assert claims["exp"] - claims["iat"] == settings.session_hours * 3600


def test_login_response_carries_the_account_facts_the_frontend_needs(api: Api) -> None:
    """响应体给出角色与用户 id（13 §8.1：会话级存储放 `user_id`/`role`）。

    前端拿到凭据后要立刻决定菜单可见性（13 §二 的矩阵），若只给一串 token，
    它必须先解开载荷才知道自己是谁 —— 那是把签名验证的逻辑抄进前端。
    """
    account_id = api.create_account(role=Role.PLANNER)

    body = api.login().json()

    assert body["user_id"] == account_id
    assert body["role"] == "planner"
    assert body["warehouse_id"] == settings.warehouse_code
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.session_hours * 3600


def test_login_marks_first_login_must_change_password(api: Api) -> None:
    """tasks 7.4 的「首次登录标记须改密」：标志取自 `initial_password_changed`。

    这条标记是前端把用户引导到改密页的唯一依据（13 §7.1 密码策略：初始密码管理员
    线下发放，首次登录强制改密）。**只给标记、不做拦截** —— 拦截属路线图，
    见 tasks.md 9.4g 的登记。
    """
    api.create_account(initial_password_changed=False)
    assert api.login().json()["must_change_password"] is True

    api.create_account(username="gtj_planner", initial_password_changed=True)
    assert api.login("gtj_planner").json()["must_change_password"] is False


def test_login_records_last_login_at(api: Api) -> None:
    """登录写回 `last_login_at`（13 §5.2 的账号字段：最后登录时间）。

    空 = 从未登录（见 `Account` 的取舍第 3 条），所以这个字段只能由登录来填。
    """
    account_id = api.create_account()
    assert api.reload(account_id).last_login_at is None

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    api.login()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    stamp = api.reload(account_id).last_login_at
    assert stamp is not None
    assert before - timedelta(seconds=5) <= stamp <= after + timedelta(seconds=5)


def test_failed_login_does_not_record_last_login(api: Api) -> None:
    """口令错的那次**不写**登录时间 —— 它不该在审计上留下「登录过」的痕迹。"""
    account_id = api.create_account()

    assert api.login(password="wrong").status_code == 401
    assert api.reload(account_id).last_login_at is None


def test_login_response_never_echoes_the_password(api: Api) -> None:
    """spec 场景「响应不回显密码」：响应体里既无明文，也无哈希。

    查的是**整个响应文本**而不只是某个字段：这类泄露通常来自「顺手把账号对象
    序列化出去」（`Account` 上有 `password_hash` 列，它的值恰好是 `$2b$…`），
    只盯着 `password` 字段名会漏掉它。
    """
    account_id = api.create_account()
    hashed = api.reload(account_id).password_hash

    text = api.login().text

    assert PASSWORD not in text
    assert hashed not in text
    assert "$2b$" not in text


def test_login_accepts_json_body(api: Api) -> None:
    """请求体形态：JSON `{username, password}`（开发阶段决策，13/22 未规定报文格式）。

    选 JSON 而非表单：前端是零构建 Vanilla JS 的同源 fetch，JSON 是它的原生形态；
    表单需要在客户端做 urlencode、在服务端引入 form 解析依赖，而这条路径上
    没有任何东西要求那样做。（文件导入的 `multipart` 是另一回事，它真有文件要传。）
    """
    api.create_account()

    response = api.client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )

    assert response.status_code == 200


def test_login_rejects_a_malformed_body_without_a_server_error(api: Api) -> None:
    """报文缺字段 → 422（FastAPI 的请求校验），不是 500、也不是 401。

    401 是「凭据不对」，422 是「报文不合法」—— 两者混同会让前端把「表单没填完」
    当成「账号密码错误」提示出来。
    """
    assert api.client.post("/api/auth/login", json={"username": USERNAME}).status_code == 422
    assert api.client.post("/api/auth/login", json={}).status_code == 422


# ------------------------------------------------------------------ 7.5 失败语义一致

def test_unknown_username_and_wrong_password_are_indistinguishable(api: Api) -> None:
    """spec 场景「失败原因不区分」：两次响应的状态码与响应体**逐字一致**。

    比「都是 401」更强的断言：只要响应体有一处不同（消息里带「用户不存在」、
    `detail` 里带上 `{"reason": …}`、甚至字段顺序不同），登录页就变成了一个
    账号存在性探测器 —— 而枚举出有效用户名，是口令爆破的第一步。
    """
    api.create_account()

    unknown = api.login(username="no_such_user")
    wrong = api.login(password="wrong-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.text == wrong.text


def test_all_login_failures_share_one_identical_response(api: Api) -> None:
    """把**全部**失败形态放在一起比：不存在 / 口令错 / 未激活 / 已停用 / 已驳回。

    非 active 的账号也被拉齐到同一份 401：状态本身就是敏感信息（离职、调岗、驳回），
    而「未激活」与「密码错」在登录页上没有任何可执行的差别 —— 用户能做的事都是
    打给管理员。22 §2.1 的失败提示也只有一条文案。
    """
    api.create_account()
    api.create_account(username="u_pending", status=AccountStatus.PENDING)
    api.create_account(username="u_disabled", status=AccountStatus.DISABLED)
    api.create_account(username="u_rejected", status=AccountStatus.REJECTED)

    responses = [
        api.login(username="no_such_user"),
        api.login(password="wrong-password"),
        api.login("u_pending", PASSWORD),
        api.login("u_disabled", PASSWORD),
        api.login("u_rejected", PASSWORD),
    ]

    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1, "失败响应必须逐字一致：%r" % (
        {r.text for r in responses},
    )


def test_failure_message_is_the_login_page_copy(api: Api) -> None:
    """文案取自 22 §2.1 的登录页：「账号或密码错误」。"""
    api.create_account()

    body = api.login(password="wrong").json()

    assert body["message"] == FAILURE_MESSAGE
    assert body["error"] == "unauthenticated"


def test_failed_login_issues_no_token(api: Api) -> None:
    """失败的响应体里不得出现任何凭据 —— 连字段都不该有。"""
    api.create_account()

    body = api.login(password="wrong").json()

    assert "access_token" not in body
    assert set(body) == {"error", "message"}


def test_unknown_username_still_runs_a_password_check(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户名不存在时**照样做一次口令校验**（抹平时序差）。

    不这么做的话，「用户名不存在」会在 bcrypt 之前返回，而 bcrypt 一次要几十毫秒
    —— 于是响应时间本身成了一个账号存在性探测器：快 = 不存在，慢 = 存在。
    响应体一致只挡住了**读**，挡不住**计时**。

    这里用替身断言那条路径真的被走到了（不去测真实耗时：那种断言在 CI 上必然飘）。
    替身收到的第一个参数必须是**一个哈希串**，否则「校验一个空哈希」会立刻返回，
    时间差原样保留。
    """
    calls: list[tuple[str, str]] = []

    def spy(plain: str, hashed: str) -> bool:
        calls.append((plain, hashed))
        return False

    monkeypatch.setattr("app.api.routes.auth.verify_password", spy)

    assert api.login(username="no_such_user").status_code == 401

    assert len(calls) == 1
    plain, hashed = calls[0]
    assert plain == PASSWORD
    assert hashed.startswith("$2b$"), "替身拿到的必须是真哈希，否则这次校验近乎零耗时"


# ------------------------------------------------------------------ 7.6 状态回查与紧急吊销

def test_disabled_account_token_dies_on_the_next_request(api: Api) -> None:
    """spec 场景「停用后旧凭据立即失效」：置 disabled 后同一份凭据立刻 401。

    注意凭据本身完全合法：`decode_session_token` 仍然通过，载荷里的 `status` claim
    写的也是 `active`（签发时它确实是）。**中间件不能只信这份快照** —— 12 §8.3 的
    紧急吊销就建在「回查当前状态」这一步上，没有它，停用要等 8 小时才生效。
    """
    account_id = api.create_account()
    token = api.login_token()
    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 200
    assert decode_session_token(token)["status"] == "active", "凭据里的快照是签发时的，本就该是 active"

    api.set_status(account_id, AccountStatus.DISABLED)

    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 401


def test_revocation_takes_effect_without_a_restart(api: Api) -> None:
    """同一个应用实例、同一个连接池、**不重启**：停用前后各请求一次即可。

    这条与上一条测的不是一回事：上一条测「状态变了要拦住」，这条测「拦截不依赖
    进程重启或缓存失效」。若哪天有人把账号查表结果放进模块级缓存"优化性能"，
    这里会红 —— 而那时 13 §8.3 的紧急吊销就废了。
    """
    account_id = api.create_account()
    client, token = api.client, api.login_token()

    assert client.get("/api/health", headers=api.auth(token)).status_code == 200
    api.set_status(account_id, AccountStatus.DISABLED)
    assert client.get("/api/health", headers=api.auth(token)).status_code == 401


def test_re_enabled_account_can_use_its_old_token_again(api: Api) -> None:
    """`disabled → active` 之后，那份**尚未过期**的旧凭据又能用了。

    这是「无服务端黑名单」（13 §8.3、spec「系统不得依赖服务端会话存储或凭据黑名单」）
    的对称语义，也是最容易被误当成 bug 的一处：停用不是吊销记录，而是**一次状态查询**。
    把它钉成断言，免得后来者"顺手"加一张吊销表 —— 那会引入本阶段刻意不要的服务端态。
    """
    account_id = api.create_account()
    token = api.login_token()

    api.set_status(account_id, AccountStatus.DISABLED)
    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 401

    api.set_status(account_id, AccountStatus.ACTIVE)
    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 200


def test_token_of_a_missing_account_is_rejected(api: Api) -> None:
    """凭据指向一个库里没有的账号 → 401（不是 500，也不是放行）。

    本系统不删账号（归档不删除），但「行不在」仍是一条真实路径：库被重建、
    账号被迁移、或有人手工改过库。签名有效但主体不存在时，唯一安全的处置是拒绝。
    """
    token = create_session_token(999_999, "admin", "active")

    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 401


def test_non_active_statuses_never_pass_the_middleware(api: Api) -> None:
    """`pending` / `rejected` / `disabled` 一律被拦 —— 门是 `status == active`，
    不是「拦住 disabled」。

    写成「拦住 disabled」是一种很自然的疏忽，后果是 pending / rejected 的账号
    只要拿到一份签名有效的凭据就能进业务端点。

    三份凭据的载荷**都写着 `active`**（冒充签发时的状态），各自指向上面那三个账号：
    拦不住的话，每一条都会变成 200。
    """
    ids = {
        status: api.create_account(username=f"u_{status.value}", status=status)
        for status in (AccountStatus.PENDING, AccountStatus.REJECTED, AccountStatus.DISABLED)
    }

    for status, account_id in ids.items():
        forged = create_session_token(account_id, "warehouse_keeper", "active", now=datetime.now(timezone.utc))
        response = api.client.get("/api/health", headers=api.auth(forged))
        assert response.status_code == 401, f"{status.value} 的凭据不该通过"


def test_middleware_accepts_the_cookie_too(api: Api) -> None:
    """Cookie 形态也认（`wms_session`）—— 中间件两条读取路径都要真的走通。

    13 §6.1 只说「从请求头 / Cookie 提取」，两条路径分流后容易只测一条，
    另一条在真机上才炸。这里把 Cookie 那条走一遍。
    """
    api.create_account()
    token = api.login_token()

    response = api.client.get("/api/health", cookies={COOKIE_NAME: token})

    assert response.status_code == 200


# ------------------------------------- 7.6b 凭据内容不合法：一律 401，不得漏成 500

def test_credential_with_an_unknown_role_is_rejected(api: Api) -> None:
    """`role` 取值不在 `Role` 枚举内 → 401（`auth` spec：凭据问题一律 401）。

    本系统只签发四个角色（13 §2.2），出现第五个只能是被改过或跨版本 ——
    「签名有效」只说明内容没被第三方改过，不说明内容合法。

    这条同时守住一件更隐蔽的事：中间件把 `role` **转成 `Role` 成员**才放进
    `request.state`。若原样放字符串，第 2 层的 `isinstance(role, Role)` 守卫会抛
    `TypeError`，于是每个带这种凭据的请求都变成 500 —— 方向恰好是最不该出错的那个。
    """
    account_id = api.create_account()
    token = create_session_token(account_id, "ceo", "active")

    response = api.client.get("/api/health", headers=api.auth(token))

    assert response.status_code == 401


#: 签名合法但 `user_id` 类型不对的载荷。`type: ignore` 是**刻意**的：正常路径造不出
#: 这些值，本用例要的正是「一份本系统绝不会签发的凭据」（见 test_token.py 的同类用例）。
_BAD_USER_IDS: tuple[object, ...] = ({}, [1, 2], "7", None, True)


@pytest.mark.parametrize("user_id", _BAD_USER_IDS, ids=[repr(v) for v in _BAD_USER_IDS])
def test_credential_with_a_malformed_user_id_is_rejected(api: Api, user_id: object) -> None:
    """`user_id` 不是整数 → 401。

    实测过这条路径的三种落点：`{}` / `[1, 2]` 让 `session.get(Account, …)` 抛
    `InvalidRequestError`（500），`"7"` 能查到（SQLite 按等值比较），`True` 会命中
    主键 1（bool 是 int 的子类）。所以「类型对不对」必须在校验层判掉，
    而不是指望下游恰好看不出差别。
    """
    token = create_session_token(user_id, "warehouse_keeper", "active")  # type: ignore[arg-type]

    assert api.client.get("/api/health", headers=api.auth(token)).status_code == 401


def test_middleware_puts_a_role_member_on_the_request_state(api: Api) -> None:
    """`request.state.role` 是 `Role` 成员，不是裸字符串。

    `Role` / `AccountStatus` 是 `str` 枚举：`Role.ADMIN == "admin"` 为真，但
    `Enum.__hash__` 取**成员名**，于是拿裸字符串去查 `ROLE_PERMISSIONS` 会**静默
    查不中**并一律返回 False（现场表现：「管理员登录后菜单全空」而日志无错）。
    `permissions.check` 为此加了 `isinstance(role, Role)` 守卫 —— 守卫要求传成员，
    所以中间件放什么类型是有后果的：放字符串，每个请求都会在守卫那里抛 `TypeError`。

    探针路由挂在本用例新建的应用实例上（`create_app()` 每个用例一次），
    不会留在模块级 `app.routes` 里影响装配完整性断言。
    """
    api.create_account(role=Role.SUPERVISOR)

    @api.client.app.get("/api/_probe/role-type")
    def _probe(request: Request) -> dict:
        role = request.state.role
        return {
            "is_member": isinstance(role, Role),
            "type": type(role).__name__,
            "value": getattr(role, "value", None),
        }

    response = api.client.get("/api/_probe/role-type", headers=api.auth(api.login_token()))

    assert response.status_code == 200
    assert response.json() == {"is_member": True, "type": "Role", "value": "supervisor"}


# ------------------------------------------------------------------ 7.7 完成标准 #4 四场景

def test_standard_4_scenario_whitelist_passes(api: Api) -> None:
    """场景一：白名单放行 —— 不带任何凭据访问 `/api/auth/login` 与 `/health`。"""
    assert api.client.get("/health").status_code == 200
    assert api.client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 401
    # 401 是「凭据不对」，不是「被中间件拦下」—— 端点本身可达（白名单生效）。
    assert api.client.get("/api/auth/login").status_code == 405, "方法不对 = 已达路由"


def test_standard_4_scenario_valid_credential_passes(api: Api) -> None:
    """场景二：有效凭据通过 —— 登录拿到凭据，再带它访问受保护端点。"""
    api.create_account()

    token = api.login_token()
    response = api.client.get("/api/health", headers=api.auth(token))

    assert response.status_code == 200
    assert response.json()["cold_path_enabled"] is False


def test_standard_4_scenario_expired_credential_is_401(api: Api) -> None:
    """场景三：过期凭据 401 —— spec 场景「08:00 登录、16:01 请求」的等价写法。

    用 `now` 把签发时刻回拨 9 小时（而不是等 8 小时），拿到一份**签名完全合法**
    但已过期的凭据：过期必须是「校验时拿服务端时钟比 `exp`」，而不是靠别的手段。
    """
    account_id = api.create_account()
    issued = datetime.now(timezone.utc) - timedelta(hours=settings.session_hours + 1)
    expired = create_session_token(account_id, Role.WAREHOUSE_KEEPER.value, "active", now=issued)

    response = api.client.get("/api/health", headers=api.auth(expired))

    assert response.status_code == 401
    assert "过期" in response.json()["message"]


def test_standard_4_scenario_permission_denied_path() -> None:
    """场景四：权限拒绝路径 —— 403 与它的响应体形状。

    D9：端点级资源鉴权**不在本阶段**（矩阵数据 + 纯判定函数在 §8 验），
    所以这里挂一条临时探针路由，直接抛 `PermissionDenied`，验的是**异常到响应的
    映射**：403 + `{"error": "permission_denied", ...}`。

    探针挂在**新建的应用实例**上（不是模块级 `app`），否则这条只为测试存在的路由
    会留在 `app.routes` 里，被 `test_smoke.py` 的「装配完整性」断言看见。
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    _create_accounts_table(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    application = create_app()
    application.state.session_factory = factory

    @application.get("/api/_probe/account-manage")
    def _probe() -> None:
        # 13 §5.3：非管理员触发账号管理 → 403。本阶段没有账号管理端点，
        # 故由探针模拟一个已通过认证、但资源级检查不通过的请求。
        raise PermissionDenied("当前角色无权管理账号", detail={"resource": "account", "action": "manage"})

    try:
        with factory() as session:
            account = Account(
                warehouse_id=settings.warehouse_code,
                username="probe_keeper",
                password_hash=hash_password(PASSWORD),
                role=Role.WAREHOUSE_KEEPER,
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            session.commit()
            account_id = account.id

        client = TestClient(application)
        token = create_session_token(account_id, "warehouse_keeper", "active")

        response = client.get("/api/_probe/account-manage", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 403
        assert response.json() == {
            "error": "permission_denied",
            "message": "当前角色无权管理账号",
            "detail": {"resource": "account", "action": "manage"},
        }
    finally:
        engine.dispose()


def test_standard_4_scenario_probe_route_needs_a_valid_credential() -> None:
    """场景四的对照：同一条探针路由**没有凭据**时是 401 而不是 403。

    两层检查的顺序是有意义的（13 §六）：认证在前，权限在后。反过来的话，
    未登录的请求会从 403 里读到「这个端点存在、只是你没权限」。
    """
    application = create_app()

    @application.get("/api/_probe/any")
    def _probe() -> None:
        raise PermissionDenied("不该走到这里")

    client = TestClient(application)
    assert client.get("/api/_probe/any").status_code == 401


# ------------------------------------------------------------------ 其余横切行为

def test_login_endpoint_is_not_shadowed_by_the_middleware(api: Api) -> None:
    """携带一份坏凭据去登录，仍然能登录成功（白名单在凭据解析**之前**）。

    若中间件先解析凭据再判白名单，一个过期凭据就会把人锁在登录页外 ——
    而这正是最需要登录的时候。
    """
    api.create_account()

    response = api.client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Authorization": "Bearer not-a-token"},
    )

    assert response.status_code == 200


def test_401_body_shape_matches_the_smoke_test_expectation(api: Api) -> None:
    """中间件的 401 响应体形状保持不变（`{"error": "unauthenticated", "message": …}`）。

    `tests/api/test_smoke.py` 只断言状态码，这里把**形状**也钉住：前端的拦截器按
    `error` 分流（401 → 跳登录页），字段改名会让它静默失效。
    """
    response = api.client.get("/api/health")

    assert response.status_code == 401
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == "unauthenticated"


def test_http_exception_from_a_route_is_not_swallowed(api: Api) -> None:
    """路由自己抛的 `HTTPException` 不被领域异常处理器吃掉 → 原样 404。

    领域异常处理器只认 `DomainError`。写宽了（例如 catch `Exception`）会把 FastAPI
    自身的 404/405 一并改写成 400，前端再也分不出「没有这个端点」与「参数不对」。
    """
    api.create_account()
    token = api.login_token()

    response = api.client.get("/api/no-such-route-at-all", headers=api.auth(token))

    assert response.status_code == 404


def test_deps_get_db_yields_a_session_bound_to_the_app_factory(api: Api) -> None:
    """`deps.get_db` 与中间件读**同一个** `app.state.session_factory`（一次覆盖两处）。"""
    from app.api.deps import get_db

    application = api.client.app

    class _Req:
        app = application

    generator = get_db(_Req())  # type: ignore[arg-type]
    session: Session = next(generator)
    try:
        assert session.get_bind() is api.factory.kw["bind"]
    finally:
        generator.close()
