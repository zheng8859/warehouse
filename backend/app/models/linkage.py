"""衔接链：ImportSession → Snapshot → InventoryItem / AisleCap（5 实体）。

事实来源：17-数据模型设计 §三；16-数据衔接与 cap 自维护
  ImportSession(导入会话) / Snapshot(快照基线) / InventoryItem(库存分布)
  AisleCap(巷道容量) / CapAlert(容量告警)

关键口径：
  - cap_total    = 巷道总格数 − 已占格数
  - cap_reserved = cap_total × 近站台预留比例（默认 40%，仅近站台）
  - cap_usable   = cap_total − cap_reserved（非 A 类可用）
  - 快照是权威、增量是过程；每次快照导入按巷道全量重算并生成新版本，旧版归档不删除

本文件为骨架占位。
"""
