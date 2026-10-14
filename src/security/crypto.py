"""Token / 敏感字段落库加密层（Fernet / AES-128-CBC + HMAC-SHA256）。

约束：
- AppSecret / access_token / refresh_token 一律不落明文
- 加密 key 走环境变量 TOKEN_CIPHER_KEY；本文件只读 settings，**绝不**硬写默认值
- 用 TypeDecorator 透明加密，业务层仍按 str 读写
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import LargeBinary, TypeDecorator

from src.config import get_settings

logger = logging.getLogger(__name__)


# =====================================================================
# 密钥获取（懒加载）
# =====================================================================
@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """获取单例 Fernet 实例。

    开发环境允许 settings.token_cipher_key 为空 → 启动期生成一个进程级 key
    并打 warning，仅用于本地 dev / pytest；生产必须显式注入。
    """
    s = get_settings()
    if not s.token_cipher_key:
        if s.app_env in ("staging", "production"):
            raise RuntimeError(
                "TOKEN_CIPHER_KEY must be set in staging/production. "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        # dev: 临时生成一个，写 warning
        key = Fernet.generate_key()
        logger.warning(
            "TOKEN_CIPHER_KEY not configured (env=%s). Generated ephemeral key; "
            "encrypted tokens will not survive process restart.",
            s.app_env,
        )
        return Fernet(key)

    try:
        return Fernet(s.token_cipher_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "TOKEN_CIPHER_KEY is invalid (must be a url-safe base64 32-byte key). "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


# =====================================================================
# 工具函数（被 TypeDecorator / 业务层使用）
# =====================================================================
def encrypt_str(plain: str | None) -> bytes | None:
    """字符串 → Fernet token（bytes）。None 直传。"""
    if plain is None:
        return None
    if not isinstance(plain, str):
        raise TypeError(f"encrypt_str expects str, got {type(plain).__name__}")
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt_bytes(cipher: bytes | None) -> str | None:
    """Fernet token（bytes）→ 字符串。None 直传。"""
    if cipher is None:
        return None
    if not isinstance(cipher, (bytes, bytearray, memoryview)):
        raise TypeError(f"decrypt_bytes expects bytes, got {type(cipher).__name__}")
    try:
        return _fernet().decrypt(bytes(cipher)).decode("utf-8")
    except InvalidToken as exc:
        # 加密 key 轮换 / 损坏 → 不要回退到明文
        logger.error("Failed to decrypt token (key rotated or ciphertext corrupted): %s", exc)
        return None


# =====================================================================
# SQLAlchemy TypeDecorator
# =====================================================================
class EncryptedText(TypeDecorator):
    """透明加密的 str 列。

    业务代码 `Mapped[str | None] = mapped_column(EncryptedText())`：
    - 写入：`shop.access_token = "xxx"` → DB 存 `bytes(fernet(...))`
    - 读取：DB 拿回 bytes → ORM 还原为 str

    ⚠️ 注意：
    - LIKE / ORDER BY 都会失败（DB 看到的是密文）
    - 不要拿这列做业务过滤，只做"完整读回"
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            # 已经是密文，避免双重加密
            return bytes(value)
        return encrypt_str(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            # 兼容历史未加密数据：直接返回。生产轮换时再迁移
            logger.warning(
                "Read plain string from EncryptedText column; please migrate via rotation script."
            )
            return value
        return decrypt_bytes(value)


__all__ = ["EncryptedText", "encrypt_str", "decrypt_bytes"]