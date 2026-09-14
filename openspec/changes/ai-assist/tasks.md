# tasks — ai-assist（冷路径 AI 辅助，后端 Phase A）

> **关键路径规则登记（不适用）**：`config.yaml` tasks 规则要求按 F1→F2→F9→F3→F4→F5→F6→F7→F8→F10 排序，并落地「评分正确性 / 作业闭环一致性」等基线层评测任务。本 change 是**阶段五冷路径增强层（P1 路线图 M8~M12 冷路径 + M13 成本配额）**，不在 F1~F10 关键路径上，且**不改** 6 因子评分、批量分配、作业闭环的既有行为 —— 故「评分正确性」「作业闭环一致性」两类基线任务在本 change 不适用，由阶段三/四既有测试（1166 passed）继续兜底；本 change 只落地与冷路径相关的基线维度：**安全隔离（脱敏出站）、配置合规与权限（`ai.*` 端点级鉴权）、KPI 计量正确性（最小聚合口径）**。
>
> 顺序按**依赖关系**排：地基（配置/实体/权限）→ 网关（脱敏/护栏/记账）→ 四类能力（规则侧 + LLM 侧）→ 对话台 → 端点/鉴权 → 边界与回归。每任务标影响文件与验证方式（pytest 单测 / API 契约）。

## 1. 配置与数据模型地基

- [x] 1.1 `core/config.py` 新增 4 配置项（`llm_provider=""` / `llm_max_tokens_per_req=4096` / `llm_max_concurrency=4` / `llm_monthly_budget=1_000_000`）。验证：`tests/models/`（或新增 `tests/core/test_config.py`）断言默认值与环境变量覆盖（env_prefix `WMS_`）。
- [x] 1.2 新增 3 实体 `AiSuggestion` / `ConversationLog` / `AiCostQuota`（`models/` 按 17 号数据链分组挂载，均 `warehouse_id` 隔离、非台账）+ Alembic 迁移（additive-only）。验证：`tests/models/` CRUD + 唯一约束 + 无 `is_deleted` 断言通过，`alembic upgrade head` 空库可跑。
- [x] 1.3 `api/permissions.py` `Permission` 增 `ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`（17→21），`ROLE_PERMISSIONS` 映射（前三者 → 仓管员/主管/管理员；`ai.toggle` → 仅管理员）。验证：权限矩阵单测（对照 `13` §2.2，计划员对 `ai.*` 全无）。

## 2. 统一 LLM 网关（脱敏 / 护栏 / 记账）

- [x] 2.1 `llm/redact.py` 正向白名单脱敏管线：出境 JSON 仅含料号/品名/批号/巷道/库位/数量/板数/聚合指标/6 因子分值/cap 格数；禁出 `order_no` + 操作员姓名 + 操作员备注。验证：出境 payload 组装器单测逐字段断言（安全隔离基线维度）。
- [x] 2.2 `llm/client.py` provider 抽象（`llm_provider` 空 = 不调用）+ 超时（`llm_request_timeout_s=2.0`）+ mock 客户端。验证：mock 客户端单测 + 超时→`llm_timeout` 降级单测。
- [x] 2.3 `llm/quota.py` 成本护栏三门槛（token/并发/月预算）+ 同步记账（更新 `AiCostQuota`）+ 入口熔断（`budget_exhausted`）。验证：注入小预算触发熔断、并发计数、同步记账单测。
- [x] 2.4 网关编排 `llm/capabilities.py`：统一链路「规则算 → 脱敏 → 护栏 → 调用 → 记账 → 双产物响应」。验证：集成单测——`provider_unconfigured` / `budget_exhausted` / `llm_timeout` / `llm_unavailable` 四种降级路径均 200 + 规则卡片。

## 3. 四类能力（规则侧 + LLM 侧）

- [x] 3.1 `services/kpi.py` 最小聚合（同物料跨巷道均值 / 加权集中度 / 采纳率），标「阶段六收编点」。验证：KPI 计量正确性单测（口径与 18 号一致）。
- [x] 3.2 ① KPI 解读 `capabilities`（规则聚合 → LLM 叙事，数值不改写）。验证：`rule` 数值与 `ai` 叙事引用的数值一致单测。
- [x] 3.3 ② 偏离归因（规则算异常清单：容量/降级/人工/非系统四类 + 事实依据，LLM 只叙事、不断言唯一根因）。验证：清单确定性单测（同输入同清单）。
- [x] 3.4 ③ 权重调优建议（≥50 批次门槛 + 反事实模拟规则算 + 影子模式=PROPOSED 态）。验证：<50 批次 → `insufficient_samples` 不调 LLM；≥50 → 反事实表确定性单测。
- [x] 3.5 ③ 采纳落地 `weight/apply`（经 `ai.weight.update` + 规则校验写入 `WeightConfig`，保留历史版本，不自动改写）。验证：未采纳不改权重、采纳才写 + 版本化单测。
- [x] 3.6 ④ 移库方案生成 `relocate/propose`（规则算多方案激进/均衡/保守 + 量化代价 + 三重校验 cap/批号不变/集中度改善；LLM 只叙事）。验证：多方案确定性 + 三重校验单测；未经确认不产生 `JobOrder`。

## 4. 对话台 L0/L2 意图

- [x] 4.1 意图识别（`{KPI_INTERPRET, DEVIATION_ATTRIBUTE, WEIGHT_TUNE, RELOCATE_PROPOSE}` + `slots` + `write_intent`，③④ 为 true）。验证：意图路由单测（读意图 `write_intent=false`、写意图 `write_intent=true`）。
- [ ] 4.2 `routes/conversation.py` `POST /api/conversation/message`（L0 结构化 + L2 脱敏 LLM；写意图路由确认卡，不直接写台账）。验证：API 契约单测——写意图返回建议 + `write_intent=true` 且零台账。

## 5. API 端点与鉴权

- [ ] 5.1 `routes/llm.py` 七个端点（toggle / kpi/interpret / deviation/attribute / weight/tune / weight/apply / relocate/propose）。验证：API 冒烟 + 双产物响应契约单测（`rule` 恒有、`ai` 可选、`ai_generated`、`degraded_reason`）。
- [ ] 5.2 `schemas/` DTO + AI Notice 强制注入（所有 `ai` 文本附「AI 建议，仅供参考，需人工核实，不自动执行」）。验证：契约单测——无标注不渲染。
- [ ] 5.3 `deps.py` / 端点接入 `require_permission`（`ai.assist` / `ai.weight.update` / `ai.relocate.propose` / `ai.toggle`）。验证：403 单测——计划员调 `ai.assist` 403、非管理员调 toggle 403。

## 6. 异常/边界与回归

- [ ] 6.1 边界测试：开关关闭 → 409 `cold_path_disabled`；LLM 失败 → 200 降级（不 5xx）；月度预算耗尽 → 熔断仅规则卡片；权限越界 → 403。验证：`tests/api/` 边界套件全绿。
- [ ] 6.2 全量回归：`python -m pytest tests/ --tb=short -q` 确认核心链路（评分/落位/后验/台账）1166 条既有测试零回归，冷路径新增测试全绿。
