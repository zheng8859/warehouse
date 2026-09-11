"""6 类 JSON 结构（对外契约）。

事实来源：17-数据模型设计 §十
  1. 推荐理由（入库）  job_id / aisles / factors{six} / scores / degraded / degrade_reason
  2. 顺路取顺序（出库）do_no / pick_sequence / weighted_concentration / threshold_n / exceeded
  3. 收拢方案（移库）  batch_no / material_code / from_aisles / target_aisle / plates /
                       expected_cross_aisle{before,after} / batch_unchanged
  4. 导入校验回执      session_id / data_time / files[{file_type,rows,fields_hit,anomalies,status}]
  5. cap 快照          snapshot_version / aisles[{aisle,total,reserved,usable,near_station}]
  6. KPI 卡片          period / weighted_concentration / same_material_cross_aisle /
                       same_batch_cross_aisle / adoption_rate / placement_accuracy

硬约束：降级时 degraded=true 且 degrade_reason 必填（降级不静默）。

本文件为骨架占位。
"""
