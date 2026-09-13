"""`/api/import/*` / `/api/snapshot/current` / `/api/cap` 路由（tasks.md 6.1 的验证）。

事实来源：16-数据衔接与 cap 自维护 附录B（7 个 import 端点 + snapshot/cap）
          spec `data-import`「数据导入页数据层」（两步交互：开始校验 / 执行导入）
          openspec/changes/data-import/design.md D7（p2 接 `/api/import/*`）
          tasks.md 6.1（session/upload/validate/result/execute/retry + snapshot/current +
          cap/recompute + cap）

服务层编排（`run_validation` / `execute_import`）已被 `test_import_execute*` 直连库锁定；
本文件钉的是**HTTP 层**：端点行为、认证（401）、错误处理（404 会话不存在 / 409 状态冲突 /
422 报文错误）、以及三步流程走通后快照与 cap 的可读。

## 端点契约（本文件的权威形状）

- `POST /api/import/session`  `{warehouse_id?, data_time}` → `{session_no, import_batch_no, status, data_time}`
- `POST /api/import/upload`   `{session_no, file_type, filename, content_base64}` → 文件元数据
- `POST /api/import/validate` `{session_no}` → `{session_no, status, receipt}`
- `GET  /api/import/result/{session_no}` → 同上（读库里的回执）
- `POST /api/import/execute`  `{session_no}` → `{session_no, status, receipt, snapshot_version_no?}`
- `POST /api/import/retry`    `{session_no}` → `{session_no, status}`
- `GET  /api/snapshot/current` → `{snapshot_id, version_no, snapshot_time, snapshot_version}`
- `GET  /api/cap?aisle=`       → 巷道 cap 列表
- `POST /api/cap/recompute`    → 对当前快照全量重算 cap（漂移校正）

文件以 **base64 走 JSON** 上传（非 multipart）：零构建前端 FileReader 直接给，测试也不必
拼 multipart 报文；`files_json` 存 `{file_type, filename, size, sha256, content_base64}`。
"""
from __future__ import annotations

import base64

from sqlalchemy import select

from app.core.enums import ImportStatus, JobType
from app.models.job import JobOrder
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

WAREHOUSE = "GTJ10036"

_INV_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"
_PODO_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"

# 巷道 01 两个去重库位、巷道 02 一个去重库位（与 test_import_execute_inv.py 同口径）。
GOOD_INV = (
    f"{_INV_HEADER}\n"
    "GTJ10036,010104,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,010205,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,020101,3005678,PET500 茉莉柚茶,B002,合格,40,2026-09-08 00:00:00\n"
)
BAD_INV = f"{_INV_HEADER}\nGTJ10036,010104,M1,可乐,,合格,40,2026-09-08 00:00:00\n"  # 批号空
PO_CSV = f"{_PODO_HEADER}\nPO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
DO_CSV = f"{_PODO_HEADER}\nDO-01,20,发货单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,120\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _create_session(client, *, headers, data_time="2026-09-08T00:00:00") -> str:
    resp = client.post(
        "/api/import/session",
        json={"warehouse_id": WAREHOUSE, "data_time": data_time},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_no"]


def _upload(client, *, headers, session_no: str, file_type: str, filename: str, text: str) -> None:
    resp = client.post(
        "/api/import/upload",
        json={
            "session_no": session_no,
            "file_type": file_type,
            "filename": filename,
            "content_base64": _b64(text),
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _validate(client, *, headers, session_no: str):
    return client.post("/api/import/validate", json={"session_no": session_no}, headers=headers)


def _upload_three(client, *, headers, session_no: str) -> None:
    _upload(client, headers=headers, session_no=session_no, file_type="PO", filename="PO.csv", text=PO_CSV)
    _upload(client, headers=headers, session_no=session_no, file_type="DO", filename="DO.csv", text=DO_CSV)
    _upload(client, headers=headers, session_no=session_no, file_type="INV", filename="INV.csv", text=GOOD_INV)


# ------------------------------------------------------------------ 端点行为（正向）

def test_full_flow_reaches_baseline(job_api) -> None:
    """session → 三文件 upload → validate → execute，全程走通到 BASELINE + 落库。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    _upload_three(client, headers=headers, session_no=session_no)

    resp = _validate(client, headers=headers, session_no=session_no)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VALIDATED"
    assert body["receipt"]["success_files"] == 3
    assert body["receipt"]["passed"] is True

    # result/{session_no} 读同一份回执。
    result = client.get(f"/api/import/result/{session_no}", headers=headers)
    assert result.status_code == 200
    assert result.json()["receipt"]["success_files"] == 3

    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "BASELINE"
    assert body["snapshot_version_no"] == 1

    with job_api.factory() as db:
        assert [o.job_type for o in db.scalars(select(JobOrder).order_by(JobOrder.id))] == [
            JobType.INBOUND,
            JobType.OUTBOUND,
        ]
        snapshots = tuple(db.scalars(select(Snapshot)))
        assert len(snapshots) == 1
        assert len(tuple(db.scalars(select(InventoryItem)))) == 3
        assert len(tuple(db.scalars(select(AisleCap)))) == 2


def test_snapshot_current_returns_latest(job_api) -> None:
    """建基准后 `GET /api/snapshot/current` 返回该快照（时点 + 版本）。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    _upload_three(client, headers=headers, session_no=session_no)
    _validate(client, headers=headers, session_no=session_no)
    client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)

    resp = client.get("/api/snapshot/current", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_no"] == 1
    assert body["snapshot_time"] == "2026-09-08T00:00:00"
    assert body["snapshot_version"] == "2026-09-08T00:00"


def test_snapshot_current_missing_404(job_api) -> None:
    """无快照时 `GET /api/snapshot/current` → 404。"""
    resp = job_api.client.get("/api/snapshot/current", headers=job_api.headers)
    assert resp.status_code == 404


def test_cap_query_returns_aisle_caps(job_api) -> None:
    """建基准后 `GET /api/cap` 返回巷道 cap；`?aisle=` 过滤。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    _upload_three(client, headers=headers, session_no=session_no)
    _validate(client, headers=headers, session_no=session_no)
    client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)

    resp = client.get("/api/cap", headers=headers)
    assert resp.status_code == 200
    assert [c["aisle_no"] for c in resp.json()] == ["01", "02"]

    resp = client.get("/api/cap", params={"aisle": "01"}, headers=headers)
    assert resp.status_code == 200
    assert [c["aisle_no"] for c in resp.json()] == ["01"]


def test_cap_recompute_resets_drifted_cap(job_api) -> None:
    """`POST /api/cap/recompute` 以快照库位重算，把被改坏的 cap 校回。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    _upload_three(client, headers=headers, session_no=session_no)
    _validate(client, headers=headers, session_no=session_no)
    client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)

    # 把 01 巷的 cap_total 改坏，模拟漂移。
    with job_api.factory() as db:
        cap = db.scalar(select(AisleCap).where(AisleCap.aisle_no == "01"))
        cap.cap_total = 999
        db.commit()

    resp = client.post("/api/cap/recompute", headers=headers)
    assert resp.status_code == 200

    with job_api.factory() as db:
        cap = db.scalar(select(AisleCap).where(AisleCap.aisle_no == "01"))
        assert cap.cap_total == 0  # 占位口径：physical == occupied → total = 0
        assert cap.cap_physical == 2


# ------------------------------------------------------------------ 认证与错误处理（负向）

def test_import_routes_require_auth(job_api) -> None:
    """无凭据 → 401（中间件全局覆盖，本文件只钉一条代表性路径）。"""
    for method, url, payload in [
        ("post", "/api/import/session", {"warehouse_id": WAREHOUSE, "data_time": "2026-09-08T00:00:00"}),
        ("post", "/api/import/upload", {"session_no": "IMP-X", "file_type": "PO", "filename": "x.csv", "content_base64": "aGk="}),
        ("post", "/api/import/validate", {"session_no": "IMP-X"}),
        ("get", "/api/import/result/IMP-X", None),
        ("post", "/api/import/execute", {"session_no": "IMP-X"}),
        ("post", "/api/import/retry", {"session_no": "IMP-X"}),
        ("get", "/api/snapshot/current", None),
        ("get", "/api/cap", None),
        ("post", "/api/cap/recompute", None),
    ]:
        resp = job_api.client.request(method, url, json=payload)
        assert resp.status_code == 401, f"{method.upper()} {url}"


def test_upload_unknown_session_404(job_api) -> None:
    resp = job_api.client.post(
        "/api/import/upload",
        json={"session_no": "IMP-NOPE", "file_type": "PO", "filename": "PO.csv", "content_base64": _b64(PO_CSV)},
        headers=job_api.headers,
    )
    assert resp.status_code == 404


def test_validate_unknown_session_404(job_api) -> None:
    resp = _validate(job_api.client, headers=job_api.headers, session_no="IMP-NOPE")
    assert resp.status_code == 404


def test_execute_on_draft_conflicts_409(job_api) -> None:
    """未校验（DRAFT）就执行导入 → 409（状态机非法迁移）。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 409


def test_upload_bad_base64_422(job_api) -> None:
    """content_base64 不是合法 base64 → 422（报文错误，非 500）。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    resp = client.post(
        "/api/import/upload",
        json={"session_no": session_no, "file_type": "PO", "filename": "PO.csv", "content_base64": "!!!not-base64!!!"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_future_data_time_blocks_validate_then_retry(job_api) -> None:
    """未来时点 → validate 落 FAILED；retry 回到 DRAFT 可重校验。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers, data_time="2099-01-01T00:00:00")
    _upload_three(client, headers=headers, session_no=session_no)

    resp = _validate(client, headers=headers, session_no=session_no)
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"

    resp = client.post("/api/import/retry", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DRAFT"


def test_partial_failure_isolates_inv(job_api) -> None:
    """INV 失败、PO/DO 通过 → VALIDATED（部分失败隔离），执行只载 PO/DO、不建基线。"""
    client, headers = job_api.client, job_api.headers
    session_no = _create_session(client, headers=headers)
    _upload(client, headers=headers, session_no=session_no, file_type="PO", filename="PO.csv", text=PO_CSV)
    _upload(client, headers=headers, session_no=session_no, file_type="DO", filename="DO.csv", text=DO_CSV)
    _upload(client, headers=headers, session_no=session_no, file_type="INV", filename="INV.csv", text=BAD_INV)

    resp = _validate(client, headers=headers, session_no=session_no)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VALIDATED"
    assert body["receipt"]["success_files"] == 2
    assert body["receipt"]["failed_files"] == 1

    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "IMPORTED"  # 无 INV → 不越到 BASELINE

    with job_api.factory() as db:
        assert len(tuple(db.scalars(select(JobOrder)))) == 2
        assert tuple(db.scalars(select(Snapshot))) == ()
