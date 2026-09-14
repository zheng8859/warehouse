# design — ai-assist（冷路径 AI 辅助）

## Context

- `backend/app/llm/` 五个模块（`__init__` / `capabilities` / `client` / `quota` / `redact`）当前是 docstring-only 骨架（「阶段五（文档 29）实现」）。
- `backend/app/api/routes/llm.py` 只有 `APIRouter(prefix="/api/llm")`，无端点（TDD，避免 501 假端点）。
- `core/config.py` 已有 `cold_path_enabled: bool = False`、`llm_request_timeout_s: float = 2.0`；缺 `llm_provider` / `llm_max_tokens_per_req` / `llm_max_concurrency` / `llm_monthly_budget`。
- `api/permissions.py` 的 `Permission` 有 17 个成员、`AUTO_ONLY = {LEDGER_WRITE, ENGINE_INVOKE}`；无 `ai.*`。
- `services/kpi.py` 只有 `list_deviations()`；`KpiSnapshot` 聚合属阶段六（本阶段只做最小口径）。
- `services/relocate.py` 的 `derive_consolidation_plan`（单方案确定性收拢 + 三重校验 cap/批号不变/集中度改善 + 降级链）与 `confirm_relocate`（写台账 + cap 增量 + 后验）是 ④ 复用与扩展的基座。
- 约束：单进程 SQLite WAL；无中间件；核心链路（评分/落位/后验/台账）不得 import 任何 LLM 依赖；冷路径默认关闭、统一外部调用、不做本地模型。

## Goals / Non-Goals

**Goals:**
- 统一 LLM 网关：脱敏 → 开关 → 护栏 → 记账，四者不可绕过。
- 四类能力「规则算数、LLM 只叙事」，数字/判定全由规则侧确定。
- 降级不报错：LLM 失败不中断、不 5xx，`rule` 卡片独立成立。
- 对话台 L0/L2 意图 + 写意图护栏。

**Non-Goals:**
- 前端（对话台 L2 建议卡 UI、各页 AI 入口、开关置灰）—— Phase B，另开 change。
- 不做 L3 自主 Agent、不做本地 LLM、不做内容安全审核（M14）、不新增平行台账。
- 不改 6 因子评分、批量分配、后验、台账的既有行为。

## Decisions

### D1 — KPI 最小聚合（阶段六收编点）

`services/kpi.py` 新增规则侧最小聚合：同物料跨巷道均值、拣货量加权集中度、推荐采纳率，作为 ①② 的 `rule` 输入；标注「阶段六收编点」——阶段六 `KpiSnapshot` 全量聚合落地时此处逻辑收编，不重复维护。

- **理由**：① 需要的数值必须规则算（不能 LLM 现算）；阶段六才做全量聚合，本阶段只做够 ① 用的最小口径。
- **备选**：等阶段六聚合再实现 ① —— 否，① 是 P1，本阶段交付。

### D2 — 开关语义：`cold_path_enabled` + 409 `cold_path_disabled`

沿用 `cold_path_enabled`（默认 false）。开关关闭时 `ai.*` 端点返回 409 `cold_path_disabled`；核心链路不检查该开关（无 LLM 依赖）。

- **理由**：开关关闭 =「能力整体关闭」是可纠正客户端错误（先开再调），用 409；LLM 侧失败 =「能力开了但 LLM 没吐字」是降级，用 200 + `degraded_reason`。两者不能都 200。
- **备选**：开关关闭也 200 + degraded_reason —— 否，前端无法区分「去开开关」与「AI 临时不可用」。

### D3 — 成本护栏配置默认值

| 配置项 | 默认 | 说明 |
|---|---|---|
| `llm_provider` | `""`（空） | 空 = 未配置 = 不调 LLM，走「规则卡片」路径（与「默认关闭」fa一致） |
| `llm_max_tokens_per_req` | `4096` | 覆盖 KPI 报告/归因输出体量（~1500–2500 token），留余量 |
| `llm_max_concurrency` | `4` | 单进程、低频，护住外部配额 |
| `llm_monthly_budget` | `1_000_000` | 引导值，够 dev 跑通且超限路径可测（测试注入小值触发熔断） |

同步记账到 `AiCostQuota`；三道护栏在请求入口即时拦截。

- **理由**：单进程低频并发 4 足够；月预算用 token 计数（与「token 上限」同单位、可直接测）。
- **备选**：月预算用美元 —— 否，token 直接、可测。

### D4 — 双产物响应契约

每个冷路径响应 = `rule`（恒有，规则算的确定数据）+ `ai`（可选，LLM 叙事）+ `ai_generated: bool` + `degraded_reason: str | null`。AI Notice 只挂 `ai` 文本，不挂 `rule`。不变量：`ai_generated == (degraded_reason is None)`。

- **理由**：前端据此渲染——`ai_generated=false` 置灰「自然语言已降级」，仍显示规则卡片；`rule` 永不因 LLM 失败缺失。
- **备选**：LLM 失败 503 —— 否（见 D10）。

### D5 — 脱敏白名单（正向枚举）

出境 JSON 仅含白名单：料号 / 品名 / 批号 / 巷道 / 库位 / 数量 / 板数 / 聚合指标 / 6 因子分值 / cap 格数。禁出 `order_no` 与操作员姓名（去标识化）；客户名 / 价格 / 供应商 / 配方 / 真实产能从来不出；操作员自由文本备注 v1 不出境。

- **理由**：正向白名单比黑名单剥离更安全（新字段默认不出境）；去标识化是「数据出域护栏 = 0」的关键。
- **备选**：黑名单剥离敏感字段 —— 否，漏一个字段就出域。

### D6 — `ai.*` 权限（17 → 21）

`Permission` 增 4 成员：`ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`。角色映射：前三者 → 仓管员 / 主管 / 管理员（`planner` ❌）；`ai.toggle` → 仅管理员。①② 只读建议归 `ai.assist`，③ 采纳归 `ai.weight.update`，④ 方案生成归 `ai.relocate.propose`。

- **理由**：与 `13` §2.1/§2.2、doc 29 前置检查一致。
- **备选**：`ai.toggle` 进 `AUTO_ONLY` —— 否，`AUTO_ONLY` 是「业务角色不可手动」的封闭权限，toggle 是管理员可操作的开关注入。

### D7 — 3 个冷路径实体（非台账）

新增 `AiSuggestion`（建议卡片：`capability_kind` / 建议文本 / 采纳状态，只读）、`ConversationLog`（问句原文 / 脱敏后文本 / LLM 产出 / 是否命中冷路径）、`AiCostQuota`（`period` / `tokens_consumed` / `updated_at`）。复用既有 `ConversationContext`。三者 `warehouse_id` 隔离，不进 `Ledger` 台账链。

- **理由**：`15-05` §4.1 列 ConversationLog/AiSuggestion、doc 29 列 AiCostQuota；配额记账必须落库（同步、可审计）。
- **备选**：配额记账用内存 —— 否，重启即失，无法审计月预算。

### D8 — ④ 移库方案：规则算多方案 + 复用 relocate.operate（覆盖 doc 10 §六）

多方案（激进/均衡/保守）与量化代价（板数/车次/时长）由**规则侧**确定性计算（复用/扩展 `relocate.derive_consolidation_plan` 的三重校验 + 降级链），LLM 只做「权衡利弊」叙事。落地复用 `relocate.operate` 二次确认（G3），不新增写台账端点。

- **理由**：doc 10 §六 说「生成多方案是 LLM 强项」，但多方案数字（跨巷道数、代价）必须确定、可复核、可进台账校验——规则算才能守住红线 3「LLM 产出不经规则校验不进台账」。LLM 只叙事，数字永远来自规则。
- **备选**：LLM 直接生成多方案数字 —— 否，违反「LLM 不算数」+ 红线 3。

### D9 — 对话台 L0/L2（取消 L1 固定模板）

对话台层级仅 L0（结构化入口，确定性）+ L2（外部 LLM NLU，脱敏）；取消 L1 规则/模板匹配层（设计决策 10.7 已取消）。L2 意图 = `{KPI_INTERPRET, DEVIATION_ATTRIBUTE, WEIGHT_TUNE, RELOCATE_PROPOSE}` + `slots`（①`{period}` ②`{material_code}` ③`{}` ④`{material_code}`）+ `write_intent: bool`（③④ = true）。`write_intent=true` → 路由二次确认卡，不直接执行。

- **理由**：`15-05` §3.1 对话台层级仅 L0/L2；「L1」= PRD 人工确认层（决策权在人），不是对话分层；LLM 只产出「意图 + 槽位 + write_intent」，不产出执行动作。
- **备选**：保留 L1 模板 —— 否，10.7 已取消；写意图由 `write_intent` 显式表达，不靠模板。

### D10 — LLM 侧失败：200 + 降级（覆盖 15-05 §5 的 503 草图）

LLM 侧任何失败一律返回 HTTP 200 + 规则卡片 + `ai_generated=false` + `degraded_reason`（`provider_unconfigured` / `budget_exhausted` / `llm_timeout` / `llm_unavailable`），**不返回 503**。

- **理由**：冷路径是增强层，`rule` 卡片独立成立；LLM 失败只是「没有自然语言」，不是「服务不可用」。503 会把「降级」错报成「系统故障」。
- **备选**：`15-05` §5 草图的 503 —— 否，草图为 code 成型前，本次对齐为「降级不报错」。

### D11 — ② 偏离归因：异常清单规则算

② 规则侧产出确定性「异常清单/排查项」（容量 / 降级 / 人工 / 非系统四类候选，各附事实依据），LLM 只做归纳叙事；系统不断言唯一根因。

- **理由**：归因的「事实」必须规则确定（可审计、不甩锅）；LLM 只把清单串成话、排主次，不新增事实。
- **备选**：LLM 直接做因果推理产出事实 —— 否，事实不可审计，违反红线。

### D12 — ③ 权重调优：>=50 批次门槛（覆盖 doc 10 §五/§八 的 >=300）

批次门槛 `>=50`（2026-09-14 拍板覆盖 doc 的 >=300）；低于门槛 → 200 + 统计摘要 + `degraded_reason=insufficient_samples`，不调 LLM。反事实模拟由规则侧确定性计算；影子模式 = 建议的 `PROPOSED` 态本身（不新增机制）；仅在用户经 `ai.weight.update` 采纳后写入 `WeightConfig`（保留历史版本）。

- **理由**：>=300 需跑满一个月、试点期太长，>=50 让 ③ 阶段五可验证；反事实模拟是确定性重放；影子模式不落地即「不生效」。
- **备选**：>=300（用户拍板改 >=50）；「影子模式」新增独立双跑机制 —— 否，PROPOSED 态本身即「不生效」。

### D13 — API 表面

| 方法 | 路径 | 能力 | 鉴权（端点级） |
|---|---|---|---|
| POST | `/api/llm/toggle` | 冷路径开关 | `ai.toggle`（仅管理员） |
| POST | `/api/conversation/message` | 对话台统一入口（L0/L2） | `conversation.operate`（既有）+ 内部路由到 `ai.assist` |
| POST | `/api/llm/kpi/interpret` | ① KPI 解读 | `ai.assist` |
| POST | `/api/llm/deviation/attribute` | ② 偏离归因 | `ai.assist` |
| POST | `/api/llm/weight/tune` | ③ 权重调优建议 | `ai.assist` |
| POST | `/api/llm/weight/apply` | ③ 采纳落地 | `ai.weight.update` |
| POST | `/api/llm/relocate/propose` | ④ 移库方案生成 | `ai.relocate.propose` |

④ 的移库确认复用 `POST /api/job/batch/confirm`（relocate 类型），不新增写台账端点。

- **理由**：读建议一个资源、写落地两个资源、开关一个资源；对话台是统一入口，按 L0/L2 内部路由。
- **备选**：每能力一个独立子路由 + 单独 relocate 确认端点 —— 否，复用既有确认链路，不新增平行台账写路径。

## Risks / Trade-offs

1. [脱敏遗漏 → 数据出域] → 正向白名单 + 出境 payload 组装器单测逐字段断言；禁出字段永不进组装器。
2. [LLM 误被核心链路 import] → 编译期隔离：核心链路模块不 import `app.llm`；入口开关守卫 + 审查；`cold_path_enabled=false` 时无 LLM 调用。
3. [护栏熔断不及时烧额度] → 入口即时拦截 + 同步记账（每次调用后更新 `AiCostQuota`），熔断在请求前判。
4. [写操作绕过 L1 确认] → ③④ 落地复用 G3 二次确认；`write_intent` 路由确认卡；无确认不产生台账。
5. [LLM 输出被当系统结论] → `ai` 字段恒带 AI Notice；前端琥珀色渲染（Phase B）。
6. [L2 误路由到写意图] → `write_intent` 布尔显式表达，写意图只回确认卡不执行。
7. [主规格 delta 时序] → 归档前主规格仍是旧措辞（data-model 说 23 实体、permission 说 10 资源），这是正常时序、非遗漏；不以旧主规格判断行为（见记忆 `openspec-main-spec-delta-discipline`）。
8. [设计文档与代码口径漂移] → 见 Open Questions 的三处回写。

## Migration Plan

- **上线**：默认关闭（`cold_path_enabled=false` + `llm_provider=""`），核心链路零影响；启用需经企业审批（安全/合规签字，明确可出境字段、留存期限、是否用于训练）。
- **回滚**：功能开关式——`POST /api/llm/toggle {enabled:false}` 或改 env 重启；不删历史台账 / 建议 / 配额记录。
- **迁移**：新增 3 表（`AiSuggestion` / `ConversationLog` / `AiCostQuota`）的 Alembic 迁移，additive-only，无数据迁移、无既有表改造。

## Open Questions

以下为事实来源回写（不阻塞本 change 的规格与任务，需 `.<时间戳>.bak`，apply 阶段随代码落地）：

1. `CONTEXT.md` §八「L0/L1/L2/L3」→「L0/L2」、§四 17 项标识 → 21 项（补 `ai.*` 4 项）、术语表补 `AiSuggestion` / `ConversationLog` / `AiCostQuota`。
2. `10-AI辅助能力（冷路径）设计.md` §五/§八 ③ 门槛 300 → 50。
3. `15-05-对话台+接口权限+测试验收.md` §5/§8 的 503 草图 → 200 + `degraded_reason`。
4. `openspec/config.yaml` 的 `context` 中「对话台意图识别分层 L0→L1→L2→L3」→「L0/L2」，及「资源·动作标识」补 `ai.*` 4 项（否则继续向后续 change 注入旧约束）。
