# evals 设计

## Context

阶段一~五已交付全部业务能力（6 因子引擎、三类作业管线、数据衔接、冷路径），且评测框架的**骨架已就位**：`backend/evals/` 下有 `run_evals.py`（刻意以退出码 3 失败）、`baseline.json`（试点前 Golden 基线 + 空通过率）、`golden/README.md`（空目录占位）、三个空测试包 `l1_unit/l2_integration/l3_quality`；`tests/logic/test_run_evals.py` 守着「exit 3」骨架契约；`.pre-commit-config.yaml` 里 `evals-baseline` pre-push 钩子注释着待启用。动机见 proposal.md。

本阶段无真实数据（阶段四已明确四类输入全空）。可复用的口径函数已存在：`services/kpi.py`（`same_material_cross_aisle_mean`/`weighted_concentration`/`adoption_rate`）、`engine/degradation.py`（`resolve_tiers`/`is_alarming`）、`engine/scoring.py`（`load_weights`/`score_aisles`/`select_best`）、`engine/factors.py`（`aisle_of`）、`llm/redact.py`（`redact`）。

## Goals / Non-Goals

**Goals:**
- 把 30号 的三层评测金字塔落成可运行的 `run_evals`，产出通过率 + 基线对比 + 劣化判定 + P0 护栏阻断。
- Golden 数据集 60 场景可溯源到 20号 编号与 14/15/18 公式。
- 冷路径 LLM 仅观测，不污染任何通过率门槛。
- 启用 pre-push 门禁（取消注释 `evals-baseline`）。

**Non-Goals:**
- 不采集真实试点数据、不做真实 Go/No-Go 判读（v1.0 试点期 runtime）。
- 不新增业务实体/API/KPI 指标，不新增平行台账。
- 不把 L3 设成简单百分比门槛。

## Decisions

### D1 — Golden 样本 = 设计推导的确定性 fixture（无真实数据）

`golden/README.md` 的「样本来自 GTJ10036 历史导出切片」不可满足（本项目无真实数据，阶段四已声明四类输入全空）。故 60 个 `golden_NNN` 用**由 14/15/18 公式推导的确定性输入**，与既有 1375 条测试同法：每个样本的 `expected` 可由设计文档公式复算，样本注释写明推导来源。

- **备选**：留空等待真实数据 —— 否决，会让阶段六整个评测层无物可测，违背 30号 目标。
- **约束**：期望值可推导、样本只增不改（口径变更是 18号 版本化的事，不改样本）。

### D2 — L3 在 CI 里测「计算正确性 + 阈值判定 + 无回归」，不是「真实业务达标」

L3 的「达验收口径」在 CI 语境下只能对**合成台账/快照 fixture** 验证：指标纯函数算得对不对、阈值判定对不对、相对基线是否劣化。真实达成率（≥70% 集中度、≥60% 采纳率）是试点期 runtime 活动，本阶段只把口径与判定逻辑测对。

- **备选**：在 CI 里用合成数据硬断言「达成率 ≥70%」—— 否决，合成数据达标与否不反映真实效果，且违背 30号 §3.1「L3 不设简单百分比门槛」。

### D3 — 路径遵循既有 `evals/` 骨架（对 30号 §6.1 的显式偏差）

30号 §6.1/§6.3 写 `tests/evals/golden_dataset/` 与 `scripts/run_evals.py`，但阶段一已在 `backend/evals/` 落地骨架，且 pre-commit 钩子 entry、CLAUDE.md、`baseline.json` 都引用 `backend/evals/run_evals.py`。**遵循既有骨架**，路径映射如下：

| 30号 §6.1 原文 | 落地位置 |
|---|---|
| `tests/evals/golden_dataset/*.json` | `backend/evals/golden/*.json`（13 维度 + `schema.json`） |
| `scripts/seed_golden.py` | `backend/scripts/seed_golden.py` |
| `tests/evals/golden_dataset.db` | `backend/evals/golden_dataset.db`（gitignore，生成物） |
| `scripts/run_evals.py` | `backend/evals/run_evals.py`（替换骨架） |

- **备选**：严格照 30号 新建 `tests/evals/` —— 否决，会让 `evals/` 骨架与 `tests/evals/` 并存两套入口，pre-commit 钩子与 CLAUDE.md 全部失配。

### D4 — `eval_utils.py` 是口径门面，不重复实现

30号 §6.5 的 5 个纯函数，3.5 个已存在：

| 30号 函数 | 收编自 |
|---|---|
| `weighted_concentration` | `services/kpi.weighted_concentration`（re-export） |
| `cross_aisle` | `services/kpi.same_material_cross_aisle_mean`（另加逐物料跨巷道计数薄包装） |
| `normalize_weights` | `engine/scoring.py` 的权重加载/归一化路径（薄包装） |
| `select_degrade` | `engine/degradation.resolve_tiers`（re-export） |
| `assert_no_pii` | `llm/redact.redact`（包装成「脱敏后不得残留敏感字段」断言） |

L1/L3 测试经 `eval_utils.py` 走这些函数，保证评测口径与业务口径**同源**，避免两处漂移。

### D5 — `run_evals.py` 编排 pytest 子进程，解析通过率

`run_evals.py` 不做测试逻辑，而是：按 `--tier`/`--dimension` 组装 pytest 路径与 marker，用 `subprocess` 调 `python -m pytest -c <backend>/pyproject.toml <eval 路径> -m <marker> -q --tb=no`，解析末尾 `N passed / M failed` 汇总行得通过率；`--compare` 把当前通过率与 `baseline.json` 的 `tiers.<tier>.pass_rate` 对比，按 ≤3%/3–5%/>5% 分流；`--save-baseline` 回填 `tiers.*.pass_rate` 与 `baseline_captured`。路径一律以 `Path(__file__)` 为锚解析，不受 cwd 影响（pre-push 钩子 cwd = 仓库根）。

- **备选**：run_evals 直接 import 各测试模块自行执行 —— 否决，丢失 pytest fixture/marker/隔离。
- **备选**：引入 `pytest-json-report` 插件 —— 可选增强，但解析 `-q` 汇总行零新依赖，优先用后者。

### D6 — Golden JSON 组织：13 维度文件 + schema.json + SQLite 镜像

Golden 数据以 **JSON 为唯一真相**（`evals/golden/<dimension>.json` ×13 + `schema.json` 约束字段），`seed_golden.py` 读 JSON、校验 schema、种入 SQLite `golden_dataset.db`（表 `golden_samples` 60 行 + `eval_runs` 运行记录）。`.db` 是生成物（gitignore），JSON 进 Git。

- **备选**：只存 JSON、不建 SQLite —— 30号 §7 显式列出 SQLite 库与 seed 脚本为交付物，且 `eval_runs` 是趋势/回归的持久记录，保留。

### D7 — 测试文件放进既有三层包，pytest 发现由 run_evals 显式指定

测试文件 `test_l1_unit_*.py` 等落在 `evals/{l1_unit,l2_integration,l3_quality}/`（既有空包）。`pyproject.toml` 的 `testpaths=["tests"]` **不动**（保持 `pre-commit` 的 L1 门禁只跑 tests/，<5s 不受 eval 慢测影响）；eval 测试由 `run_evals` 传显式路径给 pytest 发现。`conftest.py` 放 `evals/conftest.py`，提供 DB 连接（SQLite in-memory，与 `tests/conftest.py` 同模式）、Golden 加载、API client、4 角色认证 token。新增 pytest 标记 `l1/l2/l3/slow` 到 `pyproject.toml`。

- **备选**：把 eval 测试加进 `testpaths` —— 否决，会让 `pre-commit` 每次提交跑慢测/集成测，破坏 <5s 门禁。

### D8 — 冷路径观测独立统计，不进通过率

冷路径 LLM 四能力（KPI 解读/偏离归因/权重调优/移库方案）在 L3 里以**观察项**采集（覆盖主因/误导/采纳率），写入报告但不进 `pass_rate`。硬闸门只来自 L1/L2 通过率、劣化检测、P0 护栏。

### D9 — CI 双轨：pre-push 钩子（真门禁）+ GH Actions 参考

真正生效的是取消注释 `.pre-commit-config.yaml` 的 `evals-baseline` pre-push 钩子（本机 no-mistakes 流水线，`gh` 未装）。`.github/workflows/evals-ci.yml` 按 30号 §6.4 写成参考物（l1-gate 阻断 + l2-l3 劣化告警），满足交付清单，虽本机不运行。

## Risks / Trade-offs

- **[合成样本 vs 真实样本的代表性]** → 样本由设计公式推导，覆盖的是「口径/状态机/闸门」的正确性，不是真实分布的统计量；真实达成率留试点期。每个回归样本注释注明「合成、设计推导」，不冒充真实基线。
- **[L1/L2 与既有 tests/ 重叠]** → eval 层是「golden-ID 溯源 + 容差 + 通过率 + 基线对比」的编排，复用 engine/services 函数，不整批复制既有测试；重叠处只做编排引用。
- **[pre-push 钩子启用后拉长 push]** → L3 慢测仅在 `--run-slow` 时跑；pre-push entry 是 `--tier all --compare baseline.json`（不含 `--run-slow`），故每次 push 跑 L1+L2 快速集，L3 慢测单独触发。
- **[`golden_dataset.db` 与 JSON 漂移]** → `.db` 是生成物、gitignore，真相只有 JSON；seed 脚本幂等（先清表再种），漂移时重跑 seed 即可。
- **[劣化阈值与通过率粒度]** → 通过率是场景级百分比，`>5%` 劣化在 24/20 道下约 1~2 道，粒度可接受；P0 护栏单独判（不靠百分比），故护栏突破不会被少量场景稀释。

## Migration Plan

1. 实现 `eval_utils.py` + Golden JSON + `seed_golden.py`（生成 `.db`）。
2. 实现三层测试 + `evals/conftest.py` + `pyproject.toml` 标记。
3. 替换 `run_evals.py`；改写 `tests/logic/test_run_evals.py` 为真实门禁断言（删 exit-3 契约）。
4. `run_evals --tier all --run-slow --save-baseline` 建基线 → 回填 `baseline.json`。
5. 取消注释 pre-push `evals-baseline` 钩子 + 新增 GH Actions 参考工作流。
6. 回滚策略：评测层是**纯增量、零业务改动**；若门禁误拦，回滚方式 = 重新注释 `evals-baseline` 钩子，不触碰任何业务代码或台账。

## Open Questions

（无 —— 影响规格/方案/任务拆分的未知项均已在此文档定夺；剩余实现细节在 apply 阶段按既有测试惯例就地决定。）
