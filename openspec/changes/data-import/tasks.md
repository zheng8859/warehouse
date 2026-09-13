# data-import 实现任务

> 三层 TDD：models / api / logic。每个任务先写测试（RED）再实现（GREEN）。验证方式写在任务内。

## 1. ImportSession 状态机与幂等

- [x] 1.1 实现 `ImportSession` 状态机迁移守卫（8 态 / 10 迁移，非法迁移拒绝），先写 `tests/logic/test_import_session_state.py` 覆盖全部合法迁移 + 非法迁移负例，验证 `pytest tests/logic/test_import_session_state.py -q` 通过
- [x] 1.2 实现乐观锁并发守卫（`lock_version`，后到写入被拒），先写 `tests/logic/test_import_session_concurrency.py` 覆盖两端并发仅一方成功，验证逻辑测试通过
- [x] 1.3 实现重复导入判定（数据时点 + 文件校验和 SHA-256），先写 `tests/logic/test_import_dedup.py` 覆盖「同时点同校验和 → 提示跳过/覆盖」「已 BASELINE 会话幂等返回」，验证通过

## 2. 8 步管线模块（app/importer/）

- [x] 2.1 落地 `detect.py`：格式（xlsx/csv）与编码（UTF-8/UTF-8-BOM/GBK）探测，无法识别即阻断；先写 `tests/logic/test_import_detect.py` 覆盖三类编码与非法格式，验证通过
- [x] 2.2 落地 `loaders.py`：xlsx/csv 解析成行，库位号按 6 位文本读取（前导 0 不丢、日期序列号转日期），严禁按列序号硬取；先写 `tests/logic/test_import_loaders.py` 覆盖文本库位号与日期转换，验证通过
- [x] 2.3 落地 `mapping.py`：GTJ10036 单厂内置字段映射（别名/去空格/全半角/大小写容错），字段命中率 100% 未命中即阻断；先写 `tests/logic/test_import_mapping.py` 覆盖别名与全半角匹配、缺失列阻断，验证通过
- [x] 2.4 落地 `validate.py`：四层校验（结构/字段/时点/业务），选填缺失降级不阻断；先写 `tests/logic/test_import_validate.py` 覆盖数量 ≤0、批号空、库位号非 6 位、状态非法各阻断 + 选填缺失降级，验证通过
- [x] 2.5 落地 `session.py`：会话编排 + `receipt_json` 落库（可展开到文件/列/行/原因）；先写 `tests/logic/test_import_session_orchestration.py` 覆盖 FAILED→DRAFT 修正重校验、回执明细，验证通过

## 3. 建立基准（执行导入分流）

- [x] 3.1 实现 PO/DO 分流为 `JobOrder`（`PENDING`，入库队列 / 出库任务），写入失败回滚该批；先写 `tests/api/test_import_execute.py` 覆盖 PO/DO 载入与回滚，验证通过
- [x] 3.2 实现 INV 分流为 `InventoryItem`（既有库位/批次分布）+ 触发 cap 基线重算；先写 `tests/api/test_import_execute_inv.py` 覆盖库存分布写入与基线触发，验证通过
- [x] 3.3 实现部分文件失败隔离（INV 失败不阻断 PO/DO，回执「成功 N/失败 M」）；先写 `tests/logic/test_import_partial_failure.py`，验证通过

## 4. cap 基线全量重算与 cap_physical

- [x] 4.1 `AisleCap` 新增 `cap_physical` 列（迁移，`batch_alter_table`），并修正 `linkage.py` 模块 docstring 与 `cap_reserved` 列注释为 `cap_physical × 40%`；验证迁移可回放 + `pytest tests/models -q` 通过
- [x] 4.2 落地 `app/cap/baseline.py` 全量重算：按巷道 `[:2]` 聚合计算 `cap_physical/cap_total/cap_reserved/cap_usable`（`cap_reserved = cap_physical × 40%`，仅近站台巷道），生成新 `Snapshot` 版本、旧版归档；先写 `tests/logic/test_cap_baseline.py` 覆盖四项指标与版本归档、重算失败回滚，验证通过
- [x] 4.3 修正 `openspec/config.yaml` context 里 `cap_reserved = cap_total × 40%` 旧公式为 `cap_physical × 40%`；验证 `openspec validate` 退出 0

## 5. 成品清单 ABC 统计导入（脚本）

- [x] 5.1 实现 `scripts/import_abc.py`：读 BI 看板需求「成品清单」sheet（read_only 迭代），按料号聚合出库量 → 累计占比分档（A≤70%/B≤90%/C 其余）→ upsert `Material.abc_class`，料号缺失记告警跳过，幂等、不建基线、不进状态机；先写 `tests/logic/test_abc_import.py` 覆盖分档阈值边界与缺失料号跳过，验证通过
- [ ] 5.2 用真实数据冒烟：对 `BI看板需求 -GTJ10036.xlsx` 跑脚本，断言 A/B/C 料号数 = 20/31/204（对齐 `16` A.4.1 实测）；验证输出一致

## 6. API 路由与 p2 数据层

- [ ] 6.1 实现 `/api/import/*` 路由（session/upload/validate/result/execute/retry）与 `/api/snapshot/current`、`/api/cap/recompute`、`/api/cap`；先写 `tests/api/test_import_routes.py` 覆盖端点行为 + 认证 + 错误处理，验证通过
- [ ] 6.2 `assets/app.js` 增加 fetch/API 封装 + `data-import.html` 接线（时点卡 + 三文件上传 + 校验结果表 + 开始校验/执行导入两步，`setState` 四态渲染），不接线成品清单/ABC；先写 `tests/frontend/test_data_import_data_layer.py` 静态断言，验证通过

## 7. 异常 / 边界与集成

- [ ] 7.1 覆盖异常路径：时点缺失/未来时点阻断、字段未 100% 命中阻断、格式/编码不可识别阻断、快照缺失阻断（不猜测落位）、乐观锁并发冲突；对应 `tests/logic` 与 `tests/api` 负例，验证 `pytest tests/ --tb=short -q` 全绿
- [ ] 7.2 端到端集成冒烟：上传三类文件 → 校验 → 执行导入 → 快照/队列/任务/cap 基线落库 → 重复导入幂等；验证 `tests/api/test_import_e2e.py` 通过
