"""运行时配置读写：把系统设置页的修改写回 .env 并立即生效。"""
from __future__ import annotations

import os
from pathlib import Path

from ..config import BASE_DIR

ENV_PATH = BASE_DIR / ".env"

# 允许在系统设置页修改的配置项
EDITABLE_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_CHAT_IDS",
    "VOICE_ENABLED",
    "VOICE_VOLUME",
    "VOICE_RATE",
    "VOICE_PITCH",
    "VOICE_LANG",
    "VOICE_NAME",
    "VOICE_REPEAT",
    "VOICE_ORDER_TEXT",
    "VOICE_TRACKING_TEXT",
    "VOICE_REFUND_TEXT",
    "VOICE_CLIP_ORDER",
    "VOICE_CLIP_TRACKING",
    "VOICE_CLIP_REFUND",
    "VOICE_DEFAULT_CLIP",
    "SCREENSHOT_BACKFILL_ENABLED",
    "SCREENSHOT_BACKFILL_DAYS",
    "NOTIFY_WAIT_SCREENSHOT_SECONDS",
    "BACKUP_ENABLED",
    "BACKUP_DIR",
    "BACKUP_HOUR",
    "BACKUP_KEEP_DAYS",
    "DAILY_SUMMARY_ENABLED",
    "DAILY_SUMMARY_HOUR",
    "DAILY_SUMMARY_CHAT_IDS",
    "DAILY_SUMMARY_AT_USERS",
    "DAILY_SUMMARY_SKIP_EMPTY",
    # 通知消息模板（系统设置页自定义编辑）
    "NOTIFY_RECEIPT_TEMPLATE",
    "NOTIFY_RECEIPT_AT",
    "NOTIFY_RECEIPT_AT_NAMES",
    "NOTIFY_RECEIPT_AT_OPEN_IDS",
    "NOTIFY_REFUND_DETAIL_TEMPLATE",
    "NOTIFY_REFUND_AT",
    "REFUND_ACK_TEXT",
    "REFUND_CC_TEXT",
    "REFUND_CC_NAMES",
    "REFUND_CC_OPEN_IDS",
    # —— 其余通知模板（设置页按分类编辑）——
    "NOTIFY_DUPLICATE_TEMPLATE",
    "NOTIFY_CANCEL_TEMPLATE",
    "NOTIFY_INTENT_TEMPLATE",
    "NOTIFY_MISSING_IMAGE_TEMPLATE",
    "NOTIFY_DIGEST_TEMPLATE",
    "PUSH_TEMPLATE",
    "TRACKING_TEMPLATE",
    "DAILY_SUMMARY_TEMPLATE",
    "AUTO_REFRESH_SEC",
    "OCR_REOCR_MINUTES",
    # 菜鸟物流查询（LINK 网关）
    "CAINIAO_ENABLED",
    "CAINIAO_GATEWAY_URL",
    "CAINIAO_MSG_TYPE",
    "CAINIAO_APP_SECRET",
    "CAINIAO_CP_CODE",
    "CAINIAO_APP_CODE",
    "CAINIAO_TIMEOUT_SEC",
)


def is_writable() -> bool:
    """容器部署时 .env 若未挂载进容器，则只能改运行时、无法持久化。"""
    try:
        if not ENV_PATH.exists():
            ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
            ENV_PATH.touch(exist_ok=True)
        return os.access(ENV_PATH, os.W_OK)
    except OSError:
        return False


def mask_secret(secret: str) -> str:
    """App Secret 掩码显示，避免明文出现在页面上。"""
    if not secret:
        return ""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:6]}****{secret[-4:]}"


def write_updates(updates: dict[str, str]) -> Path:
    """写回 .env：保留原有行与注释，只替换给定键；同步更新当前进程环境变量。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    return ENV_PATH
