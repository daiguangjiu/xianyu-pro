"""解析纠错的采集与复用（持续学习的地基）。

以前运营在订单页把字段改对了就结束了 —— 同样的写法下一单还会取错，还得再改一次。
这里做两件事把这条闭环接上：

  1. record_corrections（采集）：订单页改完字段，把「机器值 -> 人工值」连同原文那一行存档；
  2. apply_known_corrections（回用）：新订单解析完，按「字段 + 原文那一行」查历史样本，
     命中就直接采用人工结论，并在解析说明里写明「依据哪一单改过」。

只针对「解析出来的字段」，不包含推单状态 / 物流这些由人推进的状态。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Order, ParseCorrection
from ..parser.engine import ParseResult
from ..parser.rules import get_rules

logger = logging.getLogger("order-hub.learn")

# 值得学习的字段（key -> 中文标签）：这些才是解析错了该被纠正的
LEARNABLE_FIELDS: dict[str, str] = {
    "product_name": "产品名称",
    "spec": "规格",
    "buyer_nick": "买家昵称",
    "receiver_name": "收货人",
    "receiver_phone": "手机号码",
    "receiver_address": "地址信息",
    "order_no": "订单号",
    "sale_amount": "售价",
    "deal_amount": "成交价",
    "total_amount": "商品总价",
    "cost_amount": "成本价",
    "shipping_fee": "运费",
    "platform_fee": "平台技术服务费",
    "quantity": "数量",
    "xianyu_account": "出货闲鱼号",
    "payee_name": "回款支付宝归属人",
    "supplier_name": "所属供应链",
}


def _cost_reason(sale: object, value: object) -> str:
    """这个值当成本对不对（空串 = 没问题）：复用成本护栏的判据，避免两套标准。"""
    from .cost_guard import cost_reason_for

    return cost_reason_for(sale, value)


def as_value_text(value: object) -> str:
    """统一转成可比较的字符串：59.0 与 59 应当看成同一个值。"""
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def field_labels(field_key: str) -> list[str]:
    """该字段在规则里配的标签名（收货人 / 售价 / 成本价……）。"""
    for platform in get_rules().platforms.values():
        for fr in platform.fields:
            if fr.key == field_key:
                return [str(x) for x in fr.labels if x]
    return []


def find_source_line(raw_text: str | None, value: object, field_key: str = "") -> str:
    """原文里「标签 + 值」的那一行，作为认下一次同样写法的指纹。

    必须连标签一起认：只按值去找会把「售价 59」误记到含 59 的手机号行上
    （15920384664 里就有 59），下次遇到任何带 59 的手机号都会被乱改。
    """
    needle = as_value_text(value)
    if not needle or not raw_text:
        return ""
    labels = field_labels(field_key) if field_key else []
    for line in raw_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if not labels:
            # 没配标签的字段退一步：要求整行几乎就是这个值，避免子串误伤
            if stripped == needle:
                return stripped[:255]
            continue
        for label in labels:
            if re.search(
                rf"{re.escape(label)}\s*[:：]?\s*{re.escape(needle)}(?:\s|$|[,，。」」)）])",
                stripped,
            ):
                return stripped[:255]
    return ""


def snapshot_parsed(order: Order) -> dict[str, str]:
    """改订单之前先把解析字段的值拍个快照，改完比对差异就知道人工纠了什么。"""
    return {key: as_value_text(getattr(order, key, None)) for key in LEARNABLE_FIELDS}


def record_corrections(
    session: Session,
    order: Order,
    before: dict[str, str],
    operator: str = "",
    commit: bool = False,
) -> list[ParseCorrection]:
    """把「机器取错、人工改对」的差异存成纠错样本。

    只记录真正的纠正：机器有值、人也改成了别的值。补空值（原来没取到）不算，
    那是补资料不是纠错；改回原样（值没变）也不算。
    """
    saved: list[ParseCorrection] = []
    for key, label in LEARNABLE_FIELDS.items():
        old = before.get(key, "")
        new = as_value_text(getattr(order, key, None))
        if not old or not new or old == new:
            continue
        if key == "cost_amount":
            # 「成本比售价还高」的改动多半是手滑（真亏本也该人工确认一次再落数据）：
            # 这种值一旦被当成「人工结论」学进去，后面所有同样写法的单都会被它改错
            # （2026-10-01：某一单 45 → 133 的改动，让 118 / 122 / 124 / 130 连续中招）。
            reason = _cost_reason(order.sale_amount or order.deal_amount, new)
            if reason:
                logger.info("这条成本纠正不学习（%s）：%s → %s", reason, old, new)
                continue
        saved.append(
            ParseCorrection(
                order_id=order.id,
                order_no=order.order_no or "",
                platform=order.platform or "",
                field_key=key,
                field_label=label,
                machine_value=old[:255],
                human_value=new[:255],
                source_line=find_source_line(order.raw_text, old, key),
                raw_snippet=(order.raw_text or "")[:500],
                operator=operator,
            )
        )
        session.add(saved[-1])
    if commit and saved:
        session.commit()
    return saved


def _coerce(text: str, sample: object) -> object:
    """把样本里的人工值还原成字段原本的类型（金额是 float、数量是 int）。"""
    if isinstance(sample, bool):
        return text.lower() in ("1", "true", "yes")
    if isinstance(sample, float):
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(sample, int):
        try:
            return int(float(text))
        except ValueError:
            return None
    return text


def apply_known_corrections(
    session: Session,
    result: ParseResult,
    raw_text: str | None,
) -> int:
    """解析完之后，用历史人工纠正校准同类写法。

    命中「同字段 + 原文同一行」的样本就直接采用人工结论，并把依据写进解析说明
    （notes，不进 warnings —— 有据可查的自动纠正不该让运营再核对一遍）。
    """
    applied = 0
    for key, label in LEARNABLE_FIELDS.items():
        value = result.fields.get(key)
        if value is None:
            continue
        line = find_source_line(raw_text, value, key)
        if not line:
            continue
        samples = list(
            session.scalars(
                select(ParseCorrection)
                .where(
                    ParseCorrection.field_key == key,
                    ParseCorrection.source_line == line,
                )
                .order_by(ParseCorrection.id.desc())
                .limit(5)
            )
        )
        if not samples:
            continue
        # 同样写法被改成了不同的值（单人反复改 / 两人改得不一样）：有歧义，不自动采用
        if len({s.human_value for s in samples}) > 1:
            continue
        sample = samples[0]
        human = _coerce(sample.human_value, value)
        if human is None or human == value:
            continue
        if key == "cost_amount":
            # 历史样本里的成本值不合理（典型：比售价还高）就不采用 —— 否则一条脏样本会把
            # 后面每一单同样写法的成本都改错。这里退回原文解析出来的值，并写明原因让人知道。
            sale = (
                result.fields.get("deal_amount")
                or result.fields.get("sale_amount")
                or 0
            )
            reason = _cost_reason(sale, human)
            if reason:
                result.warnings.append(
                    f"历史修正样本里的「{label}」是 {as_value_text(human)}（{reason}），"
                    f"已忽略该样本；本次按原文取到的值是 {as_value_text(value)}"
                )
                continue
        result.fields[key] = human
        result.sources[key] = "learned:correction"
        result.notes.append(
            f"「{label}」原文写法与第 {sample.order_no or sample.order_id} 单一致，"
            f"已按当时的修正自动改为「{as_value_text(human)}」（原 {as_value_text(value)}）"
        )
        applied += 1
    return applied
