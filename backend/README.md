# backend — 成品库位智能推荐 后端

单进程 FastAPI + SQLite 应用。目录与模块划分依据 `D:\成品库位智能推荐\产品设计\`
的设计文档（不是自拟的），每个模块的 docstring 标注了它的事实来源文档号与章节。

**当前状态：阶段一（文档 25）· 骨架**。
骨架只搭结构与接线，大量模块是只有 docstring 的占位文件；
「哪些是真实现、哪些是占位」在下面「骨架状态」一节列明。

## 快速开始

```bash
# 1. 建虚拟环境（阶段一待办，尚未执行）
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt

# 2. 启动（必须单进程 —— SQLite WAL 下多读单写）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. 探活（注意用 /health，不是 /api/health —— 见下）
curl http://localhost:8000/health
```

API 文档：`http://localhost:8000/docs`。

## 测试

```bash
python -m pytest tests/ --tb=short -x -q     # L1 单元，<5s，pre-commit 门禁
python -m pytest tests/logic -m logic        # 只跑逻辑层（6 因子/状态机/cap）
```

三层测试对应三层 TDD（`00-总体开发方案 §3.1`）：
`tests/models` 模型 CRUD 与约束 · `tests/api` 端点行为与认证 · `tests/logic` 业务逻辑。

## 目录

```
app/
  main.py        应用装配（中间件、路由、异常映射）
  core/          横切：config / db / security / enums / errors
  models/        23 实体，按 17 号四条数据链分组
  schemas/       Pydantic DTO + 17 §10 的 6 类 JSON 结构
  api/           routes/ + middleware + permissions + deps
  engine/        6 因子评分 · 批量竞争分配 · 预留池 · 降级链
  services/      inbound / outbound / relocate / verify / ledger / kpi
  importer/      四类文件导入管线（16 号）
  cap/           基线全量重算 · 事务增量 · 对账 · 告警
  llm/           冷路径（脱敏 · 4 类能力 · 成本护栏）
tests/           models / api / logic
evals/           golden / l1_unit / l2_integration / l3_quality + baseline.json
scripts/         init_db · seed_dev · check_commit_msg
data/            运行时数据（SQLite 库、导入原文件）—— 已 gitignore
```

## 骨架状态

**已实现（可运行、有真实逻辑）**

| 文件 | 内容 |
|---|---|
| `app/core/enums.py` | 11 个枚举，逐条搬运自 `17 §九` |
| `app/core/config.py` | pydantic-settings 配置，含生产环境安全检查 |
| `app/core/db.py` | SQLite 引擎（WAL / 外键 / busy_timeout）+ Session 工厂 |
| `app/core/security.py` | bcrypt 哈希 + JWT 会话凭据（13 §7.2 的 claims） |
| `app/core/errors.py` | `DomainError` 体系与 HTTP 状态映射 |
| `app/api/permissions.py` | `13 §2.2` 角色-权限矩阵的逐条搬运 |
| `app/api/middleware.py` | 认证中间件（白名单 + Bearer/Cookie 提取 + 401） |
| `app/api/routes/health.py` | `GET /health`（免认证）与 `GET /api/health`（需认证） |
| `app/main.py` | `create_app()` 装配 |
| `scripts/check_commit_msg.py` | Conventional Commits 校验（commit-msg 钩子） |
| `evals/run_evals.py` | Evals 入口；**未实现评测时以退出码 3 失败**，防静默通过 |

**占位（只有 docstring 说明计划中的端点，无实现）**

`app/api/routes/` 下 8 个模块：`auth` · `import_` · `snapshot` · `cap` · `allocate` ·
`job` · `kpi` · `llm`，以及 `app/models/` · `app/schemas/` · `app/engine/` ·
`app/services/` · `app/importer/` · `app/cap/` · `app/llm/` 下的全部文件。

> 占位模块**刻意不注册返回 501 的空壳端点** —— 避免用「能调通但什么都不做」的接口
> 冒充已完成。阶段二起按 `26`~`33` 号实施文档逐阶段填充。

## 注意事项

- **单进程运行**：SQLite WAL 下多读单写，**不要加 `--workers`**。
  「cap 与台账同事务写入」必须由真实事务保证，不能靠应用层补偿。
- **不要引入 passlib**：本机 passlib 1.7.4 + bcrypt 5.x + Python 3.14 下调用即抛
  `ValueError`。直接用 `bcrypt`，见 `app/core/security.py`。
- **`/health` vs `/api/health`**：`19` 附录B 定义的是 `/api/health`，但 `13 §6.1`
  的免认证白名单只列 `/health`。当前实现按 `13` 字面执行 —— **探活请用 `/health`**。
- 库位号按**文本**读取（6 位，前导 0 不可丢）；严禁按 Excel 列序号硬取字段。

## 相关文档

项目根目录：`CLAUDE.md`（行为准则与工具链）· `CONTEXT.md`（领域术语表）·
`openspec/config.yaml`（给 OpenSpec 产物的项目约束）。
设计文档：`D:\成品库位智能推荐\产品设计\`。
