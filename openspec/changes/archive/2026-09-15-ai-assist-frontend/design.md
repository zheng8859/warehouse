## Context

动机见 `proposal.md`。当前状态：后端冷路径已就绪——`POST /api/llm/{toggle,kpi/interpret,deviation/attribute,weight/tune,weight/apply,relocate/propose}` 六端点 + `POST /api/conversation/message` 统一入口，均返回双产物（`rule` 恒有 + `ai` 可选 + `ai_generated` + `degraded_reason`），`ai` 已由服务端 `field_serializer` 注入「AI 建议，仅供参考，需人工核实，不自动执行」标注。前端 8 页 shell 已存在（阶段七），但除 `data-import.html` 外均未接业务 API；`app.js` 已提供 `window.api(path, opts)`（补 `/api` 前缀 + Bearer + JSON + 非 2xx 抛错带 `status/body`）与 `setState(el, state, opts)` 四态钩子。两个已知缺口：登录处理只存 `token/role/user_id`、未存 `warehouse_id`（`/api/llm/*` 与 `/api/conversation/message` 均要求 `warehouse_id`，缺失 422）；`.cf-switch` 点击仅切 CSS `.off` 类，未接任何端点，且同时服务「冷路径开关」与「预留开关」两处。本设计只新增前端接线与呈现，不新增/改动后端端点。

## Goals / Non-Goals

**Goals:**
- 把 6 项交付接到已就绪端点，保持零构建（原生 HTML/CSS/JS + `:root` 令牌），不引入框架/构建。
- 双产物渲染逻辑、写意图二次确认、`warehouse_id` 注入、AI 开关置灰四处跨页复用，避免四页各自复制。
- 守住三条冷路径红线：AI 只建议（琥珀标注）、写操作必二次确认、LLM 产出不经规则校验不进台账。

**Non-Goals:**
- 不改后端端点 / 双产物契约 / AI_NOTICE 注入逻辑（标注已服务端注入，前端只琥珀渲染、不重复前置）。
- 不新增平行台账、不直接触库、不动热路径页面（评分/落位/出库/后验）既有行为。
- 不做 L3 自主规划 Agent 前端、不做 SSO / 埋点 / 多厂映射等 P1 路线图项。

## Decisions

**D1 — 零构建 + 复用 `data-import.html` 参考模式。** 每页内联 `<script>` 只调 `window.api` + `setState`，共享逻辑进 `assets/app.js`。备选（抽 JS 模块或上打包器）否决——违反「零构建」红线。

**D2 — 共享双产物渲染助手 `renderDualProduct(container, resp)`。** 一处实现「`rule` 常规卡 + `ai` 琥珀卡 + `ai_generated=false` 时降级说明」，4 页复用。服务端已注入 AI_NOTICE，故助手对 `ai` 逐字渲染、**不再前置标注**（防重复）；降级时展示 `degraded_reason` 人话（`provider_unconfigured`/`budget_exhausted`/`llm_timeout`/`llm_unavailable`/`insufficient_samples` 各配文案）。理由：6 项交付同吃 `DualProductResponse` 形状，集中实现把「琥珀 + 不重复标注 + 降级不静默」钉在一处。

**D3 — `warehouse_id` 会话存储 + 按端点注入。** 登录处理器补 `sessionStorage.setItem('warehouse_id', data.warehouse_id)`（`auth.LoginResponse` 已回传该字段）；新增 `aiBody(payload)` 助手合并 `{warehouse_id, ...payload}`，仅用于 6 个要求它的端点（`toggle` 只收 `{enabled}`，不套用）。`warehouse_id` 缺失时不出请求、直接 `setState('error', {text:'请重新登录'})`。备选（在 `window.api` 内无条件注入）否决——`toggle` 不收该字段，且会污染其它业务端点。

**D4 — 写意图二次确认卡。** 对话台响应 `write_intent=true` 时渲染确认卡（携带 `rule` 载荷），确认后才发写请求：③ 以 `suggestion_id` 调 `POST /api/llm/weight/apply`；④ 不直接写，走 D5 的落地边界。理由：红线 2「LLM 不直接执行写操作」。

**D5 — ④ 移库多方案：试算与落地分离。** ④ 卡片（对话台 `RELOCATE_PROPOSE` 与 `transfer.html`「多方案对比」）只并列展示三档方案（激进/均衡/保守）+ 量化代价（板数/车次/时长）+ `valid` 标记，**永不写**；用户选中某档后回填既有移库执行表单，复用 `relocate.operate` 的二次确认与台账写入流。理由：LLM 不直接执行写操作；移库执行属 `transaction-base` 既有流，不新增写端点。

**D6 — AI 开关接线 + 角色置灰，与预留开关隔离。** 给冷路径开关加 `data-toggle="ai"` 属性以区分「预留开关」（后者保持纯 CSS 态）；处理器按 `data-toggle` 分支：非 `admin` → 置灰禁用、不发请求；`admin` → `POST /api/llm/toggle {enabled}` 并回显返回的 `cold_path_enabled`。**无只读状态端点**，故开关是「动作驱动」：页面加载按默认关闭态渲染，每次切换后与返回值同步。备选（新增 GET 状态端点）否决——属后端改动、超出本次前端-only 范围，记入 Open Questions。

**D7 — 冷路径 409/403 与角色映射到四态异常。** `cold_path_disabled`（409）与 `permission_denied`（403）经 `setState('error', ...)` 呈现友好文案（非裸错误码）；入口按钮保持可点（页面加载时无 GET 可预知开关态），错误态即「降级不静默」的前端呈现。`planner` 角色无 `ai.*` 权限，其 AI 入口按既有 MENU 矩阵同一机制隐藏/置灰。

**D8 — 琥珀/确认卡样式复用既有令牌。** `styles.css` 新增 `.ai-card`（边框/底色取 `--amber`/`--amber-soft`）与 `.confirm-card`（中性底 + 主色确认钮），不新增任何色值——满足 `frontend-foundation`「设计令牌权威值一致」。加载态复用 `.spin` + `setState('loading')`。

## Risks / Trade-offs

- **[开关无只读端点 → 初始态可能失真]** 若此前已开启冷路径，页面加载仍显示「关」；首次切换后才与真实态对齐。→ 缓解：切换后即回显；如需加载即准确，另开后端小改动补 GET 状态端点（见 Open Questions）。
- **[`.cf-switch` 双用途 → 误接线]** 若把开关事件统一接 toggle，会污染「预留开关」。→ 缓解：`data-toggle="ai"` 精确作用域 + 回归断言预留开关仍只切 CSS。
- **[旧会话缺 `warehouse_id` → 首次 AI 调用 422]** 本次改动前登录的会话无该字段。→ 缓解：`aiBody` 前置守卫，缺字段时提示重新登录而非发请求。
- **[AI 标注重复前置]** 若各页自行拼标注，会与已注入的服务端标注叠加。→ 缓解：`renderDualProduct` 单点渲染 `ai` 原文，静态断言禁止客户端再拼标注。
- **[④ 试算与落地混淆]** 用户可能以为查看方案即执行移库。→ 缓解：卡片只读 + 落地显式回填既有 `relocate.operate` 二次确认流，未确认不产生 `JobOrder`/台账。

## Migration Plan

纯前端静态文件替换，无数据库迁移。部署 = 更新 `backend/frontend/` 下 HTML/CSS/JS；回滚 = 前端文件 git revert（配合功能开关式回滚：如 AI 输出不可用，管理员 `ai.toggle` 一键关闭冷路径，不删任何历史台账、不影响热路径）。无 `Ledger`/`WeightConfig` 之外的任何数据副作用（③ 采纳写入 `WeightConfig` 由后端 `weight/apply` 既有规则校验兜底）。

## Open Questions

- 是否新增 `GET /api/llm/toggle`（或等价只读状态端点），让开关与各 AI 入口在页面加载时即反映真实开启态？当前无只读端点，前端只能回显切换结果；若需要，属后端小改动，应另开 change，不在本前端-only 范围内。
