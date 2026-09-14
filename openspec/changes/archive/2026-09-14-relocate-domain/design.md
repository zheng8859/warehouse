## Context

移库确认链已落地：`confirm_relocate`（`app/services/relocate.py`）走 `_confirm_and_execute` 完成 `PLANNED → CONFIRMED → EXECUTED`，并在台账写前取「移库前」的同物料跨巷道数交给 `verify_relocate`（相对阈值，`后 < 前` 才达标）。台账/后验底座复用 `transaction-base`。缺的是收拢方案派生、批量派生接口。本变更补这两样，动机见 proposal.md。

可复用的既有件：

- `SnapshotIndex.profile(material_code)` → `InventoryProfile`（`plates_by_aisle` = 每巷板数、`batches_by_aisle` = 每巷批号集、`cross_aisle_count`），`aisle_of(loc) = loc[:2]`、`load_snapshot_index(session, snapshot_id)`（`app/engine/factors.py`）。主巷道与集中度判定**就是**对这一 profile 的按巷道聚合。
- `available_cap(*, cap, abc_class, release_at, now) -> int`（`app/engine/reserved.py`）：该巷道对本单可用格数（A 类总额 / 非 A 类 `cap_usable` 或释放后总额）。三重校验①的「cap 充足」读它。
- `to_occupied_cells(qty, *, cartons_per_pallet=None)`（`app/engine/allocator.py`）：板-格换算唯一落点，v1 恒等（1 板 = 1 格）。
- `next_bulk_batch_no` / `load_bulk_batch_nos`（`app/engine/reasons.py`）、`_current_snapshot` / `_render_snapshot_version`（`app/api/routes/allocate.py`）。
- `RecommendationPlan`：`job_order_id`（不唯一，当前方案 = 该单 `id` 最大）、`plan_kind`、`payload_json`、`degraded`/`degrade_reason`（`app/models/job.py`）。`PlanKind.CONSOLIDATE = "收拢"` 已定义。
- 状态机 `assert_transition`：`PENDING → PLANNED` 已合法。
- `GET /api/jobs?type=relocate` 由兄弟变更 `inbound-domain` 以类型无关端点提供，本变更只消费。

## Goals / Non-Goals

**Goals:**
- 收拢方案派生为**纯函数**（确定性、无会话、无写），落在移库服务域。
- `POST /api/job/batch/relocate-plan` 批量派生 + 三重校验 + 降级链 + 幂等。

**Non-Goals:**
- 不新增实体字段、不做迁移（幂等所需版本号塞进 `payload_json`，见 D4）。
- 不重做移库确认链/台账/后验底座。
- 不碰入库/出库确认语义。

## Decisions

### D1 — 主巷道与次选巷道的确定性口径（评审报告 F15 定稿）

主巷道 = `profile.plates_by_aisle` 中板数最多的巷道；**并列时按巷道号文本升序取最小**（确定性，符合「同样输入必得同样输出」红线）。收拢过程中**不动态切换**主巷道 —— 一次方案一次性定死 `target_aisle`，分布变了由下一轮批量重算。

次选巷道 = 板数第二多的巷道；并列同样升序。次选仍不足则该单移出批量（降级链：主巷道 → 次选巷道 → 移出批量，`15-04` §4.3）。

### D2 — 收拢方案派生 = 纯函数 `derive_consolidation_plan`

新增 `derive_consolidation_plan(*, material_code, batch_no, profile: InventoryProfile, batch_plates_by_aisle: Mapping[str, int], available: Mapping[str, int]) -> ConsolidationPlanResult`（`app/services/relocate.py`）。`ConsolidationPlanResult` 为冻结 dataclass：`plan: dict | None` 与 `moved_out_reason: str | None` **恰好一个非空**（`plan` 为 `17` §10.3 形状；`moved_out_reason` 为移出批量的降级原因）。

```json
{
  "batch_no": "…", "material_code": "…",
  "from_aisles": ["21", "25", "31"],
  "target_aisle": "01", "plates": 40,
  "expected_cross_aisle": { "before": 6, "after": 2 },
  "batch_unchanged": true
}
```

- `target_aisle` = 主巷道（D1）。
- `from_aisles` = `batch_plates_by_aisle` 中**非主巷道**的巷道，按巷道号升序。
- `plates` = `sum(batch_plates_by_aisle[a] for a in from_aisles)`（该批号散落板总数）。
- `expected_cross_aisle.before` = `profile.cross_aisle_count`；`.after` = `before` − 收拢后**变空**的 from_aisle 数（某 from_aisle 在该批号移出后无其他物料板 ⇒ 变空；判据 = `batch_plates_by_aisle[a] == profile.plates_by_aisle[a]`）。
- `batch_unchanged = true`（移库不改批号，结构性保证 —— 函数不产出任何改批号的字段）。
- 纯函数：不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`。**确定性**：同样输入必得同样输出。

**三重校验**（`15-04` §4.2）落在纯函数内：

1. **cap 充足**：`available[target_aisle] >= plates`；不足 → 降级次选巷道再判。
2. **批号一致**：`batch_unchanged = true`。
3. **集中度规则**：`after < before`；不满足（`after >= before`，如该批号已集中于主巷道、或每个 from_aisle 都与其他物料共占）→ 移出批量。

**降级链**：主巷道 cap 不足 → 次选巷道（cap 充足且 `after < before` 仍成立）→ 仍不可行 → `moved_out_reason`（降级不静默、原因可追溯）。

备选：把聚合逻辑内联进端点——否决，纯函数才可进逻辑层单测（三层 TDD），且「同样输入必得同样输出」在函数边界最可断言。

### D3 — 端点 `POST /api/job/batch/relocate-plan`

落在 `app/api/routes/job.py`（与 `/batch/confirm` 同文件、同前缀 `/api/job`）。请求 `{warehouse_id, job_order_ids: [str], snapshot_version?: str | None}`；守卫 `require_permission(Permission.RELOCATE_OPERATE)`（对齐 `allocate` 的 `inbound.operate` 端点级鉴权，`13` §2.2 仓管员/主管/管理员可 operate）。

流程（复用 `allocate` 的批量编排骨架）：

1. 报文级校验：`job_order_ids` 非空、全为 `^[1-9]\d*$`、去重；加载后全为 `RELOCATE`，否则整批 422。
2. `_current_snapshot`（无快照 → `BlockedMissingPrerequisite` 409，提示重新导入，不猜测）。
3. `load_snapshot_index(session, snapshot_id=snapshot.id)`。
4. 取 `available`：对候选巷道（主/次选）读 `AisleCap`，`available_cap(cap, abc_class, release_at, now)` 得 `{aisle: 可用}`。
5. 逐单：`profile = index.profile(material_code)`；`batch_plates_by_aisle` = 该单 `(material_code, batch_no)` 的库存行按巷道聚合板数（D5）；`derive_consolidation_plan` → 有方案写 `RecommendationPlan(plan_kind=CONSOLIDATE, payload_json=…)` + `assert_transition(PENDING, PLANNED)`；移出批量的单记入 `moved_out[]`（含 `degrade_reason`）。
6. 响应分列 `plans[]` 与 `moved_out[]`。

批量上限复用 `MAX_JOB_ORDERS_PER_BATCH = 50`（`app/schemas/reason.py`）。

### D4 — 幂等键 = `bulk_batch_no` × 库存视图版本

与 `outbound-domain` D3 同一机制：幂等键由 `bulk_batch_no` 与**库存视图版本**合成；版本号落进 `RecommendationPlan.payload_json` 的簿记字段 `snapshot_version`。

逐单判定：

- `PENDING` 单 → 派生、写方案（含 `snapshot_version`）、迁 `PLANNED`、回写 `bulk_batch_no`。
- 已是 `PLANNED` 的单 → 读其「当前方案」（该单 `id` 最大的 `plan_kind=CONSOLIDATE` 行）的 `snapshot_version`：相同 → 返回既有方案（幂等命中）；不同 → 重新派生，追加一行新方案。

### D5 — 批号级取数 `batch_plates_by_aisle`

`profile` 只到物料级（`plates_by_aisle` 是物料板数），而 `from_aisles` / `plates` / `.after` 需要**批号级**板数。端点按 `(material_code, batch_no)` 从该快照的 `InventoryItem` 行聚合出 `{aisle: qty}`，作为纯函数入参。不扩 `SnapshotIndex`（避免为移库单开一个更细的索引层，且批号级只在生成方案的这一刻需要，不是评分热路径）。

### D6 — schema 落点

新增 `ConsolidationCrossAisle` / `ConsolidationPlan` / `BatchRelocatePlanRequest` / `BatchRelocatePlanResponse`（`app/schemas/reason.py`，与 `BatchAllocateRequest` 同文件），响应 DTO 形状对齐 `17` §10.3。

## Risks / Trade-offs

- **[R] `payload_json` 加 `snapshot_version` 簿记字段，轻微偏离 `17` §10.3** → 只加字段不删改，消费方忽略未知键。
- **[R] `expected_cross_aisle.after` 的「变空」判据 = 批号板数 == 物料板数** → 是「该巷道是否只剩这一批」的确定性近似，非跨巷道分布的全量重算；与 15-04 §4.2「集中度规则 = 收拢后跨巷道数 < 收拢前」口径一致（只关心巷道是否变空）。
- **[R] 收拢后集中度不下降（`after >= before`）** → 三重校验③拦截，移出批量并写降级原因，不静默。
- **[R] 主巷道算法并列/动态切换** → D1 已定稿（并列升序、不动态切换），F15 销账。
- **[R] cap 校验依赖「板 = 格」v1 恒等** → 与 `to_occupied_cells` 同一口径；真换算（D14）到齐后只改 `available` 的计算，不影响纯函数签名。

## Migration Plan

无 DB 迁移：不新增列 / 表 / 约束。变更面 = `services/relocate.py`（加 `derive_consolidation_plan` + `ConsolidationPlanResult`）、`api/routes/job.py`（加 `/batch/relocate-plan`）、`schemas/reason.py`（DTO）。回滚 = 还原这几个文件；功能开关式回滚，不删历史台账（本变更新增的 `payload_json` 只是方案行内一列数据，回滚不影响既有台账）。
