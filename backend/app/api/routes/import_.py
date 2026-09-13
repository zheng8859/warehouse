"""文件导入 路由（`/api/import/*`）。

事实来源：16-数据衔接与 cap 自维护 附录B（7 个端点：session / upload / validate /
          result/{session_id} / execute / retry / run）
          spec `data-import`「数据导入页数据层」（两步交互：开始校验 / 执行导入）
          openspec/changes/data-import/design.md D7（p2 接 `/api/import/*`）
          tasks.md 6.1

## 端点 = 组装点，不掺业务规则

与 `allocate.py` 同一立场：本文件只把「报文 → 会话 → 服务层编排函数」串起来，**不自己
评一个分、不迁一个状态**。校验（`run_validation`）与分流（`execute_import`）的逻辑在
`app/importer/`，端点只负责：

1. 把 `files_json` 里的 base64 内容重建为 `SourceFile`（`_source_files`）；
2. 调编排函数（`run_validation` / `execute_import` / `return_to_draft`）；
3. **提交**（`deps.get_db` 只回滚不提交，事务边界在这里）。

于是「同一批文件为什么出这个结果」仍可只用编排函数的入参复算，不必重放 HTTP。

## 文件存哪（base64 走 JSON，不是 multipart）

`POST /api/import/upload` 收 `content_base64`，落 `ImportSession.files_json`（一条
`{file_type, filename, size, sha256, content_base64}`）。**内容与元数据同落一处**，
validate / execute 据此重建 `SourceFile`；不另建文件实体（23 实体已定，17 §三），也不
把内容留进程内存（重启即丢）。同类文件重复上传 = 覆盖（修正后重传的场景）。

## 错误码

- 401 凭据（中间件全局覆盖，本文件不自己造）
- 404 会话不存在（`NotFound`）
- 409 状态机非法迁移（上传不在 DRAFT / 校验不在 DRAFT / 执行不在 VALIDATED，均
  `StateConflict`，由 `app/core/import_state.py` 或本文件的 DRAFT 守卫抛出）
- 422 报文错（base64 不合法 → `ValidationBlocked`；请求体形状错 → Pydantic）
- **校验不过不是 HTTP 4xx**：`run_validation` 落 `FAILED` + 回执，端点 200 返回
  `status=FAILED`（spec「回执显示字段命中 n/m」，不把校验结果伪装成传输错误）
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import date, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.enums import FileType, ImportStatus
from app.core.errors import NotFound, StateConflict, ValidationBlocked
from app.importer.execute import execute_import
from app.importer.session import SourceFile, return_to_draft, run_validation
from app.models.linkage import ImportSession
from app.schemas.import_ import CreateSessionRequest, SessionRef, UploadRequest

router = APIRouter(prefix="/api/import", tags=["import"])


def _get_session(db: Session, session_no: str) -> ImportSession:
    """按会话号取会话；不存在 → 404。

    带 `warehouse_id` 过滤（CLAUDE.md §七 数据隔离）：单厂部署下它是 `settings.warehouse_code`，
    与「会话创建时缺省取单厂」同一口径。不带过滤会读进别的仓的同号会话，而那在评分/分流
    层已经按单厂硬编码 —— 两处口径必须一致。
    """
    row = db.scalar(
        select(ImportSession).where(
            ImportSession.warehouse_id == settings.warehouse_code,
            ImportSession.session_no == session_no,
        )
    )
    if row is None:
        raise NotFound(f"导入会话 {session_no} 不存在", detail={"session_no": session_no})
    return row


def _next_seq(db: Session, *, warehouse_id: str, data_time: datetime) -> int:
    """当日会话序号 = 同日期前缀的会话数 + 1（`IMP-YYYYMMDD-NN` 的 NN）。

    单写者下「读 count + 1」不会并发撞号（WAL 单写，CLAUDE.md §四）；`session_no` 的
    唯一约束 `(warehouse_id, session_no)` 是最终防线。
    """
    prefix = f"IMP-{data_time:%Y%m%d}-%"
    count = db.scalar(
        select(func.count())
        .select_from(ImportSession)
        .where(
            ImportSession.warehouse_id == warehouse_id,
            ImportSession.session_no.like(prefix),
        )
    )
    return (count or 0) + 1


def _files_entries(row: ImportSession) -> list[dict]:
    """会话的已上传文件清单（`files_json.files`，无则空表）。"""
    return list((row.files_json or {}).get("files", []))


def _source_files(row: ImportSession) -> list[SourceFile]:
    """把 `files_json` 里的 base64 内容重建为 `SourceFile`（校验/执行都要用它）。"""
    sources: list[SourceFile] = []
    for entry in _files_entries(row):
        sources.append(
            SourceFile(
                file_type=FileType(entry["file_type"]),
                filename=entry["filename"],
                content=base64.b64decode(entry["content_base64"], validate=True),
            )
        )
    return sources


@router.post("/session")
def create_session(payload: CreateSessionRequest, db: Session = Depends(get_db)) -> dict:
    """创建导入会话（DRAFT）：时点 + 单厂仓库，生成会话号与批次号。"""
    seq = _next_seq(db, warehouse_id=payload.warehouse_id, data_time=payload.data_time)
    row = ImportSession(
        warehouse_id=payload.warehouse_id,
        session_no=f"IMP-{payload.data_time:%Y%m%d}-{seq:02d}",
        import_batch_no=f"BAT-{payload.data_time:%Y%m%d}-{seq:02d}",
        data_time=payload.data_time,
    )
    db.add(row)
    db.commit()
    return {
        "session_no": row.session_no,
        "import_batch_no": row.import_batch_no,
        "status": row.status.value,
        "data_time": row.data_time.isoformat(),
    }


@router.post("/upload")
def upload(payload: UploadRequest, db: Session = Depends(get_db)) -> dict:
    """上传单类文件：存 base64 内容 + 校验和到 `files_json`，同类覆盖。"""
    row = _get_session(db, payload.session_no)
    if row.status is not ImportStatus.DRAFT:
        # 文件只在 DRAFT 阶段追加；VALIDATED/FAILED 后要改文件须先 retry 回 DRAFT。
        raise StateConflict(
            f"会话 {row.session_no} 状态为 {row.status.value}，只能在待校验（DRAFT）阶段上传文件",
            detail={"session_no": row.session_no, "current": row.status.value},
        )

    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationBlocked(
            "content_base64 不是合法 base64",
            detail={"filename": payload.filename, "file_type": payload.file_type.value},
        ) from None

    entry = {
        "file_type": payload.file_type.value,
        "filename": payload.filename,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": payload.content_base64,
    }
    files = [f for f in _files_entries(row) if f.get("file_type") != payload.file_type.value]
    files.append(entry)
    row.files_json = {"files": files}
    db.commit()

    return {
        "session_no": row.session_no,
        "file_type": payload.file_type.value,
        "filename": payload.filename,
        "size": len(content),
        "sha256": entry["sha256"],
    }


@router.post("/validate")
def validate(payload: SessionRef, db: Session = Depends(get_db)) -> dict:
    """开始校验：DRAFT → VALIDATING → VALIDATED | FAILED，回执落库。

    校验不过**不是** 4xx —— 200 返回 `status=FAILED` + 回执（spec「数据导入页数据层」）；
    状态非法（如已 VALIDATED 再校验）由 `run_validation` 抛 `StateConflict`（409）。
    """
    row = _get_session(db, payload.session_no)
    run_validation(db, row, _source_files(row), now=date.today())
    db.commit()
    return {"session_no": row.session_no, "status": row.status.value, "receipt": row.receipt_json}


@router.get("/result/{session_no}")
def result(session_no: str, db: Session = Depends(get_db)) -> dict:
    """查校验回执（库里的 `receipt_json`，未校验时为 `null`）。"""
    row = _get_session(db, session_no)
    return {"session_no": row.session_no, "status": row.status.value, "receipt": row.receipt_json}


@router.post("/execute")
def execute(payload: SessionRef, db: Session = Depends(get_db)) -> dict:
    """执行导入并分流：VALIDATED → IMPORTING → IMPORTED → [BASELINE]。

    - 状态非法（如 DRAFT 就执行）→ `execute_import` 抛 `StateConflict`（409）。
    - 写入失败 → 会话落 `FAILED`（200 返回），不产半成品（`execute_import` 的 savepoint）。
    - 基线重算失败（`establish_baseline`）→ 原样上抛（事务整体回滚），提示重导。
    """
    row = _get_session(db, payload.session_no)
    execute_import(db, row, _source_files(row))
    db.commit()
    body = {"session_no": row.session_no, "status": row.status.value, "receipt": row.receipt_json}
    if row.snapshot_version_no is not None:
        body["snapshot_version_no"] = row.snapshot_version_no
    return body


@router.post("/retry")
def retry(payload: SessionRef, db: Session = Depends(get_db)) -> dict:
    """修正后重新校验：FAILED → DRAFT（16 §3.1 唯一的回边）。"""
    row = _get_session(db, payload.session_no)
    return_to_draft(db, row)
    db.commit()
    return {"session_no": row.session_no, "status": row.status.value}
