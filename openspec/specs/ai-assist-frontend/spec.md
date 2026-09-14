# ai-assist-frontend Specification

## Purpose
冷路径 AI 辅助的前端呈现层：在零构建 8 页 shell 上把后端已就绪的四类冷路径能力与对话台统一入口接成可点击交互，将 LLM 叙事以琥珀色「AI 建议」呈现、写意图路由二次确认卡，并支撑 AI 开关的角色化置灰与 `warehouse_id` 会话上下文。

## Requirements

### Requirement: 对话台 L2 问句 chip 触发统一入口

系统必须把对话台 L2 问句 chip（`.qs.l2`）从静态标题改为可点击，点击后以该 chip 文案作为 L2 自由文本 `question` 调 `POST /api/conversation/message`，并在响应返回后渲染双产物建议卡。请求体不得同时携带 `intent`（L2 与 L0 互斥）。

#### Scenario: 点击 L2 chip 发 L2 问句
- **GIVEN** 用户已登录且处于对话台页
- **WHEN** 用户点击某 L2 问句 chip
- **THEN** 前端以该 chip 文案为 `question` 调 `POST /api/conversation/message`，且请求体不含 `intent`

#### Scenario: 请求携带 warehouse_id
- **WHEN** 前端发送对话台消息
- **THEN** 请求体 `warehouse_id` 恒等于会话存储的当前仓库编码，不缺失

### Requirement: 双产物建议卡渲染

系统必须把对话台与冷路径端点返回的双产物渲染为「规则卡片（`rule`，系统结论，恒有）+ AI 建议（`ai`，琥珀，可选）」两块；`ai` 的「AI 建议，仅供参考，需人工核实，不自动执行」标注由服务端注入，前端只负责琥珀渲染、不得重复前置；`ai_generated=false` 时展示降级说明、不报错、不中断主流程。

#### Scenario: 正常双产物
- **GIVEN** 响应 `ai_generated=true` 且 `ai` 非空
- **THEN** 界面同屏展示 `rule`（常规样式）与 `ai`（琥珀样式），且 `ai` 文本仅出现一次 AI 标注前缀

#### Scenario: 降级仅规则卡
- **GIVEN** 响应 `ai_generated=false` 且 `degraded_reason` 非空
- **THEN** 界面展示 `rule` 与降级说明（`degraded_reason`），不渲染 AI 叙事区，且不报错、不中断

### Requirement: 写意图二次确认卡

系统必须在响应 `write_intent=true` 时弹二次确认卡、不直接执行写操作；用户确认前不得发出任何写请求。

#### Scenario: 写意图路由确认卡
- **GIVEN** 对话台响应 `write_intent=true`
- **THEN** 前端渲染二次确认卡（而非直出结果），在用户点击确认前不发出采纳/执行类写请求

#### Scenario: 读意图直出
- **GIVEN** 对话台响应 `write_intent=false`
- **THEN** 前端直出结果卡片，不弹二次确认卡

### Requirement: p6 KPI 解读入口

系统必须在 KPI 看板（`kpi-dashboard.html`）提供「AI 解读」入口，点击后以当前看板周期调 `POST /api/llm/kpi/interpret`，并渲染 `rule` 数值与 `ai` 琥珀叙事。

#### Scenario: 触发 KPI 解读
- **WHEN** 用户在 KPI 看板点击「AI 解读」
- **THEN** 前端以 `warehouse_id` + 当前周期调 `POST /api/llm/kpi/interpret`，响应 `rule` 与 `ai` 同屏展示（`ai` 琥珀）

### Requirement: 偏离批次归因入口

系统必须在 KPI 看板的偏离批次列表提供「归因」动作，点击后以该批次的 `material_code`（及可选 `batch_no`）调 `POST /api/llm/deviation/attribute`，并把归纳结果渲染为「可能原因」而非唯一结论。

#### Scenario: 触发偏离归因
- **WHEN** 用户对某偏离批次点击「归因」
- **THEN** 前端以该批次 `material_code` 调 `POST /api/llm/deviation/attribute`，结果卡片以「可能原因」措辞展示，不出现「根因」「确定为」等唯一结论措辞

### Requirement: p7 权重影子建议卡

系统必须在配置页（`config.html`）提供「AI 权重建议」入口，点击后调 `POST /api/llm/weight/tune`；建议默认影子模式展示、不生效；用户点「采纳」须经 `POST /api/llm/weight/apply` 二次确认后才写入 `WeightConfig`；样本不足时展示统计摘要与「样本不足」说明、不展示采纳按钮。

#### Scenario: 影子建议不生效
- **WHEN** 用户查看权重建议卡
- **THEN** 建议以「影子模式（未生效）」呈现，且前端不发出 `weight/apply` 请求

#### Scenario: 采纳二次确认
- **WHEN** 用户点击「采纳」
- **THEN** 前端弹二次确认卡，确认后以 `suggestion_id` 调 `POST /api/llm/weight/apply`，未确认不发请求、未确认不产生 `WeightConfig` 变更

#### Scenario: 样本不足
- **GIVEN** 响应 `degraded_reason=insufficient_samples`
- **THEN** 界面展示统计摘要与「样本不足」说明，不渲染 AI 叙事、不展示采纳按钮

### Requirement: p5 移库多方案对比卡

系统必须在移库页（`transfer.html`）提供「多方案对比」入口，点击后调 `POST /api/llm/relocate/propose`，将规则算的三档方案（激进/均衡/保守）与量化代价并列展示；方案仅试算，落地须复用 `relocate.operate` 二次确认，未确认不产生 `JobOrder`、不写台账。

#### Scenario: 三档方案并列
- **WHEN** 用户触发移库多方案对比
- **THEN** 前端并列展示三档方案及其量化代价（板数/车次/时长），每档标明 `valid` 与否

#### Scenario: 试算不落地
- **WHEN** 用户仅查看方案、未二次确认
- **THEN** 前端不发出任何写请求（不产生 `JobOrder`、不写台账）

### Requirement: AI 开关置灰与角色权限呈现

系统必须把 AI 开关接线到 `POST /api/llm/toggle`（仅 `admin`）；非 `admin` 角色或冷路径关闭时开关置灰禁用；前端只做呈现，不放宽后端 RBAC。

#### Scenario: 非管理员开关置灰
- **GIVEN** 当前角色非 `admin`
- **THEN** AI 开关控件置灰禁用，不可触发 `toggle` 请求

#### Scenario: 管理员可切换
- **GIVEN** 当前角色为 `admin`
- **THEN** 开关可用，切换时调 `POST /api/llm/toggle` 并回显返回的 `cold_path_enabled`

### Requirement: warehouse_id 会话上下文

系统必须在登录成功后把登录响应中的 `warehouse_id` 存入会话存储，供所有冷路径与对话台请求携带；会话中无 `warehouse_id` 时不得发起依赖它的请求（避免 422）。

#### Scenario: 登录后存 warehouse_id
- **GIVEN** 登录响应含 `warehouse_id`
- **THEN** 前端将其写入会话存储，后续 `POST /api/conversation/message` 与 `POST /api/llm/*` 请求体均携带该值

#### Scenario: 无 warehouse_id 阻断
- **GIVEN** 会话中无 `warehouse_id`
- **THEN** 前端不得发起 `POST /api/conversation/message` 或 `POST /api/llm/*` 请求，改为提示重新登录
