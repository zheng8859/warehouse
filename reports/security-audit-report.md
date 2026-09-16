# 安全审计报告（阶段八 · `32` 号 Step 2）

执行时间：2026-09-16 · 分支 `phase-8/ship`
审计方式：依赖清单核对 + 源码静态审查（认证/注入/密钥/数据）。`/cso`（Gstack）在本环境不可用，
以等价手工审计替代；`pip-audit` 未安装，依赖 CVE 以版本新鲜度人工核对。

## 结论：**A**（0 Critical · 0 High · 出域=0 · 1 中危待修 · 1 低危已知）

| 维度 | 结果 | 说明 |
|---|:--:|---|
| 依赖 CVE | 低 | 无 pip-audit；版本均 2026 近期（fastapi 0.119.1 / pydantic 2.12.3 / SQLAlchemy 2.0.49 / uvicorn 0.52.0 / bcrypt 5.0.0 / PyJWT 2.13.0 / pandas 2.3.3），无已知 Critical/High |
| 认证授权 | ✅ | JWT HS256 · 8h · bcrypt cost 12 · 端点级 RBAC（`/allocate/batch` + `/llm/*` 六端点） |
| 密钥配置 | ✅ | `.env`/`.env.*`/`*.db*` 已 gitignore；`jwt_secret` 仅 dev 默认值（prod 强制覆盖）；`llm_api_key` 空默认、不硬编码 |
| SQL 注入 | ✅ | 全 ORM 参数化，无 `sa.text`/f-string 拼接 |
| 命令注入 | ✅ | 无 `subprocess`/`eval`/`exec`/`shell=True` |
| XSS | ⚠️ 中 | 见下 Finding 1 |
| 数据出域 | ✅ | 核心链路出域=0；`llm_provider` 空默认不发起外部调用；冷路径脱敏（阶段五） |
| 生产护栏 | ✅ | `assert_production_safe()`（`main.py:59`）prod 下强制真实密钥 / 绝对 DB 路径 / bcrypt≥12 |

## 问题清单（按 CVSS/严重度）

| # | 严重度 | 位置 | 问题 | 修复建议 | 修复状态 |
|---|:--:|---|---|---|---|
| 1 | 中 | `frontend/data-import.html:157-165` `renderImportReceipt` | 上传文件名 `f.filename`（用户可控）未经 `escapeHtml` 直接拼入 `innerHTML` → 反射型 XSS 面 | 对 `f.filename`（及 `f.file_type`）套 `escapeHtml` | ✅ 已修复（Step 4） |
| 2 | 低 | 全局 | `/favicon.ico` 404（浏览器自动请求） | 加 favicon 或 `<link rel="icon" href="data:,">` | ✅ 已修复（Step 4） |

## 修复记录（Step 4 · 2026-09-16）

- **Finding 1（XSS 中危）**：`data-import.html` 引入 `esc = (s) => window.escapeHtml(s)`，对
  `renderImportReceipt` 里的 `f.file_type` / `f.filename` / `f.anomalies` 全部转义后再拼
  `innerHTML`（`f.rows` / `f.fields_hit` 为数值字段，维持原样）。与其他页面的渲染器口径一致
  （`inbound/outbound/transfer/kpi-dashboard` 已用 `esc()`）。
- **Finding 2（favicon 低危）**：8 个功能页 `<head>` 统一加 `<link rel="icon" href="data:,">`
  抑制浏览器对 `/favicon.ico` 的自动请求，首屏不再产生 404。

## 逐项依据

- **XSS 转义面排查**：`app.js:215 escapeHtml` 全局 helper；`inbound/outbound/transfer/kpi-dashboard`
  四页的数据渲染器均对物料号/批号/库位/状态套 `esc()`（已核对
  `inbound.html:211`、`kpi-dashboard.html:189`、`transfer.html:449+`）。唯一例外是
  `data-import.html` 的导入回执表。
- **密钥**：`app/core/config.py` `jwt_secret` 默认 `dev-only-insecure-change-me`，
  但 `assert_production_safe` 在 `environment=prod` 时对默认值直接 `RuntimeError`；
  `llm_api_key`/`llm_provider` 默认空串 → 冷路径默认不发起任何出站调用。
- **RBAC**：`app/api/permissions.py` 的 `require_permission` 已挂 `/allocate/batch`
  （`inbound.operate`）与 `/llm/*` 六端点（`ai.*`），`toggle` 仅 admin —— 与 `13` §2.2 矩阵一致。
