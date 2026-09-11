"""ImportSession 状态机：DRAFT→VALIDATING→VALIDATED/FAILED→IMPORTING→IMPORTED→BASELINE/DISCARDED。16 §3.1。含乐观锁版本号与重复导入判定（数据时点 + 文件校验和）。

阶段四（文档 28）实现，本文件为骨架占位。
"""
