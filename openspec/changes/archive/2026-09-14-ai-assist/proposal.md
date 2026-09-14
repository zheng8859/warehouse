# 冷路径 AI 辅助能力（ai-assist）

## Why

阶段五（`v0.5.0`）落地冷路径 LLM 辅助。核心链路（6 因子评分 / 落位 / 后验 / 台账）已由阶段三、四确定性落地并打标；本阶段只补「人读的部分」——让 LLM 做解释、分析、建议，数字与判定全部由本地规则算（`10` §一「让 LLM 做"人读的部分"」）。目标：把看板周报、偏离排查、移库排单、权重调参从「人工拼结论 / 拍脑袋」变成「规则算数 + LLM 叙事 + 规则校验 + L1 确认」，同时守住「核心链路不出域、决策权在人、数据可审计」三条产品价值观。

## What Changes

- 新增**统一 LLM 调用网关**：脱敏管线（正向白名单出站）+ 一键关闭开关 + 成本护栏三门槛（单请求 token 上限 / 并发上限 / 月度预算硬上限 + 令牌熔断）+ 同步记账（`AiCostQuota`）。冷路径默认关闭。
- 新增**四类冷路径能力**：① KPI 解读（P1，只读）、② 偏离归因（P1，只读）、③ 权重调优建议（P2，人采纳才生效）、④ 移库方案生成（P1，规则校验 + L1 确认）。四者统一「规则算数、LLM 只叙事」。
- 新增**双产物响应契约**：`rule`（恒有，规则算的确定数据）+ `ai`（可选，LLM 叙事）+ `ai_generated` 布尔 + `degraded_reason`。LLM 侧任何失败一律返回 HTTP 200 + 规则卡片 + `degraded_reason`（降级不报错、不中断）；开关关闭返回 409 `cold_path_disabled`。
- 新增**对话台 L0/L2 意图识别**（L2 走外部 LLM 脱敏调用）：L2 意图 = `{KPI_INTERPRET, DEVIATION_ATTRIBUTE, WEIGHT_TUNE, RELOCATE_PROPOSE}` + `slots` + `write_intent` 布尔；`write_intent=true` 路由到二次确认卡，不直接执行。
- 新增**数据脱敏白名单**：出境 JSON 仅含料号 / 品名 / 批号 / 巷道 / 库位 / 数量 / 板数 / 聚合指标 / 6 因子分值 / cap 格数；禁出 `order_no` 与操作员姓名；操作员自由文本备注 v1 不出境。
- 新增 **`ai.*` 权限 4 项**：`ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`。角色映射：①②③④ → 仓管员 / 主管 / 管理员（计划员 403）；`ai.toggle` 仅管理员。
- 新增 **3 个实体**：`AiSuggestion`（建议卡片，只读）/ `ConversationLog`（对话日志）/ `AiCostQuota`（成本配额记账）。
- 新增 **4 项配置**：`llm_provider` / `llm_max_tokens_per_req` / `llm_max_concurrency` / `llm_monthly_budget`（补齐既有 `cold_path_enabled` / `llm_request_timeout_s`）。
- 新增 **KPI 结构化数据最小聚合**（同物料跨巷道均值 / 加权集中度 / 采纳率，规则算，阶段六收编点），作为 ①② 的规则侧输入。

## Capabilities

### New Capabilities
- `ai-assist`: 冷路径 LLM 辅助 —— 统一 LLM 网关（脱敏 / 开关 / 护栏 / 熔断）、四类能力（KPI 解读 / 偏离归因 / 权重调优 / 移库方案）、对话台 L0/L2 意图识别、双产物降级契约与 AI 建议标注。

### Modified Capabilities
- `data-model`: 新增 3 个冷路径实体 `AiSuggestion` / `ConversationLog` / `AiCostQuota`（字段与枚举）。
- `permission`: 新增 `ai` 资源及 4 个动作（`ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`）、角色映射，及冷路径端点的端点级鉴权实例。

## Impact

- **代码**：`backend/app/llm/`（5 个 stub 落地：`capabilities` / `client` / `quota` / `redact` / `__init__`）、`backend/app/api/routes/llm.py` + `backend/app/api/routes/conversation.py`、`backend/app/models/`（3 实体 + 迁移）、`backend/app/core/config.py`（4 配置项）、`backend/app/api/permissions.py`（4 权限）、`backend/app/services/kpi.py`（最小聚合）、`backend/app/schemas/`（DTO）。
- **API 路由**（`/api/*` 约定）：`POST /api/llm/toggle`（开关，`ai.toggle`）、`POST /api/conversation/message`（对话台统一入口）、`POST /api/llm/kpi/interpret`（①）、`POST /api/llm/deviation/attribute`（②）、`POST /api/llm/weight/tune`（③ 建议）、`POST /api/llm/weight/apply`（③ 采纳落地，`ai.weight.update`）、`POST /api/llm/relocate/propose`（④ 方案生成，`ai.relocate.propose`）。④ 的移库确认复用既有 `relocate.operate` 确认链路，不新增写台账端点。
- **KPI 指标**：同物料跨巷道均值 / 拣货量加权集中度 / 推荐采纳率 —— 均为「规则算、LLM 叙事」的**输入**，LLM 不改写数字。
- **依赖**：外部 LLM（`llm_provider` 可配置，默认关闭）；无新增运行时中间件（配额记账走本地库 `AiCostQuota` + 同步记账）。

## 非目标

- **L3 自主规划型 Agent 不做**；LLM 不参与实时评分 / 排序、不直接写台账、产出不经规则校验不进台账（三条红线）。
- 不做 LLM 生成 KPI / 指标对外推送 / KPI 阈值自学习 / 跨厂对比统计 / 实时流式计算。
- 不做 A/B 测试（单厂单一活跃队列无法拆流量，用前后对比）；不做用户行为埋点。
- 不做内容安全审核（M14 路线图）、不做本地部署 LLM（统一外部调用、不做本地模型）。
- **不做前端**：对话台 L2 建议卡片 UI、KPI 解读按钮、偏离归因入口、权重影子卡、移库多方案对比卡、AI 开关置灰 —— 属 Phase B（`29`），另开 change。
- 不新增平行台账：`AiSuggestion` 是只读建议卡片、`AiCostQuota` 是配额记账，均不进入 `Ledger` 台账链（`Ledger` 仍是唯一一套台账）。

## 关键路径归属

本 change 不在 `08` 号 F1→F10 关键路径上，属 P1 路线图 `M8~M12 冷路径 + M13 成本配额` 的**可选增强层**：`AI_ASSIST_ENABLED=false`（默认）时，核心链路（评分 / 落位 / 后验 / 台账）编译期与运行期均无 LLM 调用、零功能下降。热路径对 LLM 的引用仅存在于 `app/llm/` 模块内，且由入口开关守卫，核心链路模块不得 import 任何 LLM 依赖。
