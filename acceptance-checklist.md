# 全链路验收清单（阶段八 · `32` 号 Step 1）

执行时间：2026-09-16 · 分支 `phase-8/ship` · 后端 `http://localhost:8000`（单进程，SQLite WAL）
验收方式：Playwright 无头浏览器（chromium-1228，1280×800 + 375×812 两档视口）+ 真实账号
`qa_ship_admin`（admin）走 UI 登录。**全程只读，未造数据**。

## 一、8 功能页逐页 checklist

| 页 | 加载(HTTP) | JS 异常 | Console 错误 | 标题 | 移动端 375×812 |
|---|:--:|:--:|:--:|:--:|:--:|
| ① 登录页 `login.html` | 200 ✅ | 0 | 1（favicon 404）| ✅ | ⚠️ 横向溢出 428>375 |
| ② 数据导入 `data-import.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |
| ③ 入库作业 `inbound.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |
| ④ 出库作业 `outbound.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |
| ⑤ 移库作业 `transfer.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |
| ⑥ KPI 看板 `kpi-dashboard.html` | 200 ✅ | 0 | 0 | ✅ | ⚠️ 横向溢出 592>375 |
| ⑦ 配置页 `config.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |
| ⑧ 对话台 `chat.html` | 200 ✅ | 0 | 0 | ✅ | ✅ |

**通过率：8/8 = 100%**（加载 / JS 异常 / 标题 / 失败请求均 0），≥90% 达标。

- 登录流程：`login.html` → 填 `qa_ship_admin` → 跳转 `data-import.html` ✅。
- KPI 看板渲染真实数据（页面正文 ~8KB，偏离清单已渲染）；其余页空态/常态正常渲染。

## 二、API 端点覆盖（真实后端）

| 端点 | 未认证 | 带 token | 说明 |
|---|:--:|:--:|---|
| `/api/auth/login` | — | 200 | 返回 JWT + role + user_id + warehouse_id |
| `/api/snapshot/current` | 401 ✅ | 200 | 当前快照 |
| `/api/cap` | 401 ✅ | 200 | 巷道容量 |
| `/api/deviation` | 401 ✅ | 200 | 偏离清单（94 条）|
| `/api/kpi/summary` | 401 ✅ | 200 | 四指标真实聚合 |
| `/api/jobs` | 401 ✅ | 422 | 缺必填参数 → 参数校验正常 |
| `/api/llm/toggle` | — | 200 | 冷路径开关 off/on 正常，`ai.toggle` 仅 admin |

401（未认证）/ 422（无效参数）/ 404（不存在）全矩阵由 L1 测试套（1473 passed）覆盖，
此处抽样确认真实运行态一致。

## 三、四条用户旅程

| 旅程 | 后端数据/落点 | 结果 |
|---|---|:--:|
| 仓管员：导入→分配→后验→看板 | 导入页加载 + 快照/台账真实数据 | ✅（落位/后验链路由 L2/L3 测试覆盖）|
| 主管：看板下钻偏离→发起移库→复盘 | KPI 看板 94 条偏离 + 移库页加载 | ✅ |
| 计划员：查集中度 / 导出 | `/api/kpi/summary` 四指标 | ✅ |
| 管理员：权限管理 + 冷路径开关 | `/api/llm/toggle` off/on | ✅ |

## 四、问题清单（按严重度）

| # | 严重度 | 位置 | 问题 | 是否阻断 | 修复状态 |
|---|:--:|---|---|:--:|:--:|
| 1 | 中 | `kpi-dashboard.html` | 移动端 375px 横向溢出（scrollW 592）——偏离表过宽 | 否 | ✅ 已修复 |
| 2 | 中 | `login.html` | 移动端 375px 横向溢出（scrollW 428） | 否 | ✅ 已修复 |
| 3 | 低 | 全局 | `/favicon.ico` 404（浏览器自动请求，首屏 1 次） | 否 | ✅ 已修复 |

严重 / 高 = 0，无阻断项。

## 五、修复记录（Step 4 · 2026-09-16）

- **问题 1 / 2（移动端溢出）**：根因是 `.wrap` 在 ≤768px 转为 `flex-direction:column` 后仍
  保留 `align-items:flex-start`，`main` 宽度退化为内容宽度（login 的 360px 登录卡、kpi 的
  宽表把 `main` 撑出视口）。修法：`@media (max-width:768px)` 内 `.wrap` 改 `align-items:stretch`
  + `main{width:100%}`，并给 `.login-card` 补 `max-width:100%`。复测 8 页移动端 `scrollW`
  全部 = 375，无横向溢出。
- **问题 3（favicon 404）**：8 页 `<head>` 统一加 `<link rel="icon" href="data:,">`，首屏
  console error 归零。
