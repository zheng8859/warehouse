"""声明式基类与共享 Mixin。

事实来源：17-数据模型设计 §十一（数据隔离）、§十二（留存）

开发阶段决策（17 号明确把字段类型/索引/数据库实现留给开发阶段定）：
  - 主键：整型自增 id 作代理键；业务键（job_id / session_id / do_no 等）加唯一约束
  - 表名：snake_case 复数（如 JobOrder → job_orders）
  - 不做软删除 —— 17 号用「归档不删除 + 版本化」，不引入 is_deleted / deleted_at
  - 全部实体携带 warehouse_id 作为过滤维度

本文件为骨架占位。
"""
