"""消息分类：只处理「订单模板消息」与「闲鱼订单截图」，其余过滤不处理。"""
from __future__ import annotations

import logging

from ..config import settings
from ..db import session_scope
from ..parser.engine import extract_kv_lines, parse_text
from ..parser.rules import get_rules
from . import policy as policy_service

logger = logging.getLogger(__name__)

# 出现任意一个即判定为订单截图（强特征，不需要凑关键词数）
STRONG_SCREENSHOT_HINTS = ("订单编号", "订单号", "交易单号", "平台单号", "担保账户")

TEXT_TYPES = ("text", "post")

# 订单强特征字段：只写 1 个标签行时，必须命中其中之一才算订单消息
ORDER_STRONG_FIELDS = (
    "receiver_name", "receiver_phone", "receiver_address",
    "sale_amount", "deal_amount", "total_amount", "order_no",
)
# 模板消息至少要有的「标签: 值」行数（真模板有 6~10 行，闲聊 / 通知基本没有）
MIN_TEMPLATE_LINES = 2
# 到这个行数就认为「很确定是订单模板」：人工标记学到的排除词不再生效，
# 免得真模板的备注里写了「全新未拆封」之类被误杀
SOLID_TEMPLATE_LINES = 3

# 机器人自身发出的消息（含回执）。飞书里机器人 sender_type 为 app/bot；
# 若不排除，回执文本会被当成新的订单模板消息重新解析 -> 无限自激回执。
BOT_SENDER_TYPES = ("app", "bot")
RECEIPT_PREFIXES = ("✅ 已收到并处理", "📦 已收到并处理")
# 回执正文特征：回执会复述订单号 / 产品 / 金额 / 收货，很容易被当成「订单模板消息」；
# 它前面常带 @提及，光看行首认不全，所以按正文特征再兜一层。
# 回执既不能被当新订单解析，也不能抢走群里的订单截图 —— 图挂到回执上等于丢图
# （2026-09-29 王小萌第二单就是这么丢的：截图被挂到了回执上）。
RECEIPT_MARKERS = ("你的订单已收到", "系统已经处理订单归档", "系统已开始处理")


def is_bot_message(sender_type: str = "", text: str = "") -> bool:
    """是否为机器人自身消息（含回执文本）。命中即不解析、不回执。"""
    if (sender_type or "").strip().lower() in BOT_SENDER_TYPES:
        return True
    head = (text or "").lstrip()
    if any(head.startswith(p) for p in RECEIPT_PREFIXES):
        return True
    return any(mark in (text or "") for mark in RECEIPT_MARKERS)


def policy_verdict(text: str, sender_id: str = "") -> tuple[str, str, bool]:
    """查一次人工策略（消息页标记学到的）。查不到 / 出错都返回空结论，交给规则判断。"""
    if not text or not settings.parse_policy_enabled:
        return "", "", False
    try:
        with session_scope() as session:
            return policy_service.decide(session, text, sender_id)
    except Exception as exc:  # noqa: BLE001  策略查询失败不能影响解析
        logger.warning("人工策略查询失败：%s", exc)
        return "", "", False


def is_template_message(text: str) -> bool:
    """文本消息是否为订单模板消息。

    判定分三步（宁可漏判也不误判：漏判可以在消息页人工标「其实是订单」补回来，
    误判会把群里的通知 / 闲聊当成订单归档，脏数据要人工一笔笔清理）：
      0. 人工策略：运营亲眼标过这一模一样的一条消息，直接采信；只是命中学到的话术
         （泛化推测）时，只在消息不像订单模板的情况下才采纳 —— 免得误杀真订单的备注；
      1. 能解析出平台，且命中必填字段（如「产品：」）；
      2. 消息要有模板结构：至少 2 个「标签: 值」行，或者命中收货人 / 手机号 /
         地址 / 金额这类订单强特征 —— 群里通知常说「戴森的产品大家注意下…」，
         只有一句人话、没有标签行，不算订单。
    """
    text = (text or "").strip()
    if not text:
        return False
    result = parse_text(text)
    kv_lines = len(extract_kv_lines(text)) if result.matched else 0
    has_strong = any(k in result.fields for k in ORDER_STRONG_FIELDS)
    structured = kv_lines >= MIN_TEMPLATE_LINES or has_strong
    solid = kv_lines >= SOLID_TEMPLATE_LINES or has_strong

    verdict, _reason, exact = policy_verdict(text)
    if verdict == policy_service.VERDICT_ORDER:
        return True
    # 退单话术（人工标记学到 / 规则里漏掉的「此单退」这类）：退单是对存量订单的处理，
    # 不能当新订单归档 —— 交给意图处理去原订单里找单退掉
    if verdict == policy_service.VERDICT_REFUND:
        return False
    if verdict == policy_service.VERDICT_EXCLUDE and (exact or not solid):
        return False

    if not result.matched:
        return False
    platform = get_rules().platform(result.platform)
    required = [f.key for f in (platform.fields if platform else []) if f.required]
    if required:
        if not any(k in result.fields for k in required):
            return False
    elif not result.fields:
        return False
    return structured


def is_order_screenshot(ocr_text: str) -> bool:
    """OCR 文本是否来自闲鱼订单截图。"""
    text = (ocr_text or "").strip()
    if not text:
        return False
    if any(k in text for k in STRONG_SCREENSHOT_HINTS):
        return True
    hits = sum(1 for k in settings.screenshot_keywords if k in text)
    return hits >= max(settings.screenshot_min_hits, 1)


def candidate_kind(msg_type: str) -> str:
    """入库时的候选类别：文本需解析后确认，故留空。"""
    if msg_type == "image":
        return "screenshot"
    if msg_type in TEXT_TYPES:
        return ""
    return "other"
