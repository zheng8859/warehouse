# evals 任务分解

> **规则适用性登记**：`config.yaml` 的 tasks 规则含两条 F1~F10 全链口径（「按关键路径排序
> F1→F2→F9→F3→F4→F5→F6→F7→F8→F10」「必须包含后验与 KpiSnapshot 聚合的实现任务」）。
> 二者**不适用于本 change**：关键路径各环节已在阶段二~五落地并归档，本 change 只建评测层；
> `KpiSnapshot` 全量聚合（按周/月落库 + 环比）属阶段六 KPI 看板（F10）的独立 change，
> 本 change 的 L3 只测其**指标纯函数计算与阈值判定**（复用 `services/kpi.py` 既有函数），不实现聚合落库。

## 1. Golden 数据集（60 场景 JSON + schema）

- [x] 1.1 写 `evals/golden/schema.json`，定义样本字段约束（`id`/`layer`/`dimension`/`input`/`expected`/`tolerance`/`eval_type`/`tags`，回归样本额外 `baseline_value`+`comparison`）；验证：`python -c "import json; json.load(open(...))"` 通过
- [x] 1.2 写基线层 5 维度 JSON（`evals/golden/*.json`）：评分正确性/作业闭环/KPI 计量/配置权限/安全隔离，共 24 场景（`golden_001~024`），每样本注释可溯源 20号 SC/CL/AC/TR 编号；验证：`seed_golden.py` 种入 24 行
- [x] 1.3 写边界层 4 维度 JSON：数据接入质量/错误降级/分配合规/指令可靠，共 20 场景（`golden_030~049` 区间）；验证：种入 20 行
- [x] 1.4 写回归层 4 维度 JSON：集中度趋势/移库有效/性能/KPI 基线达标，共 16 场景（`golden_050~060` 区间），回归样本含 `baseline_value`+`comparison`；验证：种入 16 行、合计 60 行
- [x] 1.5 写 `scripts/seed_golden.py`（读 JSON → 校验 schema → 种 SQLite `evals/golden_dataset.db`，表 `golden_samples`+`eval_runs`，幂等先清表）；验证：`sqlite3 evals/golden_dataset.db "select count(*) from golden_samples"` = 60

## 2. eval_utils 门面

- [x] 2.1 写 `evals/eval_utils.py` 5 纯函数（`weighted_concentration`/`cross_aisle`/`normalize_weights`/`select_degrade`/`assert_no_pii`），re-export 既有 `services/kpi.py`、`engine/degradation.py`、`engine/scoring.py`、`llm/redact.py` 口径；验证：单测断言门面输出与业务函数输出一致（同源不漂移）

## 3. 测试框架基础

- [x] 3.1 `pyproject.toml` 新增 markers `l1`/`l2`/`l3`/`slow`；验证：`pytest --markers` 列出四者
- [x] 3.2 写 `evals/conftest.py`（DB 连接 in-memory、Golden JSON 加载、API client、4 角色认证 token）；验证：一条冒烟测试取到 `client` 与 4 角色 token

## 4. L1 单元层（24 道，纯函数，≥95%）

- [x] 4.1 写 `evals/l1_unit/test_l1_unit_scoring.py`（6 因子评分 6 道，`golden_001`/`golden_006`，确定性 100%：同输入两次输出一致）；验证：`run_evals --tier l1 --dimension 评分正确性` 通过
- [x] 4.2 写 `evals/l1_unit/test_l1_unit_cap.py`（cap 竞价/预留扣减 5 道，含 `golden_033` 超总格回滚 100% 触发）；验证：`golden_033` 断言回滚
- [x] 4.3 写 `evals/l1_unit/test_l1_unit_degrade.py`（降级链 5 道，含 `golden_035` cap 不足逐级降级、`golden_040` A 类降级告警 + 理由可见）；验证：`golden_040` 必含告警与 `degrade_reason`
- [x] 4.4 写 `evals/l1_unit/test_l1_unit_distance_fifo.py`（巷道距离/FIFO 4 道）+ `test_l1_unit_weights.py`（权重归一化 4 道，权重=0 时贡献=0）；验证：L1 全层通过率 ≥95%

## 5. L2 集成层（24 道，API 行为，≥90%）

- [x] 5.1 写 `evals/l2_integration/test_l2_import.py`（导入 6 道：`golden_030` 缺必填列四层校验阻断、`golden_036` 货未入库顺路取提示不阻断、快照过期出库阻断）；验证：导入 API 场景通过
- [x] 5.2 写 `evals/l2_integration/test_l2_job.py`（作业 8 道：JobOrder 状态机 + Ledger 写入 + `golden_010` 未二次确认拦截不写台账 + 乐观锁并发冲突）；验证：`golden_010` 拦截且无台账行
- [x] 5.3 写 `evals/l2_integration/test_l2_kpi.py`（KPI 聚合 5 道：`golden_018` 同物料跨巷道 = 实际占用巷道数 ≤5）；验证：`golden_018` PASS
- [x] 5.4 写 `evals/l2_integration/test_l2_permission.py`（权限守卫 5 道：401/422/404 + `golden_022` 核心链路出域事件=0）；验证：`golden_022` 出域=0、401/422/404 各命中
- [x] 5.5 L2 全层通过率 ≥90%；验证：`run_evals --tier l2` 达标

## 6. L3 质量层（16+ 道，验收口径 + 冷路径观测，非百分比门槛）

- [x] 6.1 写 `evals/l3_quality/test_l3_concentration.py`（集中度趋势 5 道：`golden_050` 达成率≥70% 判定）；验证：`golden_050` 判定正确
- [x] 6.2 写 `evals/l3_quality/test_l3_relocate.py`（移库有效 4 道：`golden_055` 移库后跨巷道下降）；验证：`golden_055` 达标
- [x] 6.3 写 `evals/l3_quality/test_l3_perf.py`（性能基线 4 道：`golden_058` 大队列批量分配 P95<阈值）；验证：P95 断言通过
- [x] 6.4 写 `evals/l3_quality/test_l3_kpi_baseline.py`（KPI 基线达标 3 道：同物料≤5/同批≤3 `golden_060`）+ 冷路径观测（4 能力抽样，覆盖/误导/采纳率，非闸门）；验证：观测项不进入 `pass_rate`

## 7. run_evals CLI

- [ ] 7.1 替换 `evals/run_evals.py`（`--tier`/`--dimension`/`--compare`/`--save-baseline`/`--run-slow`/`--json`，subprocess 调 pytest 解析 `-q` 汇总行得通过率，路径以 `Path(__file__)` 锚定）；验证：`--list` 列三层、`--tier l1` 输出通过率与退出码
- [ ] 7.2 劣化检测分流（≤3% 记录 / 3–5% 告警 / >5% 阻断）+ P0 护栏（台账完整性/决策可追溯/出域>0）立即阻断；验证：构造 >5% 劣化基线对比以失败退出码返回
- [ ] 7.3 `--save-baseline` 回填 `baseline.json`（`tiers.*.pass_rate` + `baseline_captured`，拒绝基线下调）；验证：保存后 `baseline.json` 有实测通过率

## 8. CI 门禁

- [ ] 8.1 取消注释 `.pre-commit-config.yaml` 的 `evals-baseline` pre-push 钩子；验证：`pre-commit run evals-baseline --hook-stage push` 可执行
- [ ] 8.2 写 `.github/workflows/evals-ci.yml`（l1-gate 阻断 + l2-l3 劣化告警，参考物）；验证：YAML 语法合法
- [ ] 8.3 改写 `tests/logic/test_run_evals.py` 为真实门禁断言（删 exit-3 骨架契约）；验证：`pytest tests/logic/test_run_evals.py` 通过

## 9. 基线建立与全量验证

- [ ] 9.1 `run_evals --tier all --run-slow --save-baseline` 建基线，确认 L1≥95% / L2≥90% / L3 达验收口径、6 个回归样本（`golden_001`/`022`/`033`/`040`/`045`/`050`）全过、`baseline.json` 回填；验证：命令退出 0 且报告显示达标
- [ ] 9.2 全量 `pytest tests/` 零回归；验证：无失败（既有 1375 passed 不降）
