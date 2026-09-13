# 设计决策

> 事实来源：`21-设计系统` / `22-前端页面设计规格` / `13-权限分级与访问控制系统`。基线：`backend/frontend/`（8 页 HTML + `assets/styles.css` + `assets/app.js`）。

## D1. 令牌纠偏只改值、不动结构

`styles.css` `:root` 已含 12 个色令牌 + 3 个阴影令牌，变量名与 `21` §3.3 逐一对得上，只色值偏离。故纠偏**只替换色值、不增删变量、不改组件结构**——这符合 `31` 号的「仅修正偏差、不整体重构」。

三处「派生冲突色」也一并修，理由与令牌同源：`24` §6.3 说「生成时所有色值取自 21 号 `:root`，禁止硬编码冲突色值」。

| 位置 | 现状 | 权威值 | 出处 |
|---|---|---|---|
| `.pill.ok` 背景 | `#eaf5ee` | `#e6f6ec` | `21` §3.4 极浅绿 |
| `.btab th` 背景 | `#eef4fc` | `#f3f8ff` | `21` §7.6 表头蓝浅底 |
| `.dock` 阴影 | `rgba(91,141,239,.4)` | `rgba(47,111,237,.4)` | 旧 `--blue` 的派生 |
| `.qs.l2` 描边 | `#a99bc8` | `#5b4b96` | `24` §6.3 紫灰 |
| `.reason .rl b` 列宽 | `72px` | `64px` | `22` §三 |

`header` 底色 `#2a3f5c`（深藏青）不在 `21` 令牌集里、也不与任何令牌冲突（它是壳级品牌条，`21` §3.2 未定义 header 色），**不改**——不在「冲突色值」之列。

## D2. 角色菜单过滤是「导航渲染过滤」，不是访问控制

`13` §三 明说 v1 通过「前端导航渲染时按当前角色过滤菜单」实现可见性控制，后端 RBAC 是路线图。故本层只**隐藏 `nav` 菜单项**（加 `.hidden` class），**不拦截**直接 URL 访问（那是 28/路由与后端 RBAC 的职责）。

`.hidden`（class，specificity 0,1,0）能可靠盖过 `nav a{display:flex}`（0,0,2）；用 `hidden` **属性**则会被 `nav a{display:flex}`（author 样式恒胜 UA 样式）覆盖，故**不用 `hidden` 属性**。

## D3. 角色来源 = 登录壳的演示下拉 + sessionStorage

真实认证（JWT + 角色回传）归 `28`。本层登录壳是演示登录，故在登录卡加一个「角色（演示）」下拉（4 角色，默认 `admin`），登录后 `sessionStorage.setItem('role', …)` 再跳转。`getRole()` 读 `sessionStorage`，无角色（未登录）不过滤、显示全部 8 项（登录页作原型总览）。

角色取值严格用 `13` §一 的枚举：`warehouse_keeper` / `planner` / `supervisor` / `admin`，不自行造名。

## D4. 菜单矩阵以 `13` §3.1 为唯一权威

`app.js` 的 `MENU` 映射逐行抄 `13` §3.1：

| 角色 | 可见 `data-tab` | 数 |
|---|---|---|
| `warehouse_keeper` | p1 p2 p3 p4 p5 p6 p8 | 7 |
| `planner` | p1 p2 p6 p8 | 4 |
| `supervisor` | p1 p4 p5 p6 p7 p8 | 6 |
| `admin` | p1~p8 | 8 |

（配置页 p7：仅 admin 全开、supervisor 可见、keeper/planner 不可见。数据导入 p2：keeper/planner 可见、supervisor 不可见。入库 p3：keeper/admin 可见。出库 p4：keeper/supervisor/admin 可见。移库 p5：keeper/supervisor/admin 可见。）

过滤同时**收起被清空的组标签**（`.grp` / `.nt`）：某组下所有 `a` 均被隐藏时，该组标签一并隐藏（否则计划员会看到孤立的「— 三类作业 —」标签）。

## D5. 四态是全局样式 + 触发钩子，本层不注入各页

四态（`21` §7.11）的口径权威 = `11` §五 + `22` 各页状态表。本层只交付：

- **全局样式**：`.state`（占位容器）+ `.state.empty` / `.state.loading` / `.state.success` / `.state.error` 四态 + `.spin`（加载转圈）。
- **触发钩子**：`app.js` 的 `setState(el, state, opts)`——把容器切到四态之一（空态引导文字、加载态转圈、成功态绿字、异常态红字 + 重试）。

**不在本层**把四态注入各页正文（哪页何时切哪态由 `28` 组装时接）。冒烟只验 class 齐备 + 钩子可达。

## D6. `.node` 只就位组件，不注入作业页

`.node` 三态（entry 蓝 / exec 绿 / ledger 琥珀，`21` §7.2/§7.8）在 `styles.css` 就位，配 `.flow` 管线容器 + `.flow .arrow` 连接。作业页（入库/出库/移库）的管线可视化由 `28` 组装时消费。冒烟只验三态配色对齐 `21`。

## D7. 组件冒烟用 pytest 静态断言（无浏览器）

本层「无业务指标、无浏览器验收」（`31` 号完成标准）。冒烟用确定性静态断言代替视觉：

- 令牌值：读 `styles.css` `:root` 块，逐键断言 12 色值与 `21` §3.3 相等（**值写死在用例里**，不与被测方共用一份——与 `recommendation-engine` 的六因子名同源手法）。
- 菜单矩阵：读 `app.js` 断言 `MENU` 四键、每键的集合与计数（7/4/6/8）与 `13` §3.1 一致。
- 四态 / `.node`：断言 `styles.css` 含 `.state.empty/loading/success/error`、`.node.entry/exec/ledger` 且三态分别引用 `--blue/--green/--amber`。

视觉一致性（令牌生效后整站观感）留人工核验，不在 CI 断言。
