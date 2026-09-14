## Why

阶段一~五已交付评分引擎、三类作业管线、数据衔接与冷路径，但**没有任何自动化评测层**能回答「这次改动让系统变好还是变差」——三层评测金字塔（20号）、验收口径（18号）都停留在设计文档。阶段六要把它落成可运行的 Golden 数据集 + L1/L2/L3 框架 + 基线对比 + CI 门禁，为 v1.0 试点提供 Go/No-Go 质量闸门。

## What Changes

- **Golden 数据集**：60 命名场景（`golden_001~060`），按 20号 §三 分三层（基线 24 / 边界 20 / 回归 16）、13 维度，可溯源到 SC/CL/AC/TR 编号；确定性 fixture（本阶段无真实数据，样本由 14/15/18 公式推导，非理想化手写）。
- **`eval_utils.py`**：5 个纯函数门面（`weighted_concentration` / `cross_aisle` / `normalize_weights` / `select_degrade` / `assert_no_pii`），收编既有 `services/kpi.py`、`engine/degradation.py`、`engine/scoring.py`、`llm/redact.py` 的口径，不重复实现。
- **三层测试框架**：`test_l1_unit_*.py`（24 道）、`test_l2_integration_*.py`（24 道）、`test_l3_quality_*.py`（16+ 道），带 `l1/l2/l3/slow` 标记 + `conftest.py` fixtures。
- **`run_evals.py` CLI**：`--tier` / `--dimension` / `--compare` / `--save-baseline` / `--run-slow` / `--json`，通过率统计 + 劣化检测（≤3% 记录 / 3–5% 告警 / >5% 阻断）+ P0 护栏（台账完整性 / 决策可追溯 / 出域事件=0）立即阻断。
- **基线**：`baseline.json` 回填实测通过率（Step 6 `--save-baseline`），版本随 tag（v0.6.0）。
- **CI 门禁**：取消注释 pre-push `evals-baseline` 钩子 + 新增参考 `.github/workflows/evals-ci.yml`。
- **冷路径 LLM 观测**：非闸门 —— 只统计「建议可用性」（覆盖主因 / 误导 / 人工采纳率），不进 L3 通过率门槛（20号 §十）。
- **守卫测试改写**：`tests/logic/test_run_evals.py` 从「断言 exit 3」改写成真实 eval 门禁断言。

## Capabilities

### New Capabilities

- `evals`: 三层评测框架 —— Golden 数据集（60 场景）、L1/L2/L3 通过标准、基线对比与劣化检测、CI 门禁、冷路径 LLM「建议可用性」观测（非闸门）。

### Modified Capabilities

（无 —— 本变更只新增评测层，不改动任何既有能力的规格级行为；L3 所测的 KPI 指标计算为既有 `services/kpi.py` 纯函数，不新增业务行为。）

## 非目标

- 不做真实试点数据的采集与 Go/No-Go 判读 —— 那是 v1.0 试点期 runtime 活动（20号 §7.5），本阶段只建评测框架与基线。
- 不做冷路径 LLM 输出质量的硬闸门 —— LLM 输出非确定，仅观测（20号 §十）。
- 不新增/不修改任何业务实体、API 路由或 KPI 指标 —— 评测层只消费既有能力。
- 不做 A/B 测试（单厂单一活跃队列，20号 §三）。
- 不把 L3 设为简单百分比门槛 —— L3 为趋势判定，放行以 20号 §7.5 闸门为准（30号 §3.1）。

## Impact

- **代码**：`backend/evals/`（golden_dataset JSON + `eval_utils.py` + `run_evals.py` + 三层测试 + `conftest.py`）、`backend/scripts/seed_golden.py`、`backend/pyproject.toml`（新增 `l1/l2/l3/slow` 标记）、`backend/.pre-commit-config.yaml`（取消注释 `evals-baseline`）、`.github/workflows/evals-ci.yml`。
- **测试**：改写 `backend/tests/logic/test_run_evals.py`（去掉 exit-3 骨架契约）。
- **实体/API/KPI**：无新增实体；无新增 API 路由；KPI 指标复用 18号 口径（同物料跨巷道 ≤5、同批跨巷道 ≤3、加权集中度 80% 落 ≤N=5 达成率 ≥70%、采纳率 ≥60%）。
- **关键路径**：评测层不占 F1→F2→F9→F3→F4→F5→F6→F7→F8→F10 的顺序位，但它是这串关键路径的质量守护 —— v1.0 发布前必须通过其 Go/No-Go 闸门（20号 §7.5）。
