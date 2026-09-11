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
    jwt_algorithm: str = "HS256"
    #: 13 §7.1：建议 8 小时（一个班次），超时重新登录。无 refresh token。
    session_hours: int = 8

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
    #: 默认关闭；关闭时核心链路不受任何影响。
    cold_path_enabled: bool = False
    llm_request_timeout_s: float = 2.0

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


settings = Settings()
