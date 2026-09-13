"""端到端集成冒烟（tasks.md 7.2）。

一条链跑到底：创建会话 → 上传三类文件（PO/DO/INV）→ 校验 → 执行导入 → 落库断言
（快照 / 队列 / 任务 / cap 基线）→ 重复导入幂等（已 BASELINE 会话再提交不重复建基线）。

事实来源：spec `data-import`「执行导入与建立基准」「cap 基线全量重算」「重复导入判定与幂等」
          design.md D4 / D5 / D1
          tasks.md 7.2

与 `test_import_routes.py` 的分工：那里按端点逐条钉行为与错误码；这里只跑一条全链路，
钉「串起来之后各落点齐全 + 再导一遍不产生第二条基线」。
"""
from __future__ import annotations

import base64

from sqlalchemy import select

from app.core.enums import JobType
from app.models.job import JobOrder
from app.models.linkage import AisleCap, InventoryItem, Snapshot

WAREHOUSE = "GTJ10036"

_INV_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"
_PODO_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"

GOOD_INV = (
    f"{_INV_HEADER}\n"
    "GTJ10036,010104,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,010205,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,020101,3005678,PET500 茉莉柚茶,B002,合格,40,2026-09-08 00:00:00\n"
)
PO_CSV = f"{_PODO_HEADER}\nPO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
DO_CSV = f"{_PODO_HEADER}\nDO-01,20,发货单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,120\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _run_full_import(client, headers, *, data_time: str = "2026-09-08T00:00:00") -> str:
    """session → 三文件 upload → validate，返回 session_no（不 execute）。"""
    session_no = client.post(
        "/api/import/session",
        json={"warehouse_id": WAREHOUSE, "data_time": data_time},
        headers=headers,
    ).json()["session_no"]
    for ft, fn, text in (("PO", "PO.csv", PO_CSV), ("DO", "DO.csv", DO_CSV), ("INV", "INV.csv", GOOD_INV)):
        resp = client.post(
            "/api/import/upload",
            json={"session_no": session_no, "file_type": ft, "filename": fn, "content_base64": _b64(text)},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    assert client.post("/api/import/validate", json={"session_no": session_no}, headers=headers).status_code == 200
    return session_no


def test_full_import_lands_artifacts(job_api) -> None:
    """三类文件走通全链：快照 / 入库队列 / 出库任务 / cap 基线 各落其位。"""
    client, headers = job_api.client, job_api.headers
    session_no = _run_full_import(client, headers)

    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "BASELINE"
    assert resp.json()["snapshot_version_no"] == 1

    with job_api.factory() as db:
        assert [o.job_type for o in db.scalars(select(JobOrder).order_by(JobOrder.id))] == [
            JobType.INBOUND,
            JobType.OUTBOUND,
        ]
        assert len(tuple(db.scalars(select(Snapshot)))) == 1
        assert len(tuple(db.scalars(select(InventoryItem)))) == 3
        assert [c.aisle_no for c in db.scalars(select(AisleCap).order_by(AisleCap.aisle_no))] == ["01", "02"]


def test_reimport_is_idempotent(job_api) -> None:
    """已 BASELINE 会话重复执行导入 → 幂等返回既有基线，不产生第二条基线。"""
    client, headers = job_api.client, job_api.headers
    session_no = _run_full_import(client, headers)
    client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)

    resp = client.post("/api/import/execute", json={"session_no": session_no}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "BASELINE"
    assert body["snapshot_version_no"] == 1

    with job_api.factory() as db:
        assert len(tuple(db.scalars(select(Snapshot)))) == 1  # 不重复建基线
