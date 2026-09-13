## ADDED Requirements

### Requirement: ImportSession 状态机

`ImportSession` 状态必须限于 `DRAFT` / `VALIDATING` / `VALIDATED` / `FAILED` / `IMPORTING` / `IMPORTED` / `BASELINE` / `DISCARDED` 八值，迁移必须限于：`DRAFT → VALIDATING`（点击「开始校验」）、`DRAFT → DISCARDED`（用户取消）、`VALIDATING → VALIDATED`（校验全部通过）、`VALIDATING → FAILED`（字段未命中 / 口径异常 / 时点缺失）、`FAILED → DRAFT`（修正文件后重新校验）、`VALIDATED → IMPORTING`（点击「执行导入」）、`VALIDATED → DISCARDED`（用户取消）、`IMPORTING → IMPORTED`（写入与分流成功）、`IMPORTING → FAILED`（写入异常 / 回滚）、`IMPORTED → BASELINE`（快照重算 cap 基线完成）。终态为 `BASELINE` 与 `DISCARDED`。系统不得新增未定义状态，也不得放行未列出的迁移。

#### Scenario: 合法迁移被接受
- **GIVEN** 会话状态为 `DRAFT`
- **WHEN** 操作员点击「开始校验」
- **THEN** 状态迁移为 `VALIDATING`

#### Scenario: 未定义迁移被拒绝
- **GIVEN** 会话状态为 `DRAFT`
- **WHEN** 尝试直接迁移到 `IMPORTED`
- **THEN** 迁移被拒绝，状态保持 `DRAFT`

#### Scenario: 校验失败可修正重校验
- **GIVEN** 会话状态为 `FAILED`
- **WHEN** 操作员修正文件后重新校验
- **THEN** 状态迁移为 `DRAFT` 后重新进入 `VALIDATING`

#### Scenario: 取消置 DISCARDED 终态
- **GIVEN** 会话状态为 `DRAFT` 或 `VALIDATED`
- **WHEN** 用户取消本次导入
- **THEN** 状态迁移为 `DISCARDED`，成为终态
