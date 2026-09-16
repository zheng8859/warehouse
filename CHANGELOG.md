# Changelog

## [v1.0.0] - 2026-09-16

成品库位智能推荐（康饮台塑立体库）—— 物料号级「集中就近落位」推荐引擎 v1.0.0 正式发布。

八阶段（25→33）全量交付：数据模型/JWT/权限 → 6 因子引擎与降级链 → 三类作业管线与台账 →
冷路径 LLM（脱敏+一键关闭）→ Evals 三层评测 → 8 页零构建前端 → 全链路验收 → 发布与复盘。

### Features（feat）

- feat(job): 三作业链路可视性/粒度补齐 + D14/D11 落地
- feat(eval): KPI 看板聚合——GET /api/kpi/summary 四指标从真实数据计算
- feat(ai): 冷路径默认打开 + 接真实 provider + 对话台 L2 NLU 修复
- feat(eval): run_evals CLI 门禁编排 + 真实门禁断言 + CI 接线
- feat(eval): eval_utils 口径门面 + evals/conftest 夹具 + pytest 标记
- feat(ai): 冷路径 AI 前端接线——对话台输入框、双产物建议卡、开关/权重/KPI/归因/移库入口
- feat(ai): AI Notice 强制注入（field_serializer 挂所有 ai 叙事）
- feat(ai): 冷路径 6 端点 POST /api/llm/*（开关 + 四能力 + 采纳落地）
- feat(ai): 对话台统一入口 POST /api/conversation/message（L0/L2 + 写意图零台账）
- feat(ai): 对话台意图路由表（4 类意图 → 能力/槽位/write_intent）
- feat(ai): ④ 移库多方案 relocate/propose（规则算三档 + 量化代价 + 三重校验）
- feat(ai): ③ 采纳落地 weight/apply（规则校验写新版 WeightConfig + 版本化）
- feat(ai): ③ 权重调优建议（≥50 批次反事实模拟 + 影子模式 PROPOSED）
- feat(ai): ② 偏离归因（规则算四类候选异常清单 → LLM 归纳）
- feat(ai): ① KPI 解读（规则聚合同物料跨巷道均值 → LLM 叙事）
- feat(ai): KPI 最小聚合（同物料跨巷道均值/加权集中度/采纳率）
- feat(ai): 冷路径网关编排（统一链路 + 双产物响应）
- feat(ai): 成本三道护栏 + 同步记账（token/并发/月预算）
- feat(ai): 外部 LLM 客户端（provider 抽象 + 超时 + mock）
- feat(ai): 出境脱敏管线（正向白名单 10 字段）
- feat(ai): ai.* 权限四标识（17→21）+ 角色矩阵映射
- feat(ai): 冷路径成本护栏配置与 3 实体 + 迁移
- feat(job): 批量确认响应补 summary 部分成功聚合
- feat(auth): 登录接真实认证，移除演示角色下拉
- feat(job): 出库/移库作业页 p4/p5 数据层接后端
- feat(job): 批量收拢方案接口
- feat(job): 批量顺路取接口与出库确认拣货路径入账
- feat(job): 入库作业队列查询与推荐理由读接口及 p3 数据层接线
- feat(import): 端到端集成冒烟 + 已建基准会话幂等返回
- feat(import): 数据导入页 p2 数据层接线（fetch 封装 + 三步上传 + 两步交互）
- feat(import): /api/import/* 与 snapshot/cap 路由 + cap 漂移重算
- feat(import): 成品清单 ABC 统计导入脚本（分档 + upsert）
- feat(import): 部分文件失败隔离（INV 失败不阻断 PO/DO）
- feat(import): INV 分流为 InventoryItem 并触发 cap 基线
- feat(cap): 落地 cap 基线全量重算 baseline.py
- feat(cap): AisleCap 新增 cap_physical 列
- feat(import): PO/DO 分流为 JobOrder（PENDING）execute.py
- feat(import): 会话编排与回执落库 session.py
- feat(import): 四层校验与回执 validate.py
- feat(import): GTJ10036 单厂内置字段映射 mapping.py
- feat(import): 文件解析与单元格值归一 loaders.py
- feat(import): 格式与编码探测 detect.py
- feat(import): 重复导入判定（数据时点 + SHA-256 校验和）
- feat(import): ImportSession 状态机迁移守卫（8 态 / 10 迁移）
- feat(job): 偏离批次清单读端点
- feat(job): 作业单写操作与验证读端点
- feat(job): 冲正编排
- feat(job): 三类作业确认与同步后验编排
- feat(job): 台账写入与 cap 增量
- feat(job): 状态机扩展至 10 态并落地台账反向行 schema
- feat(ui): 四态占位与 .node 流程状态卡
- feat(ui): 角色菜单可见性（13 §3.1）
- feat(ui): 纳入 9 页原型与静态资源
- feat(engine): 阶段三核心引擎 —— 6 因子评分 · 批量竞争分配 · 降级链 · 端点
- feat(model): 移除 InventoryItem.zone 列及其衔接链迁移
- feat(auth): 权限矩阵补测与枚举入参守卫
- feat(auth): 登录端点、失败语义一致与中间件状态回查
- feat(auth): 账号状态机迁移表
- feat(auth): 口令哈希与自实现 HS256 会话凭据
- feat(model): 配置与对话 5 实体与迁移
- feat(model): 度量与身份 2 实体与迁移
- feat(model): 作业链 5 实体与状态机与迁移
- feat(model): 衔接链 5 实体与迁移
- feat(model): 主数据链 6 实体与迁移
- feat(model): 乐观锁校验辅助
- feat(env): 接入 Alembic 迁移链与建库入口
- feat(model): 建立声明式基类与内存库测试夹具

### Bug Fixes（fix）

- fix(ui): 修复 32 号验收/审计发现——XSS 转义 + 移动端横向溢出 + favicon
- fix(eval): 修复 32 号评测劣化——L3 移库三用例对齐新契约 + L2 冷路径用例补开关
- fix(eval): 收口代码评审缺陷——P0 护栏接线 / 冷路径补测 / 基线守卫
- fix(ai): 冷路径护栏拒绝映射 4xx（token 超限 413 / 并发饱和 429）
- fix(ui): 后端托管前端静态页（显式路由 + /assets 挂载）
- fix(ui): 前端 window.api 统一补 /api 前缀
- fix(ui): 入库分配后重读 lock_version 避免乐观锁 409
- fix(import): PO/DO 类型与生产日期为选填，对齐 16 A.2/A.3
- fix(job): 台账数量按实际执行量记录
- fix(job): 快照缺失阻断确认并收窄异常边界
- fix(ui): 令牌色值对齐 21/24 权威值
- fix(ui): 原型正文对齐 16/14/13 号文档
- fix(auth): 分配端点按 inbound.operate 施加端点级 403
- fix(auth): 收敛凭据校验与 401 响应渲染
- fix(eval): 修正 run_evals 帮助文本里的裸百分号
- fix(env): 修正 pre-commit 钩子不可用的两处问题

### Refactors（refactor）

- refactor(ui): P1/P2/P3/P4 界面精简——删除说明性文字 + P2 上传位精简
- refactor(ai): 冷路径开关守卫收敛为共享依赖 require_cold_path_enabled

### Tests（test）

- test(eval): L3 质量层 17 道 + 冷路径观测（验收口径/移库/性能/KPI 基线）
- test(eval): L2 集成层 24 道（导入/作业/KPI/权限 + 出域护栏）
- test(eval): L1 单元层 24 道（评分/cap/降级/距离FIFO/权重）
- test(ai): 冷路径端点异常/边界套件（409/降级/熔断）
- test(ai): 冷路径端点级鉴权 403 单测（计划员无 ai.* / toggle 仅管理员）
- test(import): 补 7.1 异常路径 API 负例（时点缺失 422 / 格式不可识别 / 字段未命中）
- test(import): ImportSession 乐观锁并发守卫测试
- test(job): 基线层闭环/计量/并发断言并收尾任务清单
- test(ui): 组件冒烟
- test(auth): 钉住凭据校验 401 保留失败原因
- test(model): 补全表软删除扫描断言
- test(env): 阶段一装配冒烟测试

### Documentation（docs）

- docs(eval): 32 号 Step 4 交付物——验收签字（发布建议：可以发布）
- docs(eval): 32 号 Step 1/2 交付物——全链路验收清单 + 安全审计报告（含 Step 4 修复记录）
- docs(eval): KPI 看板 F10 收尾 —— KpiSnapshot 按周/月落库 + 环比聚合明确不做
- docs(eval): 更新 CLAUDE.md —— KPI 看板四指标实时聚合已提交
- docs(ai): 冷路径默认打开（一键关闭）口径对账
- docs(eval): 更新 CLAUDE.md 阶段六收尾状态（v0.6.0）
- docs(eval): 阶段六 Evals 评测体系变更规划产物（propose）
- docs(ai): 阶段五前端 Phase B 收尾，记录 ai-assist-frontend 合并与主规格写入
- docs(ai): 阶段五收尾并打 v0.5.0，CLAUDE.md 状态推进到阶段六
- docs(ai): CLAUDE.md §八 补 /api/llm/* 端点级鉴权事实
- docs(ai): 回写冷路径脱敏白名单到 10 字段正向枚举
- docs(ai): 回写冷路径决策到 CONTEXT.md 与 config.yaml
- docs(ai): ai-assist change 规划产物（proposal / specs / design / tasks）
- docs(job): 归档 crosscut-contract-alignment 并同步主规格
- docs(job): 归档阶段四三个变更（inbound/outbound/relocate-domain）并同步主规格
- docs(env): 更新 CLAUDE.md 阶段四收尾（v0.4.0 已合并打标）
- docs(import): 归档 data-import 变更并同步主规格
- docs(import): data-import change 规划产物（proposal / specs / design）
- docs(import): 标记 5.2 ABC 冒烟通过（A 20/B 31/C 204）
- docs(import): 修正 config.yaml context 的 cap 预留公式
- docs(job): 归档 transaction-base 变更并同步主规格
- docs(job): 登记快照阻断与实际执行量等口径偏离
- docs(job): 登记 transaction-base 交易底座变更规划产物
- docs(ui): 登记阶段七收尾并同步待办
- docs(ui): 归档 frontend-foundation 变更并同步主规格
- docs(ui): 标记前端基础层任务完成
- docs(ui): OpenSpec 前端基础层变更提案
- docs(engine): 阶段三收尾并登记阶段四交接
- docs(model): 归档 retire-zone-column 变更
- docs(engine): 归档 recommendation-engine 变更并同步主规格
- docs(engine): 登记阶段三的变更产物与状态
- docs(model): 订正批号生成时点的口径
- docs(model): 收尾 retire-zone-column 的验收记录与状态登记
- docs(engine): 登记阶段三推荐引擎与 zone 列清退变更产物
- docs(model): 术语表清退品质库区与可行巷道集旧判据
- docs(auth): 标记归档 change 的合并落点
- docs(auth): 归档 401 失败语义收窄并同步主规格
- docs(auth): 订正中间件类 docstring 的 401 边界表述
- docs(auth): 登记 401 失败语义收窄的变更产物
- docs(auth): 订正三处注释口径并去重跨层重复的测试语料
- docs(model): 归档 data-model-permission 并同步规格到 openspec/specs
- docs(model): 标记 9.5 完成并记录合并与打标证据
- docs(model): 登记合并前代码评审的处置与复测数据
- docs(env): 精确化 Alembic env.py 的入口措辞
- docs(model): 阶段二验收记录（9.1~9.3）与两处新登记
- docs(model): 登记 §2 暴露的字段口径待确认项
- docs(model): 登记阶段二数据模型与权限变更产物
- docs(env): 订正 §11 的 v0.1.0 指向与 .gitattributes
- docs(env): 阶段一收尾状态更新

### Chores（chore）

- chore(env): 数据脚本 + 启停脚本 + 期初数据导入手册
- chore(eval): 归档 evals 变更，写入主规格 delta
- chore(eval): 建立评测基线 + 全量零回归验证（L1/L2/L3 100% · 1392 passed）
- chore(ai): 归档 ai-assist-frontend 变更，写入主规格 delta
- chore(ai): 归档 ai-assist 变更，写入主规格 delta
- chore(ai): 冷路径全量回归通过（1373 passed，零回归）
- chore(env): 新增 ai scope（冷路径 AI 辅助子系统）
- chore(env): 忽略 gstack 本地产物目录（QA 报告/会话）
- chore(env): 统一换行符为 LF
- chore(env): initial project scaffold
