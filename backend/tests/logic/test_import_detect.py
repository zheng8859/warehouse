"""格式与编码探测的契约测试（tasks.md 2.1 的验证）。

事实来源：16 §4.1（格式与编码识别）
          spec `data-import`「文件解析与字段映射」（Scenario：格式或编码不可识别阻断）
          openspec/changes/data-import/design.md D2

探测是**纯函数**：本文件不建库、不建临时文件，全部用例只把 `bytes` 喂给
`app/importer/detect.py`。三类编码（UTF-8 / UTF-8-BOM / GBK）与非法格式各有一条
正向或阻断断言 —— 将来有人把「BOM → UTF-8 → GBK」的顺序改反，或把「无法识别即阻断」
放宽成「默认按 UTF-8」，红的是这里的用例，而不是生产上某张被读成乱码的快照。
"""
from __future__ import annotations

import pytest

from app.core.errors import DomainError, ValidationBlocked
from app.importer.detect import (
    Detected,
    FileFormat,
    TextEncoding,
    detect,
    detect_encoding,
)

pytestmark = pytest.mark.logic

#: 一个真实 zip 文件头（xlsx 是 zip 容器）。内容不必是合法 xlsx —— 探测只看魔数。
XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 32


# ------------------------------------------------------------------ 格式识别

def test_xlsx_by_extension_and_magic() -> None:
    assert detect("data.xlsx", XLSX_BYTES) == Detected(FileFormat.XLSX, None)


def test_xlsx_magic_is_case_insensitive_extension() -> None:
    """扩展名 `.XLSX` 也要认（上传来的文件名大小写不可控）。"""
    assert detect("data.XLSX", XLSX_BYTES).format is FileFormat.XLSX


def test_xlsx_without_zip_magic_is_corrupted() -> None:
    """扩展名说是 xlsx、内容却不是 zip → 「已损坏」阻断。"""
    with pytest.raises(ValidationBlocked) as excinfo:
        detect("data.xlsx", b"not a zip at all")
    assert "已损坏" in str(excinfo.value)


# ------------------------------------------------------------------ 编码识别

def test_utf8() -> None:
    assert detect("data.csv", "中文".encode("utf-8")) == Detected(
        FileFormat.CSV, TextEncoding.UTF_8
    )


def test_utf8_bom() -> None:
    """BOM 是唯一确定信号，必须先于 UTF-8 试 —— 否则 BOM 会被当正文首字符读进去。"""
    content = b"\xef\xbb\xbf" + "中文".encode("utf-8")
    assert detect_encoding(content) is TextEncoding.UTF_8_BOM


def test_gbk() -> None:
    """GBK 中文在 UTF-8 下解不开（非法字节序列），必须落到 GBK 分支。"""
    assert detect("data.csv", "中文".encode("gbk")) == Detected(
        FileFormat.CSV, TextEncoding.GBK
    )


def test_ascii_prefers_utf8_not_gbk() -> None:
    """纯 ASCII 在 UTF-8 与 GBK 下都能解 —— 必须先试 UTF-8，否则全 ASCII 文件被误判 GBK。"""
    assert detect_encoding(b"abc,123") is TextEncoding.UTF_8


def test_undetectable_encoding_is_blocked() -> None:
    """`\\xff` 在 UTF-8 与 GBK 下都非法 → 「请以 UTF-8 或 GBK 重新保存」。"""
    with pytest.raises(ValidationBlocked) as excinfo:
        detect_encoding(b"\xff\xff\xfe")
    assert "UTF-8 或 GBK" in str(excinfo.value)


# ------------------------------------------------------------------ 非法格式与错误形状

def test_unknown_extension_is_blocked() -> None:
    """spec Scenario「非 xlsx / csv」→ 格式不支持，不猜测。"""
    with pytest.raises(ValidationBlocked) as excinfo:
        detect("data.txt", b"whatever")
    assert "格式不支持" in str(excinfo.value)


def test_no_extension_is_blocked() -> None:
    with pytest.raises(ValidationBlocked):
        detect("data", b"whatever")


def test_blocking_is_a_domain_error_422() -> None:
    """阻断必须走 `DomainError` 体系，且是 422（校验失败）—— 不得被上层吞掉继续执行。"""
    with pytest.raises(ValidationBlocked) as excinfo:
        detect("data.bin", b"x")
    assert isinstance(excinfo.value, DomainError)
    assert excinfo.value.http_status == 422
    assert excinfo.value.code == "validation_blocked"


def test_empty_csv_is_utf8() -> None:
    """空文件编码上无可争议（UTF-8），「非空」由结构层校验把关，不在这里阻断。"""
    assert detect_encoding(b"") is TextEncoding.UTF_8
