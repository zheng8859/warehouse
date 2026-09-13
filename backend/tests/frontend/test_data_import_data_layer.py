"""数据导入页 p2 数据层静态断言（tasks.md 6.2）。

无浏览器、无业务级 /qa —— 静态核对数据层的接线契约：
1. `assets/app.js` 有 fetch/API 封装（Bearer 认证 + JSON 编解码 + 非 2xx 抛错）与
   文件→base64 读取（`FileReader.readAsDataURL`）；
2. `data-import.html` 接线：时点卡（date 输入）、三文件上传位（PO/DO/INV）、校验结果表、
   「开始校验 / 执行导入」两步、`setState` 四态渲染；
3. 不接线成品清单/ABC（无 BI/ABC 上传位、无结果卡）。

事实来源：spec `data-import`「数据导入页数据层」「不进数据导入页」；design.md D7；
          tasks.md 6.2。断言值硬编码，与实现契约一致（改契约须同步改测试）。
"""
from pathlib import Path

# backend/tests/frontend/test_data_import_data_layer.py
#   -> parents[0] = backend/tests/frontend
#   -> parents[1] = backend/tests
#   -> parents[2] = backend
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
APP_JS = (FRONTEND / "assets" / "app.js").read_text(encoding="utf-8")
DATA_IMPORT = (FRONTEND / "data-import.html").read_text(encoding="utf-8")


# ---------- 1. fetch/API 封装（app.js） ----------

def test_api_fetch_wrapper_present():
    """app.js 有 fetch/API 封装并挂到 window（tasks 6.2 数据层）。"""
    assert "async function api(" in APP_JS
    assert "window.api = api" in APP_JS
    assert "fetch(" in APP_JS


def test_api_sends_bearer_token():
    """API 封装带 Authorization: Bearer（13 §六 认证）。"""
    assert "'Authorization'" in APP_JS
    assert "'Bearer '" in APP_JS


def test_file_reader_base64_present():
    """文件→base64 读取钩子存在（零构建上传走 JSON，design.md D7）。"""
    assert "readFileAsBase64" in APP_JS
    assert "readAsDataURL" in APP_JS


# ---------- 2. 时点卡与三文件上传位（data-import.html） ----------

def test_time_card_date_input():
    """时点卡 = date 输入（spec「数据时点标注与导入即基准」，必填）。"""
    assert 'id="importDataTime"' in DATA_IMPORT
    assert 'type="date"' in DATA_IMPORT


def test_three_file_slots_po_do_inv():
    """PO / DO / INV 三个上传位齐备（spec「文件解析与字段映射」）。"""
    for ft in ("PO", "DO", "INV"):
        assert f'data-file-type="{ft}"' in DATA_IMPORT, f"缺 {ft} 上传位"


def test_no_bi_or_abc_upload_slot():
    """无成品清单/ABC 上传位（spec「不进数据导入页」Scenario）。"""
    assert 'data-file-type="BI"' not in DATA_IMPORT
    assert 'data-file-type="ABC"' not in DATA_IMPORT


# ---------- 3. 两步交互与校验结果表（data-import.html） ----------

def test_two_step_buttons():
    """「开始校验 / 执行导入」两步交互按钮存在（spec「数据导入页数据层」）。"""
    assert 'id="importValidateBtn"' in DATA_IMPORT
    assert 'id="importExecuteBtn"' in DATA_IMPORT
    assert "开始校验" in DATA_IMPORT
    assert "执行导入" in DATA_IMPORT


def test_receipt_table_and_state_container():
    """校验结果表 + 四态容器 + 回执渲染钩子存在。"""
    assert 'id="importReceipt"' in DATA_IMPORT
    assert 'id="importReceiptTable"' in DATA_IMPORT
    assert "renderImportReceipt" in DATA_IMPORT


# ---------- 4. 数据层接线（页面脚本调 window.api / setState） ----------

def test_page_wires_api_and_set_state():
    """页面脚本通过 window.api 打接口、用 setState 渲染四态。"""
    assert "window.api(" in DATA_IMPORT
    assert "setState(" in DATA_IMPORT
