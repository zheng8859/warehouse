"""作业与交易服务（阶段四 / 设计 15）。

三类作业统一状态机：PENDING → PLANNED → CONFIRMED → EXECUTED → VERIFIED，
另有 REJECTED / CANCELLED 分支，以及写台账失败时 CONFIRMED → PLANNED 回退。
写操作必须经二次确认；未确认不产生台账。
"""
