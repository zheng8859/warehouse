# ai-assist Specification

## Purpose
冷路径 AI 辅助能力：在不触碰核心链路（6 因子评分 / 落位 / 后验 / 台账）的前提下，用外部 LLM 把本地规则算好的结构化数据「翻译成人话」——KPI 解读、偏离归因、权重调优建议、移库方案叙事，并统一脱敏、开关、成本护栏与降级契约。

## Requirements

### Requirement: 冷路径开关守卫与一键关闭

系统必须在 `cold_path_enabled=false`（默认）时拦截所有冷路径端点并返回 409 `cold_path_disabled`，且核心链路（评分 / 落位 / 后验 / 台账）不调用任何 LLM、功能零下降。系统必须仅允许管理员经 `ai.toggle` 通过 `POST /api/llm/toggle` 开启或关闭冷路径。

#### Scenario: 关闭时访问冷路径端点
- **WHEN** `cold_path_enabled=false` 且任意角色调用 `POST /api/llm/kpi/interpret`
- **THEN** 系统返回 409 `cold_path_disabled`，且不调用外部 LLM

#### Scenario: 关闭时核心链路零影响
- **WHEN** `cold_path_enabled=false` 且执行入库批量分配 `POST /api/allocate/batch`
- **THEN** 系统正常评分落位，不调用任何 LLM，响应不含 AI 字段

#### Scenario: 非管理员切换开关
- **WHEN** 非管理员角色调用 `POST /api/llm/toggle`
- **THEN** 系统返回 403（`permission_denied`）

### Requirement: 脱敏白名单出站

系统向外部 LLM 出站时，出境 JSON 必须仅含白名单字段：料号 / 品名 / 批号 / 巷道 / 库位 / 数量 / 板数 / 聚合指标 / 6 因子分值 / cap 格数。系统不得出站 `order_no`、操作员姓名，也不得出站客户名 / 价格 / 供应商 / 配方 / 真实产能；操作员自由文本备注在 v1 不得出站。

#### Scenario: 出站 payload 只含白名单
- **WHEN** 系统在调用外部 LLM 前组装出站 JSON
- **THEN** 该 JSON 仅含白名单字段，不含 `order_no` 与操作员姓名

#### Scenario: 未配置 provider 不调用
- **WHEN** `llm_provider` 为空字符串
- **THEN** 系统不发起任何外部 LLM 调用，返回 200 + 规则卡片 + `degraded_reason=provider_unconfigured`

### Requirement: 双产物响应契约

系统对每个冷路径能力必须返回 `rule`（规则算的确定数据，恒有）与 `ai`（LLM 叙事，可选）两块，并附 `ai_generated` 布尔与 `degraded_reason`（可空）字段。

#### Scenario: 正常产出 AI 叙事
- **WHEN** 冷路径开启、provider 已配置、护栏未熔断，且调用某能力成功
- **THEN** 系统返回 `rule` 非空、`ai` 非空、`ai_generated=true`、`degraded_reason=null`

### Requirement: LLM 侧失败降级不报错

系统在 LLM 侧失败时必须返回 HTTP 200 + 规则卡片 + `ai_generated=false` + `degraded_reason`（取值 `provider_unconfigured` / `budget_exhausted` / `llm_timeout` / `llm_unavailable`），不得返回 5xx，也不得中断主流程。

#### Scenario: LLM 调用超时
- **WHEN** 外部 LLM 调用超过 `llm_request_timeout_s`（默认 2.0 秒）
- **THEN** 系统返回 200 + 规则卡片 + `ai_generated=false` + `degraded_reason=llm_timeout`

#### Scenario: 外部服务不可用
- **WHEN** 外部 LLM 服务连接失败或返回不可用
- **THEN** 系统返回 200 + 规则卡片 + `ai_generated=false` + `degraded_reason=llm_unavailable`

### Requirement: 成本护栏与熔断

系统必须在每次冷路径调用前检查三道护栏：单请求 token 上限（默认 4096）、并发上限（默认 4）、月度预算硬上限（默认 1_000_000 token）。系统必须同步记账到 `AiCostQuota`；月度预算耗尽时在请求入口即时熔断并降级为「仅规则卡片」（`degraded_reason=budget_exhausted`），不得报错。

#### Scenario: 月度预算耗尽熔断
- **WHEN** `AiCostQuota.consumed >= llm_monthly_budget`
- **THEN** 系统不调用外部 LLM，返回 200 + 规则卡片 + `degraded_reason=budget_exhausted`

#### Scenario: 单请求超 token 上限
- **WHEN** 某次请求输入超过 `llm_max_tokens_per_req`
- **THEN** 系统拒绝该请求并提示拆分（不截断文本）

#### Scenario: 并发达上限
- **WHEN** 在途 LLM 调用数达到 `llm_max_concurrency`
- **THEN** 系统排队或拒绝新请求，不突破并发上限

### Requirement: ① KPI 解读（只读）

系统必须以规则侧聚合的 KPI 结构化数据（同物料跨巷道均值 / 拣货量加权集中度 / 推荐采纳率）为输入，由 LLM 生成自然语言解读；叙事中引用的数值必须与 `rule` 一致，LLM 不得改写数值。① 只读，权限 `ai.assist`。

#### Scenario: KPI 解读数值一致
- **WHEN** 用户调用 ① 且冷路径开启、护栏未熔断
- **THEN** 系统返回规则算的 KPI 数值（`rule`）与 LLM 叙事（`ai`），叙事引用的数值与 `rule` 完全一致

### Requirement: ② 偏离归因（只读，不断言唯一根因）

系统必须由规则侧拉取偏离批次的推荐巷道集 + 6 因子分值 + 实际落位 + 当时 cap 快照，由 LLM 归纳「可能原因与排查项」，系统不得断言唯一根因。② 只读，权限 `ai.assist`。

#### Scenario: 偏离归因列可能原因
- **WHEN** 用户对某偏离批次调用 ②
- **THEN** 系统返回规则侧多源明细 + LLM 归纳（列为「可能原因」而非唯一结论）

### Requirement: ③ 权重调优（≥50 批次，人采纳才生效）

系统必须仅当历史批次样本 ≥50 时产出权重调优建议，否则返回 200 + 统计摘要 + `degraded_reason=insufficient_samples` 且不调用 LLM。反事实模拟必须由规则侧计算；建议默认影子模式（不生效）；系统必须仅在用户经 `ai.weight.update` 采纳后才写入 `WeightConfig`，不得自动改写权重。

#### Scenario: 样本不足
- **WHEN** 历史批次 < 50
- **THEN** 系统返回 200 + 统计摘要 + `degraded_reason=insufficient_samples`，不调用 LLM

#### Scenario: 未采纳不改权重
- **WHEN** 用户对建议未点「采纳」
- **THEN** `WeightConfig` 保持不变（影子模式，不生效）

#### Scenario: 采纳才写权重
- **WHEN** 用户经 `ai.weight.update` 采纳某条建议
- **THEN** 系统经规则校验后写入 `WeightConfig` 并保留历史版本

### Requirement: ④ 移库方案（规则算多方案，规则校验 + L1 确认）

系统必须由规则侧计算多方案（激进 / 均衡 / 保守）与量化代价（板数 / 车次 / 时长），LLM 仅做权衡叙事；每个方案必须通过规则校验（cap 充足、批号不变、集中度改善、不占 A 类预留池）。系统必须经 `ai.relocate.propose` 生成方案，落地复用 `relocate.operate` 二次确认，未确认不产生 `JobOrder`、不写台账。

#### Scenario: 多方案生成与校验
- **WHEN** 用户对某料号调用 ④
- **THEN** 系统返回规则算的多方案（含跨巷道数与代价）+ LLM 权衡叙事，所有方案均通过规则校验

#### Scenario: 未经确认不落库
- **WHEN** 用户仅生成方案、未二次确认
- **THEN** 系统不产生 `JobOrder`、不写台账

### Requirement: 对话台 L0/L2 意图识别

系统必须把对话台意图识别为 `KPI_INTERPRET` / `DEVIATION_ATTRIBUTE` / `WEIGHT_TUNE` / `RELOCATE_PROPOSE` 之一，并产出 `slots` 与 `write_intent` 布尔（③④ 为 true）。系统必须在 `write_intent=true` 时路由到二次确认卡，不得直接执行写操作；L2 识别走外部 LLM 脱敏调用。

#### Scenario: 写意图路由确认卡
- **WHEN** 对话台识别出写意图（如「把 `3001234` 收拢一下」）
- **THEN** 系统返回建议 + `write_intent=true`，前端弹二次确认卡，不直接写台账

#### Scenario: 读意图直出结果
- **WHEN** 对话台识别出读意图（如「上周集中度怎么样」）
- **THEN** 系统直出结果卡片 + `write_intent=false`

### Requirement: AI 建议标注

系统必须将所有 LLM 产出渲染为琥珀色并标注「AI 建议，仅供参考，需人工核实，不自动执行」，不得渲染成系统结论。

#### Scenario: 标注强制注入
- **WHEN** 系统返回任何 LLM 叙事
- **THEN** 该叙事附带「AI 建议，仅供参考，需人工核实，不自动执行」标注
