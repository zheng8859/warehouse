# ai-assist-frontend 任务清单

> 说明：本 change 是**纯前端接线**，不改后端端点/契约/实体。`tasks` 规则中的「F1→F10 关键路径」「引擎单测（评分正确性/作业闭环/KPI 计量）」「cap 不足降级/乐观锁并发」「后验与 KpiSnapshot 聚合」均为后端引擎/交易链路的落地任务，**本 change 不适用**——它们已在阶段三/四的 `recommendation-engine` / `transaction-base` 变更中落地。本清单按前端依赖顺序拆解，异常/边界以「409 冷路径关闭、403 越权、降级 `degraded_reason`、样本不足 `insufficient_samples`」的前端呈现方式覆盖。

## 1. 共享层与基础设施（`backend/frontend/assets/app.js` / `styles.css`）

- [x] 1.1 登录处理器补存 `warehouse_id`——`app.js` 登录成功处 `sessionStorage.setItem('warehouse_id', data.warehouse_id)`。验证：静态断言「登录处理写入 `warehouse_id`」通过（见 6.1）。
- [x] 1.2 新增 `aiBody(payload)` 助手合并 `{warehouse_id, ...payload}`，供 6 个要求 `warehouse_id` 的端点使用（`toggle` 不套用）；`warehouse_id` 缺失时不出请求、改抛「请重新登录」。验证：静态断言「`aiBody` 缺 `warehouse_id` 时阻断而非发请求」通过。
- [x] 1.3 新增 `renderDualProduct(container, resp)` 共享渲染助手：`rule` 常规卡 + `ai` 琥珀卡 + `ai_generated=false` 时 `degraded_reason` 降级说明；`ai` 逐字渲染、不重复前置 AI 标注。验证：静态断言「`ai` 渲染路径不再拼接 AI_NOTICE 前缀」通过。
- [x] 1.4 `styles.css` 新增 `.ai-card`（`--amber`/`--amber-soft`）与 `.confirm-card` 样式，不新增色值。验证：静态断言「新类只引用既有 `:root` 令牌、无硬编码新色值」通过。
- [x] 1.5 AI 开关接线：给冷路径开关加 `data-toggle="ai"`，`app.js` 按该属性分支——非 `admin` 置灰禁用、不发请求；`admin` 调 `POST /api/llm/toggle` 并回显 `cold_path_enabled`；「预留开关」保持纯 CSS 态不受影响。验证：静态断言「`data-toggle="ai"` 开关存在、非 admin 分支不调用 toggle」通过。

## 2. 对话台 p8（`backend/frontend/chat.html`）

- [x] 2.1 L2 问句 chip（`.qs.l2`）绑点击：以 chip 文案为 `question`（不带 `intent`）调 `POST /api/conversation/message`，响应交给 `renderDualProduct` 渲染建议卡。验证：静态断言「`.qs.l2` 存在点击绑定且请求体不含 `intent`」通过。
- [x] 2.2 `write_intent=true` 时渲染二次确认卡：③ 确认后以 `suggestion_id` 调 `POST /api/llm/weight/apply`；④ 不直接写，交移库落地流；确认前不发写请求。验证：静态断言「`write_intent` 分支渲染确认卡、未确认不发写请求」通过。

## 3. KPI 看板 p6（`backend/frontend/kpi-dashboard.html`）

- [x] 3.1 新增「AI 解读」按钮：以 `warehouse_id` + 当前周期调 `POST /api/llm/kpi/interpret`，`rule` 数值与 `ai` 琥珀叙事同屏。验证：静态断言「KPI 解读入口存在且调 `/llm/kpi/interpret`」通过。
- [x] 3.2 偏离批次列表行内新增「归因」动作：以该批次 `material_code`（可选 `batch_no`）调 `POST /api/llm/deviation/attribute`，结果以「可能原因」措辞渲染。验证：静态断言「偏离归因入口存在且调 `/llm/deviation/attribute`」通过。

## 4. 配置页 p7（`backend/frontend/config.html`）

- [x] 4.1 新增「AI 权重建议」入口调 `POST /api/llm/weight/tune`；影子模式展示（标注未生效）；「采纳」经二次确认后以 `suggestion_id` 调 `POST /api/llm/weight/apply`；`degraded_reason=insufficient_samples` 时展示统计摘要、不显示采纳按钮。验证：静态断言「权重建议入口存在、样本不足分支无采纳按钮」通过。

## 5. 移库页 p5（`backend/frontend/transfer.html`）

- [x] 5.1 新增「多方案对比」入口调 `POST /api/llm/relocate/propose`，三档方案（激进/均衡/保守）+ 量化代价（板数/车次/时长）+ `valid` 标记并列展示；试算只读，选中方案回填既有移库执行表单（复用 `relocate.operate` 二次确认）。验证：静态断言「多方案对比入口存在且卡片只读、不触发写请求」通过。

## 6. 前端静态断言测试（`backend/tests/frontend/`）

- [x] 6.1 新增 `backend/tests/frontend/test_frontend_ai_assist.py`，参照 `test_frontend_foundation.py` 的静态断言模式，逐条覆盖 `ai-assist-frontend` 规格 9 条需求（6 处入口存在性、双产物琥珀/降级渲染、写意图确认卡、`warehouse_id` 会话存储、AI 开关置灰与 `data-toggle` 作用域、`ai` 不重复前缀）。验证：`cd backend && python -m pytest tests/frontend/ --tb=short -q` 全绿且与既有 9 条 `test_frontend_foundation.py` 不冲突。
- [x] 6.2 全量回归：`cd backend && python -m pytest tests/ --tb=short -q` 通过，确认前端改动未破坏既有 1375 条用例（后端零行为变更）。验证：退出码 0、无回归。
