"""飞书事件回调：校验、解密、转成标准消息并即时解析。"""
from __future__ import annotations

import hashlib
import json
import logging
from base64 import b64decode
from typing import Any

from ..config import settings
from ..db import session_scope
from ..models import RawMessage
from .messages import normalize

logger = logging.getLogger(__name__)


def decrypt(encrypted: str, encrypt_key: str) -> str:
    """飞书事件加密：AES-256-CBC，key = sha256(encrypt_key)，iv = 密文前 16 字节。"""
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要安装 pycryptodome 才能解密飞书事件：pip install pycryptodome") from exc

    raw = b64decode(encrypted)
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = raw[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plain = cipher.decrypt(raw[16:])
    pad = plain[-1]
    return plain[:-pad].decode("utf-8")


def unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """含 encrypt 字段时解密；配置了 verification token 时校验。"""
    if "encrypt" in payload:
        if not settings.feishu_encrypt_key:
            raise RuntimeError("收到加密事件但未配置 FEISHU_ENCRYPT_KEY")
        payload = json.loads(decrypt(payload["encrypt"], settings.feishu_encrypt_key))
    token = settings.feishu_verification_token
    if token and payload.get("token") and payload["token"] != token:
        raise RuntimeError("verification token 校验失败")
    return payload


def event_to_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """把 im.message.receive_v1 的事件体转换成与历史消息一致的格式。"""
    msg = (event or {}).get("message") or {}
    if not msg.get("message_id"):
        return None
    sender = (event.get("sender") or {}).get("sender_id") or {}
    return {
        "message_id": msg.get("message_id", ""),
        "chat_id": msg.get("chat_id", ""),
        "msg_type": msg.get("message_type", ""),
        "create_time": msg.get("create_time", "0"),
        # 回复 / 引用了一条群消息：记下被引用消息的 id。
        # 群里很常见「引用原订单消息 + 只写退单两个字」，这条线索比消息正文更硬。
        "parent_id": msg.get("parent_id") or msg.get("root_id") or "",
        "body": {"content": msg.get("content", "{}")},
        "sender": {
            "id": sender.get("open_id", "") or sender.get("user_id", ""),
            "sender_type": (event.get("sender") or {}).get("sender_type", ""),
        },
    }


def recalled_event_to_ids(event: dict[str, Any]) -> tuple[str, str, str]:
    """从撤回事件体里取 (message_id, chat_id, 撤回人)：兼容 v1/v2 与内嵌 message 结构。"""
    event = event or {}
    msg = event.get("message") or {}
    message_id = event.get("message_id") or msg.get("message_id") or ""
    if isinstance(message_id, list):
        message_id = message_id[0] if message_id else ""
    chat_id = event.get("chat_id") or msg.get("chat_id") or ""
    recaller = event.get("recaller_id") or event.get("operator_id") or ""
    if isinstance(recaller, dict):
        recaller = recaller.get("open_id") or recaller.get("user_id") or ""
    return str(message_id), str(chat_id), str(recaller or "")


def handle_message_recalled(message_id: str, chat_id: str = "", recaller: str = "") -> dict:
    """消息被撤回：标记消息与关联订单为「撤回」（订单只留档，不参与统计与流程）。"""
    from ..services import recall as recall_service

    with session_scope() as session:
        return recall_service.handle_message_recalled(
            session, message_id, chat_id=chat_id, recaller=recaller
        )


def handle_im_message(raw_msg: dict[str, Any]) -> dict[str, Any]:
    """写入数据库并立即解析，返回处理结果。"""
    from ..services.ingest import (
        attach_images_to_text,
        process_pending,
        process_pending_images,
        upsert_raw_message,
    )

    item = normalize(raw_msg)
    with session_scope() as session:
        msg, is_new = upsert_raw_message(session, item)
        session.commit()
        chat_id = item["chat_id"]
        if item["image_keys"]:
            attach_images_to_text(session, chat_id)
        empty = {
            "parsed": 0, "ignored": 0, "filtered": 0,
            "failed": 0, "notified": 0, "errors": [],
        }
        if is_new or msg.parse_status == "pending":
            stats = process_pending(session)
            img_stats = process_pending_images(session)
            stats = {
                k: stats.get(k, 0) + img_stats.get(k, 0)
                if isinstance(stats.get(k, 0), int)
                else stats.get(k, []) + img_stats.get(k, [])
                for k in empty
            }
        else:
            stats = dict(empty)
        order = None
        if msg.parse_status == "parsed":
            from ..models import Order

            order = session.query(Order).filter(
                Order.source_message_id == msg.message_id
            ).first()
        result = {
            "message_id": msg.message_id,
            "is_new": is_new,
            "status": msg.parse_status,
            "kind": msg.kind,
            "notify_status": msg.notify_status,
            "stats": stats,
            "order_id": order.id if order else None,
        }
    return result


def handle_event(payload: dict[str, Any]) -> dict[str, Any]:
    """事件入口，返回结果字典（含 challenge 直通）。"""
    if "challenge" in payload:
        logger.info("收到飞书回调地址验证请求（challenge 校验）")
        return {"challenge": payload["challenge"]}

    payload = unwrap(payload)
    header = payload.get("header") or {}
    event_type = header.get("event_type") or payload.get("type") or ""
    logger.info("收到飞书事件: %s", event_type or "(无事件类型)")

    # 旧版 v1 结构：{"type":"im.message.receive_v1", "event": {...}}
    if event_type.startswith("im.message.receive"):
        normalized = event_to_message(payload.get("event", {}))
        if normalized is None:
            return {"ok": False, "reason": "消息体缺少 message_id"}
        return {"ok": True, "event_type": event_type, **handle_im_message(normalized)}

    # v1 直传消息结构（部分回调）
    if event_type in ("im.message", "message"):
        normalized = event_to_message(payload.get("event", {}))
        if normalized:
            return {"ok": True, "event_type": event_type, **handle_im_message(normalized)}

    # 消息被撤回：把对应的订单标记成「撤回订单」（只留档，不参与统计与后续流程）
    if "recalled" in event_type or "recall" in event_type:
        message_id, chat_id, recaller = recalled_event_to_ids(
            payload.get("event", {}) or payload
        )
        if not message_id:
            return {"ok": False, "reason": "撤回事件缺少 message_id", "event_type": event_type}
        return {
            "ok": True,
            "event_type": event_type,
            **handle_message_recalled(message_id, chat_id=chat_id, recaller=recaller),
        }

    logger.info("忽略未处理的事件类型: %s", event_type)
    return {"ok": True, "ignored": True, "event_type": event_type}


def pending_message_count() -> int:
    with session_scope() as session:
        return session.query(RawMessage).filter(RawMessage.parse_status == "pending").count()
