# CLAUDE.md

成品库位智能推荐（康饮台塑立体库）—— 物料号级「集中就近落位」推荐引擎。
单端、单主流程、单厂试点（`GTJ10036`）。

**本项目的工程立场**：AI CODING = 规范驱动 + OPSX 推理链。
不是"一句话让 AI 生成功能"，而是可复现、可审查的工程流程。

---

## 一、动手前必读

| 要知道什么 | 去哪里 |
|---|---|
| 领域术语（中英文 + 一句话定义） | `CONTEXT.md` |
| 项目约束的完整版（给 OpenSpec 产物用） | `openspec/config.yaml` 的 `context` 字段 |
| 设计与事实来源（**不在本仓库内**） | `D:\成品库位智能推荐\产品设计\` 下的 `00`、`09`~`25` 号文档 |
| 工具链、阶段划分、质量门禁 | `D:\成品库位智能推荐\产品设计\00-总体开发方案.md` |
| 数据模型 23 实体 / 11 枚举 | `17-数据模型设计.md` |

**设计文档是事实来源，代码是它的派生物。** 设计文档与代码冲突时，先确认哪个对，不要默认改代码。

文档编号速查：`13` 权限 · `14` 推荐引擎 · `15` 三类作业与后验 · `16` 数据衔接与 cap ·
`17` 数据模型 · `18` KPI · `19` 架构 · `20` 评测 · `21` 设计系统 · `22` 前端规格 ·
`23` 十一维自检 · `24` 原型。

---

## 二、常用命令

```bash
# 启动（必须单进程，见第四节"SQLite"）
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 测试
python -m pytest tests/ --tb=short -x -q          # L1 单元（<5s，pre-commit 门禁）
python -m pytest tests/logic -m logic             # 只跑逻辑测试（6 因子/状态机/cap）

# 建库与种子数据
python scripts/init_db.py                         # 阶段二实现
python scripts/seed_dev.py                        # 阶段二实现

# Evals（阶段六实现；现在跑会以退出码 3 失败，这是刻意的）
python evals/run_evals.py --list
python evals/run_evals.py --tier all --compare evals/baseline.json

# OpenSpec
openspec list
openspec new change <name> && openspec instructions <artifact> --change <name> --json

# no-mistakes（详见第十二节）
no-mistakes doctor                                # 体检
no-mistakes status                                # 本仓库门禁状态
git push no-mistakes <branch>                     # 推过门禁，触发流水线
```

环境已就绪：Python 3.14.4 · git 2.55.0 · node v20.20.2 · OpenSpec 1.13.0 · Gstack 1.60.1.0。
**尚未执行**：`git init`、创建 venv。

---

## 三、架构与目录

五层架构（`19` §2.2）：

```
① 数据衔接层  backend/app/importer/ , backend/app/cap/
② 应用服务层  backend/app/engine/ , backend/app/services/
③ 前端层      零构建静态页（阶段七，文档 31）
④ 冷路径      backend/app/llm/（默认关闭）
  横切        backend/app/core/ , models/ , schemas/ , api/
```

```
backend/
├── app/
│   ├── main.py          应用装配：中间件、路由注册、异常映射
│   ├── core/            config(db/security/enums/errors) —— 横切关注点
│   ├── models/          23 实体，按 17 号四条数据链分组
│   ├── schemas/         Pydantic DTO（含 17 §10 的 6 类 JSON 结构）
│   ├── api/             routes/ + deps.py + middleware.py + permissions.py
│   ├── engine/          6 因子评分 · 批量竞争分配 · 预留池 · 降级链
│   ├── services/        inbound / outbound / relocate / verify / ledger / kpi
│   ├── importer/        四类文件导入管线（格式探测 → 映射 → 四层校验 → 分流）
│   ├── cap/             基线全量重算 · 事务增量 · 对账 · 告警
│   └── llm/             冷路径（脱敏 · 4 类能力 · 成本护栏）
├── tests/               models / api / logic —— 对应三层 TDD
├── evals/               golden / l1_unit / l2_integration / l3_quality + baseline.json
└── scripts/             init_db · seed_dev · check_commit_msg（commit-msg 钩子）
```

---

## 四、不可违反的红线

这些是设计决策，**不是建议**。任何 change 都不得绕过；需要改动时先改设计文档。

### 决策权在人（L1 软推荐）
- 系统只给建议与理由，**绝不静默改写落位**。
- 写操作（落位 / 出库确认 / 移库执行）**必须弹二次确认卡**；**未确认不产生台账**。
- 方案须**逐单确认**，不自动落位。

### 核心链路不出域
- 评分 / 落位 / 后验 / 台账全部**本地确定性计算**，不依赖任何外部 LLM / 搜索 / API。
- **同样输入必得同样输出**：不用随机搜索，不用大模型生成落位。
- 冷路径 LLM 仅外发**脱敏后**的意图文本；默认关闭，且必须可一键关闭。

### 冷路径的三条不可逾越红线（`10`）
1. LLM 不参与实时评分与排序
2. LLM 不直接执行写操作
3. LLM 产出不经规则校验不进台账

AI 输出必须渲染为琥珀色并标注「AI 建议，仅供参考，需人工核实，不自动执行」，
**不得渲染成系统结论**。

### 数据与状态
- **台账只有一套**（`Ledger`），由引擎 / 作业流自动写；`ledger.write` 与 `engine.invoke`
  **不向任何角色开放手动入口**。不新增平行台账。
- 同一作业单 `EXECUTED` 后**不允许重复写台账**（状态机守卫 + 乐观锁）。
- **出库是只读派生**：顺路取不触发评分引擎、不重新决定落位。
- **移库不改批号**：只调整所在巷道。
- **非 A 类不得占用近站台预留池**。
- 快照缺失或过期（出库）→ **阻断并提示重新导入，不猜测落位**。
- 校验失败 → **阻断导入，不得带病入库**。
- **降级不静默**：任何降级都必须在推荐理由中写明 `degrade_reason`。
- 服务端**不做多 worker 并发写**（见下）。

### SQLite 并发约束
WAL 下多读单写。应用**必须单进程运行**，**不得加 `--workers`**。
写并发由 `JobOrder` / `ImportSession` 的乐观锁版本号保证，`busy_timeout` 仅兜底。
「cap 与台账同事务写入 + 整体回滚」**必须由真实事务保证**，不得用应用层补偿代替 ——
否则会破坏「台账是 cap 增量唯一来源」这个不变量。

---

## 五、技术栈（既定决策，不要重新论证）

| 层 | 选型 |
|---|---|
| 后端 | Python + FastAPI + SQLAlchemy 2.0 风格（`DeclarativeBase` / `Mapped`） |
| 数据库 | **SQLite** 单库 + WAL（`foreign_keys=ON` 必须显式打开） |
| 文件解析 | pandas + openpyxl（xlsx）· 标准库 csv |
| 前端 | **零构建**：原生 HTML + CSS 变量令牌 + Vanilla JS。无框架、无组件库、无构建工具 |
| 中间件 | **无** Redis / RabbitMQ —— 任务队列用本地库状态表 |
| 外部接口 | **0 个实时接口**；与 WMS / SAP 仅文件级衔接 |
| 认证 | JWT（HS256）8 小时 · bcrypt 哈希 |

选型来源：`09` 号注册表「25-环境搭建」+ 团队确认。形态来自 `19`，选型来自 `09`。

**密码哈希不要用 passlib** —— 本机 passlib 1.7.4 + bcrypt 5.x + Python 3.14 下调用即抛
`ValueError`。直接用 `bcrypt`，见 `app/core/security.py`。

前端仅用于评审/原型（`25` 附录A）。**前端方案必须保持零构建**，不引入框架或打包器。

---

## 六、工具链与工作流

五层工具（`00-总体开发方案` §一）：

```
思考层  /office-hours  /plan-ceo-review  /plan-eng-review     ← Gstack
规格层  /opsx:propose → /opsx:apply → /opsx:archive           ← OpenSpec
实现层  写测试(RED) → 生成实现(GREEN) → 重构                    ← TDD
质量层  L1 单元 → L2 集成 → L3 Golden → 基线对比               ← Evals
流程层  /review /cso /qa /ship /retro /investigate            ← Gstack
门禁层  /no-mistakes  (git push no-mistakes <branch>)          ← no-mistakes（独立工具，见第十二节）
骨架层  branch commit tag merge revert push                    ← Git
```

`09`~`25` 已完成详细设计，所以**多数阶段跳过思考层，直接 propose**。
仅核心引擎（`14`）容量与降级链定型时保留 `/plan-eng-review`。

### 每个阶段的标准流程（`00` §2.3）

```
1. git checkout -b phase-N/xxx
2. /opsx:propose "实现XXX功能"
3. /opsx:apply          内部 TDD 循环：写测试 → 生成实现 → pytest → commit
                        Evals 检查：通过则 tag，劣化则 /investigate
4. /review
5. /qa browse（如有 UI）
6. git checkout main && git merge --no-ff phase-N/xxx && git tag -a v0.N.0
7. /opsx:archive
```

### 阶段与版本节奏（`00` §2.1 / §4.4）

| 版本 | 阶段 | 内容 |
|:--:|---|---|
| v0.1.0 | 一（文档25） | 环境搭建 + 项目骨架 ← **当前** |
| v0.2.0 | 二（26 · 设计17+13） | 23 个模型 + JWT + 权限矩阵 |
| v0.3.0 | 三（27 · 设计14） | 6 因子引擎 + 批量分配 + 降级链 |
| v0.4.0 | 四（28 · 设计15+16） | 三类作业管线 + 文件导入 + cap 自维护 |
| v0.5.0 | 五（29 · 设计10） | 冷路径 LLM（脱敏 + 审批） |
| v0.6.0 | 六（30 · 设计18+20） | Golden 数据集 + Evals + Go/No-Go |
| v0.7.0 | 七（31 · 设计22+21） | 8 页 Vanilla + 对话台 + 设计令牌 |
| v1.0.0-rc.1 | 八（32） | 全链路验收 + 安全审计 |
| v1.0.0 | 八（33） | 灰度发布 + 复盘 |

关键路径：`F1 → F2 → F9 → F3 → F4 → F5 → F6 → F7 → F8 → F10`，缺一不可。
**27 必须先于 28**（引擎产出被交易消费）；**26 必须最先**（数据模型 + 权限）。

### Git 规范（`00` §4.2）

```
<type>(<scope>): <subject>
type : feat / fix / test / refactor / docs / chore / perf
scope: model / auth / engine / job / import / cap / eval / ui / env / golden-NNN
```

`env` 为 2026-09-11 新增，`00` §4.2 正本与 `check_commit_msg.py` 已同步。
它专用于**环境搭建 / 脚手架**等非子系统改动（阶段一）；其余 scope 均对应一个子系统。
**scope 必填** —— 校验正则硬要求括号，`chore: xxx` 不通过。

原子化 commit，**不攒批提交**。每完成一个模型/组件就提交。
门禁：`pre-commit` 跑 pytest L1（<5s）· `commit-msg` 校验格式 · `pre-push` 跑 Evals（>5% 劣化阻断）。

---

## 七、编码约定

- **实体名 PascalCase**，**表名 snake_case 复数**（`JobOrder` → `job_orders`）。
- **字段名 snake_case**；**枚举值以 `app/core/enums.py` 为准，不得增删** ——
  `Role` 与 `AccountStatus` 是 lowercase，其余 UPPERCASE，不要"顺手统一"。
- 开发阶段决策（`17` 号明确留给开发阶段）：整型自增 `id` 作代理键 + 业务键唯一约束；
  **不做软删除**（设计用「归档不删除 + 版本化」，不引入 `is_deleted`）。
- 全部实体携带 `warehouse_id` 过滤。
- **库位号按文本读取**（6 位，前 2 位 = 巷道，聚合对 `[:10]` 切片 → 实为 `[:2]`）；
  Excel 数值化会丢前导 0，日期序列号须先转日期。**严禁按列序号硬取字段**。
- 业务错误走 `app/core/errors.py` 的 `DomainError` 体系；
  **降级不是异常** —— 降级用 `degraded` / `degrade_reason` 表达。
- 每个模块的 docstring 已注明事实来源文档号与章节，**改动时同步更新**。
- 注释密度：解释「为什么」和设计文档依据，不复述代码在做什么。

### 三层 TDD（`00` §3.1）

| 层 | 内容 | 测试位置 |
|---|---|---|
| 模型 | CRUD + 约束 + 默认值 | `tests/models/` |
| API | 端点行为 + 认证 + 错误处理 | `tests/api/` |
| 逻辑 | 6 因子评分 / 批量分配 / 状态机 / cap 计算 / 降级链 | `tests/logic/` |

**先写测试（RED），再生成实现（GREEN）。** 逻辑测试保留完整代码。

---

## 八、权限（4 角色）

`warehouse_keeper` 仓管员 · `planner` 计划员 · `supervisor` 主管 · `admin` 管理员。

矩阵的权威定义在 `app/api/permissions.py`（`13` §2.2 的逐条搬运）。
**v1 事实：细粒度 RBAC 部分落地** —— 角色菜单可见性 + 写操作二次确认，外加
`POST /api/allocate/batch` 的端点级资源鉴权（`inbound.operate`，仅仓管员/管理员，经
`require_permission` 依赖）；该矩阵是目标模型，其余端点的 RBAC（M5）落地后由后端 checker 强制。

三层检查：认证中间件（v1，401）→ PermissionChecker（`/api/allocate/batch` 已生效，其余端点路线图，403）→ 操作确认（v1，前端确认卡）。
白名单：`/api/auth/login`、`/health`、`/docs`、`/openapi.json`。

---

## 九、不做清单（不得加回）

通用 WMS 替代 · 接管落位决策 · 板号级推荐 · 批号级推荐 · 待检状态转换与品管联动 ·
平置仓 / 环穿立库 · 与 WMS 实时同步 · 系统间实时接口对接 · 核心链路依赖外部大模型或数据出域 ·
冷路径未经脱敏即调外部 LLM · 自主规划型 Agent（静默执行写操作）。

后三项是 **Kano 反向质量** —— 做了反而损害产品价值观（确定性、可审计、数据不出域）。

另明确不做：实时流式计算 · 跨厂对比统计 · KPI 阈值自学习 · LLM 生成 KPI · 指标对外推送 ·
A/B 测试（用前后对比）· 用户行为埋点。

---

## 十、验收基线（写规格与代码时落到具体数字）

| 指标 | 阈值 |
|---|---|
| 拣货量加权集中度 | 80% 拣货量落在 ≤5 巷道（**统一验收指标**） |
| 同物料跨巷道 | ≤5 |
| 同批跨巷道 | ≤3 |
| 推荐采纳率 | ≥60% |
| 落位准确率 | ≥99% |
| 北极星：周有效收拢入库批次 | 试点期 ≥30 |

Go/No-Go 闸门：集中度达成率 ≥70% 且趋势向好 + 护栏全过 → Go；任一护栏失败 → 立即回滚
（功能开关式，**不删历史台账**）。

性能 SLA：解析 ≤5min/文件 · cap 查询 ≤100ms · 单物料评分 ≤1s · 落位写入 ≤200ms ·
后验 ≤1s · 看板刷新 ≤3s。

---

## 十一、当前状态与待办（阶段三）

**阶段一、二已收尾并打标（`v0.1.0` / `v0.2.0`）。阶段三实现在 `phase-3/engine` 分支上：
主体已完成，尚未合并、尚未打 `v0.3.0`。**

**阶段一（`v0.1.0`）**：`openspec/` 已初始化且 `config.yaml` 已填 · `backend/` 骨架 · 本文件 ·
`CONTEXT.md` · `git init`（`main` 分支，origin = 本地裸仓库 `D:\成品库位智能推荐\warehouse-origin.git`）·
no-mistakes 已 `init`（二进制本机已存在，见第十二节）· graphify 已验证端到端可用（见第十三节）·
新增 `env` scope（`00` §4.2 正本 + 校验脚本同步，见第六节）·
venv（`backend/.venv`）已建、`requirements.txt` 已装 ·
**pre-commit 三类钩子已装并实测通过**（装法见下）·
`backend/tests/api/test_smoke.py`：9 条装配冒烟测试（装配完整性 / 认证中间件 / 白名单）·
换行符统一为 LF（`.gitattributes`）· 首次提交 `6fdc73c`

**阶段二（`v0.2.0`）**：23 个实体（按 `17` 四条数据链分组）·
Alembic 迁移链（`backend/migrations/versions/`，7 个修订）·
JWT（HS256）认证 + 权限矩阵骨架（`13` §2.2 的逐条搬运）· 乐观锁校验辅助 ·
`backend/scripts/init_db.py` / `seed_dev.py` · **546 passed**。
变更 `data-model-permission` 已归档，主规格 `openspec/specs/{auth,data-model,permission}/spec.md`
已由 delta 写入

**阶段二/三交界**：`InventoryItem.zone` 已清退（变更 `retire-zone-column`，迁移 `99ed4f7e48cd`）——
该列的两处文档来源（`17` §3.3 字段枚举、`16` A.1 INV 模版）都已清退，且全仓从无读写方
（无 DTO 字段、无 importer 映射、无因子读它），留一个无来源又无消费方的列只会让
「实体字段以 `17` 为准」这条口径持续失真。该变更**尚未归档**（分支已合并、已删）

**阶段三（`27` · 设计 `14`）**：`app/engine/` 六个模块 —— 因子取数与归一化（`factors.py`）、
权重 / 候选集 / 综合评分（`scoring.py`）、近站台预留池（`reserved.py`）、四级降级链
（`degradation.py`）、队列优先级（`priority.py`）、批量竞争分配主循环（`allocator.py`）；
推荐理由的可追溯契约（`reasons.py` + `schemas/reason.py`，含每巷 × 每因子取值与两种降级标记）；
`POST /api/allocate/batch`（`app/api/routes/allocate.py`）。
**本阶段无真实数据**（导入管线属阶段四），四类输入全空是预期状态（`16` §394）。
当前 **784 passed**（本次墙钟 4.82s；本阶段多次实测 4.8–5.7s，**横跨 `00` 的 `<5s` 预算线**，
钩子不强制，见记忆条目 `l1-budget-pressure`），其中逻辑层 493 条 / 约 1.5s。
变更 `recommendation-engine` 的任务 **40/40 完成**（全部实现并变异验证），**已提交**
（本阶段 3 条原子提交：引擎与测试 / 批号口径订正 / 变更产物）—— **尚未合并、尚未打
`v0.3.0`、尚未归档**。`openspec validate` 退出 0（13 条 `⚠` 全是 RFC2119 假警报，见记忆条目）。

**D14 的默认值**：四项里 **N 已确认 = 3 天**（业务方 2026-09-12；正本 `14` §3.2，
落地 `priority.DEFAULT_OUTBOUND_WINDOW_DAYS`）。**本阶段零行为变更** —— 阶段三无取数来源，
`outbound_qty` 由调用方给出，N 到阶段四聚合落地才参与计算。仍待确认三项：优先级权重系数、
板-格换算规则、分档阈值与溢出区形态（`grep -rn "TODO(design.md D14" backend/app/` 得全量）。
另注意 `16` §360 的「ABC 统计窗口 / N 天」是**另一个 N**，尚未确认

**未完成**：

- `backend/evals/run_evals.py` —— 阶段六实现；现在跑刻意以退出码 3 失败
- 四类文件导入管线与 cap 自维护（`app/importer/` / `app/cap/`）—— 阶段四
- 三类作业管线与台账（`app/services/`）—— 阶段四
- 冷路径 LLM（`app/llm/`）、KPI 看板、前端 8 页 —— 阶段五 / 六 / 七
- 两条**已合并**的本地分支仍在（`phase-2/data-model-permission`、
  `clarify/401-failure-semantics`）—— 待清理

### pre-commit 钩子：装之前先读这条

钩子**必须**这样装（在 `backend/` 下执行）：

```bash
PYTHONUTF8=1 pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

**漏掉 `PYTHONUTF8=1` 会让之后每一次提交都失败**，且报错是误导性的
「pre-commit not found. Did you forget to activate your virtualenv?」。

原因：本仓库路径含非 ASCII（`成品库位智能推荐`），而 pre-commit 以 locale 编码
（本机 cp936）写钩子脚本，Git 的 sh 却按 UTF-8 解释 → 脚本内那条绝对路径解析不出来。
`PYTHONUTF8=1` 让 Python 以 UTF-8 写脚本，从源头消除错配。
**完整成因与另一处 entry 路径 bug 见 `backend/.pre-commit-config.yaml` 头部注释。**

**已知待确认项**（见交付说明，不要擅自"修正"）：

- `GET /api/health` 在 `19` 附录B 有定义，但 `13` §6.1 白名单只列 `/health`。
  当前实现按 `13` 字面执行 —— **探活请用 `/health`**。
- 原型页 `assets/styles.css` 的令牌取值与 `21`/`24` 的规范值不一致（`21` §3.3 声明
  以本文为准）；前端的单文件 vs 拆页形态也尚未决策 —— 留待阶段七。

---

## 十二、no-mistakes

`00` §五 把 `/no-mistakes` 列为文档 33 的流程层工具。**它不是 Gstack 的一部分**，是独立工具。
2026-09-11 在本项目完成 `init`。

| 项 | 值 |
|---|---|
| 二进制 | `C:\Users\E0764\.local\bin\no-mistakes.exe`（**PATH 上已有，2026-08-01 起就在**） |
| 构建源 | 课程资料包 `no-mistakes-main` 快照（Jul 25，552 个 .go 文件） |
| Go 工具链 | `D:\tools\go`（绿色解压，未改系统 PATH；仅重建/升级时用得上） |
| 数据目录 | `C:\Users\E0764\.no-mistakes`（用户级） |
| **origin** | `D:\成品库位智能推荐\warehouse-origin.git`（本地裸仓库 —— 本机 `github.com` 不通） |
| gate | `C:\Users\E0764\.no-mistakes\repos\da317198e22a.git` |
| skill | `C:\Users\E0764\.claude\skills\no-mistakes\`（**用户级，全机器可用**） |

推送流程：`git push no-mistakes <branch>` —— 推到本地 gate，触发流水线（审查/测试/lint/文档/PR）。

> 二进制版本号显示 `dev (unknown)`：2026-08-01 那次是裸 `go build`，没走 Makefile 的
> `-ldflags`，所以没有版本号。**不代表它过期** —— 2026-09-11 用同一份源码重建比对，
> 命令列表 md5 完全一致。

### 两条外发通道（已处理）

| 通道 | 目标 | 关闭方式 |
|---|---|---|
| 遥测 | `a.kunchenguid.com` | **构建期**：未注入 `TelemetryWebsiteID`，二进制走 `noopSink`，不建 HTTP client |
| 版本检查 | `api.github.com/.../releases/latest` | **运行时**：用户级环境变量 `NO_MISTAKES_NO_UPDATE_CHECK=1` |

> 构建期那道锁的原理：`telemetry.Default()` 在读到空 `websiteID` 时**先于**解析 host 返回
> `noopSink`，所以硬编码的兜底域名 `a.kunchenguid.com` 虽仍在二进制里，代码路径不可达。
> **注意**：环境变量优先级高于构建期 —— 别在别处设 `NO_MISTAKES_UMAMI_WEBSITE_ID`，那会
> 把遥测重新打开。用户级已设 `NO_MISTAKES_TELEMETRY=off` 作为第二道锁。
>
> 版本检查那条只上报「no-mistakes 在本机跑过」，**不含任何项目数据**，不是「数据不出域」
> 红线问题；设锁只是洁癖级收尾。

### 必须知道的行为

- **`intent.enabled: true`（全局默认开启）**：push 分支时它会**读取本机 Claude Code 的会话
  记录**，挑出产生该改动的会话，摘要成「用户意图」喂给各步骤的 agent。数据不出机器，
  但读取范围是完整会话记录。要关就改 `C:\Users\E0764\.no-mistakes\config.yaml`。
- **常驻 daemon**：`no-mistakes daemon status` 查，`daemon stop` 停。
- `gh` 未安装 → 流水线的 **PR / CI 环节不可用**（`doctor` 标为 optional）。gate 与本地
  流水线不受影响。
- 项目 `.claude/` 下是 **OpenSpec 的 skill**（`openspec-*`），与 no-mistakes 无关。

---

## 十三、graphify（代码知识图谱）

2026-09-11 完成端到端验证。**已安装且可用**，非本仓库依赖——是通用工具。

| 项 | 值 |
|---|---|
| CLI | `C:\Users\E0764\.local\bin\graphify.exe`（0.9.25，已在 PATH） |
| skill | `C:\Users\E0764\.claude\skills\graphify\`（用户级） |
| 产物 | `backend/graphify-out/` —— **已 gitignore**（见根 `.gitignore`） |

### 用法

```bash
graphify extract ./backend --code-only   # 纯 AST，无 LLM / 无 key / 不联网，约 4 秒
graphify cluster-only ./backend --no-label   # 生成报告与 HTML，跳过 LLM 社区命名
graphify god-nodes                       # 架构枢纽
graphify query "…" / explain "X" / path "A" "B" / affected "X"
graphify diagnose multigraph             # 完整性门禁，只读
```

验证结果：78 个代码文件 → 237 节点 / 239 边 / 66 社区，`Token cost: 0 input · 0 output`，
完整性门禁全 0（无悬空边 / 自环 / 重复边）。

### 两条必须知道的事

- **`graph.html` 不自包含**：唯一的 `https://unpkg.com/vis-network@9.1.6/...` 是**打开页面时**
  从 CDN 拉库。图谱数据不上传（单向下载），但会在网络层留下记录。介意就不开 HTML，
  用 `query` / `explain` 等 CLI 子命令。
- **社区名默认是 `Community N` 占位符**：真名要跑 `graphify label`，那步调 LLM。本机
  `GEMINI_API_KEY` 等云 key **全部未设置**，所以会落到本地 ollama 或 host agent——**不外发**。

### 未做（需要时再说）

`graphify claude install` 会往本文件写一节 **并装 PreToolUse 钩子**（改变本项目的 agent 行为），
本次**没有执行**。要装再单独决定。
