"""冷路径 AI 前端接线冒烟（29 号 Phase B · 6 项交付 + warehouse_id + 开关置灰）。

无浏览器 —— 静态断言核对：
1. 登录存 warehouse_id、aiBody 缺失阻断、renderDualProduct 不重复前置 AI 标注；
2. .ai-card / .confirm-card 复用既有 :root 令牌、无硬编码新色值；
3. AI 开关 data-toggle="ai" + 非 admin 置灰；
4. 6 处入口（对话台 L2 / KPI 解读 / 偏离归因 / 权重影子卡 / 移库多方案 / 开关）接线到对应端点。

事实来源：29 号 Phase B；断言值硬编码，与 openspec change ai-assist-frontend 的 spec 一致。
"""

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
STYLES = (FRONTEND / "assets" / "styles.css").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "assets" / "app.js").read_text(encoding="utf-8")
CHAT_HTML = (FRONTEND / "chat.html").read_text(encoding="utf-8")
KPI_HTML = (FRONTEND / "kpi-dashboard.html").read_text(encoding="utf-8")
CONFIG_HTML = (FRONTEND / "config.html").read_text(encoding="utf-8")
TRANSFER_HTML = (FRONTEND / "transfer.html").read_text(encoding="utf-8")

# 服务端 field_serializer 注入的完整标注（spec「双产物建议卡渲染」），前端不得再拼一遍。
AI_NOTICE = "AI 建议，仅供参考，需人工核实，不自动执行"


# ---------- 1. 会话上下文 + 共享渲染助手 ----------

def test_login_stores_warehouse_id():
    assert "sessionStorage.setItem('warehouse_id'," in APP_JS


def test_ai_body_guards_missing_warehouse_id():
    assert "function aiBody" in APP_JS
    assert "window.aiBody = aiBody" in APP_JS
    assert "请重新登录" in APP_JS


def test_render_dual_product_does_not_reinject_ai_notice():
    # 服务端已注入 AI_NOTICE，前端渲染助手不得再拼一遍 —— 前端不持有该常量。
    assert "function renderDualProduct" in APP_JS
    assert "AI_NOTICE" not in APP_JS
    assert AI_NOTICE not in APP_JS


# ---------- 2. 样式 ----------

def test_ai_card_and_confirm_card_reuse_tokens():
    assert ".ai-card{border:1px solid var(--amber);background:var(--amber-soft);" in STYLES
    assert ".confirm-card{border:2px solid var(--amber);background:var(--amber-soft);" in STYLES
    assert ".ai-rule{" in STYLES


# ---------- 3. AI 开关置灰 ----------

def test_ai_switch_scoped_and_admin_gated():
    assert 'id="aiSwitch"' in CONFIG_HTML
    assert 'data-toggle="ai"' in CONFIG_HTML
    assert "dataset.toggle === 'ai'" in APP_JS
    assert "getRole() !== 'admin'" in APP_JS


# ---------- 4. 对话台 L2 chip + 写意图确认卡 ----------

def test_chat_l2_chip_calls_conversation_with_question_only():
    assert "'/conversation/message'" in CHAT_HTML
    assert "aiBody({ question })" in CHAT_HTML
    assert "write_intent" in CHAT_HTML


def test_chat_write_intent_renders_confirm_card():
    assert "confirm-card" in CHAT_HTML
    assert "'/llm/weight/apply'" in CHAT_HTML


# ---------- 5. KPI 解读 + 偏离归因 ----------

def test_kpi_interpret_entry():
    assert "kpiInterpretBtn" in KPI_HTML
    assert "'/llm/kpi/interpret'" in KPI_HTML
    assert "period: currentPeriod()" in KPI_HTML


def test_deviation_attribution_entry():
    # 偏离批次表改为动态渲染（P6 查询修复）：归因入口不再用静态 `data-attr`，
    # 而是行内 `data-act="attr"` 委托到 `POST /llm/deviation/attribute`。
    assert 'data-act="attr"' in KPI_HTML
    assert "'/llm/deviation/attribute'" in KPI_HTML
    assert "可能原因" in KPI_HTML


# ---------- 6. 权重影子卡 + 移库多方案 ----------

def test_weight_tune_entry_shadow_mode():
    assert "weightTuneBtn" in CONFIG_HTML
    assert "'/llm/weight/tune'" in CONFIG_HTML
    assert "'/llm/weight/apply'" in CONFIG_HTML
    assert "insufficient_samples" in CONFIG_HTML


def test_relocate_propose_entry_readonly():
    assert "relocateProposeBtn" in TRANSFER_HTML
    assert "'/llm/relocate/propose'" in TRANSFER_HTML
    assert "批号一致" in TRANSFER_HTML


# ---------- 7. 对话台自由输入框 + 说明性文字清理 ----------

def test_chat_has_free_input_box():
    assert 'id="chatInput"' in CHAT_HTML
    assert 'id="chatSendBtn"' in CHAT_HTML
    assert "'keydown'" in CHAT_HTML
    assert "'Enter'" in CHAT_HTML


def test_chat_explanatory_note_removed():
    assert "规则/模板匹配层" not in CHAT_HTML
    assert "L3 自主规划" not in CHAT_HTML
    assert "对话台层级" not in CHAT_HTML
    assert "中部对话区" not in CHAT_HTML
