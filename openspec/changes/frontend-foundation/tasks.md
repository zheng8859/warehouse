> 排序说明：本变更按 `31` 号「先核对、后补缺」的顺序：令牌纠偏（修偏差）→ 角色菜单可见性 → 四态占位 → `.node` → 冒烟。
> 验证约定：本阶段一律 `backend/tests/` 下的 pytest 静态断言（`31` 号「无业务级 /qa，仅组件冒烟」）。视觉一致性留人工核验。

## 1. 令牌纠偏（偏差修正，以 21/24 为权威）

- [ ] 1.1 `assets/styles.css` `:root` 的 12 个色令牌色值对齐 `21` §3.3（=`24` §3.1）：`--bg #f5f7fa` / `--ink #1f2d3d` / `--muted #6b7a90` / `--line #c9d4e0` / `--blue #2f6fed` / `--blue-soft #e8f0fe` / `--amber #e0a000` / `--amber-soft #fff7e0` / `--green #1f9d55` / `--red #d64545` / `--grid #e4ecf5`（`--panel #fff` 已一致）。验证：`tests/frontend/` 断言 12 色值逐键相等（值写死在用例里）
- [ ] 1.2 三处派生冲突色 + L2 描边 + `.reason` 列宽对齐：`.pill.ok` 背景 `#e6f6ec`（21 §3.4）、`.btab th` 背景 `#f3f8ff`（21 §7.6）、`.dock` 阴影 `rgba(47,111,237,.4)`、`.qs.l2` 描边 `#5b4b96`（24 §6.3）、`.reason .rl b` `flex:0 0 64px`（22 §三）。验证：同 1.1 的静态断言覆盖这些点

## 2. 角色菜单可见性（13 §3.1 前端过滤）

- [ ] 2.1 `app.js` 落 `MENU`（四角色 → `data-tab` 集合，逐行抄 13 §3.1）+ `getRole()`（读 `sessionStorage`）+ `applyMenuFilter()`（隐藏非可见 `a[data-tab]` 与空组标签）。验证：`tests/frontend/` 断言 `MENU` 四键、计数 7/4/6/8、各集合与 13 §3.1 一致
- [ ] 2.2 `login.html` 登录卡新增「角色（演示）」下拉（`warehouse_keeper`/`planner`/`supervisor`/`admin`，默认 `admin`）；`app.js` 登录处理器写 `sessionStorage.role` 再跳转。验证：登录处理器读该下拉、写入 `sessionStorage`（静态断言 + 逻辑可达性）

## 3. 四态占位系统化（21 §7.11）

- [ ] 3.1 `styles.css` 落 `.state` 容器 + `.state.empty` / `.state.loading` / `.state.success` / `.state.error` + `.spin` 转圈。验证：`tests/frontend/` 断言四态 class 齐备、`.spin` 存在
- [ ] 3.2 `app.js` 落 `setState(el, state, opts)` 触发钩子（切空/加载/成功/异常）。验证：钩子可达、四态 class 正确切换（逻辑可达性，不接各页）

## 4. `.node` 流程状态卡（21 §7.2 / §7.8）

- [ ] 4.1 `styles.css` 落 `.flow` 容器 + `.node`（entry 蓝 / exec 绿 / ledger 琥珀）+ `.flow .arrow` 连接。验证：`tests/frontend/` 断言 `.node.entry/.exec/.ledger` 存在且分别引用 `--blue` / `--green` / `--amber`

## 5. 组件冒烟

- [ ] 5.1 `backend/tests/frontend/test_frontend_foundation.py` 汇总静态断言（令牌值 / 菜单矩阵 / 四态 / `.node`），跑通并计入全量
