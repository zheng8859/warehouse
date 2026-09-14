## 1. 契约层：批量确认响应补 summary

- [x] 1.1 在 `backend/app/schemas/job.py` 新增 `BatchSummary`（`total/success/failed: int`）并给 `BatchConfirmResponse` 加 `summary: BatchSummary` 字段 —— 验证：`python -c "from app.schemas.job import BatchConfirmResponse; assert {'results','summary'} <= set(BatchConfirmResponse.model_fields)"` 通过
- [x] 1.2 在 `backend/app/api/routes/job.py` 的 `batch_confirm` 聚合 `summary`（`total=len(results)`、`success` 计 `status∈{VERIFIED,VERIFY_FAILED}`、`failed=total-success`）并随 `BatchConfirmResponse` 返回 —— 验证：既有 `tests/api/` 确认用例仍绿

## 2. 测试：部分成功计数

- [x] 2.1 在 `tests/api/` 新增批量确认部分成功用例：3 单中 2 成功 1 源状态非 `PLANNED` 被拒，断言 `summary == {total:3, success:2, failed:1}` 且 `results[]` 逐单一致 —— 验证：`python -m pytest tests/api/ -k confirm -q` 通过
- [x] 2.2 跑全量 `python -m pytest tests/ -q`，确认无回归 —— 验证：退出码 0
