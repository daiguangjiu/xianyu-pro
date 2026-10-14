"""飞书消息结构 -> 统一结构（文本 / 图片 / 文件）。"""
from __future__ import annotations

import json
from typing import Any

from ..timeutil import from_epoch_ms

IMAGE_TYPES = {"image"}
FILE_TYPES = {"file", "audio", "media", "sticker"}
IGNORE_TYPES = {"system", "share_chat", "share_user", "interactive", "todo", "calendar"}


def _load_content(msg: dict[str, Any]) -> dict[str, Any]:
    raw = (msg.get("body") or {}).get("content") or "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_post(content: dict[str, Any]) -> tuple[str, list[str]]:
    """富文本：返回 (拼接文本, 图片 key 列表)。"""
    texts: list[str] = []
    images: list[str] = []
    title = (content.get("title") or "").strip()
    if title:
        texts.append(title)
    for block in content.get("content") or []:
        if isinstance(block, list):
            nodes = block
        elif isinstance(block, dict):
            nodes = block.get("content", []) if isinstance(block.get("content"), list) else [block]
        else:
            continue
        line: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text":
                line.append(node.get("text") or "")
            elif tag == "img":
                images.append(node.get("image_key") or "")
            elif tag == "a":
                line.append(node.get("text") or node.get("href") or "")
        if line:
            texts.append("".join(line))
    return "\n".join(t for t in texts if t), [i for i in images if i]


def normalize(msg: dict[str, Any]) -> dict[str, Any]:
    msg_type = msg.get("msg_type", "")
    content = _load_content(msg)
    text = ""
    images: list[str] = []
    files: list[dict[str, str]] = []

    if msg_type == "text":
        text = content.get("text") or ""
    elif msg_type == "post":
        text, images = _extract_post(content)
    elif msg_type in IMAGE_TYPES:
        images = [content.get("image_key") or ""] if content.get("image_key") else []
    elif msg_type in FILE_TYPES:
        files = [
            {
                "key": content.get("file_key", ""),
                "name": content.get("file_name", ""),
                "type": msg_type,
            }
        ]
    elif msg_type not in IGNORE_TYPES:
        # 兜底：尝试取常见文本字段
        text = content.get("text") or content.get("content") or ""

    sender = msg.get("sender") or {}
    create_ms = int(msg.get("create_time") or 0)
    # 飞书给的是毫秒时间戳：统一转成东八区（北京时间），全站时间口径一致
    created = from_epoch_ms(create_ms)
    # 发送人姓名：历史消息接口不带，只有部分事件体/自建回调会带；没有则由上层按 open_id 反查
    sender_name = str(sender.get("name") or sender.get("sender_name") or msg.get("sender_name") or "")

    return {
        "message_id": msg.get("message_id", ""),
        "chat_id": msg.get("chat_id", ""),
        "msg_type": msg_type,
        "create_ms": create_ms,
        "created_at": created,
        "sender_id": sender.get("id", ""),
        "sender_name": sender_name.strip(),
        "sender_type": sender.get("sender_type", ""),
        "text": (text or "").strip(),
        # 被引用 / 被回复的那条消息 id（事件体与群历史接口都给 parent_id 字段）：
        # 后面靠它找回原订单，见 services.ingest._quoted_fields
        "parent_message_id": str(
            msg.get("parent_id") or msg.get("parent_message_id") or ""
        ).strip(),
        "image_keys": [i for i in images if i],
        "files": [f for f in files if f.get("key")],
        "raw": msg,
    }
