"""商家账号的密码哈希与登录凭证（JWT）。

选型说明：
- 密码哈希用 `bcrypt`（自适应成本，抗彩虹表）。不存明文、不用可逆加密。
- 登录态用 JWT（HS256），签名密钥取自 `settings.app_secret_key`；
  生产环境该值由 `.env` 注入且非默认（config.py 启动期已硬校验）。
- JWT 放 httpOnly Cookie（而非 localStorage），避免 XSS 直接读取。

Cookie 名与现有 portal / sso_user 体系独立，互不影响。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import bcrypt
import jwt

from src.config import get_settings


# ====== Cookie / 凭证常量 ======
AUTH_COOKIE = "scx_auth"
TOKEN_TTL_SECONDS = 60 * 60 * 12  # 12 小时


# ====== 密码哈希 ======
def hash_password(plain: str) -> str:
    """明文密码 → bcrypt 哈希（含随机 salt，同一明文每次结果不同）。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。"""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ====== JWT 签发 / 校验 ======
def _secret() -> str:
    return get_settings().app_secret_key


def create_token(user_id: int, phone: str, role: str) -> str:
    """签发登录凭证。payload 只放最小必要信息，不放敏感字段。"""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "phone": phone,
        "role": role,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any] | None:
    """解析凭证；签名错误 / 过期 / 格式异常一律返回 None（不抛）。"""
    if not token:
        return None
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ====== 提现 / 结算流水号（可复现、不依赖 DB 序列） ======
def gen_serial(prefix: str, seed: str) -> str:
    """生成业务单号，如 SET-20260922-A1B2C3。

    同一 seed 永远得到同一单号（幂等重试不会产生重复单）。
    """
    ts = time.strftime("%Y%m%d")
    digest = hmac.new(
        _secret().encode("utf-8"), seed.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:6].upper()
    return f"{prefix}-{ts}-{digest}"


__all__ = [
    "AUTH_COOKIE",
    "TOKEN_TTL_SECONDS",
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "gen_serial",
]
