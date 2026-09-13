"""文件导入端点的请求 DTO（`app/api/routes/import_.py` 的入参）。

事实来源：16-数据衔接与 cap 自维护 附录B（7 个 import 端点）
          spec `data-import`「数据导入页数据层」（两步交互）
          openspec/changes/data-import/design.md D7

文件以 **base64 走 JSON** 上传（非 multipart）：零构建前端的 `FileReader` 直接给
`content_base64`，服务端 `files_json` 存 `{file_type, filename, size, sha256,
content_base64}` —— 内容与元数据同落一处，validate / execute 据此重建 `SourceFile`。

`data_time` 必填（Pydantic 层拒绝缺失），**不做「兜底当天」**：spec「数据时点标注」
明令「时点缺失时不得静默兜底为当天」，未来时点由校验层（`validate_time`）阻断。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.enums import FileType


class CreateSessionRequest(BaseModel):
    """`POST /api/import/session`：创建导入会话（时点 + 单厂仓库）。

    `warehouse_id` 缺省取单厂（`settings.warehouse_code`）—— 首期单厂试点，前端无需
    关心仓库维度；字段留着是为了与 `17` §十一「全部实体携带 warehouse_id」的口径对齐，
    而非开放多厂选择。
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(default=settings.warehouse_code, min_length=1, max_length=32)
    data_time: datetime


class UploadRequest(BaseModel):
    """`POST /api/import/upload`：上传单类文件（PO / DO / INV），base64 走 JSON。

    同类文件重复上传 = **覆盖**（修正文件后重新上传的场景）；三类各至多一份（16 附录A）。
    """

    model_config = ConfigDict(extra="forbid")

    session_no: str = Field(min_length=1, max_length=32)
    file_type: FileType
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class SessionRef(BaseModel):
    """validate / execute / retry 共用的「按会话号定位」入参。

    会话号是前端在 `POST /api/import/session` 拿到的唯一标识，后续动作都只带它；
    单厂部署下会话号即够定位（`17` §3.1 的 `session_no` 形如 `IMP-20260908-01`）。
    """

    model_config = ConfigDict(extra="forbid")

    session_no: str = Field(min_length=1, max_length=32)
