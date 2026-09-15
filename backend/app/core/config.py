"""应用运行配置（Settings）。

事实来源：
  16-数据衔接与 cap 自维护 §10.6（工程参数表）
  14-推荐引擎与评分流程设计 §3.3（预留比例 / 释放时点）
  18-KPI 与验收度量设计 §8.6（统一验收 N / 看板周期）
  15-入库出库移库与后验流程设计 §10.6（批量上限 / 阈值）
  19-系统架构与部署视图 §3.3（单库）

本模块是**运行期配置**（部署时按环境覆盖），不是领域配置实体 ——
领域配置（WeightConfig / CapacityConfig 等）在 app/models/configuration.py，
支持全量版本保留与回滚，两者不要混淆。

这里的数值是**初始默认值 / 引导值**：CapacityConfig 首次落库时从这里取默认，
之后以数据库中的版本为准。
"""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------ 基础
    app_name: str = "成品库位智能推荐"
    environment: str = "dev"  # dev / prod
    api_prefix: str = "/api"

    #: 首期单厂（GTJ1 广州顶津旗下单个 WMS 仓库代码，非全厂）。
    warehouse_code: str = "GTJ10036"

    # ------------------------------------------------------------ 数据库
    #: SQLite 单库。WAL 模式由 app/core/db.py 在每个连接上设置。
    database_url: str = "sqlite:///./data/warehouse.db"
    sqlalchemy_echo: bool = False
    #: WAL 下多读单写；冲突时重试等待上限。
    sqlite_busy_timeout_ms: int = 5000

    # ------------------------------------------------------------ 会话 / 认证
    #: 生产必须通过 WMS_JWT_SECRET 覆盖。
    jwt_secret: str = "dev-only-insecure-change-me"
    #: 13 号未指定签名算法 —— 开发阶段决策，对称密钥 + 单应用部署下 HS256 足够。
    #: **不支持协商**：security.py 固定用这个值，头部里的 alg 只被核对、不参与选择。
    jwt_algorithm: str = "HS256"
    #: 13 §7.1：建议 8 小时（一个班次），超时重新登录。无 refresh token。
    session_hours: int = 8
    #: bcrypt 成本因子。12 是 bcrypt 库的默认量级，单次约 0.28s（本机实测）——
    #: 登录是低频动作，这个代价换的是离线爆破成本。测试把它降到 4（每次约 0.001s），
    #: 否则几十次哈希就把整包推出 pre-commit 的 L1 门禁（<5s）；
    #: 生产侧由 assert_production_safe 挡住低于 12 的取值。
    bcrypt_cost: int = 12

    # ------------------------------------------------------------ 容量与阈值（16 §10.6）
    #: 近站台预留比例，仅近站台巷道参与预留计算。
    reserve_ratio: float = 0.40
    #: 预留超时释放时点；未被 A 类用完则自动释放给 B/C 类。
    reserve_release_at: str = "18:00"
    #: cap 漂移告警阈值（建议 >1%，可按巷道格数调整）。
    cap_drift_alert_ratio: float = 0.01

    # ------------------------------------------------------------ 验收与后验口径（18 附录A）
    #: 拣货量加权集中度 —— 统一验收指标：80% 拣货量落在 ≤N 巷道。
    concentration_n: int = 5
    same_material_cross_aisle_max: int = 5
    same_batch_cross_aisle_max: int = 3
    placement_accuracy_min: float = 0.99
    adoption_rate_min: float = 0.60

    # ------------------------------------------------------------ 作业约束（15 §10.6）
    #: 单次批量单据上限。
    batch_order_limit: int = 50

    # ------------------------------------------------------------ 导入（16 §10.6）
    #: 支持格式；其他格式阻断。
    import_allowed_formats: tuple[str, ...] = ("xlsx", "csv")
    #: 编码自动探测顺序；失败即阻断。
    import_encodings: tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk")
    #: 字段命中率要求 100%，未命中即阻断。
    import_field_hit_ratio: float = 1.0

    # ------------------------------------------------------------ 冷路径（10 §七）
    #: 默认打开（一键关闭）；关闭时核心链路不受任何影响。
    cold_path_enabled: bool = True
    #: 外部 LLM 供应商标识；空字符串 = 未配置 = 不发起任何外部调用，走「仅规则卡片」
    #: 降级路径（`degraded_reason=provider_unconfigured`）。与 `cold_path_enabled` 是
    #: 两个独立的门：开关管「能力整体是否可用」，provider 管「出了开关有没有模型可调」。
    #: 取值：""=不调用 · "mock"=确定性回显（测试）· "openai"/"openai_compatible"=真实
    #: OpenAI 兼容 `/chat/completions` 后端（配合 `llm_api_key`/`llm_base_url`/`llm_model`）。
    llm_provider: str = ""
    #: 出站 API Key（provider 为 OpenAI 兼容时使用）。空 = 未配置 → 视同不可用
    #: （`llm_unavailable`），不会带着空 key 出站。
    llm_api_key: str = ""
    #: 出站 base URL（末尾 `/v1` 可带可不带，backend 会补 `/chat/completions`）。
    #: 空 = 回落 OpenAI 官方 `https://api.openai.com/v1`。
    llm_base_url: str = ""
    #: 模型名。空 = 回落 `gpt-4o-mini`。常见：deepseek-chat / qwen-plus / glm-4 / moonshot-v1-8k。
    llm_model: str = ""
    #: 单请求 token 上限（10 §七 成本护栏第 ① 道）。覆盖 KPI 报告 / 归因输出体量
    #: （~1500–2500 token）留余量。超限**拒绝并提示拆分**，不截断文本。
    llm_max_tokens_per_req: int = 4096
    #: 在途 LLM 调用并发上限（护栏第 ② 道）。单进程低频，护住外部配额。
    llm_max_concurrency: int = 4
    #: 月度预算硬上限（护栏第 ③ 道），单位 = token（与 `llm_max_tokens_per_req` 同单位，
    #: 直接可比、可直接测）。超限在请求入口熔断，降级为「仅规则卡片」。
    llm_monthly_budget: int = 1_000_000
    #: 单请求超时（秒）。真实 OpenAI 兼容服务一次 KPI 解读约 3~4s（本机 DeepSeek 实测），
    #: 10 §七 原「2.0s」是在「默认关闭 + 仅 mock」口径下定死的，接真模型后会误伤成
    #: `llm_timeout`，故上调到 10s（仍保留「单进程低频、不占写库」的护栏语义）。
    llm_request_timeout_s: float = 10.0

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, v: str) -> str:
        if v not in ("dev", "prod"):
            raise ValueError("environment 必须是 dev 或 prod")
        return v

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    def assert_production_safe(self) -> None:
        """生产启动前的护栏：不允许带着开发默认值上线。"""
        if not self.is_prod:
            return
        if self.jwt_secret == "dev-only-insecure-change-me":
            raise RuntimeError("生产环境必须通过 WMS_JWT_SECRET 设置真实的 JWT 密钥")
        if self.database_url.startswith("sqlite"):
            # 允许，但必须是显式配置的持久化路径而非默认相对路径。
            if "./data/" in self.database_url:
                raise RuntimeError("生产环境请显式配置 WMS_DATABASE_URL 的绝对路径")
        if self.bcrypt_cost < 12:
            # 不许把测试用的低成本因子带到生产 —— 它是**唯一**能调低口令哈希强度
            # 的旋钮，而调低它不会有任何功能表现异常。
            raise RuntimeError("生产环境 bcrypt_cost 不得低于 12（测试用低成本因子）")


settings = Settings()
