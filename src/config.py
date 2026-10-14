"""应用配置：使用 pydantic-settings 从 .env 读取所有运行时配置。

所有 AppSecret / Token 仅在此处引用，绝不向下传递到日志、前端或仓库。

启动校验：
- staging/production: 拒绝默认 APP_SECRET_KEY / 缺失 TOKEN_CIPHER_KEY
- development:        warn 但不抛（.env.example 默认值即可启动，方便本地）
"""
import logging
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


# ====== 默认值黑名单（用于启动期硬校验） ======
_DEFAULT_APP_SECRET_KEY = "change-me"

# dev warn 缓冲（避免每次 reload 重复打）
_warns: list[str] = []


class Settings(BaseSettings):
    """全局配置。

    通过环境变量或 .env 文件覆盖默认值。所有 ALI_* 字段必须保密。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ====== 应用 ======
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # 与渔枢系统同一个进程时，渔货栈的页面挂在这个前缀下（渔枢系统自己有 /orders、/changelog，
    # 不加前缀会撞车）。独立部署（只有渔货栈）时设成空字符串，路径与以前一致。
    self_prefix: str = "/self"
    app_secret_key: str = _DEFAULT_APP_SECRET_KEY

    # ====== 数据库 ======
    # 两端已合并成一套库（2026-09-30）：渔货栈与渔枢系统用同一个库，所以直接认
    # 渔枢系统的 DATABASE_URL。只有要把渔货栈数据临时分到别的库时才设 SELF_DATABASE_URL
    # （一套系统一套库的部署下不用设）。
    database_url: str = Field(
        default="postgresql+psycopg2://supply_chain:supply_chain_pwd@localhost:5432/supply_chain_xiaoyu",
        validation_alias=AliasChoices("SELF_DATABASE_URL", "DATABASE_URL"),
    )
    # 渔枢系统（写入库）的库地址：渔货栈的商品来自那边的自营品与供应链商品，
    # 详情页的成本价要按**那边的渔货价**算（见 src/goods/hub_price.py）。
    # 名字不能用 HUB_DATABASE_URL —— 那是上面「与渔枢系统同进程时本端库」的别名，会撞。
    # 没配就连不上，成本回落渔货栈自己的渔货价，不会拖垮详情页。
    platform_database_url: str = ""

    # ====== Redis ======
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("HUB_REDIS_URL", "REDIS_URL"),
    )
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ====== 平台管理员（渔货栈审核台的登录账号） ======
    # platform_admin 不开放注册，只能由此处配置在库内生成 / 纠正角色。
    # demo 默认值仅用于本地与演示环境；生产部署必须换成强密码。
    platform_admin_phone: str = "13800000000"
    platform_admin_password: str = "admin123456"  # noqa: S105 - 服务端环境变量

    # ====== Token 落库加密 key（Fernet） ======
    # 生成：python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # 留空 + env != production → dev 临时生成（每次重启加密值不可逆）
    token_cipher_key: str = ""

    # ====== 选品默认值 ======
    default_min_margin: float = 0.20
    default_min_stock: int = 5
    default_max_return_rate: float = 0.10
    default_price_stability: float = 0.15
    default_price_min: float = 5.0
    default_price_max: float = 1000.0

    # ====== 闲鱼渠道 ======
    xianyu_enabled: bool = True
    xianyu_api_base: str = "https://www.goofish.com"

    # ====== SaaS 版「闲参谋」联动（上架到店铺 → 闲鱼商品发布页） ======
    # 渔货栈只负责生成「闲鱼发布预填包 + 一次性票据」，真正把字段带入
    # 闲鱼发布页由闲参谋前端完成（契约见 src/channel_xianyu/publish.py 模块头）。
    # 留空 = 未接入闲参谋，前端退化为「复制预填字段 + 导出 JSON」。
    xianmou_publish_url: str = ""
    # 预填包票据有效期（秒）——票据一次性、短期有效，防止链接被转发复用
    xianmou_ticket_ttl: int = 600

    # 闲参谋服务端 OpenAPI 共享密钥；为空时身份同步与商品接口保持关闭。
    xianmou_identity_sync_secret: str = ""

    # ----------------------------------------------------------------
    # 启动校验
    # ----------------------------------------------------------------
    @model_validator(mode="after")
    def _enforce_secrets(self) -> "Settings":
        """生产/staging 必须替换默认值；dev 仅 warn。"""
        is_prod = self.app_env in ("staging", "production")

        checks = [
            (self.app_secret_key == _DEFAULT_APP_SECRET_KEY,
             "APP_SECRET_KEY 仍为默认值 'change-me'"),
            (not self.token_cipher_key,
             "TOKEN_CIPHER_KEY 为空 → access_token/refresh_token 将无法持久化"),
        ]
        problems: list[str] = []
        for hit, msg in checks:
            if not hit:
                continue
            if is_prod:
                problems.append(msg)
            else:
                if msg not in _warns:
                    _warns.append(msg)
                    logger.warning("[%s] %s", self.app_env, msg)

        if problems:
            joined = "; ".join(problems)
            raise ValueError(
                f"[{self.app_env}] 启动校验失败: {joined}. "
                "生产/staging 必须替换占位值"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例（单例）。"""
    return Settings()