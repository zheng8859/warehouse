"""成品库位智能推荐 — 应用包。

事实来源：19-系统架构与部署视图 §2.2（五层架构）
  ① 数据衔接层 → app/importer/ , app/cap/
  ② 应用服务层 → app/engine/ , app/services/
  ③ 前端层     → 零构建静态页（阶段七，文档 31）
  ④ 冷路径     → app/llm/（默认关闭，文档 29）
横切关注点   → app/core/ , app/models/ , app/schemas/ , app/api/

核心链路（评分/落位/后验/台账）必须保持本地确定性、零外部依赖、数据不出域。
"""
