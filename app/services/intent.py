"""群消息意图识别：不看固定格式，按「动作意图」判断这条消息要干什么。

业务口径（2026-09）：
  1 退单（refund）：群里发「此单退 / 退货 / 退款 / 不要了 / 拒收」等表达「要退」的消息，
    都按退单处理 —— 能从消息里解析出订单信息就精确匹配原订单；只有一句人话时，
    按消息里的订单号 / 手机号（含尾号）/ 产品名 / 收货人打分，再兜底到「同一发送人最近的在途订单」；
    未发货 -> 标记「已退单」并提示同步上游撤回发货；已发货也受理 —— 未收到货的同步物流拦截、
    已收到货的走退货退款（维护退单物流单号）；
  2 撤单（cancel）：群里发「撤单 / 取消订单 / 作废 / 不发了」等，未发货的订单置「已撤单」
    （只留档，不参与统计与后续流程）；已发货的不撤单，只提示走退货退款；
  3 撤回消息（recalled）：群里把订单消息撤回，按被撤回的消息定位订单，同样走撤单口径。
  详见 recall.py。

识别思路：
  - 订单模板消息只看第一行（沿用老口径：正文里出现「七天无理由退货」不算退单）；
  - 一句人话看整条消息：强意图词直接判定；弱意图词（「退了」「撤了」…）要能确认
    「就是在说某笔订单」才动手 —— 拿不准时只记一笔并提示人工确认，避免把闲聊当退单。

关键词可在 .env 配置：REFUND_KEYWORDS / CANCEL_KEYWORDS（逗号分隔）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    ORDER_STATE_CANCELED,
    ORDER_STATE_REFUNDED,
    Order,
    RawMessage,
    not_recalled,
)
from ..timeutil import now
from . import dedupe
from . import order_state as order_state_service

logger = logging.getLogger(__name__)

INTENT_ORDER = "order"
INTENT_REFUND = "refund"
INTENT_CANCEL = "cancel"
INTENT_COMPLETE = "complete"
INTENT_LABELS = {
    INTENT_ORDER: "新订单",
    INTENT_REFUND: "退单",
    INTENT_CANCEL: "撤单",
    INTENT_COMPLETE: "补全资料",
}

# 模板消息只看消息开头这么多字（避免正文里的「退货」字样被误判）
HEAD_LIMIT = 20

# 退单：强意图词 —— 命中即判定为退单
REFUND_STRONG = [
    "退单", "退货", "退款", "此单退", "这单退", "该单退", "要退", "申请退",
    "不要了", "不想要", "不收了", "拒收", "退回", "退回去",
]
# 退单：弱意图词 —— 需要「确实在说某笔订单」的证据（订单号 / 单 / 引用回复 / 该发送人有在途订单）
REFUND_WEAK = ["退了", "退一下", "退了吧", "退掉", "退这单", "退那单", "帮忙退", "麻烦退", "能退"]

# 撤单：强意图词
CANCEL_STRONG = [
    "撤单", "作废", "取消订单", "此单取消", "这单取消", "取消发货", "不发了", "别发了", "取消吧",
]
# 撤单：弱意图词
CANCEL_WEAK = ["撤回", "撤销", "撤掉", "撤了", "撤一下", "取消一下", "取消掉"]

# 补全资料：群里补发截图 / 补填字段（现象：订单先归档了，图或资料后到）
COMPLETE_STRONG = [
    "补资料", "补充资料", "资料补充", "补齐资料", "资料在下面", "补发截图", "补截图",
    "截图补发", "漏发截图", "漏了截图", "资料漏了", "补资料如下",
]
# 补全资料：弱意图词 —— 「补一下」这类要说清楚在补什么才动手
COMPLETE_WEAK = ["补充", "补齐", "补一下", "补上", "上面漏", "刚才漏", "忘了填", "少填了"]

# 消息里提到「订单」的线索（弱意图词需要它才动手）
ORDER_CONTEXT_HINTS = (
    "单", "订单", "货款", "商品", "买家", "客户", "刚才", "上面", "闲鱼", "老板", "这件", "这个",
)

# 判定为同一笔订单的最低分：手机号 5 / 手机尾号 4 / 产品 3 + 收货人 2 / 产品 3 + 同一发送人 2
MIN_SCORE = 5
SCORE_PHONE = 5
SCORE_PHONE_TAIL = 4
SCORE_PRODUCT = 3
SCORE_RECEIVER = 2
SCORE_ADDRESS = 1        # 地址前 6 字出现在消息里
SCORE_ADDRESS_FULL = 3   # 地址完全一致（地址串很长，撞上的概率极低，是很硬的证据）
SCORE_SENDER = 2
# 打到这个分算「很确定」
HIGH_SCORE = 8

_NOISE_RE = re.compile(r"[\s，。、！？!?,.;；:：~\-—_()（）\[\]【】\"'“”‘’/\\|*#]+")
_ORDER_NO_RE = re.compile(r"(?:订单编号|订单号|交易单号|平台单号|单号)[:：]?\s*([0-9A-Za-z\-]{6,})")
# 裸订单号：闲鱼 / 淘宝订单号是 16~24 位纯数字，群里经常只甩一串数字过来（不带「订单号」前缀）
_BARE_ORDER_NO_RE = re.compile(r"(?<!\d)(\d{16,24})(?!\d)")
# 物流单号：SF5155067424154 / YT1234567890 / JD0123456789 这类「字母前缀 + 长数字」
_TRACKING_NO_RE = re.compile(r"\b([A-Za-z]{1,4}\d{8,}[A-Za-z0-9]*)\b")
_DIGITS_RE = re.compile(r"\d{4,}")


def normalize(text: str) -> str:
    """归一化：去掉空白与常见标点，便于短语匹配（「退 单」也能命中「退单」）。"""
    return _NOISE_RE.sub("", str(text or ""))


def head(text: str) -> str:
    """消息的第一行（跳过空行），模板消息只看这一行。"""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


@dataclass(frozen=True)
class Intent:
    """一条消息的动作意图。"""

    kind: str = ""            # ""（没识别出来）| refund 退单 | cancel 撤单
    keyword: str = ""         # 命中的词
    needs_context: bool = False  # 弱意图词：需要「确实在说某笔订单」的证据才动手
    source: str = ""          # template（模板消息首行）| message（一句人话）

    @property
    def matched(self) -> bool:
        return bool(self.kind)

    @property
    def label(self) -> str:
        return INTENT_LABELS.get(self.kind, "")


def _configured(values: list[str] | None) -> list[str]:
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def classify(text: str, *, template: bool = False) -> Intent:
    """判断消息的动作意图。

    template=True（订单模板消息）只看第一行，避免正文里的「退货」字样误判；
    一句人话看整条消息：.env 配置的关键词最优先，其次是内置强意图词，最后才是弱意图词。
    """
    raw = str(text or "").strip()
    if not raw:
        return Intent()
    scope = head(raw) if template else raw
    body = normalize(scope)
    if not body:
        return Intent()
    source = "template" if template else "message"

    for kw in _configured(settings.refund_keywords):  # .env 里配置的，优先
        if normalize(kw) and normalize(kw) in body:
            return Intent(INTENT_REFUND, kw, False, source)
    for kw in _configured(settings.cancel_keywords):
        if normalize(kw) and normalize(kw) in body:
            return Intent(INTENT_CANCEL, kw, False, source)
    for kw in REFUND_STRONG:
        if kw in body:
            return Intent(INTENT_REFUND, kw, False, source)
    for kw in CANCEL_STRONG:
        if kw in body:
            return Intent(INTENT_CANCEL, kw, False, source)
    for kw in REFUND_WEAK:
        if kw in body:
            return Intent(INTENT_REFUND, kw, True, source)
    for kw in CANCEL_WEAK:
        if kw in body:
            return Intent(INTENT_CANCEL, kw, True, source)
    # 补全资料放在最后：只有退单 / 撤单都认不出来时才考虑，避免抢了上面两类
    for kw in COMPLETE_STRONG:
        if kw in body:
            return Intent(INTENT_COMPLETE, kw, False, source)
    for kw in COMPLETE_WEAK:
        if kw in body:
            return Intent(INTENT_COMPLETE, kw, True, source)
    return Intent()


def route(text: str, *, is_template: bool = False) -> Intent:
    """统一入口：一条消息进来先看它想干什么 —— 新订单 / 退单 / 撤单 / 补全资料。

    is_template 由调用方给出（这块现在由 filter.is_template_message 判定）：
    模板消息就是新订单；一句人话再交给 classify 按意图词打分。
    以后要加意图（查物流 / 催发货）只改这里，上层不用各自凑关键词。
    """
    if is_template:
        return Intent(INTENT_ORDER, "", False, "template")
    if not str(text or "").strip():
        return Intent()
    return classify(text)


def has_order_context(text: str) -> bool:
    """消息里有没有「在说某笔订单」的线索（弱意图词要配合它才动手）。"""
    body = normalize(text)
    return any(h in body for h in ORDER_CONTEXT_HINTS)


def extract_order_no(text: str) -> str:
    """从人话里抠订单号：「订单号 2026... 」/「单号：5127743...」/ 光秃秃一串长数字。"""
    raw = str(text or "")
    match = _ORDER_NO_RE.search(raw)
    if match:
        return match.group(1)
    bare = _BARE_ORDER_NO_RE.search(raw)
    return bare.group(1) if bare else ""


def extract_tracking_no(text: str) -> str:
    """从人话里抠物流单号：「SF5155067424154 这个退单」里的 SF5155067424154。"""
    match = _TRACKING_NO_RE.search(str(text or ""))
    return match.group(1) if match else ""


def norm_tracking(text: str) -> str:
    """物流单号归一化：去空白、转大写（群里复制过来常带空格 / 小写）。"""
    return re.sub(r"\s+", "", str(text or "")).upper()


def find_by_tracking_no(session: Session, tracking_no: str) -> Order | None:
    """按物流单号直接定位订单：已发货的单才有物流单号，命中即唯一证据。"""
    wanted = norm_tracking(tracking_no)
    if not wanted:
        return None
    rows = session.scalars(
        select(Order).where(not_recalled()).where(Order.tracking_no != "").limit(2000)
    ).all()
    for order in rows:
        if norm_tracking(order.tracking_no) == wanted:
            return order
    return None


def find_by_order_no(session: Session, order_no: str) -> Order | None:
    """按订单号直接定位订单：精确号 > 尾号（群里常只发后几位）。"""
    wanted = str(order_no or "").strip()
    if not wanted:
        return None
    stmt = select(Order).where(not_recalled()).order_by(Order.id.desc())
    for row in session.scalars(stmt.where(Order.order_no == wanted)).all():
        return row
    for row in session.scalars(stmt.where(Order.temp_order_no == wanted)).all():
        return row
    if len(wanted) >= 8:
        tail = f"%{wanted}"
        for row in session.scalars(
            stmt.where(Order.order_no.like(tail)).limit(5)
        ).all():
            return row
        for row in session.scalars(
            stmt.where(Order.temp_order_no.like(tail)).limit(5)
        ).all():
            return row
    return None


def message_age_days(msg: RawMessage) -> int:
    """这条消息离现在多少天（没有消息时间算 0，即按新消息处理）。"""
    when = getattr(msg, "msg_time", None)
    if not isinstance(when, datetime):
        return 0
    return max(0, (now() - when).days)


def digits_of(text: str) -> list[str]:
    """消息里的数字串（≥4 位），用于手机号 / 尾号比对。"""
    return _DIGITS_RE.findall(str(text or ""))


@dataclass
class Match:
    """消息 -> 订单 的匹配结果。"""

    order: Order | None = None
    confidence: str = ""      # high 很确定 | medium 比较确定 | low 拿不准
    reason: str = ""
    hits: list[str] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.order is not None and self.confidence in ("high", "medium")


def _recent_orders(session: Session, days: int) -> list[Order]:
    stmt = select(Order).where(not_recalled())
    if days and days > 0:
        stmt = stmt.where(Order.created_at >= now() - timedelta(days=days))
    return list(session.scalars(stmt.order_by(Order.id.desc()).limit(500)).all())


def _sender_key(order: Order) -> tuple[str, str]:
    return str(order.sender_id or ""), str(order.sender_name or "")


def _score(
    order: Order,
    *,
    body: str,
    digits: list[str],
    order_no: str,
    tracking_no: str,
    phone: str,
    product: str,
    receiver: str,
    address: str,
    sender_id: str,
    sender_name: str,
) -> tuple[int, list[str]]:
    """给一笔候选订单打分，返回 (分数, 命中的线索)。"""
    hits: list[str] = []
    score = 0

    if order_no and str(order.order_no or "") == order_no:
        score += 100
        hits.append(f"订单号 {order_no}")
    elif order_no and str(order.temp_order_no or "") == order_no:
        score += 100
        hits.append(f"订单号 {order_no}（临时编号）")
    elif order_no and len(order_no) >= 8 and (
        str(order.order_no or "").endswith(order_no)
        or str(order.temp_order_no or "").endswith(order_no)
    ):
        score += 60
        hits.append(f"订单号尾号 {order_no}")

    if tracking_no and norm_tracking(order.tracking_no) == norm_tracking(tracking_no):
        score += 100
        hits.append(f"物流单号 {order.tracking_no}")

    cur_phone = dedupe.norm_digits(order.receiver_phone)
    wanted_phone = dedupe.norm_digits(phone)
    if wanted_phone and cur_phone == wanted_phone:
        score += SCORE_PHONE
        hits.append("手机号一致")
    elif cur_phone:
        # 人话里常只写后四位 / 尾号
        if any(len(d) >= 4 and cur_phone.endswith(d) for d in digits) or (
            wanted_phone and cur_phone.endswith(wanted_phone[-4:])
        ):
            score += SCORE_PHONE_TAIL
            hits.append("手机尾号一致")

    product_norm = dedupe.norm_text(order.product_name)
    wanted_product = dedupe.norm_text(product)
    if wanted_product and product_norm == wanted_product:
        score += SCORE_PRODUCT
        hits.append("产品一致")
    elif len(product_norm) >= 4 and product_norm[:4] in body:
        # 「徕芬吹风机那单退了」：产品名前 4 个字出现即可（再配合其它线索确认）
        score += SCORE_PRODUCT
        hits.append(f"产品名（{order.product_name[:4]}）一致")

    receiver_norm = dedupe.norm_text(order.receiver_name)
    if len(receiver_norm) >= 2 and receiver_norm in body:
        score += SCORE_RECEIVER
        hits.append("收货人一致")
    elif receiver and receiver_norm and receiver_norm == dedupe.norm_text(receiver):
        score += SCORE_RECEIVER
        hits.append("收货人一致")

    address_norm = dedupe.norm_text(order.receiver_address)
    if address and len(address_norm) >= 6 and dedupe.norm_text(address) == address_norm:
        # 地址完全一致：收货地址写一长串，撞上的概率极低，比只露前几个字硬得多
        score += SCORE_ADDRESS_FULL
        hits.append("地址一致")
    elif len(address_norm) >= 6 and address_norm[:6] in body:
        score += SCORE_ADDRESS
        hits.append("地址一致（前 6 字）")

    if sender_id and str(order.sender_id or "") == sender_id:
        score += SCORE_SENDER
        hits.append("同一发送人")
    elif sender_name and str(order.sender_name or "") == sender_name:
        score += SCORE_SENDER
        hits.append("同一发送人")
    return score, hits


def find_order(
    session: Session,
    msg: RawMessage,
    fields: dict | None = None,
    *,
    days: int | None = None,
) -> Match:
    """按这条消息找相关订单。

    顺序：引用的消息 > 消息里的订单号 > 手机号/产品/收货人/地址打分 > 同一发送人最近的在途订单。
    找不到就返回空 Match，调用方据此提示人工处理（绝不乱动别的订单）。
    """
    from . import recall as recall_service  # 局部导入：避免与 recall <-> intent 互相 import

    fields = dict(fields or {})
    text = str(getattr(msg, "raw_text", "") or "")
    body = normalize(text)
    digits = digits_of(text)
    sender_id = str(getattr(msg, "sender_id", "") or "")
    sender_name = str(getattr(msg, "sender_name", "") or "")

    # 1) 回复 / 引用了某条消息：那条消息对应的订单最可信
    parent_id = str(getattr(msg, "parent_message_id", "") or "")
    if parent_id:
        quoted = [o for o in recall_service.orders_of_message(session, parent_id) if not o.is_recalled]
        if len(quoted) == 1:
            order = quoted[0]
            return Match(order, "high", f"引用了这条消息对应的订单 {order.order_no}", ["引用消息"])
        if len(quoted) > 1:
            logger.info("引用的消息 %s 对应多笔订单：%s", parent_id, [o.id for o in quoted])

    order_no = str(fields.get("order_no") or "").strip() or extract_order_no(text)
    tracking_no = (
        str(fields.get("tracking_no") or "").strip() or extract_tracking_no(text)
    )
    phone = str(fields.get("receiver_phone") or "")
    product = str(fields.get("product_name") or "")
    receiver = str(fields.get("receiver_name") or "")
    address = str(fields.get("receiver_address") or "")

    # 2) 订单号 / 物流单号：最硬的证据，按号直查（不受「最近 N 天」窗口限制，
    #    几天前的单、补录进来的历史消息也能认出来）
    by_no = find_by_order_no(session, order_no)
    if by_no is not None:
        return Match(
            by_no, "high", f"按订单号 {order_no} 匹配到订单 {by_no.order_no}", ["订单号一致"]
        )
    by_tracking = find_by_tracking_no(session, tracking_no)
    if by_tracking is not None:
        return Match(
            by_tracking,
            "high",
            f"按物流单号 {by_tracking.tracking_no} 匹配到订单 {by_tracking.order_no}",
            ["物流单号一致"],
        )

    # 3) 打分：手机号 / 尾号 / 产品 / 收货人 / 地址 / 物流单号 / 发送人
    best: Order | None = None
    best_score = 0
    best_hits: list[str] = []
    window = settings.refund_match_days if days is None else days
    for order in _recent_orders(session, window):
        score, hits = _score(
            order,
            body=body,
            digits=digits,
            order_no=order_no,
            tracking_no=tracking_no,
            phone=phone,
            product=product,
            receiver=receiver,
            address=address,
            sender_id=sender_id,
            sender_name=sender_name,
        )
        if score > best_score:
            best, best_score, best_hits = order, score, hits
    if best is not None and best_score >= MIN_SCORE:
        confidence = "high" if best_score >= HIGH_SCORE else "medium"
        return Match(
            best,
            confidence,
            f"按 { '、'.join(best_hits) } 匹配到订单 {best.order_no}",
            best_hits,
        )

    # 4) 兜底：只有消息里完全没有任何订单线索时，才退到「同一发送人最近的在途订单」。
    #    带了订单号 / 物流单号 / 手机号却一笔都没对上，一律不猜 —— 猜错就是把别人的
    #    正常订单误判成退单，宁可提示人工补信息。
    if (
        parent_id
        or order_no
        or tracking_no
        or phone
        or product
        or receiver
        or address
        or digits
    ):
        return Match(
            None,
            "",
            "消息里有订单号 / 物流单号 / 手机号等线索，但没对上任何订单："
            "请补一个订单号或收货人手机号再发一次，或到订单页手工处理",
            [],
        )

    fallback_days = settings.intent_match_days if days is None else days
    candidates = [
        o
        for o in _recent_orders(session, fallback_days)
        if (sender_id and o.sender_id == sender_id)
        or (sender_name and o.sender_name == sender_name)
    ]
    alive = [
        o for o in candidates if o.order_state not in (ORDER_STATE_CANCELED, ORDER_STATE_REFUNDED)
    ]
    if not alive:
        return Match(None, "", "没找到相关订单：消息里没有订单号 / 手机号，该发送人也没有在途订单")
    order = alive[0]
    if len(alive) == 1:
        return Match(
            order,
            "medium",
            f"消息里没有订单号 / 手机号，按发送人兜底匹配到唯一在途订单 {order.order_no}",
            ["同一发送人的在途订单"],
        )
    return Match(
        order,
        "low",
        f"该发送人有 {len(alive)} 笔在途订单（{alive[0].order_no} 等），无法确定是哪一笔",
        [],
    )


def handle(
    session: Session,
    msg: RawMessage,
    intent: Intent,
    fields: dict | None = None,
    *,
    commit: bool = True,
) -> dict:
    """按意图处理一条群消息，返回处理结果（供回执、消息页与日志使用）。

    返回：{order, action, accepted, confidence, note}
      action：refund 已按退单处理 | cancel 已按撤单处理 | review 拿不准，留给人工 | "" 没匹配到
    """
    from . import recall as recall_service
    from . import refund as refund_service

    match = find_order(session, msg, fields)
    order = match.order
    result = {
        "order": order,
        "action": "",
        "accepted": False,
        "confidence": match.confidence,
        "note": match.reason,
        "intent": intent.kind,
        "keyword": intent.keyword,
    }
    if order is None:
        result["note"] = (
            f"识别到{intent.label}意图（{intent.keyword}），但没匹配到对应订单："
            "请让发送人补充订单号 / 手机号，或到订单页手工处理"
        )
        return result

    # 同步历史群消息（过去很久的）只有「很确定」的匹配才动手：否则很容易误伤现在在途的订单
    age = message_age_days(msg)
    if age > settings.intent_match_days and match.confidence != "high":
        result["note"] = (
            f"识别到{intent.label}意图（{intent.keyword}），但这条消息已过去 {age} 天"
            "（多为同步历史群消息），把握不足先不动订单：请人工核对后处理"
        )
        return result

    # 拿不准是哪一笔（发送人名下挂着多笔在途订单，说不清退哪单）：一律不动任何订单。
    # 这类发送人多为运营 / 客服，名下好几单同时挂着，随便挑一笔就是把人家的正常订单
    # 误判成退单 —— 比「没处理」严重得多。留痕 + 回群问清（带订单号 / 引用原订单消息）。
    if match.confidence == "low":
        result["order"] = None
        result["action"] = "review"
        result["note"] = (
            f"识别到{intent.label}意图（{intent.keyword}），但{match.reason}："
            "没有把握，未改动任何订单。请带上订单号，或引用原订单消息再发一次"
        )
        if commit:
            session.commit()
        return result

    if intent.kind == INTENT_REFUND:
        order, accepted = refund_service.mark_refund(
            session, order, msg, intent.keyword, commit=False
        )
        result.update(order=order, action="refund", accepted=accepted)
        result["note"] = (
            f"已按退单处理（{intent.keyword}）：{match.reason}"
            if accepted
            else f"该订单已撤单（只留档）：退单请求未受理 —— {match.reason}"
        )
        if commit:
            session.commit()
        return result

    # 撤单：未发货才能撤（已发货的只提示走退单流程，不改状态、不动统计）
    if order_state_service.is_shipped(order):
        result["note"] = (
            f"该订单已发货（物流单号 {(order.tracking_no or '').strip()}）：不能撤单，"
            "用户要退请走退单流程（没收到货的同步物流拦截、已收货的维护退单物流单号）"
        )
        return result

    recall_service.mark_recalled(
        session,
        order,
        msg,
        recaller=str(getattr(msg, "sender_name", "") or getattr(msg, "sender_id", "") or ""),
        reason=f"群里发消息撤单（{intent.keyword}）",
        commit=False,
    )
    result.update(action="cancel", accepted=True)
    result["note"] = f"已按撤单处理（{intent.keyword}）：{match.reason}，订单只留档"
    if commit:
        session.commit()
    return result
