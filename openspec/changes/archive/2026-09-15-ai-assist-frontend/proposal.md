## Why

阶段五「冷路径 LLM」的 Phase A（后端 `app/llm/` 四类能力 + 对话台统一入口 + 开关）已收尾并打 `v0.5.0`，但 Phase B（前端呈现）尚未开始——操作员在 8 页 shell 里看不到任何 AI 入口，冷路径对使用者而言形同不存在。本 change 把后端已就绪的冷路径能力接到前端 6 处交互上，让「AI 建议（琥珀、仅供参考）」真正进入操作流。

## What Changes

前端零构建接入已就绪的冷路径端点，新增 6 项交付（均为纯前端接线，无后端行为变更）：

- **对话台 L2 问句 chip + AI 建议卡片**（`chat.html`，p8）：`.qs.l2` chip 由静态标题改为可点击，点击后以 `question` 调 `POST /api/conversation/message`；渲染双产物建议卡（`rule` 恒有 + `ai` 琥珀标注）；`write_intent=true` 时弹二次确认卡、不自动执行。
- **p6 KPI 解读按钮**（`kpi-dashboard.html`）：新增「AI 解读」按钮，调 `POST /api/llm/kpi/interpret`，卡片内 `rule` 数值 + `ai` 琥珀叙事并排展示。
- **偏离批次归因入口**（`kpi-dashboard.html`）：偏离批次列表行内新增「归因」动作，调 `POST /api/llm/deviation/attribute`，展示「可能原因」而非唯一结论。
- **p7 权重影子建议卡**（`config.html`）：权重配置页新增「AI 权重建议」入口，调 `POST /api/llm/weight/tune`；影子模式展示，采纳走 `POST /api/llm/weight/apply` 二次确认后生效。
- **p5 移库多方案对比卡**（`transfer.html`）：移库页新增「多方案对比」入口，调 `POST /api/llm/relocate/propose`，三档方案（激进/均衡/保守）并列对比；落地复用 `relocate.operate` 二次确认。
- **AI 开关置灰**（`config.html` + `app.js`）：`.cf-switch` 由纯 CSS 态改为接线 `POST /api/llm/toggle`；非 `admin` 角色或冷路径关闭时置灰禁用，前端只做呈现、不放宽权限。

配套的会话与样式缺口修复：

- `app.js` 登录处理补存 `warehouse_id` 到 `sessionStorage`（`auth.LoginResponse` 已回传该字段；`/api/conversation/message` 与 6 个 `llm` 端点都要求 `warehouse_id`，缺失会 422）。
- `assets/styles.css` 新增 AI 建议卡琥珀样式与 `write_intent` 二次确认卡样式，复用既有 `:root` 令牌（`--amber` / `--amber-soft`），不新增色值。

无 **BREAKING** 变更：全部为新增交互，不删除或改写既有 shell 行为。

## Capabilities

### New Capabilities

- `ai-assist-frontend`: 冷路径 AI 辅助的前端呈现与接线——对话台 L2 问句 chip + AI 建议卡片、p6 KPI 解读按钮、偏离批次归因入口、p7 权重影子建议卡、p5 移库多方案对比卡、AI 开关置灰，以及支撑它们的 `warehouse_id` 会话存储与 `.cf-switch` 端点接线。它消费 `ai-assist`（后端冷路径）已定义的双产物契约，本身只规定「前端如何呈现与路由」，不新增后端行为。

### Modified Capabilities

（无）——`frontend-foundation` 的 4 条需求（设计令牌 / 角色菜单可见性 / 四态占位 / `.node` 三态）均不变；本 change 只在其 shell 之上新增 AI 交互层，不改动既有需求语义。

## Impact

- **前端文件**：`backend/frontend/chat.html`、`kpi-dashboard.html`、`config.html`、`transfer.html`、`assets/app.js`、`assets/styles.css`。
- **消费的 API 路由**（已实现，Phase A）：`POST /api/conversation/message`、`POST /api/llm/toggle`、`POST /api/llm/kpi/interpret`、`POST /api/llm/deviation/attribute`、`POST /api/llm/weight/tune`、`POST /api/llm/weight/apply`、`POST /api/llm/relocate/propose`。
- **受影响的实体**（经 API 读/写，前端不直接触库）：`KpiSnapshot`（① 读）、`Deviation`（② 读）、`WeightConfig`（③ 采纳写、影子读）、`Snapshot` / `InventoryItem`（④ 分布读）、`AiCostQuota`（开关/成本，只读呈现）。
- **KPI 指标**（出现在 ① 解读卡 `rule`）：同物料跨巷道均值、拣货量加权集中度、推荐采纳率。
- **测试**：新增 `backend/tests/frontend/` 静态断言（参照 `test_frontend_foundation.py`），覆盖 6 处入口存在性、`.cf-switch` 角色置灰、`warehouse_id` 会话存储、建议卡琥珀标注 class。
- **权限**：`ai.assist`（①②③ 读）、`ai.weight.update`（③ 采纳）、`ai.relocate.propose`（④）、`ai.toggle`（开关，仅 `admin`）；`planner` 角色无 `ai.*` 权限，前端须按角色隐藏/置灰对应入口。

## 非目标

- 不实现后端冷路径能力本身（Phase A 已完成），本 change 只做前端接线与呈现。
- 不新增后端 API 端点、不改变 `ai-assist` 双产物契约或 `AI_NOTICE` 注入（标注已服务端注入，前端不重复加前缀，只负责琥珀渲染）。
- 不触碰热路径 UI：6 因子评分、入库软推荐、落位执行、出库顺路取、后验比对的既有页面行为不变。
- 不做 L3 自主规划型 Agent 前端（对话台无「自动执行」入口）；③④ 写意图一律路由二次确认卡。
- 不做多厂编码映射、SSO、审计日志、用户行为埋点等 P1 路线图项。
- AI 开关的权限边界（`ai.toggle` 仅 `admin`）由后端强制，前端只做置灰呈现，不自行放宽。
