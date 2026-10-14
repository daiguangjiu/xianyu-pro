"""处理完成后的回执通知：告知发送人「已收到并处理」。

推送策略（系统设置页可配置，写入 .env 后即时生效）：
- notify_mode               reply 群内回复 / direct 私信发送人 / off 彻底关闭
- notify_active_hours       允许下发的时段（如 09:00-22:00），之外为静默期
- notify_quiet_action       静默期策略：hold 挂起（恢复后合并补发）/ drop 丢弃
- notify_min_interval_seconds  同一订单的最小回执间隔，防止重复刷屏
- notify_max_per_hour       每小时下发上限（0=不限），超限挂起等下一窗口补发
- notify_digest             挂起的回执恢复后是否合并成一条发送
- notify_utc_offset         时段判断所用时区偏移（小时），默认东八区
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from jinja2 import Environment
from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..feishu.client import FeishuClient, get_client
from ..messages_registry import CH_GROUP, CH_PM_SENDER, CH_PM_SYNC
from ..models import REFUND_STATUS_NOT_PUSHED, Order, RawMessage
from ..timeutil import now, utc_now
from .filter import is_bot_message
from . import message_rules

logger = logging.getLogger(__name__)

# 退单回执里 @ 供应链对接人那句：还没推给供应商时说这句（说「联系供应链拦截」是空话）
REFUND_CC_TEXT_NOT_PUSHED = (
    "这单还没推给供应商，无需继续推单发货，也请发起人安排退款给客户。"
    "如果已退款给客户，也请及时同步！"
)


def message_age_minutes(msg: RawMessage) -> float | None:
    """这条消息过去多少分钟；超过回执时效（notify_max_age_minutes）就返回分钟数，否则 None。

    用来兜住两个场景：补录几天前的历史消息、服务中断后一次性补拉 ——
    这些老消息该入库、该解析，但不该再往群里补几十条「已收到」。
    """
    limit = max(int(getattr(settings, "notify_max_age_minutes", 0) or 0), 0)
    if limit <= 0 or msg.msg_time is None:
        return None
    age = (now() - msg.msg_time).total_seconds() / 60
    if age > limit:
        return age
    return None


def mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    return f"{p[:3]}****{p[-4:]}" if len(p) >= 7 else p


# 订单上标的「待核对」写在 review_reason 的「待核对：地址、手机号」这一段里
_PENDING_CHECK_RE = re.compile(r"待核对：([^；]+)")


def pending_check_labels(order: Order) -> list[str]:
    """这张单有哪些收货字段待核对（「地址（只有截图里有）」这种，带原因）。

    地址 / 收货人 / 手机号写错会发错货，回群通知和推单信息里都要带上这句提醒，
    不能只留一句笼统的「部分字段待人工确认」—— 运营看不见到底该核对什么。
    """
    match = _PENDING_CHECK_RE.search(getattr(order, "review_reason", "") or "")
    if not match:
        return []
    # 只按顿号切：原因写在括号里（「地址（只有截图里有）」），括号里的逗号不能切
    return [part.strip() for part in match.group(1).split("、") if part.strip()]


def pending_check_names(order: Order) -> list[str]:
    """待核对的字段名（去掉括号里的原因）：通知文案里用，短一点运营才看得进去。"""
    return [re.sub(r"（[^）]*）", "", label).strip() for label in pending_check_labels(order)]


def pending_check_line(order: Order) -> str:
    """通知里那句待核对提醒（没有待核对就是空串）。"""
    names = pending_check_names(order)
    if not names:
        return ""
    # 全都是「两边都没取到」：这是缺信息要补录，不是识别存疑，措辞要分开
    if all("都没有" in label for label in pending_check_labels(order)):
        return f"⚠️ {'、'.join(names)}缺失：群里和截图都没有，请先补录再发货"
    return f"⚠️ {'、'.join(names)}待核对：截图识别不确定或与群里对不上，发货前请先核对"


# ------------------------------------------------------------------ 通知消息模板
# 收到模板消息 / 订单截图后的回执模板（系统设置页可编辑，留空 = 用下面的内置格式）
DEFAULT_RECEIPT_TEMPLATE = """✅ 已收到并处理，订单已归档
{% if 订单号 %}
订单号：{{ 订单号 }}
{% endif %}{% if 产品 %}
产品：{{ 产品 }}
{% endif %}{% if 备注 %}
备注：{{ 备注 }}
{% endif %}{% if 金额 %}

金额：{{ 金额 }}
{% endif %}{% if 收货 %}
收货：{{ 收货 }}
{% endif %}{% if 供应商 %}
供应商：{{ 供应商 }}
{% endif %}{% if 待核对 %}

⚠️ {{ 待核对 }}待核对：截图识别不确定或与群里对不上，发货前请先核对
{% elif 待确认 %}

⚠️ 部分字段待人工确认，请到系统补录
{% endif %}{% if 详情链接 %}

详情：{{ 详情链接 }}
{% endif %}"""

# 退单通知里「订单明细」那几行的模板
DEFAULT_REFUND_DETAIL_TEMPLATE = """{% if 订单号 %}订单号：{{ 订单号 }}
{% endif %}{% if 产品 %}产品：{{ 产品 }}
{% endif %}{% if 收货 %}收货：{{ 收货 }}
{% endif %}处理：{{ 处理 }}{% if 详情链接 %}
详情：{{ 详情链接 }}{% endif %}"""


def render_notify(template: str, ctx: dict) -> str:
    """渲染通知模板：{{ 订单号 }} 这类变量 + {% if %} 条件，写错也不影响下发。

    模板为空或语法写错时返回空串，调用方退回内置格式，保证通知一定发得出去。
    """
    text = (template or "").strip()
    if not text:
        return ""
    try:
        env = Environment(trim_blocks=True, lstrip_blocks=True)
        return env.from_string(text).render(**ctx).strip()
    except Exception as exc:  # noqa: BLE001  模板写错不该让通知发不出去
        logger.warning("通知模板渲染失败，按内置格式发送：%s", exc)
        return ""


def notify_context(order: Order, msg: RawMessage | None = None, **extra) -> dict:
    """通知模板可使用的变量（中文命名，设置页里直接写人话就行）。"""
    sale = float(order.sale_amount or 0.0)
    cost = float(order.cost_amount or 0.0)
    ship = float(order.shipping_fee or 0.0)
    fee = float(order.platform_fee or 0.0)
    money = [f"售价 ¥{sale:.2f}"] if sale else []
    if cost:
        money.append(f"成本 ¥{cost:.2f}")
    if ship:
        money.append(f"运费 ¥{ship:.2f}")
    if fee:
        money.append(f"平台技术服务费 ¥{fee:.2f}")
    order_no = order.order_no or ""
    if order_no.startswith(("AUTO-", "TMP-")):
        order_no = ""  # 临时单号：等截图补到正式单号再显示
    spec = f" / {order.spec}" if order.spec else ""
    qty = f" x{order.quantity}" if (order.quantity or 0) > 1 else ""
    receiver = ""
    if order.receiver_name or order.receiver_phone:
        receiver = (
            f"{order.receiver_name or '-'} {mask_phone(order.receiver_phone)}".rstrip()
        )
    base = settings.public_base_url.rstrip("/")
    ctx = {
        "订单号": order_no,
        "产品": f"{order.product_name}{spec}{qty}" if order.product_name else "",
        "产品名称": order.product_name or "",
        "规格": order.spec or "",
        "数量": str(order.quantity or 1),
        "售价": f"{sale:.2f}",
        "成本": f"{cost:.2f}",
        "运费": f"{ship:.2f}",
        # 「平台技术服务费」是统一后的叫法；老的模板里写的是「手续费」，两个变量都给，别把已配好的模板搞坏
        "手续费": f"{fee:.2f}",
        "平台技术服务费": f"{fee:.2f}",
        "毛利": f"{round(sale - cost - ship - fee, 2):.2f}",
        "金额": " ｜ ".join(money),
        "收货": receiver,
        "收货人": order.receiver_name or "",
        "收货电话": mask_phone(order.receiver_phone),
        "买家": order.buyer_nick or "",
        "供应商": order.supplier_name or "",
        "闲鱼号": order.xianyu_account or "",
        "归属人": order.payee_name or "",
        "备注": order.note or "",
        "待确认": "是" if order.needs_review else "",
        # 收货信息待核对（地址 / 收货人 / 手机号）：通知里要写清楚核对什么
        "待核对": "、".join(pending_check_names(order)),
        "待核对提醒": pending_check_line(order),
        "详情链接": (
            f"{base}/orders/{order.id}" if (settings.notify_with_link and order.id) else ""
        ),
        # 发货后同步物流单号的通知要用（发货模板）
        "物流单号": order.tracking_no or "",
        "物流公司": (order.tracking_company or "").strip(),
        # 「是否更新」：这次是改单号再通知一次（"是"），第一次填写为空 —— 发货模板里可据此写不同话术
        "是否更新": "",
        "发送人姓名": (getattr(msg, "sender_name", "") or "") if msg else "",
    }
    ctx.update(extra)
    return ctx


def _at_enabled(rule: str, key: str) -> bool:
    """@ 规则里有没有这一项：notify_refund_at=sender,cc 这类逗号分隔的开关。"""
    return key in [v.strip().lower() for v in (rule or "").split(",") if v.strip()]


def build_receipt(msg: RawMessage, order: Order) -> str:
    """收到模板消息 / 订单截图、订单归档后的回执。

    设置页配了模板就按模板渲染，没配（或模板写错）就用内置格式；
    @ 谁由 notify_receipt_at 决定：sender=@发送人、names=@名单里的人（可多个），
    同一个不会被 @ 两遍；私信模式下 @ 无效、只发文字。
    """
    text = render_notify(settings.notify_receipt_template, notify_context(order, msg))
    if not text:
        text = _default_receipt_text(msg, order)
    if settings.notify_mode == "direct":
        return text  # 私信：@ 无效，只发文字

    at_ids: list[str] = []
    at_lines: list[str] = []
    if _at_enabled(settings.notify_receipt_at, "sender") and (msg.sender_id or "").startswith(
        "ou_"
    ):
        at_ids.append(msg.sender_id)
    if _at_enabled(settings.notify_receipt_at, "names"):
        people = parse_at_people(settings.notify_receipt_at_names)
        found = _lookup_open_ids(
            [name for name, _ in people],
            msg.chat_id,
            preset=settings.notify_receipt_at_open_ids,
            label="回执",
        )
        for name, note in people:
            open_id = found.get(name, "")
            if not open_id:
                continue
            if open_id not in at_ids:
                at_ids.append(open_id)
            if note:
                # 这个人配了单独一句话：正文后面再 @ 他一次并带上这句话
                at_lines.append(_at(open_id, note))
    if at_ids:
        text = " ".join(f'<at user_id="{open_id}"></at>' for open_id in at_ids) + " " + text
    if at_lines:
        text = f"{text}\n\n" + "\n".join(at_lines)
    return text


def _default_receipt_text(msg: RawMessage, order: Order) -> str:
    """构造回执文本（纯文本，飞书 text 消息）。

    排版：按「订单信息 / 金额 / 收货与供应商 / 待确认与链接」分组，组间留空行，
    手机上也能一眼扫完。订单号还没从截图补全（TMP- 临时号）时先不显示，
    等截图解析完再由补发线程发出带正式订单号的回执。
    """
    lines = ["✅ 已收到并处理，订单已归档"]

    info: list[str] = []
    if order.order_no and not order.order_no.startswith(("AUTO-", "TMP-")):
        info.append(f"订单号：{order.order_no}")
    if order.product_name:
        spec = f" / {order.spec}" if order.spec else ""
        qty = f" x{order.quantity}" if order.quantity and order.quantity > 1 else ""
        info.append(f"产品：{order.product_name}{spec}{qty}")
    if order.note:
        info.append(f"备注：{order.note}")
    if info:
        lines += [""] + info

    money = [f"售价 ¥{order.sale_amount:.2f}"] if order.sale_amount else []
    if order.cost_amount:
        money.append(f"成本 ¥{order.cost_amount:.2f}")
    if order.shipping_fee:
        money.append(f"运费 ¥{order.shipping_fee:.2f}")
    if order.platform_fee:
        money.append(f"平台技术服务费 ¥{order.platform_fee:.2f}")
    if money:
        lines += ["", "金额：" + " ｜ ".join(money)]

    extra: list[str] = []
    if order.receiver_name or order.receiver_phone:
        extra.append(
            f"收货：{order.receiver_name or '-'} {mask_phone(order.receiver_phone)}".rstrip()
        )
    elif order.buyer_nick:
        extra.append(f"买家：{order.buyer_nick}")
    if order.supplier_name:
        extra.append(f"供应商：{order.supplier_name}")
    if extra:
        lines += [""] + extra

    pending = pending_check_line(order)
    if pending:
        lines += ["", pending]
    elif order.needs_review:
        lines += ["", "⚠️ 部分字段待人工确认，请到系统补录"]

    if settings.notify_with_link and order.id:
        base = settings.public_base_url.rstrip("/")
        lines += ["", f"详情：{base}/orders/{order.id}"]

    return "\n".join(lines)


def build_duplicate_receipt(msg: RawMessage, order: Order) -> str:
    """重复订单回执：群里又发了一遍相同内容 -> 提醒「已收到，未重复归档」。

    设置页配了模板就按模板渲染，没配（或模板写错）就用内置格式。
    """
    text = render_notify(settings.notify_duplicate_template, notify_context(order, msg))
    if not text:
        lines = ["✅ 这笔订单已收到过，内容没有变化，系统未重复归档也未重复推单"]
        if order.order_no:
            lines.append(f"订单号：{order.order_no}")
        if order.product_name:
            lines.append(f"产品：{order.product_name}")
        if order.receiver_phone:
            lines.append(f"收货：{order.receiver_name} {mask_phone(order.receiver_phone)}")
        lines.append("如信息有变更，请重新发送修改后的订单模板。")
        text = "\n".join(lines)
    if settings.notify_mode != "direct" and (msg.sender_id or "").startswith("ou_"):
        text = f'<at user_id="{msg.sender_id}"></at> {text}'
    return text


def _refund_cc_text(order: Order) -> str:
    """@ 供应链对接人那句处理口径：按「推到哪一步了」说人话。

    还没推给供应商的单，供应商那边压根没下过单，说「联系供应链拦截」是句空话
    （2026-10-02 修）：改成「直接不推，退款给客户」。
    """
    from .refund import PATH_NOT_PUSHED, refund_path  # 局部导入，避免 notify <-> refund 循环依赖
    if (order.refund_status or "").strip() == REFUND_STATUS_NOT_PUSHED or (
        not (order.refund_status or "").strip() and refund_path(order) == PATH_NOT_PUSHED
    ):
        return REFUND_CC_TEXT_NOT_PUSHED
    return settings.refund_cc_text


def build_refund_receipt(
    msg: RawMessage,
    order: Order,
    keyword: str = "",
    client: FeishuClient | None = None,
) -> str:
    """退单回执（业务口径 2026-09）：

        @发送人 退单信息已收到！
        @供应链对接人（牛磊） 已识别到本单退单申请，会尽快联系供应链拦截处理！有问题随时沟通。
        （空行）
        订单号 / 产品 / 收货 / 处理 / 详情

    第一行 @ 发送人确认「退单信息已收到」，第二行 @ 固定的供应链对接人（默认牛磊，
    .env 的 REFUND_CC_NAMES 可改）告知处理口径，两行之后空一行再列订单明细。
    keyword 仅保留给调用方（文案里不再重复关键词，处理口径见「处理：」行）。
    """
    from .refund import PATH_NOT_PUSHED, intercept_hint, refund_path  # 局部导入，避免循环依赖

    # @ 谁（设置页可配）：sender=发消息的人、cc=供应链对接人，可只选一个或都不 @
    in_group = settings.notify_mode != "direct"
    at_sender = _at_enabled(settings.notify_refund_at, "sender")
    at_cc = _at_enabled(settings.notify_refund_at, "cc")
    lines: list[str] = []
    if in_group:
        lines.append(
            _at(recipient_open_id(order, msg), settings.refund_ack_text)
            if at_sender
            else settings.refund_ack_text
        )
        # @ 到人才发这句处理口径；没勾 @ 对接人 / 查不到人就不发（避免无人负责的空话）
        # 名单里可以写「牛磊|请优先拦截」，没写就用下面那句默认文案
        if at_cc:
            people = parse_at_people(settings.refund_cc_names)
            found = _lookup_open_ids(
                [name for name, _ in people],
                msg.chat_id,
                client,
                preset=settings.refund_cc_open_ids,
                label="退单回执",
            )
            lines += [
                _at(found[name], note or _refund_cc_text(order))
                for name, note in people
                if found.get(name)
            ]
    else:
        # 私信发送人：@ 无效，只发文字
        lines.append(settings.refund_ack_text)

    lines.append("")
    ctx = notify_context(order, msg, 处理=intercept_hint(order), 退单关键词=keyword)
    detail = render_notify(settings.notify_refund_detail_template, ctx)
    if detail:
        lines.append(detail)
    else:
        if order.order_no:
            lines.append(f"订单号：{order.order_no}")
        if order.product_name:
            lines.append(f"产品：{order.product_name}")
        if order.receiver_name or order.receiver_phone:
            lines.append(
                f"收货：{order.receiver_name} {mask_phone(order.receiver_phone)}"
            )
        lines.append(f"处理：{intercept_hint(order)}")
        if settings.notify_with_link and order.id:
            base = settings.public_base_url.rstrip("/")
            lines.append(f"详情：{base}/orders/{order.id}")
    return "\n".join(lines)


def _at(open_id: str, text: str) -> str:
    """@ 某人：只有拿到 open_id 才是真 @（群里才会通知到本人），否则只发文字。"""
    value = str(open_id or "")
    return f'<at user_id="{value}"></at> {text}' if value.startswith("ou_") else text


_MEMBER_CACHE_TTL = 600.0
_MEMBER_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def _chat_members(client: FeishuClient | None, chat_id: str) -> dict[str, str]:
    """群成员 open_id -> 姓名（10 分钟缓存，取不到返回空字典）。"""
    chat_id = str(chat_id or "")
    if not chat_id or client is None or not client.enabled:
        return {}
    cached = _MEMBER_CACHE.get(chat_id)
    if cached and (time.time() - cached[0]) < _MEMBER_CACHE_TTL:
        return cached[1]
    getter = getattr(client, "get_chat_members", None)
    if not callable(getter):
        return {}
    try:
        members = getter(chat_id) or {}
    except Exception:  # noqa: BLE001  群成员拉不到不影响回执下发
        return {}
    if members:
        _MEMBER_CACHE[chat_id] = (time.time(), members)
    return members


def parse_at_people(raw: list[str] | None) -> list[tuple[str, str]]:
    """解析 @ 名单：每项形如「牛磊」或「牛磊|要给他说的一句话」。

    支持每行一个 / 逗号分隔；只写姓名 = 只 @ 人、不带额外文案。
    文案里可以再出现 |（只按第一个 | 拆）。
    """
    people: list[tuple[str, str]] = []
    for item in raw or []:
        for part in str(item).replace("\n", ",").split(","):
            name, _, text = str(part).partition("|")
            name, text = name.strip(), text.strip()
            if name:
                people.append((name, text))
    return people


def _lookup_open_ids(
    names: list[str],
    chat_id: str,
    client: FeishuClient | None = None,
    preset: list[str] | None = None,
    label: str = "通知",
) -> dict[str, str]:
    """按姓名查 open_id：返回 {姓名: open_id}，查不到的人不出现在结果里。

    优先用 .env 直接配的 open_id（与姓名一一对应）；没配的按姓名去群成员名单里找
    （带缓存）。某个人找不到就跳过他、只记一条日志，不影响通知正常下发。
    """
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return {}
    preset = [str(i).strip() for i in (preset or [])]
    # 名单全部由 .env 里直接配的 open_id 覆盖时，不必再去拉群成员名单
    if len(preset) >= len(names) and all(
        preset[idx].startswith("ou_") for idx in range(len(names))
    ):
        return {name: preset[idx] for idx, name in enumerate(names)}
    members = _chat_members(client or get_client(), chat_id)
    out: dict[str, str] = {}
    for idx, name in enumerate(names):
        if name in out:
            continue
        direct = preset[idx] if idx < len(preset) else ""
        found = direct if direct.startswith("ou_") else ""
        if not found:
            for open_id, member_name in members.items():
                if str(member_name).strip() == name:
                    found = open_id
                    break
        if not found:
            for open_id, member_name in members.items():
                if name in str(member_name):
                    found = open_id
                    break
        if found:
            out[name] = found
        else:
            logger.warning(
                "%s 要 @ 的「%s」没找到 open_id：确认他在该群里，"
                "或在 .env 用对应的 OPEN_IDS 直接指定",
                label,
                name,
            )
    return out


def open_ids_by_names(
    names: list[str],
    chat_id: str,
    client: FeishuClient | None = None,
    preset: list[str] | None = None,
    label: str = "通知",
) -> list[str]:
    """按姓名查 open_id，支持一次 @ 多个人（返回查到的 open_id 列表）。"""
    return list(_lookup_open_ids(names, chat_id, client, preset, label).values())


def cc_open_ids(chat_id: str, client: FeishuClient | None = None) -> list[str]:
    """退单回执要一起 @ 的固定人员（默认「牛磊」，可配多个）的 open_id。"""
    return open_ids_by_names(
        settings.refund_cc_names,
        chat_id,
        client,
        preset=settings.refund_cc_open_ids,
        label="退单回执",
    )


def receipt_at_open_ids(chat_id: str, client: FeishuClient | None = None) -> list[str]:
    """回执要额外 @ 的人（可多个，姓名逗号分隔）的 open_id。"""
    return open_ids_by_names(
        settings.notify_receipt_at_names,
        chat_id,
        client,
        preset=settings.notify_receipt_at_open_ids,
        label="回执",
    )


def build_cancel_receipt(msg: RawMessage, order: Order, keyword: str = "") -> str:
    """撤单回执：还没发货就撤掉，订单只留档（不参与统计与推单流程）。

    设置页配了模板就按模板渲染（可用 {{ 撤单关键词 }}），没配（或写错）用内置格式。
    """
    text = render_notify(
        settings.notify_cancel_template,
        notify_context(order, msg, 撤单关键词=keyword or "撤单"),
    )
    if text:
        if settings.notify_mode != "direct" and (msg.sender_id or "").startswith("ou_"):
            text = f'<at user_id="{msg.sender_id}"></at> {text}'
        return text

    lines = [f"🚫 已按撤单处理（{keyword or '撤单'}）：该订单还没发货，已置为「已撤单」留档"]
    if order.order_no:
        lines.append(f"订单号：{order.order_no}")
    if order.product_name:
        lines.append(f"产品：{order.product_name}")
    if order.receiver_name or order.receiver_phone:
        lines.append(f"收货：{order.receiver_name} {mask_phone(order.receiver_phone)}")
    lines.append("撤单后不再参与统计与推单流程；如需恢复，请在订单详情里「撤销撤回」。")
    if settings.notify_with_link and order.id:
        base = settings.public_base_url.rstrip("/")
        lines.append(f"详情：{base}/orders/{order.id}")
    text = "\n".join(lines)
    if settings.notify_mode != "direct" and (msg.sender_id or "").startswith("ou_"):
        text = f'<at user_id="{msg.sender_id}"></at> {text}'
    return text


def build_intent_receipt(msg: RawMessage, title: str, note: str = "", hint: str = "") -> str:
    """意图命中但没能自动处理（没匹配到订单 / 拿不准是哪一笔）：回群提示补充信息。

    模板变量：{{ 标题 }} {{ 说明 }} {{ 提示 }}；留空 / 写错用内置格式。
    """
    text = render_notify(
        settings.notify_intent_template,
        {
            "标题": title or "",
            "说明": note or "",
            "提示": hint or "",
            "发送人姓名": getattr(msg, "sender_name", "") or "",
        },
    )
    if text:
        if settings.notify_mode != "direct" and (msg.sender_id or "").startswith("ou_"):
            text = f'<at user_id="{msg.sender_id}"></at> {text}'
        return text

    lines = [f"⚠️ {title}"]
    if note:
        lines.append(note)
    if hint:
        lines += ["", hint]
    text = "\n".join(lines)
    if settings.notify_mode != "direct" and (msg.sender_id or "").startswith("ou_"):
        text = f'<at user_id="{msg.sender_id}"></at> {text}'
    return text


# ------------------------------------------------------------------ 时段判定
def _to_minutes(hhmm: str) -> int:
    hour, _, minute = hhmm.strip().partition(":")
    return int(hour) * 60 + int(minute or 0)


def parse_active_windows(spec: str | None = None) -> list[tuple[int, int]]:
    """解析允许下发的时段，返回分钟区间列表；留空表示全天可发。"""
    text = settings.notify_active_hours if spec is None else spec
    windows: list[tuple[int, int]] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        raw_start, _, raw_end = part.partition("-")
        try:
            start, end = _to_minutes(raw_start), _to_minutes(raw_end)
        except ValueError:
            logger.warning("忽略无效的时段配置: %s", part)
            continue
        if start == end:
            continue
        if start < end:
            windows.append((start, end))
        else:  # 跨天，如 22:00-08:00
            windows.append((start, 24 * 60))
            windows.append((0, end))
    return windows


def local_now() -> datetime:
    """按配置的时区偏移（UTC+N 小时）取「当地现在」。

    库里的时间统一是北京时间（naive），这里不读系统时区，避免容器 TZ 影响判断。
    """
    return utc_now() + timedelta(hours=settings.notify_utc_offset)


def is_active_time(now: datetime | None = None) -> bool:
    """当前是否处于允许下发的时间窗口内。"""
    windows = parse_active_windows()
    if not windows:
        return True
    now = now or local_now()
    current = now.hour * 60 + now.minute
    return any(start <= current < end for start, end in windows)


# ------------------------------------------------------------------ 补发合并
def receipt_kind(msg: RawMessage) -> str:
    """这条消息的回执类型：优先用发回执时记下的 notify_kind，老数据用意图兜底。"""
    kind = str(getattr(msg, "notify_kind", "") or "")
    if kind:
        return kind
    intent = str(getattr(msg, "intent", "") or "")
    return intent if intent in ("refund", "cancel") else "receipt"


def _digest_line(msg: RawMessage, order: Order) -> str:
    parts: list[str] = []
    kind = receipt_kind(msg)
    if kind == "refund":
        parts.append("🔴 退单")
    elif kind == "cancel":
        parts.append("⚪ 撤单")
    if order.order_no and not order.order_no.startswith("AUTO-"):
        parts.append(f"订单号 {order.order_no}")
    if order.product_name:
        parts.append(order.product_name)
    if order.sale_amount:
        parts.append(f"¥{order.sale_amount:.2f}")
    return " · ".join(parts) or "订单已归档"


def build_digest(entries: list[tuple[RawMessage, Order]]) -> str:
    """把多条挂起回执合并成一条摘要。

    模板变量：{{ 条数 }} {{ 明细 }}（每行一条）{{ 更多 }}（超过 20 条时显示还有几条）；
    留空 / 写错用内置格式。
    """
    detail = "\n".join(
        f"{idx}. {_digest_line(msg, order)}"
        for idx, (msg, order) in enumerate(entries[:20], start=1)
    )
    rest = len(entries) - 20
    text = render_notify(
        settings.notify_digest_template,
        {
            "条数": str(len(entries)),
            "明细": detail,
            "更多": str(rest) if rest > 0 else "",
        },
    )
    if text:
        return text

    lines = [f"📦 已收到并处理 {len(entries)} 条订单（静默期合并回执）"]
    for idx, (msg, order) in enumerate(entries[:20], start=1):
        lines.append(f"{idx}. {_digest_line(msg, order)}")
    if len(entries) > 20:
        lines.append(f"… 其余 {len(entries) - 20} 条已一并归档")
    return "\n".join(lines)


def wait_screenshot_seconds() -> int:
    """等订单截图补全订单号的时长（秒），0 = 不等待、立即回执。"""
    try:
        return max(int(settings.notify_wait_screenshot_seconds or 0), 0)
    except (TypeError, ValueError):
        return 0


def waiting_for_screenshot(msg: RawMessage, order: Order | None) -> bool:
    """订单号还停在临时编号（TMP-）：先压住回执，等截图 OCR 补全正式单号。

    等待窗口内截图到达 -> 文本消息会被重新解析，直接用正式订单号回执；
    窗口过后仍没补全 -> 由补发线程发出「不带临时单号」的回执，不会漏回执。
    """
    seconds = wait_screenshot_seconds()
    if seconds <= 0 or order is None or not order.id:
        return False
    if not str(order.order_no or "").startswith("TMP-"):
        return False
    base = msg.msg_time or msg.created_at
    if base is None:
        return False
    return (now() - base).total_seconds() < seconds


def sync_open_ids(client: FeishuClient | None, msg: RawMessage) -> list[str]:
    """通知必须同步到的关键人（默认「牛磊」）：私信要发给他，群里要 @ 他。

    名单在设置页维护（写入 .env 的 REFUND_CC_NAMES），按姓名在群里查 open_id。
    """
    ids: list[str] = []
    chat_id = str(getattr(msg, "chat_id", "") or "")
    try:
        for open_id in cc_open_ids(chat_id, client):
            if open_id and open_id not in ids:
                ids.append(open_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询通知同步人失败 chat=%s: %s", msg.chat_id, exc)
    return ids


def pm_receivers(client: FeishuClient | None, msg: RawMessage) -> list[str]:
    """私信下发时发给谁：消息发送人 + 通知同步人（牛磊等），去重后按序返回。"""
    receivers: list[str] = []
    sender = str(msg.sender_id or "")
    if sender.startswith("ou_"):
        receivers.append(sender)
    for open_id in sync_open_ids(client, msg):
        if open_id not in receivers:
            receivers.append(open_id)
    return receivers


def _at_prefix(open_ids: list[str]) -> str:
    """把若干人拼成 @ 前缀（群里才有效）。"""
    return "".join(f'<at user_id="{open_id}"></at> ' for open_id in open_ids)


def with_mentions(
    client: FeishuClient | None, msg: RawMessage, text: str
) -> str:
    """群里回复：没 @ 过人的通知，统一 @ 发送人 + 通知同步人（牛磊等）。

    已经带 @ 的（退单回执按配置 @ 过、整群日报 @ 所有人）保持原样，不重复加。
    """
    if "<at user_id=" in text:
        return text
    ids: list[str] = []
    sender = str(msg.sender_id or "")
    if sender.startswith("ou_"):
        ids.append(sender)
    for open_id in sync_open_ids(client, msg):
        if open_id not in ids:
            ids.append(open_id)
    return _at_prefix(ids) + text if ids else text


AT_TAG = re.compile(r'\s*<at user_id="[^"]*"></at>')


def _pm_text(text: str) -> str:
    """私信正文：群里是 @ 到人，私信里 @ 标签只是一串乱码，去掉。"""
    return AT_TAG.sub("", text or "").strip()


def _send_one(
    client: FeishuClient,
    msg: RawMessage,
    text: str,
    key: str = "",
    auto_mention: bool = True,
) -> bool:
    """按消息类型各自配的发送方式下发（消息中心里可以按条调），返回是否发出去过。

    key 见 messages_registry：receipt / duplicate / refund / cancel / intent / digest…
    没传就按「订单归档回执」的默认方式走。
    """
    mode = str(settings.notify_mode or "reply").lower()
    if mode == "off":
        return False  # 关闭下发：群里和私信都不发
    private = _pm_text(text)
    if mode == "direct":
        # 只私信、完全不打扰群：发送人 + 通知同步人各一份（这条以全局模式为准）
        receivers = pm_receivers(client, msg)
        if not receivers:
            raise ValueError("缺少发送人 open_id")
        for open_id in receivers:
            client.send_text(open_id, private, receive_id_type="open_id")
        return True

    sent = False
    if message_rules.wants(key, CH_GROUP):
        # 群里回复该消息，@ 发送人 + 通知同步人（文案里没 @ 过的自动补上）
        group_text = with_mentions(client, msg, text) if auto_mention else text
        client.reply_text(msg.message_id, group_text)
        sent = True

    targets: list[str] = []
    sender = str(getattr(msg, "sender_id", "") or "")
    if message_rules.wants(key, CH_PM_SENDER) and sender.startswith("ou_"):
        targets.append(sender)
    if message_rules.wants(key, CH_PM_SYNC):
        for open_id in sync_open_ids(client, msg):
            if open_id not in targets:
                targets.append(open_id)
    for open_id in targets:
        client.send_text(open_id, private, receive_id_type="open_id")
        sent = True
    return sent


def deliver(
    client: FeishuClient, msg: RawMessage, text: str, key: str = "", auto_mention: bool = True
) -> bool:
    """对外统一发送入口：其它模块按消息类型把消息交到这里，由它决定发群里还是私信。

    auto_mention=False 表示这条消息本来就不需要 @ 到人（例如催补退单理由，只是一条群提醒）。
    """
    return _send_one(client, msg, text, key=key, auto_mention=auto_mention)


def rebuild_held_text(
    msg: RawMessage, order: Order, client: FeishuClient | None = None
) -> str:
    """把挂起的回执补发出去时，按**原来的回执类型**重新渲染文案。

    线上实例（2026-09-27 11:36）：群里发了「此单退」，系统正确识别并标记了退单，
    但退单回执因为订单号还是临时编号被挂起，89 秒后补发时被统一按「订单已归档」
    渲染 —— 群里收到的是「✅ 你的订单已收到，系统已经处理订单归档」，
    运营根本看不出这是个退单。所以补发必须区分类型。
    """
    kind = receipt_kind(msg)
    if kind == "refund":
        return build_refund_receipt(msg, order, keyword="", client=client)
    if kind == "cancel":
        return build_cancel_receipt(msg, order)
    if kind == "duplicate":
        return build_duplicate_receipt(msg, order)
    if kind == "intent":
        return build_intent_receipt(
            msg,
            "群消息意图已记录",
            "这条回执此前被挂起，现已补发；如仍需处理请到订单页核对。",
            "",
        )
    return build_receipt(msg, order)


def flush_held_notifications(session=None, client: FeishuClient | None = None) -> dict:
    """把挂起（静默期/触发限流）的回执补发出去。

    按会话（群 / 发送人）分组，同组多条合并成一条摘要，避免恢复后瞬间刷屏。
    """
    out = {"sent": 0, "batches": 0, "waiting": 0, "errors": []}
    if settings.notify_mode == "off" or not is_active_time():
        return out

    owns_session = session is None
    session = session or SessionLocal()
    try:
        client = client or get_client()
        if not client.enabled:
            return out
        rows = session.scalars(
            select(RawMessage)
            .where(RawMessage.notify_status == "held")
            .order_by(RawMessage.msg_time)
        ).all()
        if not rows:
            return out

        groups: dict[str, list[RawMessage]] = {}
        for msg in rows:
            key = msg.chat_id if settings.notify_mode != "direct" else (msg.sender_id or msg.chat_id)
            groups.setdefault(key, []).append(msg)

        ts = now()
        for _key, msgs in groups.items():
            entries: list[tuple[RawMessage, Order]] = []
            for msg in msgs:
                order = session.get(Order, msg.order_id) if msg.order_id else None
                if order is None:
                    msg.notify_status = "skipped"
                    msg.notify_error = "订单不存在，跳过补发"
                    continue
                if receipt_kind(msg) not in ("refund", "cancel") and waiting_for_screenshot(
                    msg, order
                ):
                    # 还在等截图补全订单号：继续压着，下一轮再看（退单 / 撤单不压）
                    out["waiting"] += 1
                    continue
                entries.append((msg, order))
            if not entries:
                session.commit()
                continue
            try:
                if settings.notify_digest:
                    # 单条：按原回执类型渲染（退单 / 撤单 / 重复口径各不相同）；
                    # 多条：合并成一条摘要，退单 / 撤单会在明细里单独标出来
                    text = (
                        rebuild_held_text(entries[0][0], entries[0][1], client=client)
                        if len(entries) == 1
                        else build_digest(entries)
                    )
                    _send_one(client, entries[0][0], text, key="digest")
                    out["batches"] += 1
                else:
                    for msg, order in entries:
                        _send_one(
                            client, msg, rebuild_held_text(msg, order, client=client), key=receipt_kind(msg)
                        )
                for msg, _order in entries:
                    msg.notify_status = "sent"
                    msg.notified_at = ts
                    msg.notify_error = "静默期合并补发" if len(entries) > 1 else "补发"
                out["sent"] += len(entries)
                session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("合并补发失败: %s", exc)
                out["errors"].append(str(exc)[:200])
                session.rollback()
    finally:
        if owns_session:
            session.close()
    return out


# ------------------------------------------------------------------ 物流单号同步
# 机器人 / 应用自己发的消息（id 形如 cli_aa3f9f）统一用这个名字：
# 通讯录里查不到这类 id，不能算「未识别员工」，否则设置页会一直显示缺姓名
APP_SENDER_NAME = "闲鱼订单Agent"


def is_app_sender(sender_id: str | None) -> bool:
    """飞书应用/机器人自己的 id（cli_xxx）：不能 @，也不该写进订单当发送人。"""
    return str(sender_id or "").startswith(("cli_", "app_"))


def sender_display_name(sender_id: str | None, sender_name: str | None = "") -> str:
    """展示用的发送人：机器人固定叫「闲鱼订单Agent」，其余用真实姓名 / 原值。"""
    if is_app_sender(sender_id):
        return APP_SENDER_NAME
    return str(sender_name or "").strip() or str(sender_id or "")


def recipient_open_id(order: Order, msg: RawMessage | None = None) -> str:
    """取要 @ 的员工 open_id：优先来源消息的发送人，其次订单上记录的发送人。

    机器人 / 应用自身发出的消息（回执、通知）不能作为 @ 对象，会继续往订单里找真人。
    """
    for candidate in (
        (msg.sender_id if msg is not None else ""),
        getattr(order, "sender_id", "") or "",
    ):
        value = str(candidate or "")
        if value.startswith("ou_") and not is_app_sender(value):
            return value
    return ""


def sync_tracking_to_group(
    order: Order,
    client: FeishuClient | None = None,
    session=None,
    is_update: bool = False,
) -> tuple[str, str]:
    """把订单的物流单号同步到来源群里，并 @ 同步该订单的员工。

    is_update=True 表示这是「改单号」后再通知一次：群里文案要写明是更新，
    别让人误以为又发了一单（文案在 push.tracking_notice 里按这个开关生成）。

    返回 (status, error)：sent 已同步 / skipped 跳过 / failed 失败。
    只在系统设置里「关闭下发」时跳过；静默时段不拦截（这是运维主动操作）。
    """
    if settings.notify_mode == "off":
        return "skipped", "系统设置里已关闭飞书下发"
    if not (order.tracking_no or "").strip():
        return "skipped", "物流单号为空，未同步"

    client = client or get_client()
    if not client.enabled:
        return "skipped", "未配置飞书应用凭证"

    msg = None
    if order.source_message_id:
        owns = session is None
        session = session or SessionLocal()
        try:
            msg = session.scalar(
                select(RawMessage).where(RawMessage.message_id == order.source_message_id)
            )
        finally:
            if owns:
                session.close()

    from .push import tracking_notice  # 局部导入，避免 push <-> notify 循环依赖

    # 回单信息必须 @ 到同步该订单的员工：群内回复 / 直接发群里都要带上 @
    text = tracking_notice(order, is_update=is_update)
    mention = recipient_open_id(order, msg)
    note = ""
    private = settings.notify_mode == "direct"
    if mention and not private:
        text = f'<at user_id="{mention}"></at> {text}'
    elif not mention and not private:
        # 订单来源不是真人消息（如系统回执 / 截图转发）时拿不到 open_id，提示人工处理
        note = "未 @ 到原发送人（该订单来源不是群成员消息，请手动 @）"
    if not private:
        # 群里同步还要 @ 通知同步人（牛磊等）；私信模式下 @ 无效，改私聊发到本人
        extra = [o for o in sync_open_ids(client, msg) if o != mention]
        if extra:
            text = _at_prefix(extra) + text

    try:
        if private:
            receivers = pm_receivers(client, msg) if msg is not None else []
            if not receivers:
                return "skipped", "私信模式：拿不到接收人 open_id，无法私聊同步"
            for open_id in receivers:
                client.send_text(open_id, text, receive_id_type="open_id")
        elif msg is not None:
            # 按消息中心里「物流单号同步」勾的方式发（默认：群里 @ 到人 + 私信同步给本人）
            if not deliver(client, msg, text, key="tracking"):
                return "skipped", "这条消息在消息中心里被关掉了，未同步"
        elif order.chat_id:
            if not message_rules.wants("tracking", CH_GROUP):
                return "skipped", "已关闭群内下发"
            client.send_text(order.chat_id, text)
        else:
            return "skipped", "订单没有来源消息与群 ID，无处同步"
    except Exception as exc:  # noqa: BLE001
        logger.warning("物流单号群同步失败 order=%s: %s", order.id, exc)
        return "failed", str(exc)[:300]

    order.tracking_synced_at = now()
    return "sent", note


# ------------------------------------------------------------------ 主入口
def _send_receipt(
    msg: RawMessage,
    order: Order,
    client: FeishuClient | None,
    session,
    text: str,
    force: bool = False,
    use_interval: bool = True,
    reason: str = "",
    kind: str = "",
) -> tuple[str, str]:
    """回执下发公共逻辑：时段、最小间隔、限流；text 为已经组装好的文本。

    kind（receipt / refund / cancel / duplicate）会记到消息上：回执被挂起后补发时，
    要按原类型重新渲染，不能被改写成「订单已归档」口径（线上实例踩过）。
    """
    if kind:
        msg.notify_kind = kind
    if settings.notify_mode == "off":
        return "skipped", "已关闭回执下发"
    # 补录历史消息 / 服务中断恢复：太老的消息不再补发回执，避免群里一次性刷屏
    if not force:
        too_old = message_age_minutes(msg)
        if too_old is not None:
            return "skipped", (
                f"消息已过去 {int(too_old)} 分钟（超过 {settings.notify_max_age_minutes} 分钟），"
                "不补发回执"
            )
    # 幂等：同一条消息只回执一次
    if not force and msg.notify_status == "sent":
        return "skipped", "已回执过，跳过重复下发"
    if is_bot_message(msg.sender_type, msg.raw_text):
        return "skipped", "机器人自身消息不下发"

    # 订单号还没补全（订单截图未解析完）：先挂起，等正式单号到了再回执
    # 退单 / 撤单不压着：这两类回执是催人来拦截、来补理由的，压住既耽误处理，
    # 补发时还容易被改写成归档口径（线上实例：退单回执补发成了「订单已收到」）
    if (
        not force
        and kind not in ("refund", "cancel")
        and waiting_for_screenshot(msg, order)
    ):
        return "held", "等待订单截图补全订单号"

    # 静默时段：挂起等待合并补发，或按策略直接丢弃
    # 退单 / 撤单不压：这两类回执是要人**马上**去拦截、去补理由的，
    # 压到静默期结束（比如第二天早上）就耽误事了
    if not force and kind not in ("refund", "cancel") and not is_active_time():
        if settings.notify_quiet_action == "drop":
            return "skipped", "静默期，按策略丢弃不补发"
        return "held", "静默期，已挂起等待合并补发"

    ts = now()
    # 同一订单的最小回执间隔，避免同一订单反复刷屏（退单等关键回执可跳过间隔）
    if (
        use_interval
        and not force
        and session is not None
        and settings.notify_min_interval_seconds > 0
        and order.id
    ):
        last = session.scalar(
            select(RawMessage.notified_at)
            .where(RawMessage.order_id == order.id, RawMessage.notify_status == "sent")
            .order_by(RawMessage.notified_at.desc())
            .limit(1)
        )
        if last and (ts - last).total_seconds() < settings.notify_min_interval_seconds:
            return "skipped", "同订单已在最小间隔内回执过"

    # 每小时下发上限，超出部分挂起到下一个窗口补发
    if not force and session is not None and settings.notify_max_per_hour > 0:
        sent_in_hour = session.scalar(
            select(func.count(RawMessage.id)).where(
                RawMessage.notify_status == "sent",
                RawMessage.notified_at >= ts - timedelta(hours=1),
            )
        ) or 0
        if sent_in_hour >= settings.notify_max_per_hour:
            return "held", "达到每小时下发上限，挂起等待补发"

    if client is None or not client.enabled:
        return "skipped", "未配置飞书凭证"

    try:
        _send_one(client, msg, text, key=kind or "receipt")
        return "sent", reason or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("回执通知失败 %s: %s", msg.message_id, exc)
        return "failed", str(exc)[:300]


def notify_processed(
    msg: RawMessage,
    order: Order,
    client: FeishuClient | None = None,
    session=None,
    force: bool = False,
) -> tuple[str, str]:
    """发送回执，返回 (notify_status, error)。不抛异常，避免影响入库主流程。

    notify_status：sent 已发 / held 挂起等待补发 / skipped 跳过 / failed 失败
    force=True 为管理员手动补发：忽略时段、间隔与限流（关闭下发仍然生效）。
    """
    return _send_receipt(
        msg,
        order,
        client,
        session,
        build_receipt(msg, order),
        force=force,
        kind="receipt",
    )


def notify_duplicate(
    msg: RawMessage,
    order: Order,
    client: FeishuClient | None = None,
    session=None,
) -> tuple[str, str]:
    """群里重复发了同一笔订单（内容无变化）：只提醒「已收到」，不重复处理。"""
    if not settings.dedupe_notify:
        return "skipped", "未开启重复提醒"
    # 不受最小回执间隔限制：用户重复发了，就该明确告诉他「已收到过」
    return _send_receipt(
        msg,
        order,
        client,
        session,
        build_duplicate_receipt(msg, order),
        use_interval=False,
        reason="重复订单提醒",
        kind="duplicate",
    )


def notify_refund(
    msg: RawMessage,
    order: Order,
    client: FeishuClient | None = None,
    session=None,
    keyword: str = "",
) -> tuple[str, str]:
    """退单回执：@ 发送人确认收到 + @ 供应链对接人告知处理口径（不受最小回执间隔限制）。"""
    if not settings.refund_notify:
        return "skipped", "未开启退单提醒"
    return _send_receipt(
        msg,
        order,
        client,
        session,
        build_refund_receipt(msg, order, keyword, client=client),
        use_interval=False,
        reason="退单提醒",
        kind="refund",
    )


def notify_cancel(
    msg: RawMessage,
    order: Order,
    client: FeishuClient | None = None,
    session=None,
    keyword: str = "",
) -> tuple[str, str]:
    """撤单回执：已按撤单处理（只留档），告知发送人处理结果。"""
    if not settings.cancel_notify:
        return "skipped", "未开启撤单提醒"
    return _send_receipt(
        msg,
        order,
        client,
        session,
        build_cancel_receipt(msg, order, keyword),
        use_interval=False,
        reason="撤单提醒",
        kind="cancel",
    )


def notify_text(
    msg: RawMessage,
    text: str,
    client: FeishuClient | None = None,
    session=None,
    throttle: bool = True,
) -> tuple[str, str]:
    """意图回执（可能没有订单可挂）：只做基础校验后回复。

    throttle=True 时按最小回执间隔限流（避免群里反复发「要退」被提示刷屏）；
    「请求未受理」这类提示传 False，确保运营一定看得到。
    """
    if settings.notify_mode == "off":
        return "skipped", "已关闭回执下发"
    # 记下回执类型：静默期挂起后补发时按原类型渲染（意图提示用通用口径）
    msg.notify_kind = msg.notify_kind or "intent"
    if msg.notify_status == "sent":
        return "skipped", "已回执过，跳过重复下发"
    if is_bot_message(msg.sender_type, msg.raw_text):
        return "skipped", "机器人自身消息不下发"
    # 同回执：补录的历史消息不往群里补提示
    too_old = message_age_minutes(msg)
    if too_old is not None:
        return "skipped", (
            f"消息已过去 {int(too_old)} 分钟（超过 {settings.notify_max_age_minutes} 分钟），不补发"
        )
    if not is_active_time():
        if settings.notify_quiet_action == "drop":
            return "skipped", "静默期，按策略丢弃不补发"
        return "held", "静默期，已挂起等待合并补发"
    if throttle and session is not None and settings.notify_min_interval_seconds > 0 and msg.sender_id:
        last = session.scalar(
            select(RawMessage.notified_at)
            .where(
                RawMessage.sender_id == msg.sender_id,
                RawMessage.notify_status == "sent",
                RawMessage.id != msg.id,
            )
            .order_by(RawMessage.notified_at.desc())
            .limit(1)
        )
        if last and (now() - last).total_seconds() < settings.notify_min_interval_seconds:
            return "skipped", "同一发送人已在最小间隔内回执过"
    if client is None or not client.enabled:
        return "skipped", "未配置飞书凭证"
    try:
        _send_one(client, msg, text, key="intent")
        return "sent", ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("回执通知失败 %s: %s", msg.message_id, exc)
        return "failed", str(exc)[:300]
