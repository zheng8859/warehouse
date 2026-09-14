"""L2 数据接入（6 道）：`/api/import/*` 四步流程经 HTTP 端点。

对应 golden `golden_030`（缺必填列四层校验阻断）、`golden_029`（四层校验第 4 层=业务层失败）。
场景可溯源 20号 / 16号 §4.2（字段命中率 100% 才可执行，未命中即阻断）。

本层钉住「校验不过不是 HTTP 4xx」—— 200 返回 `status=FAILED` + 回执（spec「数据导入页
数据层」），回执可展开到 `文件 + 列 + 行 + 原因`。
"""
from __future__ import annotations

import base64

import pytest

from app.core.enums import JobStatus, JobType

pytestmark = pytest.mark.l2

WAREHOUSE = "GTJ10036"

_PODO_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"
PO_CSV = f"{_PODO_HEADER}\nPO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
# 缺「料号」列 —— 必填列未 100% 命中（golden_030）。
PO_CSV_NO_MATERIAL = (
    "单据号码,行号,类型,仓库号,品名,生产日期,数量\n"
    "PO-01,10,生产订单,GTJ10036,PET500 茉莉柚茶,2026-09-05,500\n"
)
# 数量 = 0 —— 第 4 层（业务层）校验失败（golden_029）。
PO_CSV_ZERO_QTY = f"{_PODO_HEADER}\nPO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,0\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _create_session(client, headers) -> str:
    resp = client.post(
        "/api/import/session",
        json={"warehouse_id": WAREHOUSE, "data_time": "2026-09-08T00:00:00"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_no"]


def _upload(client, headers, session_no: str, filename: str, text: str, file_type: str = "PO"):
    return client.post(
        "/api/import/upload",
        json={
            "session_no": session_no,
            "file_type": file_type,
            "filename": filename,
            "content_base64": _b64(text),
        },
        headers=headers,
    )


def _validate(client, headers, session_no: str):
    return client.post("/api/import/validate", json={"session_no": session_no}, headers=headers)


def test_golden_030_missing_required_column_blocks_validation(eval_api):
    """golden_030：缺必填列（料号）→ 字段命中 <100% → 校验落 FAILED，回执可展开到列。"""
    client, headers = eval_api.client, eval_api.headers()
    session_no = _create_session(client, headers)
    _upload(client, headers, session_no, "PO.csv", PO_CSV_NO_MATERIAL)

    resp = _validate(client, headers, session_no)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    receipt = body["receipt"]
    assert receipt["passed"] is False
    assert receipt["failed_files"] == 1
    assert any(
        "缺失必填列" in issue["reason"] and issue["column"] == "material_code"
        for issue in receipt["issues"]
    )


def test_import_session_upload_validate_happy_path(eval_api):
    """session → upload → validate 走通 → VALIDATED，回执 passed。"""
    client, headers = eval_api.client, eval_api.headers()
    session_no = _create_session(client, headers)
    _upload(client, headers, session_no, "PO.csv", PO_CSV)

    resp = _validate(client, headers, session_no)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VALIDATED"
    assert body["receipt"]["passed"] is True
    assert body["receipt"]["success_files"] == 1


def test_golden_029_business_layer_rejects_nonpositive_qty(eval_api):
    """golden_029：第 4 层（业务层）校验 —— 数量必须 > 0，否则落 FAILED。"""
    client, headers = eval_api.client, eval_api.headers()
    session_no = _create_session(client, headers)
    _upload(client, headers, session_no, "PO.csv", PO_CSV_ZERO_QTY)

    resp = _validate(client, headers, session_no)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert any(
        "数量必须大于 0" in issue["reason"] and issue["column"] == "qty"
        for issue in body["receipt"]["issues"]
    )


def test_execute_po_creates_pending_inbound_order(eval_api):
    """校验通过后 execute → IMPORTED，PO 分流为一张 `PENDING` 入库 JobOrder。"""
    client, headers = eval_api.client, eval_api.headers()
    session_no = _create_session(client, headers)
    _upload(client, headers, session_no, "PO.csv", PO_CSV)
    _validate(client, headers, session_no)

    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "IMPORTED"

    orders = eval_api.orders()
    assert len(orders) == 1
    assert orders[0].job_type is JobType.INBOUND
    assert orders[0].status is JobStatus.PENDING


def test_upload_after_validate_conflicts_409(eval_api):
    """已 VALIDATED 的会话再上传 → 409（文件只在 DRAFT 阶段追加）。"""
    client, headers = eval_api.client, eval_api.headers()
    session_no = _create_session(client, headers)
    _upload(client, headers, session_no, "PO.csv", PO_CSV)
    _validate(client, headers, session_no)

    resp = _upload(client, headers, session_no, "PO.csv", PO_CSV)

    assert resp.status_code == 409
    assert resp.json()["error"] == "state_conflict"


def test_upload_unknown_session_404(eval_api):
    """不存在的会话号 → 404。"""
    resp = _upload(eval_api.client, eval_api.headers(), "IMP-NOPE", "PO.csv", PO_CSV)

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
