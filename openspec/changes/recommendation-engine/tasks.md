> 排序说明：rules 要求「按关键路径 `F1 → F2 → F9 → F3 → F4 → …` 排序」。本变更只占 **F3 / F4** 两环，
> 其前驱 F1 / F2 / F9 属阶段四，**不在本变更内**（见第 10 组「不适用」登记）。故组内按 F3 → F4 的数据流排序：
> 因子 → 可行集/评分 → 队列序 → 降级链 → 主循环 → 理由 → 端点。
>
> 验证方式的约定：本阶段的验证一律是 `backend/tests/` 下的 pytest（`00` §3.1 的「逻辑测试保留完整代码」）。
> `evals/` 目录属阶段六（`CLAUDE.md` §二：现在跑刻意以退出码 3 失败），故每条任务标注的是它**对应**的
> `20` §「基线层」评测场景编号（`SC-` / `AC-`），到阶段六落成 evals 用例时按此映射取数。

## 1. 前置：契约、常量与测试夹具

- [ ] 1.1 在 `backend/app/schemas/reason.py` 落 `17` §10.1 / §10.7 的 Pydantic DTO（请求 `BatchAllocateRequest`、响应 `BatchAllocateResponse`、理由 `ReasonPayload`），字段名逐个对照 `17` §10.1 与 §10.7 的表。验证：`tests/` 新增 schema 用例，断言「六因子键齐全」「`breakdown` 为 巷道 × 因子 二层结构」「`degraded_alerts` 每项含 `job_order_id` / `aisle` / `message`」；`POST /openapi.json` 能生成该模型
- [ ] 1.2 在 `backend/app/engine/__init__.py` 导出 7 个模块的公共入口，并把 D14 的四个**待确认默认值**（N、优先级三项权重、换算恒等、档位留空）集中成具名常量（含 `TODO` 指向 `design.md` D14 的表格行）。验证：`tests/logic/` 断言常量存在且六因子名与 `app/models/configuration.py:WEIGHT_FACTORS` **逐项相等**（防两处各写一份而漂移）
- [ ] 1.3 建造数夹具 `backend/tests/logic/conftest.py`：内存 SQLite（复用 `tests/` 既有夹具）+ 一个 `make_scenario()`，可参数化生成 巷道/`AisleCap`/`Snapshot`/`InventoryItem`/`Material`/`JobOrder`/`WeightConfig`/`CapacityConfig`。验证：夹具自身有冒烟用例（空库能跑、四类输入全空时不抛异常）
- [ ] 1.4 给分配器加**注入式时钟**（`now` 显式入参，引擎内不出现 `datetime.now()`）。验证：`tests/logic/` 用 grep 式静态断言「`app/engine/` 下不出现 `datetime.now(` / `random.` / `uuid`」，并有一条用例传两个不同的 `now` 断言释放判定翻转（`design.md` D2）

## 2. 六因子与权重取数（F3）

- [ ] 2.1 `factors.py` 落 `abc` / `cap` / `existing` / `batch` / `continuity` 五个因子的纯函数（取数与归一化到 `[0,1]`，各返回「值 + 取值说明」）。验证：`tests/logic/test_factors.py`，覆盖 `SC-002`（权重为 0 的因子贡献为 0）、`SC-003`（批次因子读**库存快照**既有批号分布，**不读 PO 批号**）、`SC-005`（既有同物料落位抬升同巷道得分）；含「库位号 `010104` → 巷道 `01` 按 `[:2]` 文本切片」的用例
- [ ] 2.2 `factors.py` 落 `station` 因子的取数与**降级判据**（`AisleStation` 无该巷道行 ⇒ 因子降级不参与评分）。验证：`tests/logic/` 断言首期预期状态（`AisleStation` 空表）下该因子不参与、且结果里出现 `factor_degraded["station"]` 及原因（`16` §394 / `A.4`）
- [ ] 2.3 权重取数：复用 `app/core/config_version.py` 的 `pick_current_version`，取 `WeightConfig` 当前生效版本的六列；**不存在生效版本则抛领域错误阻断**（不静默取默认）。验证：`tests/logic/test_scoring.py` 覆盖 doc 的「两版并存取新版且旧版仍可回滚」与「无生效版本 → 阻断、不产出方案」

## 3. 可行巷道集、预留池与综合评分（F3）

- [ ] 3.1 `reserved.py` 落 `available_cap()`（A 类含预留；非 A 类仅 `cap_usable`；过了 `reserved_release_at` 释放给 B/C）与释放判定（现场墙上时间，`design.md` D7）。验证：`tests/logic/test_reserved.py` 覆盖 `AC-001`（近站台紧张 + A 类大量入队 ⇒ 慢流转品不占用预留池）、「超时后 B 类可用释放额度」、「`is_near_station` 为空且 `cap_reserved=0` 时收敛为全额可用」
- [ ] 3.2 `scoring.py` 落**可行巷道集**求解：**三判据** `cap 足够` / `巷道可用`（主数据存在性，D7）/ 非 A 类排除预留池（原第四判据「品质库区匹配」已从 `14` §3.4 步 1 移除，处置与逐文档改动面见 `design.md` D7）。候选巷道遍历序固定为 `aisle_no` 升序。验证：`tests/logic/test_scoring.py` 覆盖 `SC-004`（cap 已满的巷道不进入候选集）、「遍历序与 DB 返回序无关」（打乱插入序，断言输出序不变）
- [ ] 3.3 `scoring.py` 落综合评分：可用因子加权和 ÷ 可用因子权重之和，两位小数**十进制四舍五入**，比较用量化后值。验证：`tests/logic/` 覆盖 `SC-001`（近站台巷道得分最高并入选）、`SC-006`（全默认权重下与 `14` 的公式逐项一致）；含 `17` §10.1 的可追溯恒等式复算用例（**避开恰好落在半值上的测试数据**）

## 4. 队列优先级排序（F3）

- [ ] 4.1 `priority.py` 落三项加法（未来 N 天出库量 + ABC 等级分 + 既有集中度增益），三项各自归一化后加权求和，系数取 D14 的默认常量。验证：`tests/logic/test_priority.py` 覆盖「同档内（多个 A 类）按出库量降序」、「A 类爆款先于 C 类慢流转品取得稀缺容量」
- [ ] 4.2 `priority.py` 落**排序降级**：缺未来 N 天出库量 ⇒ 退化为仅按 ABC，并在理由中标注降级排序及原因（不中断分配）。验证：`tests/logic/` 断言该场景下 `priority.degraded = true` 且 `degrade_reason` 非空
- [ ] 4.3 `priority.py` 落**全序兜底**：`(priority 降序, job_order_id 升序)`，保证同分不同单的顺序确定。验证：`tests/logic/` 造三条同分队列项，连续两次分配断言队列序逐项相同（`design.md` D2）

## 5. 降级链（F3）

- [ ] 5.1 `degradation.py` 落**档位解析器**：返回有序的巷道分组列表；本阶段填档 0（近站台 `is_near_station IS TRUE`）与档 2（非近站台），档 1 / 档 3 为空集（`design.md` D5）。验证：`tests/logic/test_degradation.py` 断言四档结构存在、空档被跳过、首期只可达档 0 与档 2
- [ ] 5.2 `degradation.py` 落降级原因措辞与 A 类告警文案：A 类爆款被迫降到远巷道 ⇒ 产出告警，文案含近站台缺口量，形如「近站台缺口 12 板，建议移库腾挪」。验证：`tests/logic/` 断言文案逐字含该句式与缺口数
- [ ] 5.3 落**四级走尽**的处理：该单标记失败、提示人工介入、**停留 `PENDING`**（不迁 `PLANNED`、不回写 `bulk_batch_no`），同批其余单照常出方案。验证：`tests/logic/` 覆盖 `AC-005`（降级链末端仍无容量 ⇒ 提示人工干预，不静默落位到非法巷道）

## 6. 批量分配主循环（F4）

- [ ] 6.1 `allocator.py` 落贪心主循环：逐单取可行集 → 选得分最高者 → **在内存快照上**扣减容量 → 下一条。验证：`tests/logic/test_allocator.py` 覆盖「选中得分最高的可行巷道」、「扣减对后续队列项生效」（剩 10 板，先占 10 再要 5 的单不再把该巷道计入可行集）
- [ ] 6.2 落**容量扣减不落库**：整批结束后断言 `AisleCap` / `Snapshot` / `Ledger` 三表**行数与内容均未变**；同一入参两次调用输出逐项相同（`design.md` D4 + D2）。验证：`tests/logic/` 的两次调用比对用例，逐项比 `aisles` / `scores` / `breakdown` / `priority` / 降级标记
- [ ] 6.3 落**批次号生成**：`BAT-<现场日期 YYYYMMDD>-<NN>`，`NN` 由当日 `bulk_batch_no` 前缀计数 + 1（无随机源）。验证：`tests/logic/` 断言格式、确定性（同输入同号）、跨日不串号（`design.md` D11）

## 7. 推荐理由与预测跨巷道（F4）

- [ ] 7.1 `reasons.py` 落 `payload_json` 组装（`17` §10.1）：每候选巷道 × 每参与因子的取值与**取值说明**、六项权重、`scores`、`priority`（含降级标记）、`factor_degraded`、方案级 `degraded` / `degrade_reason`。验证：`tests/logic/test_reasons.py` 断言三条不变量 —— 降级因子不出现在分解里、`scores` 可由取值复算、列与 JSON 的同名字段一致
- [ ] 7.2 落**预测跨巷道**回溯计算（`14` §3.4 步 5）：`| 既有快照中该物料占用的巷道 ∪ 本次分配的巷道 |`，阈值取 `CapacityConfig.same_material_cross_aisle_threshold`。验证：`tests/logic/` 覆盖 `AC-004`（既有 2 巷道 + 新分 1 巷道 ⇒ 3，未超阈）；含「库位号按 `[:2]` 切片、不数值化」的用例
- [ ] 7.3 落**同批跨巷道不预测**：入库队列无批号，理由中不含同批预测（`14` §3.1 / §3.4 步 5）。验证：`tests/logic/` 断言 `payload_json` 不含同批跨巷道键
- [ ] 7.4 落**两种降级分开承载**：列 `degraded` / `degrade_reason` 只描述降级链；`factor_degraded` 只描述缺失因子，内容不得互换（`design.md` D8）。验证：`tests/logic/` 造一个「同时容量不足与数据缺失」的方案，断言两个字段各自的内容与不交叉

## 8. 端点 `POST /api/allocate/batch`（F4）

- [ ] 8.1 按 TDD **先写端点测试**（RED），再在 `backend/app/api/routes/allocate.py` 实现，并在 `backend/app/main.py` 注册路由。验证：`tests/api/test_allocate.py` 覆盖「显式集合驱动分配」（5 条 PENDING 只提交 2 条 ⇒ 其余 3 条状态与方案均不变）、响应 `plans` 按 `priority` 降序
- [ ] 8.2 落状态迁移与回写：成功者 `bulk_batch_no` 非空且与响应一致、状态 `PENDING → PLANNED`；其余状态**整体拒绝**（不部分成功）、不新增未定义迁移。验证：`tests/api/` 覆盖「状态迁移与批次号回写」、「非待分配状态被拒绝」；含重复提交同一批（第二次必被拒）的用例 —— 即乐观锁语义经状态守卫兑现
- [ ] 8.3 落「不写台账」与「不写 cap」：一次成功的批量分配后断言 `Ledger` 与 `AisleCap` 均无新增行，作业单停在 `PLANNED`。验证：`tests/api/` 的断言用例（红线「未确认不产生台账」）
- [ ] 8.4 落认证与权限口径：未携带凭据 ⇒ 401（由既有中间件覆盖）；**不施加 403**（`permission` 规格的「v1 不做端点级资源鉴权」）；`engine.invoke` 恒不可授予（`AUTO_ONLY`）。验证：`tests/api/` 覆盖 401 用例；另有一条对照用例断言 `ROLE_PERMISSIONS` 中 `inbound.operate` 的持有者为 `warehouse_keeper` / `admin`，`planner` / `supervisor` 为否（`design.md` D10）
- [ ] 8.5 落规模上限：>50 单拒绝并提示拆分，**不静默截断**；空数组产出空 `plans`。验证：`tests/api/` 覆盖 51 单被拒、0 单空响应两条用例

## 9. 异常与边界

- [ ] 9.1 权重缺失 ⇒ 整批阻断、不产出方案（`design.md` D3 的「权重」行）。验证：`tests/api/` 断言返回阻断类错误且 `RecommendationPlan` 零新增
- [ ] 9.2 容量不足 ⇒ 走降级链而非报错（「降级不是异常」，用 `degraded` / `degrade_reason` 表达）。验证：`tests/logic/` 覆盖 `AC-002`（降级到次优巷道集，不崩溃、不丢弃）
- [ ] 9.3 四类输入**全空**的端到端形态：无 `AisleCap` / 无 `InventoryItem` / 无 `Material.abc_class` / 无 `AisleStation` ⇒ 逐单因子级降级 + 四级走尽失败，**不抛异常、不静默落位**。验证：`tests/logic/` 一条集成用例跑通该形态并断言失败原因可见（这是阶段三唯一能跑通的真实形态，见 `16` §394）
- [ ] 9.4 写库异常 ⇒ **整批整体回滚**，不留「一半有方案一半没有」的中间态。验证：`tests/logic/` 用注入式失败（如第 3 条单落库时抛错）断言事务前状态完整恢复（`design.md` D12）
- [ ] 9.5 快照缺失时的**入库**行为与出库**不同**：入库按因子级降级**不阻断**；出库的快照过期阻断属阶段四，不在本变更。验证：`tests/logic/` 断言无 `Snapshot` 行时分配照常完成且理由标注降级（防把出库口径误搬到入库）

## 10. 基线层评测场景落地与登记

- [ ] 10.1 在 `backend/tests/logic/` 落**评分正确性**基线层场景 `SC-001~006`，每条一个用例、注释注明场景编号与 `20` 的原表预期。验证：`python -m pytest tests/logic -m logic` 全绿；逐条对照 `20` §「1. 评分正确性」的预期列
- [ ] 10.2 在 `backend/tests/logic/` 落**分配合规**场景 `AC-001` / `AC-002` / `AC-004` / `AC-005`，注释注明场景编号。验证：同上命令；`AC-004` 以 `predicted_cross_aisle` 的**预演**形态验收（非落位后实测）
- [ ] 10.3 在 `tasks.md` 与 `proposal.md` 之外**显式登记本变更不适用**的规则项，并写明归属阶段：`AC-003`（同批跨巷道 ≤3 —— 入库分配时刻无批号可聚合，留待阶段四后验）、后验与 `KpiSnapshot` 聚合（F8 / F10，阶段四）、导入校验失败阻断（F1 / F2，阶段四）、出库快照过期阻断（阶段四）、`CL-001~006` 作业闭环与 `TR-001~005` 集中度趋势（需落位与台账，阶段四起）。验证：`openspec validate recommendation-engine` 通过；本节条目与 `proposal.md` 的「非目标」逐条对得上
- [ ] 10.4 更新 `CLAUDE.md` §11「当前状态」为阶段三（v0.3.0）实际完成的内容，并订正 §11 标题仍写着「（阶段一）」的问题。验证：`git diff CLAUDE.md` 只落在 §11；与 `00` §2.1 的版本表一致

## 11. 收尾

- [ ] 11.1 复核事实来源文档的改动面（**两批**，改前均 `cp` 成 `.<时间戳>.bak`、改后均 `diff` 核对）：① 本变更开始前，`17` §4.2 / §10.1 / §10.7 与 `14` §3.4 步 5 四处；② 2026-09-11 按用户「品质库区从全部文档移除」的决定，`14` §3.4 步 1（删判据）、`16`（关键字段总表 + A.2/A.3 删行 + 两条编号说明）、`17` §3.3、PRD 数据来源表两行 —— 逐处见 `design.md` D7 的改动面表。验证：对 `D:\成品库位智能推荐\产品设计\` 下的 `.bak` 做 `diff`，两批改动各自只落在预期行
- [ ] 11.2 核对**两项登记项均已了结**（二者均在 apply 之前就落定，本任务只作归档前的复核）：
  - **① 已了结** —— 用户 2026-09-11 决定「品质库区」从全部文档移除，改动已落并含 `.bak` 与 `diff` 核对（`design.md` D7 的改动面表）；连带发现 `InventoryItem.zone` **列**待独立清退，已另立 `retire-zone-column` 变更（**不由本变更夹带**）。
  - **② 已了结** —— 用户 2026-09-11 决定「订正」，已落 `specs/permission/spec.md` 的 `MODIFIED Requirements`，订正 `自动写权限封闭` 的口径（实质未变，见 `design.md` D10）。
  - 验证：`openspec validate recommendation-engine` 退出码 0；`openspec/specs/permission/spec.md` 的 `自动写权限封闭` **仍是旧措辞**（delta 未同步属正常），而 `openspec/changes/recommendation-engine/specs/permission/spec.md` 存在且含**两条原场景 + 一条新场景**；**归档时**确认同步后主规格正文已换成「以 `ledger` / `engine` 为目标资源」的资源级口径、且两条原场景一条不少
- [ ] 11.3 全量回归 + `openspec validate recommendation-engine`（**不加 `--strict`** —— 中文规格会触发 RFC2119 假警报）。验证：`python -m pytest tests/` 全绿；`openspec validate` 退出码 0 且带 `✗` 的失败为 0
