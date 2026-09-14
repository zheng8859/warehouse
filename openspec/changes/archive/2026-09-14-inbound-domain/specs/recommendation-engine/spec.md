## ADDED Requirements

### Requirement: 推荐理由读

系统必须提供只读端点 `GET /api/plan/{plan_id}`（`plan_id` 为 `RecommendationPlan.id` 的 `str` 形态），按 `warehouse_id` 隔离，返回该方案的推荐理由体 `payload_json`（含 6 因子取值与取值说明、六项权重、方案级 `degraded` / `degrade_reason` 与因子级 `factor_degraded` 降级标记），供入库作业页 p3 的推荐理由卡下钻展示。系统不得在批量分配响应 `plans[]` 内联理由体——理由只经本端点按 `plan_id` 取，保持单一来源。

#### Scenario: 按方案取理由
- **GIVEN** 一张作业单已批量分配并写入 `RecommendationPlan`
- **WHEN** 调用 `GET /api/plan/{plan_id}`（带本仓 `warehouse_id`）
- **THEN** 返回该方案的 6 因子理由与降级标记

#### Scenario: 未知或跨仓方案拒绝
- **GIVEN** 一个不存在的 `plan_id`，或属于其他仓的方案
- **WHEN** 调用 `GET /api/plan/{plan_id}`
- **THEN** 系统以 422 拒绝，不返回理由内容、不泄露其他仓的方案
