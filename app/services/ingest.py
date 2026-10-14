"""消息入库、截图聚合、订单归档主流程。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from datetime import timedelta
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..feishu.client import FeishuClient, get_client, save_bytes
from ..feishu.messages import normalize
from ..models import Order, OrderImage, PushBatchItem, RawMessage, SyncState
from ..timeutil import UTC, now
from ..parser.engine import ParseResult, looks_like_address, parse_text
from ..parser.ocr import crop_product_image, ocr_image, ocr_image_ex
from ..parser.order_no import order_no_hints_from_text
from . import learn as learn_service
from . import product as product_service
from .filter import (
    candidate_kind,
    is_bot_message,
    is_order_screenshot,
    is_template_message,
    policy_verdict,
)
from . import policy as policy_service
from .notify import (
    APP_SENDER_NAME,
    build_intent_receipt,
    deliver,
    is_app_sender,
    notify_context,
    render_notify,
    notify_cancel,
    notify_duplicate,
    notify_processed,
    notify_refund,
    notify_text,
)
from . import voice as voice_service
from . import dedupe as dedupe_service
from . import finance as finance_service
from . import intent as intent_service
from . import recall as recall_service
from . import refund as refund_service
from . import order_log as order_log_service
from . import buyer as buyer_service
from . import supplier as supplier_service
from . import order_state as order_state_service
from . import shop as shop_service

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    chat_id: str = ""
    fetched: int = 0
    created: int = 0
    parsed: int = 0
    ignored: int = 0
    filtered: int = 0
    failed: int = 0
    notified: int = 0
    images: int = 0
    # 同步过程中发现被撤回的消息数（对应订单会标记「撤回」，只留档）
    recalled: int = 0
    errors: list[str] = dc_field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "fetched": self.fetched,
            "created": self.created,
            "parsed": self.parsed,
            "ignored": self.ignored,
            "filtered": self.filtered,
            "failed": self.failed,
            "notified": self.notified,
            "images": self.images,
            "recalled": self.recalled,
            "errors": self.errors,
        }


# ------------------------------------------------------------------ 原始消息
# open_id -> 姓名：飞书通讯录接口有限频，进程内缓存，取不到就留空（页面显示 open_id 短串）
_SENDER_NAME_CACHE: dict[str, str] = {}


_MEMBER_NAME_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
# 群成员名单缓存时长（秒）：名单变动很少，10 分钟刷一次足够
_MEMBER_NAME_TTL = 600.0


def _member_names(client: FeishuClient | None, chat_id: str) -> dict[str, str]:
    """群成员 open_id -> 姓名：通讯录接口取不到人时的备用渠道（带缓存，避免反复打接口）。"""
    if not chat_id or client is None or not getattr(client, "enabled", False):
        return {}
    now = time.time()
    hit = _MEMBER_NAME_CACHE.get(chat_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        names = client.get_chat_members(chat_id) or {}
    except Exception:  # noqa: BLE001
        names = {}
    _MEMBER_NAME_CACHE[chat_id] = (now + _MEMBER_NAME_TTL, names)
    return names


def _name_from_history(session: Session | None, sender_id: str) -> str:
    """同一个人以前发言过、名字已经记下来的，直接沿用（一次接口都不用调）。"""
    if session is None or not sender_id:
        return ""
    name = session.scalar(
        select(RawMessage.sender_name)
        .where(RawMessage.sender_id == sender_id, RawMessage.sender_name != "")
        .limit(1)
    )
    return str(name or "").strip()


def resolve_sender_name(
    sender_id: str,
    client: FeishuClient | None = None,
    chat_id: str = "",
    session: Session | None = None,
) -> str:
    """按 open_id 反查发送人姓名；查不到返回空串（不抛错、不影响入库）。

    机器人 / 应用自己（cli_aa3f9f 这类 id）在通讯录里不存在，统一叫「闲鱼订单Agent」，
    不去查飞书、也不算「未识别员工」。

    反查顺序 —— 飞书通讯录偶尔会限频或没权限（这时候名字就白丢了），
    所以后面每层都留了兜底，尽量不把空名字写进库：
      1) 内存缓存
      2) 库里同一个人以前登记过的名字（最省事，一次接口都不用）
      3) 飞书通讯录接口
      4) 群成员名单（通讯录不通时的备用渠道）
    """
    sender_id = str(sender_id or "")
    if is_app_sender(sender_id):
        return APP_SENDER_NAME
    if not sender_id.startswith("ou_"):
        return ""
    if sender_id in _SENDER_NAME_CACHE:
        return _SENDER_NAME_CACHE[sender_id]

    name = _name_from_history(session, sender_id)
    if not name and chat_id:
        # 先问群成员：一次拉整份名单、还带 10 分钟缓存，几乎不额外付出代价；
        # 通讯录要逐个查、又常有权限问题，放在后面兜底
        name = _member_names(client or get_client(), chat_id).get(sender_id, "")
    if not name:
        try:
            name = (client or get_client()).get_user_name(sender_id)
        except Exception:  # noqa: BLE001
            name = ""
    if name:
        _SENDER_NAME_CACHE[sender_id] = name
    return name


def upsert_raw_message(session: Session, item: dict) -> tuple[RawMessage, bool]:
    """写入/更新原始消息，返回 (消息, 是否新增)。"""
    msg = session.scalar(select(RawMessage).where(RawMessage.message_id == item["message_id"]))
    is_new = msg is None
    if is_new:
        msg = RawMessage(message_id=item["message_id"], parse_status="pending")
        session.add(msg)
        # 只处理模板消息与订单截图，其余（文件/表情/系统消息等）入库即过滤
        kind = candidate_kind(item.get("msg_type", ""))
        msg.kind = kind
        # 机器人自身消息（含自己发出的回执）不解析、不回执，避免自激循环
        bot = is_bot_message(item.get("sender_type", ""), item.get("text", ""))
        if kind == "other" or bot:
            msg.kind = "other"
            msg.parse_status = "filtered"
            msg.ignore_reason = (
                "机器人自身消息（含自己发出的回执）：不解析、不回执"
                if bot
                else "不是订单模板消息，也不是订单截图：入库即过滤"
            )
    msg.chat_id = item.get("chat_id", "")
    msg.msg_type = item.get("msg_type", "")
    msg.msg_time = item.get("created_at")
    msg.sender_id = item.get("sender_id", "")
    # 发送人姓名：消息体带了就用；否则按 open_id 反查一次并缓存
    # （带上群 id 和当前会话：通讯录取不到时，还能用历史记录 / 群成员名单兜底）
    sender_name = str(item.get("sender_name") or "").strip()
    if not sender_name:
        sender_name = msg.sender_name or resolve_sender_name(
            msg.sender_id, chat_id=msg.chat_id, session=session
        )
    msg.sender_name = sender_name
    msg.sender_type = item.get("sender_type", "")
    msg.raw_text = item.get("text", "")
    # 回复 / 引用了某条群消息：存下被引用消息 id（补发截图也会用它挂到订单消息下，
    # 所以只在为空时写，不覆盖已有的归属关系）
    parent_id = str(item.get("parent_message_id") or "").strip()
    if parent_id and not msg.parent_message_id:
        msg.parent_message_id = parent_id
    msg.content_json = json.dumps(
        {k: v for k, v in item.items() if k != "raw"}, ensure_ascii=False, default=str
    )
    return msg, is_new


def _image_keys_of(msg: RawMessage) -> list[str]:
    try:
        data = json.loads(msg.content_json or "{}")
    except json.JSONDecodeError:
        return []
    return [k for k in data.get("image_keys", []) if k]


# ------------------------------------------------------------------ 截图聚合
# 没有订单截图时使用的临时订单编号前缀
TEMP_PREFIX = "TMP-"


def temp_order_no(msg: RawMessage, result: ParseResult) -> str:
    """临时订单编号：时间 + 产品信息（同一条消息重复处理得到同一个号）。"""
    stamp = (msg.msg_time or now()).strftime("%Y%m%d%H%M%S")
    product = str(result.fields.get("product_name") or "")
    seed = f"{msg.message_id}|{product}|{msg.chat_id}"
    code = hashlib.md5(seed.encode("utf-8")).hexdigest()[:6].upper()
    return f"{TEMP_PREFIX}{stamp}-{code}"


def _has_screenshot(session: Session, message_id: str) -> bool:
    """该消息是否已经配过订单截图（1 条模板只配 1 张）。

    只数**图片**消息：系统回执 / 补发的文本也会把 `parent_message_id` 指向这条模板消息，
    一起数进去就会误判「这条模板已经有图了」—— 下一条订单的截图从此配不进来，
    那张图最后被当「没归属」丢掉，整单跟着丢（2026-09-29 王小萌第二单）。
    """
    count = session.scalar(
        select(func.count(RawMessage.id)).where(
            RawMessage.parent_message_id == message_id,
            RawMessage.msg_type == "image",
        )
    )
    return bool(count)


def _order_image_count(session: Session, order_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(OrderImage.id)).where(OrderImage.order_id == order_id)
        )
        or 0
    )

def _pick_template_for_image(
    session: Session, chat_id: str, img: RawMessage, window: int
) -> RawMessage | None:
    """给这张截图挑一条「还没配图」的模板消息（前后都算，取时间最近的）。

    截图与模板的先后不固定：运营既会「先发文本再发图」，也会「先发图再发文本」。

    ⚠️ 这里踩过坑（2026-09-29 漏单）：**不能因为「最近的那条模板已经配过图」就把这张图丢掉**。
    连着下两单（同买家同款、地址一样）时，第一单的模板已经配了图，第二张图必须留给
    紧随其后的第二单 —— 否则第二单既没有截图（读不到订单编号），又会被「产品 + 收货人 + 手机号」
    指纹判成重复推送，直接丢单。
    """
    if img.msg_time is None:
        return None
    lo = img.msg_time - timedelta(seconds=window)
    hi = img.msg_time + timedelta(seconds=window)
    rows = session.scalars(
        select(RawMessage).where(
            RawMessage.chat_id == chat_id,
            RawMessage.msg_type.in_(("text", "post")),
            RawMessage.msg_time >= lo,
            RawMessage.msg_time <= hi,
        )
    ).all()
    # 只挑「还没配图」的订单模板消息：闲聊文本不吸引截图，避免无关消息被处理。
    # 机器人自己的回执**必须排除**：回执里复述了订单号 / 金额 / 收货，会被误判成模板消息，
    # 图挂到回执上就等于丢图（2026-09-29 王小萌第二单的截图就是这么挂到回执的）。
    rows = [
        row
        for row in rows
        if not row.is_recalled
        and not is_app_sender(row.sender_id)
        and not is_bot_message(row.sender_type, row.raw_text)
        and is_template_message(row.raw_text)
    ]
    rows.sort(key=lambda row: abs((row.msg_time - img.msg_time).total_seconds()))
    for row in rows:
        if not _has_screenshot(session, row.message_id):
            return row
    return None


def attach_images_to_text(session: Session, chat_id: str, window: int | None = None) -> int:
    """把截图消息挂到时间窗口内最近的一条文本消息上，返回挂接数量。"""
    window = settings.image_follow_window_seconds if window is None else window
    imgs = (
        session.scalars(
            select(RawMessage)
            .where(RawMessage.chat_id == chat_id, RawMessage.msg_type == "image")
            .where(RawMessage.parent_message_id == "")
            .order_by(RawMessage.msg_time)
        ).all()
    )
    attached = 0
    for img in imgs:
        # 已撤回的截图不再配给模板消息（对应订单已留档，不参与流程）
        if img.msg_time is None or img.is_recalled:
            continue
        target = _pick_template_for_image(session, chat_id, img, window)
        if target is None:
            continue
        img.parent_message_id = target.message_id
        img.kind = "screenshot"
        img.parse_status = "parsed"
        img.ignore_reason = ""
        attached += 1
        # 文本已解析过，需要补图 -> 重新解析一次
        if target.parse_status in ("parsed", "ignored"):
            target.parse_status = "pending"
    if attached:
        session.commit()
    return attached


# ------------------------------------------------------------------ 图片归档
def _should_retry_ocr(img: OrderImage) -> bool:
    """这张截图要不要重新识别一次。

    两种情况需要：上次没识别成功（失败 / 没启用 OCR）；或者是升级前的老结果
    （没跑过「订单编号」专项识别，ocr_attempts 还是 0）——算法升级后历史图不用删库重来。
    """
    if (settings.ocr_provider or "none").lower() == "none":
        return False
    path = Path(img.local_path or "")
    if not img.local_path or not path.exists():
        return False
    if img.ocr_status in ("failed", "skipped", "pending") or not (img.ocr_text or "").strip():
        return True
    return int(img.ocr_attempts or 0) == 0


def _refresh_image_ocr(img: OrderImage) -> None:
    """对已有截图重跑一次 OCR（含订单号专项识别），就地更新结果。"""
    path = Path(img.local_path or "")
    if not path.exists():
        return
    try:
        out = ocr_image_ex(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("重新识别截图失败 %s: %s", path, exc)
        return
    img.ocr_attempts = int(img.ocr_attempts or 0) + 1
    if out.status == "done" and out.text:
        img.ocr_status = out.status
        img.ocr_text = out.text
    if out.order_no:
        img.order_no_hint = ""
    elif out.order_no_hints:
        img.order_no_hint = out.order_no_hints[0]


def archive_image(
    session: Session, msg: RawMessage, file_key: str, order: Order | None, client: FeishuClient
) -> OrderImage | None:
    exists = session.scalar(
        select(OrderImage).where(
            OrderImage.message_id == msg.message_id, OrderImage.file_key == file_key
        )
    )
    if exists:
        if order and exists.order_id != order.id:
            exists.order_id = order.id
        # 上次没认出来 / 还是老算法认的：顺手再认一遍
        if _should_retry_ocr(exists):
            _refresh_image_ocr(exists)
            logger.info("截图重新识别：%s（订单 %s）", exists.filename, exists.order_id)
        if session.is_modified(exists):
            session.commit()
        return exists

    filename = f"{msg.message_id}_{file_key[:12]}.png"
    try:
        data = client.download_resource(msg.message_id, file_key, "image")
        path = save_bytes(data, filename)
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载截图失败 %s: %s", file_key, exc)
        img = OrderImage(
            order_id=order.id if order else None,
            message_id=msg.message_id,
            file_key=file_key,
            filename=filename,
            ocr_status="failed",
            ocr_text=f"下载失败: {exc}",
        )
        session.add(img)
        session.commit()
        return img

    rel = str(path)
    ocr_text, ocr_status, ocr_hint = "", "skipped", ""
    try:
        out = ocr_image_ex(path)
        ocr_text, ocr_status = out.text, out.status
        if not out.order_no and out.order_no_hints:
            ocr_hint = out.order_no_hints[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 异常: %s", exc)

    img = OrderImage(
        order_id=order.id if order else None,
        message_id=msg.message_id,
        file_key=file_key,
        filename=path.name,
        local_path=rel,
        mime="image/png",
        size=len(data),
        ocr_status=ocr_status,
        ocr_text=ocr_text or "",
        order_no_hint=ocr_hint,
        ocr_attempts=1,
    )
    session.add(img)
    session.commit()
    return img


def _collect_ocr_texts(
    session: Session, bundle: list[RawMessage], order: Order | None, client: FeishuClient
) -> tuple[list[str], int, list[OrderImage]]:
    texts: list[str] = []
    count = 0
    images: list[OrderImage] = []
    for img_msg in bundle:
        for key in _image_keys_of(img_msg):
            count += 1
            img = archive_image(session, img_msg, key, order, client)
            if img is None:
                continue
            images.append(img)
            if img.ocr_text:
                texts.append(img.ocr_text)
    return texts, count, images


# ------------------------------------------------------------------ 订单写入
# 别的模块也会往 review_reason 写原因（例如「缺退单理由」，见 refund.py）：
# 它们不是解析结论，重新解析 / 重识别截图时不能被覆盖掉，否则提醒就悄悄丢了。
_KEEP_REASON_PREFIXES: tuple[str, ...] = ("缺退单理由",)


def _mark_review(order: Order, result: ParseResult, hint: str = "") -> None:
    """标出「要不要人工确认」以及具体原因。

    原因会显示在订单列表（悬浮）与详情页，运营一眼就知道该核对哪里：
      - 解析存疑（例如字段值换行后被并进来的内容看着不对）；
      - 必填字段缺失；
      - 还没配上订单号（临时编号）：截图上疑似读到号时一并给出，可一键采用。
    后续补全（比如补到截图）不再需要确认时，原因一并清掉；但退单缺理由这类
    由别的流程挂上的原因会保留。
    """
    keep = [
        part
        for part in (order.review_reason or "").split("；")
        if part.strip().startswith(_KEEP_REASON_PREFIXES)
    ]
    temp_no = order.order_no.startswith(TEMP_PREFIX)
    order.needs_review = bool(result.needs_review or result.review_fields or temp_no or keep)
    if not order.needs_review:
        order.review_reason = ""
        return
    # 存疑的排在前面，运营一眼看到要核对什么；自动合并只是说明，排在最后
    parts: list[str] = []
    if keep:
        parts.append("；".join(keep))
    # 收货信息待核对排在存疑之前：地址 / 收货人 / 手机号错了会发错货，比别的先看到
    if result.review_fields:
        parts.append("待核对：" + "、".join(result.review_fields))
    if result.warnings:
        parts.append("存疑：" + "；".join(result.warnings))
    if result.missing_required:
        parts.append("缺失：" + "、".join(result.missing_required))
    if temp_no:
        parts.append("还没配上订单号（临时编号），请核对")
        if hint:
            parts.append(f"截图里疑似订单号 {hint}（没认准），可在详情页一键采用")
    if result.notes:
        parts.append("已合并：" + "；".join(result.notes))
    if parts:
        order.review_reason = "；".join(parts)


def _first_hint(texts: Iterable[str]) -> str:
    """OCR 文本里像订单号的长数字（没认准），用来提示人工确认。"""
    for text in texts or []:
        hints = order_no_hints_from_text(text or "")
        if hints:
            return hints[0]
    return ""


def order_no_hint_of(session: Session, order: Order) -> str:
    """该订单截图里识别到的「疑似订单号」（没认准时用来提示人工确认）。"""
    if not order.id:
        return ""
    for img in session.scalars(
        select(OrderImage).where(OrderImage.order_id == order.id).order_by(OrderImage.id)
    ):
        if img.order_no_hint:
            return img.order_no_hint
    return ""


# 一次性修复（reparse_truncated）时允许补齐的文本字段：(Order 属性, 解析字段)
REPARSE_FIELDS: tuple[tuple[str, str], ...] = (
    ("product_name", "product_name"),
    ("spec", "spec"),
    ("receiver_name", "receiver_name"),
    ("receiver_address", "receiver_address"),
    ("supplier_name", "supplier_name"),
    ("xianyu_account", "xianyu_account"),
    ("payee_name", "payee_name"),
    ("note", "note"),
)


def reparse_truncated(session: Session, limit: int = 2000) -> dict:
    """把「值被换行截断」的历史订单补回来（解析升级后的一次性修复）。

    只补「本次解析出的值是现有值的前缀 + 更长」的字段 —— 说明当初只取到了前半截；
    人工改过的值（更短或不同写法）不动，撤回的订单也不动。
    """
    from ..models import not_recalled

    fixed_fields = 0
    fixed_orders = 0
    checked = 0
    rows = session.scalars(
        select(Order).where(not_recalled(), Order.raw_text != "").limit(limit)
    ).all()
    for order in rows:
        checked += 1
        result = parse_text(order.raw_text, platform_key=order.platform or None)
        changed = False
        for attr, key in REPARSE_FIELDS:
            new_value = result.fields.get(key)
            if not isinstance(new_value, str):
                continue
            old_value = (getattr(order, attr, "") or "").strip()
            if not old_value:
                continue
            if new_value.startswith(old_value) and len(new_value) > len(old_value):
                setattr(order, attr, new_value)
                fixed_fields += 1
                changed = True
        if changed:
            fixed_orders += 1
            order.updated_at = now()
        # 重新解析后不再存疑的，把提示清掉；新发现存疑的补上说明
        _mark_review(order, result)
    if rows:
        session.commit()
    return {"checked": checked, "orders": fixed_orders, "fields": fixed_fields}


# 按订单截图 OCR 重新解析时可补齐的金额字段：(Order 属性, 解析字段)
# 只补「现在还是 0 / 空」的：人工改过的值不动
REPARSE_OCR_FIELDS: tuple[tuple[str, str], ...] = (
    ("deal_amount", "deal_amount"),
    ("total_amount", "total_amount"),
    ("shipping_fee", "shipping_fee"),
)
# 一眼就看出是 OCR 噪声的「买家昵称」：解析升级后清掉（宁缺勿错，留空让运营补）
# 口径与 services/buyer 共用一份，清掉的空格随后按「收货人 + 手机号」补成买家标识
BUYER_NICK_NOISE = buyer_service.NOISE_VALUES


def reparse_ocr_fields(session: Session, limit: int = 2000) -> dict:
    """解析规则升级后，按订单截图 OCR 把历史订单「没取到的字段」补回来。

    与 reparse_truncated 的分工：
      · reparse_truncated 补的是群消息里被换行截断的文本字段；
      · 这里补的是**只有截图里才有**的字段 —— 成交价 / 商品总价 / 运费
        （OCR 常把 ¥ 认成 Y / x，老规则认不出来就一直是 0），
        顺手清掉被 OCR 噪声填进去的买家昵称（「已付款」「了昵称」这种）。

    只补当前为空（0）的字段，人工改过的值与撤回的订单都不动；补完同步资金流水。
    """
    from ..models import not_recalled

    fixed_fields = 0
    cleaned = 0
    fixed_orders = 0
    checked = with_ocr = 0
    rows = session.scalars(
        select(Order).where(not_recalled(), Order.raw_text != "").limit(limit)
    ).all()
    for order in rows:
        checked += 1
        images = session.scalars(
            select(OrderImage).where(OrderImage.order_id == order.id)
        ).all()
        ocr_texts = [im.ocr_text for im in images if (im.ocr_text or "").strip()]
        if not ocr_texts:
            continue
        with_ocr += 1
        result = parse_text(order.raw_text, ocr_texts, platform_key=order.platform or None)
        changed = False
        for attr, key in REPARSE_OCR_FIELDS:
            value = result.fields.get(key)
            if not isinstance(value, (int, float)) or value <= 0:
                continue
            if float(getattr(order, attr, 0) or 0) > 0:  # 只补缺失的，人工改过的不动
                continue
            setattr(order, attr, round(float(value), 2))
            fixed_fields += 1
            changed = True
        if (order.buyer_nick or "").strip() in BUYER_NICK_NOISE:
            order.buyer_nick = ""
            cleaned += 1
            changed = True
        if buyer_service.fill_buyer(order):   # 清掉的 / 还空的：补成「收货人 + 手机号」
            cleaned += 1
            changed = True
        if changed:
            fixed_orders += 1
            order.updated_at = now()
            # 金额补回来了：结算流水跟着同步（应收按成交价算）
            finance_service.sync_order(session, order)
    if rows:
        session.commit()
    logger.info(
        "OCR 补字段：检查 %s 单，有截图 %s 单，补齐 %s 个字段，清理 %s 个噪声昵称",
        checked,
        with_ocr,
        fixed_fields,
        cleaned,
    )
    return {
        "checked": checked,
        "with_ocr": with_ocr,
        "orders": fixed_orders,
        "fields": fixed_fields,
        "cleaned": cleaned,
    }


def _apply_parsed(
    order: Order,
    result: ParseResult,
    only_fill_empty: bool = False,
    session: Session | None = None,
) -> None:
    mapping = {
        "order_no": "order_no",
        "buyer_nick": "buyer_nick",
        "product_name": "product_name",
        "spec": "spec",
        "quantity": "quantity",
        "sale_amount": "sale_amount",
        "deal_amount": "deal_amount",
        "total_amount": "total_amount",
        "cost_amount": "cost_amount",
        "shipping_fee": "shipping_fee",
        "platform_fee": "platform_fee",
        "receiver_name": "receiver_name",
        "receiver_phone": "receiver_phone",
        "receiver_address": "receiver_address",
        "supplier_name": "supplier_name",
        "xianyu_account": "xianyu_account",
        "payee_name": "payee_name",
        "note": "note",
    }
    for src_key, attr in mapping.items():
        if src_key not in result.fields:
            continue
        current = getattr(order, attr)
        if only_fill_empty and current not in (None, "", 0):
            continue
        value = result.fields[src_key]
        # 解析出的地址不像地址（典型：截图里的营销文案被当成了地址）—— 宁缺勿错，不写进订单
        if attr == "receiver_address" and not looks_like_address(value):
            continue
        if attr == "supplier_name" and session is not None:
            # 模板里的「所属供应链」填的是供应商编码（如 GYS001）：对上档案后
            # 统一存成「供应商名称（供应商编码）」，列表与统计就不会出现多种写法；
            # 档案里还没有的（群里第一次提到的新供应商）顺带补一条档案，运营后面补全资料
            value = supplier_service.ensure_order_supplier(
                session, str(value), source="群消息订单识别"
            )
        # 成本：自动流程写进来的值看着不对（典型：截图上的划线价被当成成本，比售价还高）
        # 就直接不写 —— 标「待核对」并留痕，人工在详情页自己填，见 services/cost_guard
        if attr == "cost_amount":
            from .cost_guard import guard_cost

            # 判据售价：订单上可能还没写（新单先算成本、后写售价），退回本次解析到的成交价 / 售价
            sale_hint = 0.0
            if not float(getattr(order, "sale_amount", 0) or 0):
                sale_hint = float(
                    result.fields.get("deal_amount")
                    or result.fields.get("sale_amount")
                    or 0
                )
            # 来源写准：值来自历史修正样本时别写成「解析（群消息 + 截图）」——
            # 否则排查会以为是截图 OCR 认错（2026-10-01 那次成本 133 就因此绕了远路）
            learned = str(result.sources.get("cost_amount") or "").startswith("learned:")
            ok, _reason = guard_cost(
                session,
                order,
                value,
                source="历史修正样本" if learned else "解析（群消息 + 截图）",
                sale_hint=sale_hint,
            )
            if not ok:
                continue
        setattr(order, attr, value)
    if "ordered_at" in result.fields and result.fields["ordered_at"]:
        if not only_fill_empty or order.ordered_at is None:
            order.ordered_at = result.fields["ordered_at"]


def _attach_product_image(order: Order, img: OrderImage, force: bool = False) -> None:
    """把截图里的商品缩略图裁出来挂到订单上（裁不出来就跳过）。

    force=True 用于手动重裁：忽略「已裁过」与开关判断，直接覆盖旧图。
    """
    if order.product_image_path or not img.local_path:
        return
    if not settings.product_image_crop and not force:
        return
    src = Path(img.local_path)
    if not src.exists():
        return
    out = src.with_name(f"{src.stem}_product.png")
    cropped = crop_product_image(src, out)
    if cropped:
        order.product_image_path = cropped


def recrop_product_image(session: Session, order: Order) -> bool:
    """按订单截图重裁单笔订单的商品图（详情页「重新裁取」按钮用）。"""
    images = [img for img in order.images if img.local_path]
    if not images:
        return False
    order.product_image_path = ""
    _attach_product_image(order, images[0], force=True)
    session.commit()
    return bool(order.product_image_path)


def backfill_product_images(
    session: Session, limit: int = 0, force: bool = False
) -> dict:
    """按订单截图补裁 / 重裁商品图（历史订单补齐、裁剪算法升级后批量重跑）。

    默认只处理还没有商品图的订单；force=True 时连已有商品图的一起重裁覆盖。
    """
    stmt = select(Order).order_by(Order.id.asc())
    if not force:
        stmt = stmt.where(
            (Order.product_image_path == "") | (Order.product_image_path.is_(None))
        )
    if limit:
        stmt = stmt.limit(limit)
    stats = {"scanned": 0, "cropped": 0, "skipped": 0}
    for order in session.scalars(stmt).all():
        stats["scanned"] += 1
        images = [img for img in order.images if img.local_path]
        if not images:
            stats["skipped"] += 1
            continue
        if force:
            order.product_image_path = ""
        _attach_product_image(order, images[0], force=True)
        if order.product_image_path:
            stats["cropped"] += 1
        else:
            stats["skipped"] += 1
        session.commit()
    return stats


_FIELD_ATTRS = (
    "buyer_nick", "receiver_name", "receiver_phone", "receiver_address",
    "product_name", "spec", "supplier_name", "xianyu_account", "payee_name",
    "chat_id", "chat_name", "raw_text", "note", "product_image_path",
    "sender_id", "sender_name",
)
_AMOUNT_ATTRS = (
    "sale_amount", "deal_amount", "total_amount", "cost_amount",
    "shipping_fee", "platform_fee", "profit_amount",
)


def _merge_orders(session: Session, src: Order, dst: Order) -> Order:
    """把临时订单并到真实订单上：截图、批次项、缺失字段迁移过去，源订单删除。"""
    src_no = src.order_no
    for img in session.scalars(select(OrderImage).where(OrderImage.order_id == src.id)).all():
        img.order_id = dst.id
    for item in session.scalars(
        select(PushBatchItem).where(PushBatchItem.order_id == src.id)
    ).all():
        dup = session.scalar(
            select(PushBatchItem).where(
                PushBatchItem.batch_id == item.batch_id, PushBatchItem.order_id == dst.id
            )
        )
        if dup is not None:
            session.delete(item)
        else:
            item.order_id = dst.id
    for attr in _FIELD_ATTRS:
        if not getattr(dst, attr, "") and getattr(src, attr, ""):
            setattr(dst, attr, getattr(src, attr))
    for attr in _AMOUNT_ATTRS:
        if not getattr(dst, attr, 0) and getattr(src, attr, 0):
            value = getattr(src, attr)
            if attr == "cost_amount":
                # 成本从临时单搬过来也要过护栏：比售价还高就留空等人工，见 services/cost_guard
                from .cost_guard import guard_cost

                ok, _reason = guard_cost(session, dst, value, source="临时单合并")
                if not ok:
                    continue
            setattr(dst, attr, value)
    if dst.ordered_at is None and src.ordered_at is not None:
        dst.ordered_at = src.ordered_at
    if dst.msg_time is None and src.msg_time is not None:
        dst.msg_time = src.msg_time
    if src.order_no and not dst.temp_order_no:
        dst.temp_order_no = src.order_no
    for m in session.scalars(select(RawMessage).where(RawMessage.order_id == src.id)).all():
        m.order_id = dst.id
    session.delete(src)
    session.flush()
    dst.updated_at = now()
    logger.info("订单合并：%s(%s) -> %s(%s)", src_no, src.id, dst.order_no, dst.id)
    return dst


def apply_real_order_no(session: Session, order: Order, real_no: str) -> Order:
    """把订单升级成真实订单号（临时编号留档备查）。

    真实编号已经被别的订单占用时，直接把这一单并过去；返回升级后（或合并后）的订单。
    补发截图、手动上传截图、定时重识别都走这里，保证三条路径行为一致。
    """
    real_no = str(real_no or "").strip()
    if not real_no or real_no == order.order_no:
        return order
    existing = session.scalar(
        select(Order).where(Order.platform == order.platform, Order.order_no == real_no)
    )
    if existing is not None and existing.id != order.id:
        logger.info("订单号 %s 已被订单 %s 占用：把 %s 并过去", real_no, existing.id, order.order_no)
        return _merge_orders(session, order, existing)
    if order.order_no.startswith(TEMP_PREFIX) and not order.temp_order_no:
        order.temp_order_no = order.order_no
    previous = order.order_no
    order.order_no = real_no
    logger.info("订单单号升级：%s -> %s（订单 %s）", previous, real_no, order.id)
    return order


def _image_digest(local_path: str | None) -> str:
    """订单截图的内容哈希（同一张图重复发 -> 同一个值；不同截图 -> 不同值）。"""
    path_text = str(local_path or "").strip()
    if not path_text:
        return ""
    try:
        path = Path(path_text)
        if not path.is_file():
            return ""
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except Exception as exc:  # noqa: BLE001  读取失败就当比不了
        logger.warning("截图内容哈希失败 %s: %s", path_text, exc)
        return ""


def _first_image(images) -> OrderImage | None:
    """一组截图里第一张「落了盘」的（没落盘的比不了哈希）。"""
    for img in images or []:
        if str(getattr(img, "local_path", "") or "").strip():
            return img
    return None


def _first_order_no_hint(images) -> str:
    """一组截图里第一张读到的疑似订单编号（整图长数字，没在标签旁确认的那种）。"""
    for img in images or []:
        hint = str(getattr(img, "order_no_hint", "") or "").strip()
        if hint:
            return hint
    return ""


def is_formal_order_no(order_no: str | None) -> bool:
    """是不是正式订单编号（截图 / 群里读到的真号），而不是临时编号 TMP-…。"""
    value = str(order_no or "").strip()
    return bool(value) and not value.startswith(TEMP_PREFIX)


def _looks_like_separate_order(order: Order, images, real_no: str = "") -> bool:
    """指纹命中的那个订单，其实是「同一买家同款下的另一笔单」吗？

    指纹只认「产品 + 收货人 + 手机号」—— 同一个买家买两件同款、地址一样，指纹必然相同，
    只看指纹就会把两笔真实订单并成一单（少发一件货、少算一笔成本）。这里找**反向证据**：

      1. 本次带了订单截图，而那个订单已有截图、两张图内容不一样 —— 下一次单一张截图；
      2. 两张截图都读到了疑似订单编号，且互不相同；
      3. 那个订单已经是正式单号（非 TMP-），本次又从截图读到另一个正式单号。
         （本次读到正式单号、那个订单还是 TMP- 的，属于「补图后拿到真号」，仍按同一单处理）

    比不了（缺截图 / 老数据没落盘 / 两边都没号）就返回 False，保持「重复同步」的老口径。
    """
    order_images = list(getattr(order, "images", None) or [])
    mine, theirs = _first_image(images), _first_image(order_images)
    if mine is not None and theirs is not None:
        my_digest, their_digest = _image_digest(mine.local_path), _image_digest(theirs.local_path)
        if my_digest and their_digest and my_digest != their_digest:
            return True
    my_hint, their_hint = _first_order_no_hint(images), _first_order_no_hint(order_images)
    if my_hint and their_hint and my_hint != their_hint:
        return True
    return bool(
        is_formal_order_no(real_no)
        and is_formal_order_no(order.order_no)
        and real_no != order.order_no
    )


def _is_independent_resend(session: Session, msg: RawMessage, order: Order) -> bool:
    """这条模板消息会不会其实是「同一买家同款下的另一笔单」，只是指纹看不出来？

    指纹只认「产品 + 收货人 + 手机号」—— 同一个买家买两件同款、地址一样，指纹必然相同。
    `_looks_like_separate_order` 比的是**截图证据**（两张图 / 两个订单编号）；这里补上
    **消息证据**，专治「两单同款、第二张图还没挂上（甚至图先发、模板后发）」的漏单
    （2026-09-29 王小萌两单只留下一单，就是这么丢的）：

      · 这条消息与撞上的那笔订单**不是同一条飞书消息** —— 群里确实又发了一次（不同 message_id）；
      · 且这条消息名下有自己独立的截图消息（图先到、模板后到也算，含还没下载识别的）。

    两条都成立就按独立订单新建，绝不静默并单：并错了会少发一件货、少算一笔成本，
    拆错了（其实是重复贴同一单）只是多一单，且会打「待复核」标记，人工一键合并即可。
    """
    if str(order.source_message_id or "") == str(msg.message_id or ""):
        return False   # 同一条消息的重复处理：幂等，不拆
    shot = session.scalar(
        select(func.count(RawMessage.id)).where(
            RawMessage.parent_message_id == msg.message_id,
            RawMessage.msg_type == "image",
        )
    )
    return bool(shot)


def _ensure_shop_of_message(
    session: Session,
    result: ParseResult,
    sender_name: str,
    order: Order | None = None,
) -> None:
    """按解析结果认领 / 建档店铺：正向出单、退单、撤单三条路都走这里（2026-10-11）。

    学员模板（带「学员店铺名称 / 学员姓名 / 带教」）：学员店铺名称就是这单的出货账号
    （这种模板没有出货闲鱼号），按学员店铺建档 —— 性质标「学员店铺」、账号运营人写
    学员姓名、带教写进「所属带教」。
    普通模板：按出货闲鱼号建档，运营人 = 飞书发送人（老口径不变）。

    建档失败只记日志，绝不打断归档 / 退单处理。
    """
    fields = getattr(result, "fields", {}) or {}
    student_shop = str(fields.get("student_shop") or "").strip()
    # 退单 / 撤单（order 为空）只处理学员店铺：普通店铺只在正向建单时按出货账号建档，
    # 免得一条退单消息就凭空补一条店铺档案
    if order is None and not student_shop:
        return
    try:
        if student_shop:
            # 订单上没写出货闲鱼号时，学员店铺名称补上去（订单要挂到店铺上，资金与统计按店铺走）
            if order is not None and not (order.xianyu_account or "").strip():
                order.xianyu_account = student_shop
            shop_service.ensure_student_shop(
                session,
                student_shop,
                student_name=str(fields.get("student_name") or "").strip(),
                coach=str(fields.get("coach") or "").strip(),
                fallback_operator=sender_name or "",
                source="群消息学员订单识别",
            )
            return
        # 订单上没写（还没走 _apply_parsed 时）就退回解析出来的出货闲鱼号
        account = (order.xianyu_account if order is not None else "") or str(
            fields.get("xianyu_account") or ""
        )
        shop_service.ensure_shop(
            session,
            account,
            operator=sender_name or "",
            source="群消息订单识别",
        )
    except Exception as exc:  # noqa: BLE001  店铺建档失败不影响归档
        logger.warning("店铺自动建档失败 %s: %s", student_shop or (order.xianyu_account if order else ""), exc)


def save_order(
    session: Session,
    msg: RawMessage,
    result: ParseResult,
    client: FeishuClient,
    ocr_summary: str = "",
    refund_keyword: str = "",
) -> Order:
    platform = result.platform or "xianyu"

    # 1) 先把本条模板消息配套的订单截图下载 + OCR（截图信息更全：订单编号、商品全称、成交价）
    bundle = session.scalars(
        select(RawMessage).where(RawMessage.parent_message_id == msg.message_id)
    ).all()
    ocr_texts, image_count, images = _collect_ocr_texts(session, list(bundle), None, client)

    # 2) 群消息 + 截图合并解析：模板消息优先，截图为补全（缺失字段补齐、更完整的字段覆盖）
    merged = (
        parse_text(msg.raw_text, ocr_texts, platform_key=result.platform or None)
        if ocr_texts
        else result
    )

    # 2.5) 用历史人工纠正校准：同样写法以前被人改过，就按那次的结果来（见 services/learn.py）
    learn_service.apply_known_corrections(session, merged, msg.raw_text)

    # 3) 定订单主键：截图里的订单编号 > 已有编号 > 临时编号（时间 + 产品信息）
    real_no = str(merged.fields.get("order_no") or "").strip()
    current = session.get(Order, msg.order_id) if msg.order_id else None
    temp_no = ""
    if real_no:
        order_no = real_no
    elif current is not None and current.order_no:
        order_no = current.order_no
    else:
        order_no = temp_order_no(msg, merged)
        temp_no = order_no

    existing = session.scalar(
        select(Order).where(Order.platform == platform, Order.order_no == order_no)
    )

    # 重复发送同一笔订单：先按业务指纹认出「同一单」，再看有没有实质变更
    duplicate_of: Order | None = None
    split_from: Order | None = None
    split_reason = ""
    split_suspect = False
    dup_changes: list[dict] = []
    fingerprint = ""
    identity_change = False   # 按「身份信息变更」认到同一单（不是内容一致的重复发送）
    if current is None and existing is None:
        fingerprint = dedupe_service.identity_fingerprint(merged.fields)
        duplicate_of = dedupe_service.find_duplicate(
            session, platform, real_no, fingerprint
        )
        if duplicate_of is None:
            # 群里「这个改一下手机号码」再贴一遍完整订单：身份指纹含手机号，
            # 改了号就认不出是同一单 —— 以前会白白多建一笔。这里按「产品 + 收货人一致、
            # 手机号确实变了」认成同一单的变更，交给下面 diff_fields / apply_changes 更新。
            duplicate_of = dedupe_service.find_by_identity_change(
                session, platform, merged.fields
            )
            if duplicate_of is not None:
                identity_change = True
                logger.info(
                    "订单 %s 身份信息有变更（手机号 %s -> %s）：按同一单更新，不新建",
                    duplicate_of.order_no,
                    dedupe_service.norm_digits(duplicate_of.receiver_phone) or "（空）",
                    dedupe_service.norm_digits(merged.fields.get("receiver_phone")) or "（空）",
                )
        if duplicate_of is not None:
            # 指纹只认「产品 + 收货人 + 手机号」：同一个买家同款下两单也长这样，
            # 所以还要看反向证据 —— 两张订单截图不是同一张、或截图上的订单编号不一样，
            # 那就是两笔真实订单，不能并成一单（并了就会少发一件货、少算一笔成本）。
            if _looks_like_separate_order(duplicate_of, images, real_no):
                logger.info(
                    "同一买家同款但属于两笔订单（截图 / 订单编号不同）：新建订单 %s，不并入 %s",
                    real_no or "（截图未读出单号）",
                    duplicate_of.order_no,
                )
                split_reason = "订单截图 / 订单编号不同"
                split_from, duplicate_of, dup_changes, existing = duplicate_of, None, [], None
            elif _is_independent_resend(session, msg, duplicate_of):
                # 内容一模一样（改都没改），但这是**另一条消息**、还带着自己的截图 ——
                # 那就是同买家同款的第二笔单，宁可拆开标「待复核」，也不静默吞掉（会少发一件货）
                logger.info(
                    "同买家同款、内容一致，但来自另一条消息且另有截图：按独立订单独立归档（%s 待复核）",
                    duplicate_of.order_no,
                )
                split_reason = "群里先后发了内容一致、但各自带截图的另一条订单消息"
                split_suspect = True
                split_from, duplicate_of, dup_changes, existing = duplicate_of, None, [], None
            else:
                dup_changes = dedupe_service.diff_fields(duplicate_of, merged.fields)
                existing = duplicate_of
                if dup_changes or image_count:
                    logger.info(
                        "订单 %s 重复同步但有变更：%s",
                        duplicate_of.order_no,
                        "、".join(f"{c['label']}" for c in dup_changes) or "补发订单截图",
                    )

    if current is not None and current.order_no != order_no:
        # 补到截图后拿到真实订单编号：临时订单直接升级（真实编号已被别的订单占用则合并过去）
        order = apply_real_order_no(session, current, order_no)
        is_new = False
    else:
        order = existing if existing is not None else current
        is_new = order is None
        if is_new:
            # 群里同步订单、刚归档出来 -> 订单状态「新建」
            order = Order(
                platform=platform,
                order_no=order_no,
                order_state=order_state_service.ORDER_STATE_NEW,
            )
            session.add(order)

    # 撤回后群里又重发了同一单：自动撤销撤回标记，订单重新参与统计与流程
    if not is_new and order is not None and order.is_recalled:
        recall_service.undo_recalled(session, order, commit=False)
        logger.info("订单 %s 撤回后重新收到群消息：已自动撤销撤回标记", order.order_no)

    if duplicate_of is not None and not dup_changes and not image_count:
        # 群里重复发了同一笔订单且内容没变：不重复归档、不重复推单、不重复播报
        msg.kind = "template"
        msg.order_id = duplicate_of.id
        msg.parse_status = "duplicate"
        msg.ignore_reason = (
            f"重复发送相同内容：与已归档订单 {duplicate_of.order_no} 完全一致，未重复归档"
        )
        session.commit()
        return duplicate_of

    if temp_no and is_new:
        order.temp_order_no = temp_no

    _apply_parsed(order, merged, only_fill_empty=not is_new, session=session)
    # 买家一栏：模板里没有买家昵称，按「收货人 + 手机号」归档（跨单认人用，空的 / 噪声才补）
    buyer_service.fill_buyer(order)
    duplicated_changed = False
    if duplicate_of is not None:
        # 重复同步但内容有变更 -> 按正常流程更新原订单
        dedupe_service.apply_changes(order, dup_changes, merged.fields)
        duplicated_changed = bool(dup_changes)
        if identity_change:
            # 手机号这类身份信息变了却认成同一单：留一条处理日志，事后能看出是「更新」而非新建
            order_log_service.log(
                session,
                order,
                order_log_service.ACTION_IDENTITY_CHANGE,
                detail=(
                    "群里这条消息改的是手机号等身份信息，按同一笔订单更新："
                    + (
                        "、".join(f"{c['label']} {c['old']} → {c['new']}" for c in dup_changes)
                        or "（字段值与档案一致，无实际变更）"
                    )
                ),
                operator=order_log_service.SYSTEM_OPERATOR,
            )
    # 重复同步/补全可能把编码原文写回来：统一收敛成「名称（编码）」
    # （新供应商顺带补档案，见 ensure_order_supplier）
    order.supplier_name = supplier_service.ensure_order_supplier(
        session, order.supplier_name, source="群消息订单识别"
    )
    # 挂到商品档案：商品维度看销量 / 毛利（商品库里没有对应的就保持为空，不硬造）
    product_service.attach_order(session, order)
    # 业务指纹：后续群里再发同一单时靠它认出来
    order.fingerprint = fingerprint or dedupe_service.order_fingerprint(order)
    # 来源消息只在还没有来源时写入：后续转发的回执 / 补图消息不能顶掉真正的下单消息，
    # 否则订单的「发送人」会变成应用自己（cli_xxx），回单同步就 @ 不到人了。
    first_source = not order.source_message_id or order.source_message_id == msg.message_id
    if not order.source_message_id:
        order.source_message_id = msg.message_id
    order.chat_id = order.chat_id or msg.chat_id
    order.chat_name = order.chat_name or msg.chat_name
    # 上行消息维度补全到订单：发送人、发送时间（只认下单那条消息，补图/转发不覆盖）
    if msg.sender_id and not is_app_sender(msg.sender_id) and (first_source or not order.sender_id):
        order.sender_id = msg.sender_id
    if msg.sender_name and (first_source or not order.sender_name):
        order.sender_name = msg.sender_name
    if msg.msg_time is not None and (first_source or order.msg_time is None):
        order.msg_time = msg.msg_time
    order.raw_text = msg.raw_text or ocr_summary
    order.parse_confidence = merged.confidence
    # 临时编号（还没配到截图）需要人工核对；解析存疑的也会标出来并写明原因
    # 截图上读到了疑似订单号（没认准）时一并带上，运营可以一键采用
    hint = order_no_hint_of(session, order) or next(
        (i.order_no_hint for i in images if i.order_no_hint), ""
    )
    _mark_review(order, merged, hint=hint)
    order.updated_at = now()
    if order.ordered_at is None:
        order.ordered_at = msg.msg_time
    session.flush()

    # 同买家同款、但确实是另一笔单：按两笔订单归档，在订单日志里留痕（可追溯）
    if split_from is not None:
        order_log_service.log(
            session,
            order,
            order_log_service.ACTION_SPLIT_DUPLICATE,
            detail=(
                f"与订单 {split_from.order_no} 是同买家同款，{split_reason}，"
                "按两笔订单分别归档（若判断为重复，可在订单详情合并）"
            ),
            operator=order_log_service.SYSTEM_OPERATOR,
        )
        if split_suspect:
            # 内容一字不差却有独立截图 / 是另一条消息：大概率是两笔真单，也可能是重复贴，
            # 标「待复核」让人工一眼看到（并错误少发货，拆错只是多一单、可合并）
            order.needs_review = True
            note = (
                f"疑似重复推送：与订单 {split_from.order_no} 内容完全一致，"
                "但另有独立截图或来自另一条群消息，已按独立订单归档；"
                "若确认是同一单，请在订单详情合并"
            )
            reason = (order.review_reason or "").strip()
            order.review_reason = reason if note in reason else (
                f"{reason}；{note}" if reason else note
            )

    # 截图归到该订单，并裁出商品缩略图（1 条模板只配 1 张截图）
    for img in images:
        if img.order_id != order.id:
            img.order_id = order.id
    if images:
        _attach_product_image(order, images[0])
    else:
        logger.debug("模板消息 %s 暂无配套订单截图", msg.message_id)

    # 出货账号（店铺）：店铺管理里还没有这个账号就自动补一条（运营人 = 飞书发送人）
    _ensure_shop_of_message(session, result, order.sender_name or "", order=order)

    msg.parse_status = "parsed"
    # 资金结算：订单归档 / 金额修正后同步两条结算流水（公司自营应收、公司自营应付）
    try:
        finance_service.sync_order(session, order)
    except Exception as exc:  # noqa: BLE001  结算流水失败不影响归档
        logger.warning("结算流水同步失败 %s: %s", order.order_no, exc)
    session.commit()
    # 群里同步到一笔新订单 -> 语音播报提醒
    # 首次建单、或重复同步但内容有变更时播报；退单消息由 refund 模块播退单文案
    if not refund_keyword and (is_new or duplicated_changed):
        try:
            voice_service.order_event(session, order)
        except Exception as exc:  # noqa: BLE001  播报失败不影响归档
            logger.warning("新订单语音播报失败 %s: %s", order.order_no, exc)
    return order


def backfill_order_source(session: Session) -> int:
    """老订单补全上行消息维度：发送人 / 发送时间 / 群名（按来源消息回填，已有值不覆盖）。"""
    pairs = session.execute(
        select(Order.id, RawMessage.id)
        .join(RawMessage, RawMessage.message_id == Order.source_message_id)
        .where(
            Order.source_message_id != "",
            (Order.sender_id == "") | (Order.msg_time.is_(None)) | (Order.chat_name == ""),
        )
    ).all()
    updated = 0
    for order_id, msg_id in pairs:
        order = session.get(Order, order_id)
        msg = session.get(RawMessage, msg_id)
        if order is None or msg is None:
            continue
        changed = False
        if not order.sender_id and msg.sender_id:
            order.sender_id = msg.sender_id
            changed = True
        if not order.sender_name and msg.sender_name:
            order.sender_name = msg.sender_name
            changed = True
        if order.msg_time is None and msg.msg_time is not None:
            order.msg_time = msg.msg_time
            changed = True
        if not order.chat_name and msg.chat_name:
            order.chat_name = msg.chat_name
            changed = True
        if changed:
            updated += 1
    if updated:
        session.commit()
    return updated


def backfill_sender_names(
    session: Session, client: FeishuClient | None = None, limit: int = 200
) -> int:
    """把历史记录里缺失的发送人姓名补全（飞书通讯录 -> 群成员兜底）。

    需要在飞书开放平台开通通讯录权限（contact:user.base:readonly）或 im:chat 权限；
    没有权限时返回 0，页面继续显示 open_id 短串，不影响其它功能。
    """
    client = client or get_client()
    take = max(int(limit), 1)
    msgs = session.scalars(
        select(RawMessage)
        .where(RawMessage.sender_id != "", RawMessage.sender_name == "")
        .order_by(RawMessage.id.desc())
        .limit(take)
    ).all()
    orders = session.scalars(
        select(Order)
        .where(Order.sender_id != "", Order.sender_name == "")
        .order_by(Order.id.desc())
        .limit(take)
    ).all()

    # 机器人 / 应用自己发的消息：固定名字，不用查飞书（没配飞书凭证时也能补上）
    bot_updated = 0
    for row in list(msgs) + list(orders):
        if is_app_sender(row.sender_id):
            row.sender_name = APP_SENDER_NAME
            bot_updated += 1
    if bot_updated:
        session.commit()
        logger.info("已补全 %s 条机器人发送人姓名（%s）", bot_updated, APP_SENDER_NAME)
    if not client.enabled:
        return bot_updated

    # 同一个人只查一次：历史记录 -> 通讯录 -> 群成员名单
    pending: list[str] = []
    chat_of: dict[str, str] = {}  # 发送人所在的群，查不到姓名时用它走群成员名单
    for row in list(msgs) + list(orders):
        if not row.sender_id or is_app_sender(row.sender_id):
            continue
        if row.sender_id not in pending:
            pending.append(row.sender_id)
        if row.chat_id and row.sender_id not in chat_of:
            chat_of[row.sender_id] = row.chat_id
    names = {
        sid: resolve_sender_name(
            sid, client, chat_id=chat_of.get(sid, ""), session=session
        )
        for sid in pending
    }

    if any(not v for v in names.values()):
        chat_ids: list[str] = []
        for row in list(msgs) + list(orders):
            if row.chat_id and row.chat_id not in chat_ids:
                chat_ids.append(row.chat_id)
        for chat_id in chat_ids:
            for sid, name in client.get_chat_members(chat_id).items():
                if name and not names.get(sid):
                    names[sid] = name
                    _SENDER_NAME_CACHE[sid] = name

    updated = 0
    for row in list(msgs) + list(orders):
        name = names.get(row.sender_id) or ""
        if not name:
            continue
        row.sender_name = name
        updated += 1
    if updated:
        session.commit()
        logger.info("已补全 %s 条发送人姓名", updated)
    return updated + bot_updated


# ------------------------------------------------------------------ 缺截图提醒
MISSING_IMAGE_TEXT = (
    "📸 这条订单还没收到订单截图，请补发 1 张订单详情截图"
    "（含订单编号 / 成交价 / 收货信息），系统会自动补全字段并把订单编号换成截图里的真实编号。"
)


def notify_missing_screenshot(
    session: Session, msg: RawMessage, order: Order, client: FeishuClient
) -> bool:
    """模板消息配不到订单截图时回群提醒发送人（同一条只提醒一次）。"""
    if not settings.notify_missing_image or settings.notify_mode == "off":
        return False
    if msg.missing_image_notified_at is not None:
        return False
    if _order_image_count(session, order.id):
        return False

    # 设置页配了缺截图提醒的模板就按模板渲染，没配（或写错）用内置那句
    text = render_notify(
        settings.notify_missing_image_template, notify_context(order, msg)
    )
    if not text:
        text = MISSING_IMAGE_TEXT
    if settings.notify_mode != "direct" and msg.sender_id.startswith("ou_"):
        text = f'<at user_id="{msg.sender_id}"></at> {text}'
    try:
        # 发群里还是私信同步给本人，按消息中心里「缺订单截图提醒」勾的来
        # （私信出口会自动去掉群里的 @ 标签）
        if not deliver(client, msg, text, key="missing_image"):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("缺截图提醒发送失败 %s: %s", msg.message_id, exc)
        return False
    msg.missing_image_notified_at = now()
    return True


def notify_missing_screenshots(
    session: Session, client: FeishuClient | None = None, window: int | None = None
) -> int:
    """给「已归档但一直没配到截图」的模板消息补发提醒。

    只处理已经超过配对窗口（image_follow_window_seconds）的消息：截图往往是紧跟模板发的，
    窗口内不判定缺图，避免误提醒。
    """
    if not settings.notify_missing_image or settings.notify_mode == "off":
        return 0
    client = client or get_client()
    window = settings.image_follow_window_seconds if window is None else window
    deadline = now() - timedelta(seconds=max(window, 0))
    msgs = session.scalars(
        select(RawMessage)
        .where(RawMessage.kind == "template", RawMessage.parse_status == "parsed")
        .where(RawMessage.missing_image_notified_at.is_(None))
        .where(RawMessage.order_id.is_not(None))
        .order_by(RawMessage.msg_time)
        .limit(50)
    ).all()
    sent = 0
    for msg in msgs:
        if msg.msg_time is not None and msg.msg_time > deadline:
            continue
        order = session.get(Order, msg.order_id) if msg.order_id else None
        # 已撤回的订单不参与后续流程：不再回群催截图、不再推送
        if order is None or order.is_recalled:
            continue
        if notify_missing_screenshot(session, msg, order, client):
            sent += 1
            session.commit()
    return sent


# ------------------------------------------------------------------ 补发截图回填
def _address_overlap(a: str, b: str) -> int:
    """两段地址的重合度：共有的 3 字片段数（OCR 地址常有错字，按片段重合来打分）。"""
    x = re.sub(r"\s+", "", str(a or ""))
    y = re.sub(r"\s+", "", str(b or ""))
    if len(x) < 6 or len(y) < 6:
        return 0
    grams = {x[i : i + 3] for i in range(len(x) - 2)}
    return sum(1 for i in range(len(y) - 2) if y[i : i + 3] in grams)


def _amount_matches(fields: dict, order: Order) -> bool:
    """截图里的金额与订单金额对得上（成交价 / 商品总价 / 售价任一相等）。"""
    mine = [float(getattr(order, key) or 0) for key in ("deal_amount", "total_amount", "sale_amount")]
    mine = [value for value in mine if value]
    if not mine:
        return False
    for key in ("deal_amount", "total_amount", "sale_amount"):
        value = fields.get(key)
        try:
            amount = float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        if amount and any(abs(amount - other) < 0.01 for other in mine):
            return True
    return False


def find_order_for_late_screenshot(
    session: Session,
    fields: dict,
    chat_id: str = "",
    msg_time=None,
    min_score_without_phone: int = 3,
) -> Order | None:
    """这张后来补发的截图属于哪一单：只在「还没配到截图」的订单里认。

    打分认单，尽量不靠单一字段：
      · 手机号对上 +5（最可靠，OCR 对手机号识别率高）；
      · 收货人姓名对上 +3；姓名认错但手机号对上只 -1，不直接排除（截图姓名常被认成乱码）；
      · 产品名全等 +2 / 互相包含 +1；
      · 金额（成交价 / 总价 / 售价）对得上 +2；
      · 地址有 3 处以上片段重合 +2；
      · 消息时间与订单时间同一天 +1；
      · 跨群 -2。
    姓名与手机号都认不出来时宁可不认（返回 None），避免把图挂到错的单上。
    """
    name = str(fields.get("receiver_name") or "").strip()
    phone = dedupe_service.norm_digits(fields.get("receiver_phone"))
    product = str(fields.get("product_name") or "").strip()
    address = str(fields.get("receiver_address") or "").strip()
    if not name and not phone:
        return None
    days = max(int(settings.screenshot_backfill_days or 0), 1)
    cutoff = now() - timedelta(days=days)
    rows = session.scalars(
        select(Order)
        .where(Order.is_recalled.is_(False), Order.created_at >= cutoff)
        .order_by(Order.created_at.desc())
        .limit(200)
    ).all()
    best: Order | None = None
    best_score = 0
    phone_ok_any = False
    for order in rows:
        if _order_image_count(session, order.id):
            continue  # 已经有截图的不抢图
        score = 0
        phone_hit = bool(phone) and dedupe_service.norm_digits(order.receiver_phone) == phone
        if phone_hit:
            score += 5
        if name and order.receiver_name:
            if order.receiver_name == name:
                score += 3
            elif phone_hit:
                # 手机号一致、姓名被 OCR 认错（实测很常见）：少给分，但不能判成不是这一单
                score -= 1
            else:
                continue  # 手机号、姓名都对不上：不是同一单
        elif name and not order.receiver_name:
            pass
        if product and order.product_name:
            if order.product_name == product:
                score += 2
            elif product in order.product_name or order.product_name in product:
                score += 1
        if _amount_matches(fields, order):
            score += 2
        if address and order.receiver_address and _address_overlap(address, order.receiver_address) >= 3:
            score += 2
        if msg_time is not None and order.msg_time is not None:
            if abs((msg_time - order.msg_time).total_seconds()) <= 86400:
                score += 1
        if not score:
            continue
        if chat_id and order.chat_id and order.chat_id != chat_id:
            score -= 2
        if score > best_score:
            best, best_score = order, score
            phone_ok_any = phone_hit
    if best is not None and best_score <= 0:
        return None
    if best is not None and not phone_ok_any:
        # 只靠姓名/产品认上的：分数得够高才认，宁缺勿错。
        # 例外：群里明说了是来补资料的（「补发截图」这类），人的意图替我们兜底，门槛降一档。
        if best_score < min_score_without_phone:
            return None
    return best


def backfill_late_screenshot(
    session: Session, msg: RawMessage, client: FeishuClient | None = None
) -> tuple[Order | None, str]:
    """群里补发的订单截图（没跟模板一起发 / 过了配对窗口）：认出是哪一单并补进去。

    命中后：截图归档到该订单、OCR 出来的字段只补空缺（订单号 / 成交价 / 收货信息…），
    原本是临时编号的订单会升级成截图里的真实编号。认不出来则该图不落盘。
    """
    client = client or get_client()
    metas: list[tuple[str, Path, bytes, str, str]] = []
    texts: list[str] = []
    for key in _image_keys_of(msg):
        filename = f"{msg.message_id}_{key[:12]}.png"
        try:
            data = client.download_resource(msg.message_id, key, "image")
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载补发截图失败 %s: %s", key, exc)
            continue
        path = save_bytes(data, filename)
        try:
            ocr_text, ocr_status = ocr_image(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("补发截图 OCR 异常: %s", exc)
            ocr_text, ocr_status = "", f"OCR 失败: {exc}"
        if not is_order_screenshot(ocr_text):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001
                pass
            continue
        texts.append(ocr_text)
        metas.append((key, path, data, ocr_status, ocr_text))

    def _drop_files() -> None:
        for _key, path, _data, _status, _text in metas:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001
                pass

    if not texts:
        return None, "不是订单截图"
    result = parse_text("", texts)
    # 群里有没有说明这张图是来干什么的：「补发截图 / 补充资料」这类明说了，认单门槛可以降一档
    hint = intent_service.classify(msg.raw_text or "")
    explicit = hint.kind == intent_service.INTENT_COMPLETE and not hint.needs_context
    order = find_order_for_late_screenshot(
        session,
        result.fields,
        msg.chat_id,
        msg_time=msg.msg_time,
        min_score_without_phone=2 if explicit else 3,
    )
    if order is None:
        _drop_files()
        return None, "认不出属于哪一单（没有收货人对得上的缺图订单）"

    real_no = str(result.fields.get("order_no") or "").strip()
    order = apply_real_order_no(session, order, real_no)

    _apply_parsed(order, result, only_fill_empty=True, session=session)
    try:
        confidence = max(float(order.parse_confidence or 0), float(result.confidence or 0))
    except (TypeError, ValueError):
        confidence = float(result.confidence or 0)
    order.parse_confidence = confidence
    _mark_review(order, result, hint=_first_hint(texts))
    order.fingerprint = dedupe_service.order_fingerprint(order)
    order.updated_at = now()
    session.flush()

    images: list[OrderImage] = []
    for key, path, data, ocr_status, ocr_text in metas:
        img = OrderImage(
            order_id=order.id,
            message_id=msg.message_id,
            file_key=key,
            filename=path.name,
            local_path=str(path),
            mime="image/png",
            size=len(data),
            ocr_status=ocr_status,
            ocr_text=ocr_text,
        )
        session.add(img)
        images.append(img)
    session.flush()
    if images:
        _attach_product_image(order, images[0])

    # 归属：挂到该订单那条模板消息下，消息页能看出它是某单的补图
    if not msg.parent_message_id:
        src = session.scalars(
            select(RawMessage)
            .where(RawMessage.order_id == order.id, RawMessage.msg_type.in_(("text", "post")))
            .order_by(RawMessage.msg_time)
            .limit(1)
        ).first()
        msg.parent_message_id = src.message_id if src else ""
    msg.kind = "screenshot"
    msg.parse_status = "parsed"
    msg.order_id = order.id
    msg.ignore_reason = ""
    session.commit()
    logger.info("补发截图已回填订单 %s（消息 %s）", order.order_no, msg.message_id)
    return order, f"已回填到订单 {order.order_no}"


# ------------------------------------------------------------------ 详情页手动维护订单截图
# 支持上传的图片格式（手机截图 / 电脑截图常见格式）
MANUAL_SCREENSHOT_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
# 单张截图大小上限：手机长截图一般 1~5MB，超过 15MB 基本是误传原图
MANUAL_SCREENSHOT_MAX_BYTES = 15 * 1024 * 1024
# 手动上传的截图只更新这些字段：成本 / 供应商 / 闲鱼号 / 回款人 / 备注属于人工维护口径，不能被截图覆盖
_SCREENSHOT_ATTRS = (
    "buyer_nick", "product_name", "spec", "quantity",
    "sale_amount", "deal_amount", "total_amount",
    "receiver_name", "receiver_phone", "receiver_address",
)
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def _norm_receiver(value: str) -> str:
    """收货人姓名归一：去掉所有空白，避免「张 三」和「张三」被当成两个人。"""
    return "".join(str(value or "").split())


def receiver_mismatch(order: Order, fields: dict) -> str:
    """上传的截图解析出的收货人 / 手机号与订单本身是否一致。

    返回不一致的说明；一致、或订单本身还没维护收货信息时返回空串（没法比对就不拦）。
    """
    name = _norm_receiver(fields.get("receiver_name"))
    phone = dedupe_service.norm_digits(fields.get("receiver_phone"))
    order_name = _norm_receiver(order.receiver_name)
    order_phone = dedupe_service.norm_digits(order.receiver_phone)
    if name and order_name and name != order_name:
        return f"解析出的收货人是「{name}」，而本订单的收货人是「{order_name}」"
    if phone and order_phone and phone != order_phone:
        return f"解析出的手机号码是「{phone}」，而本订单的手机号码是「{order_phone}」"
    return ""


def _drop_order_image_files(images: list[OrderImage]) -> None:
    """删掉订单现有截图文件与它们裁出的商品图（更新截图时旧图不再保留）。

    商品图是从截图文件名派生的（{stem}_product.png），跟着截图一起删。
    """
    for img in images:
        if not img.local_path:
            continue
        src = Path(img.local_path)
        for path in (src, src.with_name(f"{src.stem}_product.png")):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ 截图重识别（兜底）
def reocr_order(session: Session, order: Order, limit_images: int = 3) -> tuple[bool, str]:
    """对订单已有截图重新识别一遍，能读出订单号就把临时编号升级掉。

    返回 (是否升级成功, 说明)。这是「截图已经配上、单号却一直没更新」的兜底通道：
    定时任务与详情页「重新识别截图」按钮都走这里，和补发截图 / 手动上传共用同一套
    订单号识别与升级逻辑。
    """
    if (settings.ocr_provider or "none").lower() == "none":
        return False, "当前没有启用 OCR（OCR_PROVIDER=none），无法从截图里读订单号"
    images = session.scalars(
        select(OrderImage)
        .where(OrderImage.order_id == order.id)
        .order_by(OrderImage.id)
        .limit(max(limit_images, 1))
    ).all()
    if not images:
        return False, "这单还没有订单截图：先在群里补发截图，或在详情页上传"

    texts: list[str] = []
    for img in images:
        _refresh_image_ocr(img)
        if img.ocr_text:
            texts.append(img.ocr_text)
    session.flush()
    if not texts:
        return False, "截图没识别出文字（图太糊 / 只有图片没有订单信息），建议重新上传更清晰的截图"

    merged = parse_text(order.raw_text or "", texts)
    real_no = str(merged.fields.get("order_no") or "").strip()
    hint = _first_hint(texts)
    if real_no and real_no != order.order_no:
        order = apply_real_order_no(session, order, real_no)
        _apply_parsed(order, merged, only_fill_empty=True, session=session)
        buyer_service.fill_buyer(order)
        _mark_review(order, merged, hint=order_no_hint_of(session, order))
        order.fingerprint = dedupe_service.order_fingerprint(order)
        order.updated_at = now()
        session.commit()
        return True, f"已从截图里读出订单号 {real_no}，原临时编号已留档备查"
    _mark_review(order, merged, hint=hint)
    order.updated_at = now()
    session.commit()
    if hint:
        return False, f"截图里疑似订单号 {hint}（没认准），确认无误可在详情页采用或手填"
    return False, "截图里没读出订单编号（OCR 没认到数字），建议重新上传更清晰的截图"


def reocr_pending(session: Session, limit: int = 20, days: int | None = None) -> dict:
    """兜底重试：把还是临时单号（TMP-）的订单截图重新识别一遍。

    限制：只看最近 N 天的订单、距上次尝试超过 OCR_REOCR_MINUTES 才重跑、
    同一张图最多重试 OCR_MAX_ATTEMPTS 次——避免给小机器添无用的负载。
    """
    window_days = max(int(days if days is not None else settings.ocr_reocr_days or 7), 1)
    interval = max(int(settings.ocr_reocr_minutes or 0), 1) * 60
    max_attempts = max(int(settings.ocr_max_attempts or 3), 1)
    cutoff = now() - timedelta(days=window_days)
    stale = now() - timedelta(seconds=interval)
    stats = {"checked": 0, "upgraded": 0, "hint": 0, "skipped": 0}
    rows = session.scalars(
        select(Order)
        .where(Order.is_recalled.is_(False), Order.created_at >= cutoff)
        .where(Order.order_no.startswith(TEMP_PREFIX))
        .where(Order.updated_at <= stale)
        .order_by(Order.created_at.desc())
        .limit(max(limit, 1))
    ).all()
    for order in rows:
        images = session.scalars(
            select(OrderImage).where(OrderImage.order_id == order.id)
        ).all()
        if not images or all(int(img.ocr_attempts or 0) >= max_attempts for img in images):
            stats["skipped"] += 1
            continue
        stats["checked"] += 1
        try:
            upgraded, note = reocr_order(session, order)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重识别订单 %s 截图失败: %s", order.order_no, exc)
            continue
        if upgraded:
            stats["upgraded"] += 1
            logger.info("截图重识别成功（订单 %s）：%s", order.id, note)
        elif "疑似" in note:
            stats["hint"] += 1
    return stats


def save_manual_screenshot(
    session: Session, order: Order, filename: str, data: bytes
) -> tuple[bool, str, str]:
    """详情页手动上传 / 更新订单截图。

    返回 (是否成功, 提示文案, 提示级别)。成功时会解析截图里的字段补全订单，
    并先做收货人一致性检测：截图收货人与订单收货人对不上则拒绝并提示重新上传。
    """
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()
    if not data:
        return False, "上传的文件是空的，请重新选择订单截图", "danger"
    if ext not in MANUAL_SCREENSHOT_EXTS:
        return False, "只支持图片格式的订单截图（png / jpg / webp / bmp / gif），请重新上传", "danger"
    if len(data) > MANUAL_SCREENSHOT_MAX_BYTES:
        return False, "订单截图太大了（超过 15MB），请压缩或先用截图工具裁一下再上传", "danger"

    # 文件名带内容哈希：同一秒内重复上传也不会互相覆盖，也便于看出是不是同一张图
    tag = hashlib.md5(data).hexdigest()[:8]
    saved = save_bytes(data, f"manual_{order.id}_{int(time.time())}_{tag}{ext}")
    ocr_text, ocr_status, ocr_hint = "", "skipped", ""
    try:
        out = ocr_image_ex(saved)
        ocr_text, ocr_status = out.text, out.status
        if not out.order_no and out.order_no_hints:
            ocr_hint = out.order_no_hints[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("手动上传截图 OCR 异常 %s: %s", order.order_no, exc)
    ocr_text = (ocr_text or "").strip()

    parsed: ParseResult | None = None
    if ocr_text:
        if not is_order_screenshot(ocr_text):
            saved.unlink(missing_ok=True)
            return (
                False,
                "这张图看起来不是订单截图（没识别到订单编号 / 收货信息），订单截图上传不对，请重新上传",
                "danger",
            )
        parsed = parse_text("", [ocr_text])
        mismatch = receiver_mismatch(order, parsed.fields)
        if mismatch:
            saved.unlink(missing_ok=True)
            return (
                False,
                f"订单截图上传不对：{mismatch}，请重新上传",
                "danger",
            )

    # 校验通过：把旧截图换成新的（含裁出的商品图）
    old_images = session.scalars(
        select(OrderImage).where(OrderImage.order_id == order.id)
    ).all()
    _drop_order_image_files(list(old_images))
    for img in old_images:
        session.delete(img)
    order.product_image_path = ""
    session.flush()

    if parsed is not None:
        # 截图里的订单编号最权威：临时编号升级成真实编号（真实编号被占用则合并）
        order = apply_real_order_no(session, order, str(parsed.fields.get("order_no") or ""))
        for attr in _SCREENSHOT_ATTRS:
            if attr not in parsed.fields:
                continue
            value = parsed.fields[attr]
            if value in (None, ""):
                continue
            # 金额 / 数量解析成 0 时不当成有效值，避免把已有值清零
            if attr in ("sale_amount", "deal_amount", "total_amount", "quantity") and not value:
                continue
            # 地址不像地址（截图上的营销文案被当成了地址）：宁缺勿错，不覆盖订单上的地址
            if attr == "receiver_address" and not looks_like_address(value):
                continue
            setattr(order, attr, value)
        if order.ordered_at is None and parsed.fields.get("ordered_at"):
            order.ordered_at = parsed.fields["ordered_at"]
        order.parse_confidence = max(
            float(order.parse_confidence or 0), float(parsed.confidence or 0)
        )
        _mark_review(order, parsed, hint=ocr_hint)

    order.fingerprint = dedupe_service.order_fingerprint(order)
    order.updated_at = now()
    session.flush()

    img = OrderImage(
        order_id=order.id,
        message_id="",
        file_key="",
        filename=saved.name,
        local_path=str(saved),
        mime=_MIME_BY_EXT.get(ext, "image/png"),
        size=len(data),
        ocr_status=ocr_status,
        ocr_text=ocr_text,
        order_no_hint=ocr_hint,
        ocr_attempts=1,
    )
    session.add(img)
    session.flush()
    _attach_product_image(order, img, force=True)
    session.commit()

    if not ocr_text:
        return (
            True,
            "订单截图已更新；当前未启用 OCR 或识别不到文字，没能自动解析字段",
            "warn",
        )
    return True, f"订单截图已更新，已解析并补全订单信息（订单号 {order.order_no}）", ""


# ------------------------------------------------------------------ 意图处理
def _apply_intent(
    session: Session,
    msg: RawMessage,
    intent,
    fields: dict | None = None,
    client: FeishuClient | None = None,
    stats: dict | None = None,
) -> dict:
    """按意图处理消息并回执：退单 -> 已退单；撤单 -> 已撤单；拿不准 -> 待人工确认。"""
    stats = stats if stats is not None else {}
    result = intent_service.handle(session, msg, intent, fields)
    order = result.get("order")
    action = result.get("action") or ""
    note = result.get("note") or ""

    # 消息留痕：意图 + 说明（消息页/排查时一眼看清系统做了什么）
    msg.intent = intent.kind
    msg.intent_note = note
    if order is not None:
        msg.order_id = order.id

    # 「待人工确认」且没落到具体订单（拿不准是哪一笔）：只留痕，不算已处理
    if action in ("refund", "cancel") or (action == "review" and order is not None):
        msg.parse_status = "parsed"
        msg.kind = "intent"
        stats["parsed"] = stats.get("parsed", 0) + 1
        if action == "refund":
            stats["refund"] = stats.get("refund", 0) + 1
        elif action == "cancel":
            stats["cancel"] = stats.get("cancel", 0) + 1
    else:
        # 识别到意图但没匹配到订单 / 已发货不能撤：保留记录，等人工处理
        msg.parse_status = "ignored"
        msg.kind = "other"
        msg.ignore_reason = note
        stats["ignored"] = stats.get("ignored", 0) + 1

    if order is not None and action == "refund":
        status, err = notify_refund(msg, order, client, session=session, keyword=intent.keyword)
    elif order is not None and action == "cancel":
        status, err = notify_cancel(msg, order, client, session=session, keyword=intent.keyword)
    elif order is not None:
        # 关联到订单但没受理（已发货不能撤单 / 已发货不同意退单 / 拿不准是哪一笔）：
        # 这类「请求被拒绝」的提示不受回执间隔限制，免得运营看不到
        if action == "review":
            title = f"识别到「{intent.label}」意图（{intent.keyword}），但无法确定是哪一笔订单"
            hint = "请回复订单号，或直接引用原订单消息再发一次；也可到订单归档里按「待人工确认」核对后处理。"
        else:
            title = f"「{intent.label}」请求未受理"
            hint = ""
        status, err = notify_text(
            msg,
            build_intent_receipt(msg, title, note, hint),
            client,
            session=session,
            throttle=False,
        )
    else:
        title = f"识别到「{intent.label}」意图（{intent.keyword}），但没有匹配到对应订单"
        hint = (
            "请在消息里带上「订单号」或「手机号」再发一次，"
            "也可以直接引用原订单消息再发，或到订单页手工处理。"
        )
        # 没匹配上 = 这单还没被受理，提示不能被回执间隔静默掉，否则运营会漏单
        status, err = notify_text(
            msg,
            build_intent_receipt(msg, title, note, hint),
            client,
            session=session,
            throttle=False,
        )
    msg.notify_status = status
    msg.notify_error = err or note
    msg.notified_at = now() if status == "sent" else None
    if status == "sent":
        stats["notified"] = stats.get("notified", 0) + 1
    session.commit()
    return result


def _quoted_fields(session: Session, msg: RawMessage) -> dict | None:
    """「引用原订单消息 + 只写『退单』两个字」是群里最常见的沟通习惯。

    这时被引用的那条消息才是真正的指向证据：把它的内容解析成订单字段，
    交给意图匹配当依据用 —— 消息正文再短，也能落到正确的那笔订单上。
    """
    parent_id = str(getattr(msg, "parent_message_id", "") or "").strip()
    if not parent_id:
        return None
    parent = session.scalar(select(RawMessage).where(RawMessage.message_id == parent_id))
    text = str(getattr(parent, "raw_text", "") or "")
    if not text.strip():
        return None
    parsed = parse_text(text)
    fields = dict(parsed.fields) if parsed.matched else {}
    if not fields:
        # 引用的是残缺的订单信息（只贴了「收货人 / 手机号码」那几行）：
        # 至少把手机号捞出来，它是最好用的定位依据
        found = re.search(r"1[3-9]\d{9}", text)
        if found:
            fields["receiver_phone"] = found.group(0)
    return fields or None


def _handle_intent_message(
    session: Session, msg: RawMessage, client: FeishuClient | None, stats: dict
) -> bool:
    """非模板消息：按「动作意图」处理（退单 / 撤单）。

    返回 True 表示这条消息已按意图处理过（不再当闲聊过滤掉）。
    弱意图词（「退了」「撤了」）又没有任何订单指向时只留痕、不动订单 —— 避免误伤。
    """
    # 人工标记学到的排除词优先：通告里常带「无理由退货会有 30 的费用」这种说明性文字，
    # 被当成退单意图会白记一笔、还可能回执打扰群；人工标记过一次就不再识别。
    verdict, reason, _exact = policy_verdict(msg.raw_text)
    if verdict == policy_service.VERDICT_EXCLUDE:
        msg.parse_status = "filtered"
        msg.kind = "other"
        msg.ignore_reason = f"人工策略排除（{reason}）：不是订单消息，也不做退单 / 撤单识别"
        stats["filtered"] = stats.get("filtered", 0) + 1
        session.commit()
        return True
    intent = intent_service.classify(msg.raw_text)
    if not intent.matched and verdict == policy_service.VERDICT_REFUND:
        # 人工标记学到的退单话术（规则词库里没有的说法）：按退单处理存量订单，
        # 消息里带模板字段就用字段去匹配原订单，找不到原订单也不新建
        parsed = parse_text(msg.raw_text)
        fields = parsed.fields if parsed.matched else None
        learned = policy_service.hit_phrase(
            session, msg.raw_text, policy_service.KIND_REFUND
        )
        _apply_intent(
            session,
            msg,
            intent_service.Intent(
                kind=intent_service.INTENT_REFUND,
                keyword=learned or "退单",
                source="policy",
            ),
            fields,
            client,
            stats,
        )
        return True
    if not intent.matched:
        return False
    # 引用了某条群消息：被引用消息的内容就是匹配依据（「引用原订单 + 退单」场景）
    fields = _quoted_fields(session, msg)
    if intent.needs_context and not (
        intent_service.has_order_context(msg.raw_text) or msg.parent_message_id
    ):
        # 弱意图词（「退了」「撤了」）又没写清是哪一单：只有该发送人名下正好一笔在途订单才敢动，
        # 否则先留痕不动订单 —— 避免把群里的闲聊当成退单
        match = intent_service.find_order(session, msg, fields)
        if match.order is None or match.confidence not in ("high", "medium"):
            msg.intent = intent.kind
            msg.intent_note = (
                f"识别到{intent.label}意图（{intent.keyword}），但消息没有指向具体订单，先不处理"
            )
            msg.parse_status = "ignored"
            msg.kind = "other"
            msg.ignore_reason = msg.intent_note
            stats["ignored"] = stats.get("ignored", 0) + 1
            session.commit()
            return True
    _apply_intent(session, msg, intent, fields, client, stats)
    return True


def _handle_return_registration(
    session: Session, msg: RawMessage, client: FeishuClient | None, stats: dict
) -> bool:
    """退单登记（2026-10-06 口径）：群里发「原单号 + 退回单号 + 备注」的消息。

    它是对**已有订单**补退单信息（退单编号写进订单、备注累加），不是一笔新订单 ——
    以前会被当成订单模板归档，多出一笔 TMP- 的脏单要人工清理。
    返回 True 表示这条消息已经处理完，调用方 continue。
    """
    result = parse_text(msg.raw_text)
    fields = dict(getattr(result, "fields", {}) or {})
    if not (fields.get("original_order_no") or fields.get("return_no")):
        return False

    order, _changes, note = refund_service.register_return(session, msg, result, client)
    msg.intent = intent_service.INTENT_REFUND
    msg.intent_note = note
    if order is None:
        # 匹配不到：跟退单一个口径，不新建订单，留痕 + 回群提示补信息
        msg.kind = "other"
        msg.parse_status = "ignored"
        msg.ignore_reason = note
        stats["ignored"] += 1
        title = "识别到退单登记，但没有匹配到对应订单"
        hint = (
            "这条不会新建订单：请在消息里带上订单号或收货人手机号再发一次，"
            "或到订单详情页手工维护退单编号与备注。"
        )
        status, err = notify_text(
            msg, build_intent_receipt(msg, title, note, hint), client, session=session
        )
        msg.notify_status = status
        msg.notify_error = err or note
        msg.notified_at = now() if status == "sent" else None
        if status == "sent":
            stats["notified"] += 1
        session.commit()
        return True

    msg.parse_status = "parsed"
    stats["return_register"] = stats.get("return_register", 0) + 1
    stats["parsed"] += 1
    status, err = notify_text(
        msg,
        build_intent_receipt(
            msg,
            f"已登记退单信息（订单 {order.order_no}）",
            note,
            "下一步：收到退回件后在订单详情页点「标记收到退回件」，再退款并关闭完成。",
        ),
        client,
        session=session,
    )
    msg.notify_status = status
    msg.notify_error = err or note
    msg.notified_at = now() if status == "sent" else None
    if status == "sent":
        stats["notified"] += 1
    session.commit()
    return True


# ------------------------------------------------------------------ 解析入口
def process_pending(session: Session, limit: int = 500, client: FeishuClient | None = None) -> dict:
    client = client or get_client()
    msgs = (
        session.scalars(
            select(RawMessage)
            .where(RawMessage.parse_status == "pending")
            .where(RawMessage.msg_type.in_(("text", "post")))
            .order_by(RawMessage.msg_time)
            .limit(limit)
        ).all()
    )
    stats = {"parsed": 0, "ignored": 0, "filtered": 0, "failed": 0, "notified": 0, "errors": []}
    # 配对先行：截图可能比模板消息先到（运营常常先发图、再补文本），处理前先把窗口内的图配好。
    # 否则模板消息归档时看不到自己的截图 —— 同买家同款两单会被指纹判成「重复推送」而丢单
    # （2026-09-29 王小萌那单就是这么丢的）。
    for chat_id in {m.chat_id for m in msgs if m.chat_id}:
        try:
            attach_images_to_text(session, chat_id)
        except Exception as exc:  # noqa: BLE001  配不上不影响后续解析
            logger.warning("模板消息处理前配对截图失败 %s: %s", chat_id, exc)
    for msg in msgs:
        try:
            # 群里已撤回的消息：不再归档成订单（撤回的只留档，不参与后续流程）
            if msg.is_recalled:
                msg.parse_status = "filtered"
                msg.kind = "other"
                msg.ignore_reason = "这条消息在群里已被撤回：不归档成订单"
                stats["filtered"] += 1
                session.commit()
                continue
            # 机器人自身消息（含回执）直接过滤：否则会把自己发的回执再解析一遍
            if is_bot_message(msg.sender_type, msg.raw_text):
                msg.parse_status = "filtered"
                msg.kind = "other"
                msg.ignore_reason = "机器人自身消息（含自己发出的回执）：不解析、不回执"
                stats["filtered"] += 1
                session.commit()
                continue
            # 退单登记（2026-10-06 口径）：群里发「原单号 + 退回单号 + 备注」，
            # 它是给**已有订单**补退单信息，不是新订单 —— 简写的三行（没有产品 / 收货人）
            # 不算订单模板，所以在模板判定之前先认一次。
            if _handle_return_registration(session, msg, client, stats):
                continue
            # 不是订单模板消息：按「动作意图」判断（退单 / 撤单），能关联到订单就处理订单状态
            if not is_template_message(msg.raw_text):
                if _handle_intent_message(session, msg, client, stats):
                    continue
                msg.parse_status = "filtered"
                msg.kind = "other"
                msg.ignore_reason = "不是订单模板消息，也没有识别到退单 / 撤单意图（闲聊 / 通知 / 无关文本）"
                stats["filtered"] += 1
                session.commit()
                continue
            result = parse_text(msg.raw_text)
            # 意图识别：模板消息看第一行，「此单退 / 退单」-> 退单，「撤单 / 取消订单」-> 撤单
            intent = intent_service.classify(msg.raw_text, template=True)
            if intent.kind == intent_service.INTENT_REFUND:
                order, _is_new, note = refund_service.handle(
                    session, msg, result, client, intent.keyword
                )
                msg.intent = intent.kind
                msg.intent_note = f"模板消息首行命中「{intent.keyword}」：{note}"
                if order is None:
                    # 没匹配到原订单：不新建订单，留痕等人工核对（回执请对方带订单号再发一次）
                    msg.kind = "other"
                    msg.parse_status = "ignored"
                    msg.ignore_reason = note
                    stats["ignored"] += 1
                    title = f"识别到「{intent.label}」意图（{intent.keyword}），但没有匹配到对应订单"
                    hint = "这条是退单消息，不会新建订单；请在消息里带上「订单号」或「手机号」再发一次，或到订单页手工处理。"
                    status, err = notify_text(
                        msg, build_intent_receipt(msg, title, note, hint), client, session=session
                    )
                    msg.notify_status = status
                    msg.notify_error = err or note
                    msg.notified_at = now() if status == "sent" else None
                    if status == "sent":
                        stats["notified"] += 1
                    session.commit()
                    continue
                # 退单消息里也带着学员模板那几行时，学员店铺同样认领 / 建档
                _ensure_shop_of_message(session, result, msg.sender_name or "")
                msg.kind = "template"
                msg.order_id = order.id
                status, err = notify_refund(
                    msg, order, client, session=session, keyword=intent.keyword
                )
                msg.notify_status = status
                msg.notify_error = err or note
                msg.notified_at = now() if status == "sent" else None
                if status == "sent":
                    stats["notified"] += 1
                stats["refund"] = stats.get("refund", 0) + 1
                stats["parsed"] += 1
                session.commit()
                continue
            if intent.kind == intent_service.INTENT_CANCEL:
                # 撤单同理：消息里带着学员店铺信息就一并把店铺认领 / 建档
                _ensure_shop_of_message(session, result, msg.sender_name or "")
                _apply_intent(session, msg, intent, result.fields, client, stats)
                continue

            order = save_order(session, msg, result, client)
            msg.kind = "template"
            msg.order_id = order.id
            if msg.parse_status == "duplicate":
                # 重复发的同一笔订单且内容无变化：只提醒「已收到」，不重复处理
                status, err = notify_duplicate(msg, order, client, session=session)
                stats["duplicate"] = stats.get("duplicate", 0) + 1
            else:
                status, err = notify_processed(msg, order, client, session=session)
            msg.notify_status = status
            msg.notify_error = err
            msg.notified_at = now() if status == "sent" else None
            if status == "sent":
                stats["notified"] += 1
            stats["parsed"] += 1
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("解析消息失败 %s", msg.message_id)
            msg.parse_status = "failed"
            msg.parse_error = str(exc)[:500]
            stats["failed"] += 1
            stats["errors"].append(f"{msg.message_id}: {exc}")
            session.commit()

    # 缺截图的模板消息：配对窗口过去后仍未配到图才提醒，避免截图稍后到达时误提醒
    try:
        stats["missing_image_notified"] = notify_missing_screenshots(session, client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("缺截图提醒失败: %s", exc)
    return stats


def _process_image_message(
    session: Session, msg: RawMessage, client: FeishuClient
) -> Order | None:
    """独立截图（没跟在模板消息后面）：下载 -> OCR -> 判定订单截图 -> 建单。

    不是订单截图的话不归档、不建单、不留文件。
    """
    texts: list[str] = []
    files: list[tuple[str, Path, bytes, str, str]] = []
    for key in _image_keys_of(msg):
        filename = f"{msg.message_id}_{key[:12]}.png"
        try:
            data = client.download_resource(msg.message_id, key, "image")
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载截图失败 %s: %s", key, exc)
            continue
        path = save_bytes(data, filename)
        try:
            ocr_text, ocr_status = ocr_image(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 异常: %s", exc)
            ocr_text, ocr_status = "", f"OCR 失败: {exc}"
        if not is_order_screenshot(ocr_text):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001
                pass
            continue
        texts.append(ocr_text)
        files.append((key, path, data, ocr_status, ocr_text))

    if not texts:
        return None

    result = parse_text("", texts)
    order = save_order(session, msg, result, client, ocr_summary="\n\n".join(texts)[:4000])
    for key, path, data, ocr_status, ocr_text in files:
        session.add(
            OrderImage(
                order_id=order.id,
                message_id=msg.message_id,
                file_key=key,
                filename=path.name,
                local_path=str(path),
                mime="image/png",
                size=len(data),
                ocr_status=ocr_status,
                ocr_text=ocr_text,
            )
        )
    session.commit()
    return order


def process_pending_images(
    session: Session, limit: int = 100, client: FeishuClient | None = None
) -> dict:
    """未归属的图片消息：先试着回填到缺截图的订单，实在没人认再过滤。

    业务口径是「1 条模板消息配 1 张订单截图」：图片优先作为模板消息的附件归档；
    过了配对窗口还没归属的图，再按收货人去认「缺截图的订单」（群里补图常常是后补的），
    认不到的一律不建单、不落盘。
    """
    client = client or get_client()
    stats = {"parsed": 0, "filtered": 0, "failed": 0, "notified": 0, "backfilled": 0, "errors": []}
    window = max(settings.image_follow_window_seconds, 0)
    deadline = now() - timedelta(seconds=window)
    imgs = (
        session.scalars(
            select(RawMessage)
            .where(
                RawMessage.parse_status == "pending",
                RawMessage.msg_type == "image",
                RawMessage.parent_message_id == "",
            )
            .order_by(RawMessage.msg_time)
            .limit(limit)
        ).all()
    )
    for msg in imgs:
        try:
            if msg.msg_time is not None and msg.msg_time > deadline:
                # 刚发出来：先留着，模板消息到达后由 attach_images_to_text 配对
                continue
            # 群里后来补发的订单截图：按收货人回填到还没配到截图的那笔订单
            if settings.screenshot_backfill_enabled and not msg.is_recalled:
                order, note = backfill_late_screenshot(session, msg, client)
                if order is not None:
                    stats["backfilled"] += 1
                    stats["parsed"] += 1
                    logger.info("补发的订单截图 %s：%s", msg.message_id, note)
                    continue
            msg.parse_status = "filtered"
            msg.kind = "other"
            msg.ignore_reason = (
                f"没归属：超过 {window} 秒没有配到模板消息，也没认出是哪笔订单的补图"
            )
            stats["filtered"] += 1
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理截图失败 %s", msg.message_id)
            msg.parse_status = "failed"
            msg.parse_error = str(exc)[:500]
            stats["failed"] += 1
            stats["errors"].append(f"{msg.message_id}: {exc}")
            session.commit()
    return stats


def _platform_fields(platform_key: str) -> list[dict]:
    from ..parser.rules import get_rules

    rules = get_rules()
    p = rules.platform(platform_key)
    return [
        {"key": f.key, "required": f.required}
        for f in (p.fields if p else [])
    ]


# ------------------------------------------------------------------ 同步
def _is_recalled_item(item: dict) -> bool:
    """同步回来的这条消息是不是已经被撤回（飞书列表接口会带 deleted 标记）。"""
    if not item:
        return False
    if item.get("deleted") or item.get("recalled") or item.get("is_recalled"):
        return True
    return str(item.get("msg_type") or "").lower() in ("recall", "recalled")


def auto_sync_start_ms(state: SyncState | None) -> int:
    """自动同步起点（毫秒）：保证「不漏消息」。

    · 新群（从没同步过）：直接回补 SYNC_BACKFILL_DAYS 天 —— 老逻辑只回看 15 分钟，
      加进来一个新群就只能收到最近几条，加群前的历史消息永远进不来；
    · 已同步过的群：取「最近同步到的最后一条 - 1 分钟」与「回看窗口」里更早的那个，
      服务重启 / 同步停摆一段时间后也能把缺的补上。
    """
    now_ms = int(time.time() * 1000)
    start = now_ms - int(max(settings.sync_lookback_minutes, 1) * 60 * 1000)
    last = getattr(state, "last_message_time", None)
    if last is not None:
        # 库里存的是 UTC naive（2026-10-03 时间口径）：补 UTC 再转时间戳，
        # 别按服务器本地时区算，否则续拉的起点会偏 8 小时
        last_ms = int(last.replace(tzinfo=UTC).timestamp() * 1000) - 60_000
        start = min(start, last_ms)
    else:
        start = min(start, now_ms - int(max(settings.sync_backfill_days, 0) * 86400 * 1000))
    return start


def sync_chat(
    chat_id: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    client: FeishuClient | None = None,
    download_images: bool = True,
) -> SyncStats:
    client = client or get_client()
    stats = SyncStats(chat_id=chat_id)
    if not client.enabled:
        stats.errors.append("未配置飞书应用凭证，无法同步")
        return stats

    try:
        chat_name = client.get_chat_name(chat_id)
    except Exception as exc:  # noqa: BLE001
        chat_name = chat_id
        stats.errors.append(f"获取群信息失败: {exc}")

    with session_scope() as session:
        state = session.scalar(select(SyncState).where(SyncState.chat_id == chat_id))
        if state is None:
            state = SyncState(chat_id=chat_id)
            session.add(state)
        state.chat_name = chat_name
        if start_ms is None:
            start_ms = auto_sync_start_ms(state)

        for item in client.get_chat_messages(chat_id, start_time=start_ms, end_time=end_ms):
            nm = normalize(item)
            if not nm["message_id"]:
                continue
            stats.fetched += 1
            # 群里已撤回的消息：不再入库解析，对应订单标记「撤回」（只留档）
            if _is_recalled_item(item):
                recalled = recall_service.handle_message_recalled(
                    session, nm["message_id"], chat_id=chat_id
                )
                stats.recalled += int(recalled.get("marked") or 0)
                continue
            msg, is_new = upsert_raw_message(session, nm)
            msg.chat_name = chat_name
            if is_new:
                stats.created += 1
            if nm["created_at"]:
                if state.last_message_time is None or nm["created_at"] > state.last_message_time:
                    state.last_message_time = nm["created_at"]
        session.commit()
        state.message_count = (
            session.query(RawMessage).filter(RawMessage.chat_id == chat_id).count()
        )
        state.last_synced_at = now()
        session.commit()

        if download_images:
            attach_images_to_text(session, chat_id)
        result = process_pending(session, client=client)
        img_result = process_pending_images(session, client=client)
        stats.parsed = result["parsed"] + img_result["parsed"]
        stats.ignored = result["ignored"]
        stats.filtered = result["filtered"] + img_result["filtered"]
        stats.failed = result["failed"] + img_result["failed"]
        stats.notified = result["notified"] + img_result["notified"]
        stats.errors.extend(result["errors"])
        stats.errors.extend(img_result["errors"])
        stats.images = session.query(OrderImage).count()
    return stats


def sync_all(days: int | None = None, chat_ids: Iterable[str] | None = None) -> list[SyncStats]:
    client = get_client()
    ids = list(chat_ids or settings.feishu_chat_ids)
    start_ms = None
    if days:
        start_ms = int((time.time() - days * 86400) * 1000)
    out = []
    for chat_id in ids:
        try:
            out.append(sync_chat(chat_id, start_ms=start_ms, client=client))
        except Exception as exc:  # noqa: BLE001
            st = SyncStats(chat_id=chat_id)
            st.errors.append(str(exc))
            out.append(st)
    return out


def backfill_since(days: int = 7, chat_ids: Iterable[str] | None = None) -> list[SyncStats]:
    """补录历史消息：从 N 天前开始全量拉取。"""
    return sync_all(days=days, chat_ids=chat_ids)


def recent_window() -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    return now_ms - int(timedelta(minutes=5).total_seconds() * 1000), now_ms
