"""配置与对话台实体（4 + 1）。

事实来源：17-数据模型设计 §七 / §八
  WeightConfig(权重配置) / CapacityConfig(容量与阈值配置)
  FieldMappingConfig(字段映射，GTJ10036 内置) / PromptTemplate(问句模板)
  ConversationContext(对话台上下文)

关键口径：
  - 配置全量版本保留、可回滚；权重变更后下次评分生效
  - CapacityConfig 默认值：预留比例 40%、超时释放 18:00、加权集中度 N=5、
    同物料跨巷道 ≤5、同批跨巷道 ≤3
  - 字段映射首期内置，不做可视化配置（v0.11 决策）

命名说明：本模块是「领域配置实体」，与 app/core/config.py（应用运行配置）无关，
故命名为 configuration.py 以避免混淆。

本文件为骨架占位。
"""
