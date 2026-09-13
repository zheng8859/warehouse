"""格式与编码探测（**纯函数，无 IO**）。

事实来源：16-数据衔接与 cap 自维护 §4.1（格式与编码识别）与 §10.6（缺失策略）
          spec `data-import`「文件解析与字段映射」（Scenario：格式或编码不可识别阻断）
          openspec/changes/data-import/design.md D2（8 步管线模块分解）

输入一个文件（文件名 + 内容字节），判定它的格式（xlsx / csv）与文本编码
（UTF-8 / UTF-8-BOM / GBK）。**无法识别即阻断**（抛 `ValidationBlocked`），
**不猜测编码** —— 猜错编码会静默把 GBK 中文读成乱码，而乱码进了快照就再也洗不干净。

## 探测顺序（为什么是这个顺序）

1. **格式先看扩展名、再看内容**：`.xlsx` 必须是 zip（`PK` 魔数），否则「已损坏」；
   `.csv` 必须是可解码文本，否则走编码阻断。其它扩展名直接「格式不支持」。
2. **编码按「BOM → UTF-8 → GBK」依次试**：BOM 是唯一**确定**的信号，先认它；
   UTF-8 在 ASCII 上是 GBK 的超集，所以先试 UTF-8（否则纯 ASCII 文件会被误判成 GBK）；
   GBK 兜底 —— 三样都解不开才算「编码无法探测」。

不引入 chardet（技术栈既定：pandas + openpyxl + 标准库 csv）。启发式猜测编码违反
「同样输入必得同样输出」的确定性红线，这里只用**确定可判**的试解码。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.errors import ValidationBlocked

__all__ = [
    "Detected",
    "FileFormat",
    "TextEncoding",
    "detect",
    "detect_encoding",
]

#: xlsx 是 zip 容器，魔数统一为 `PK`（本地文件头 `PK\x03\x04` 或空档 `PK\x05\x06`）。
#: 只认前两字节的 `PK`：所有 zip 变体都以此为头，认死 `PK\x03\x04` 会漏掉空档 xlsx。
_XLSX_MAGIC = b"PK"

_MSG_BAD_FORMAT = "文件格式不支持或已损坏"
_MSG_BAD_ENCODING = "请以 UTF-8 或 GBK 重新保存"


class FileFormat(str, Enum):
    """文件格式。**实体局部值域**：不进 `app/core/enums.py`（那是跨模块共享的 11 个）。

    与 `FileType`（PO/DO/INV，业务文件类别）不同 —— 这是**物理**格式，先于业务分类
    存在：同一份 `.xlsx` 既可能是 PO 也可能是 INV。
    """

    XLSX = "xlsx"
    CSV = "csv"


class TextEncoding(str, Enum):
    """文本编码。仅对 csv 有意义 —— xlsx 是 zip，内部 XML 恒为 UTF-8，无此概念。"""

    UTF_8 = "UTF-8"
    UTF_8_BOM = "UTF-8-BOM"
    GBK = "GBK"


@dataclass(frozen=True)
class Detected:
    """探测结果：格式 + 文本编码。

    `encoding` 为 `None` 当且仅当 `format` 是 `XLSX`（zip 无文本编码）。
    """

    format: FileFormat
    encoding: TextEncoding | None


def detect(filename: str, content: bytes) -> Detected:
    """判定文件格式与编码，无法识别即抛 `ValidationBlocked`。

    `content` 是文件内容字节（上传接口读进来的原始 bytes，不是路径）。函数不做 IO：
    不打开文件、不查磁盘，全部判断作用于已给到的字节上 —— 这是它能在 `tests/logic/`
    里不连库、不建临时文件的原因。
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix == "xlsx":
        if not content.startswith(_XLSX_MAGIC):
            raise ValidationBlocked(_MSG_BAD_FORMAT, detail={"filename": filename})
        return Detected(format=FileFormat.XLSX, encoding=None)

    if suffix == "csv":
        return Detected(format=FileFormat.CSV, encoding=detect_encoding(content))

    raise ValidationBlocked(_MSG_BAD_FORMAT, detail={"filename": filename})


def detect_encoding(content: bytes) -> TextEncoding:
    """文本编码探测：BOM → UTF-8 → GBK 依次试，三样都失败即阻断。

    对空内容返回 `UTF-8`（空文件在编码上无可争议，「非空」由结构层校验把关，不在
    本函数职责内）。**不做启发式**：宁可阻断，也不把 GBK 中文按 UTF-8 读出乱码。
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return TextEncoding.UTF_8_BOM

    try:
        content.decode("utf-8")
        return TextEncoding.UTF_8
    except UnicodeDecodeError:
        pass

    try:
        content.decode("gbk")
        return TextEncoding.GBK
    except UnicodeDecodeError:
        pass

    raise ValidationBlocked(_MSG_BAD_ENCODING)
