"""作业链：JobOrder → RecommendationPlan → Ledger → Verification（5 实体）。

事实来源：17-数据模型设计 §四；15-入库出库移库与后验流程设计
  JobOrder(作业单，三类共用) / RecommendationPlan(推荐方案) / Ledger(台账)
  Verification(后验记录) / Deviation(偏离批次)

关键口径：
  - JobOrder 状态机 7 态：PENDING / PLANNED / CONFIRMED / REJECTED / CANCELLED
    / EXECUTED / VERIFIED
  - Ledger 三类合一，用 ledger_type 区分；台账永久保留
  - Ledger 由引擎与作业流自动写，不向任何角色开放手动入口
  - 乐观锁版本号防多端同时确认同一单
  - 后验产生的偏离又成为移库作业单的输入，形成治理回路

本文件为骨架占位。
"""
