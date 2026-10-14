"""逆向退单：群里同步「此单退 / 退单」时的匹配与拦截处理。

业务口径（2026-09）：
  用户把退单信息发到飞书群（第一行带「此单退」「退单」等关键词，其余字段和正常订单一致）：
  - 按 手机号 / 收货人 / 地址 / 产品 匹配原订单，匹配到就给该订单打上「已退单，待拦截」；
  - **匹配不到不新建订单**（退单是对存量订单的处理，凭空建单只会多一笔要清理的脏数据）：
    消息留痕在飞书消息页待人工核对，并回群提示带上订单号 / 手机号再发一次；
  - 列表、详情页最醒目红色标记，并语音播报「有退单订单请及时处理拦截！」；
  - 群里 @ 发送人回一条「已识别退单，请及时处理拦截」。

意图识别（关键词、一句人话的判定）统一在 intent.py，关键词可在 .env 的
REFUND_KEYWORDS / CANCEL_KEYWORDS 里配置（逗号分隔）。
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..models import (
    ORDER_STATE_CLOSED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_REFUND_SYNCED,
    REFUND_ORDER_STATES,
    REFUND_STATUS_CLOSED,
    REFUND_STATUS_INTERCEPTED,
    REFUND_STATUS_LABELS,
    REFUND_STATUS_NOT_PUSHED,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_RECEIVED,
    REFUND_STATUS_REFUNDED,
    REFUND_STATUS_SHIPPED,
    Order,
    RawMessage,
    is_refund_order,
)
from ..timeutil import now
from . import dedupe
from . import voice as voice_service
from . import order_state as order_state_service

logger = logging.getLogger(__name__)

# 判定为同一订单的最低匹配分（手机号命中=5，产品 3 + 收货人 2 = 5）
MIN_SCORE = 5


def detect(text: str) -> str:
    """是否为退单消息：命中退单关键词则返回命中的词（""=不是退单）。

    模板消息只看第一行（前 20 字）：正文里出现「七天无理由退货」之类不算退单。
    判定统一走 intent 模块（.env 的 REFUND_KEYWORDS / CANCEL_KEYWORDS + 内置词库）。
    """
    from . import intent as intent_service  # 局部导入，避免 refund <-> intent 互相 import

    result = intent_service.classify(text, template=True)
    if result.kind == intent_service.INTENT_REFUND:
        return result.keyword
    return ""


# 群里写退单理由常用的字段名（按这个顺序找，找到第一个非空值就是理由）
REASON_FIELDS = ("退单理由", "退款理由", "退单原因", "退款原因", "理由", "原因")


def extract_reason(text: str, keyword: str = "") -> str:
    """从退单消息里提取**退单理由**，取不到返回空（空 = 需要提醒补充）。

    优先级：
      1 显式的字段行：「退单理由：客户拍错了」；
      2 命中关键词那一行里关键词后面的话：「此单退，客户不想要了」；
      3 都没有 -> 空（调用方据此回群提醒补充，运营也能手工维护）。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        for field in REASON_FIELDS:
            for sep in ("：", ":"):
                prefix = field + sep
                if line.startswith(prefix):
                    value = line[len(prefix):].strip()
                    if value:
                        return value[:200]
    if keyword:
        for line in raw.splitlines():
            if keyword in line:
                tail = line.split(keyword, 1)[1].strip(" ,，。.：:")
                if len(tail) >= 2:
                    return tail[:200]
    return ""


def match_original(
    session,
    msg,
    fields: dict,
    platform: str = "xianyu",
    days: int | None = None,
    exclude_id: int | None = None,
) -> Order | None:
    """按退单消息里的信息找原订单：订单号 > 手机号 / 收货人 / 地址 / 产品（综合打分）。

    打分口径统一走 intent.find_order：手机号一致 5 分、地址完全一致 3 分、产品 3 分、
    收货人 2 分，够 5 分才算匹配上（同一收货人 + 同一地址也够分，产品名写法不同不影响）。
    只认「很确定 / 比较确定」的匹配，且不接受「同一发送人兜底」这种弱证据 ——
    匹配不到就返回 None，调用方据此**不新建订单**、留人工核对。
    """
    from . import intent as intent_service  # 局部导入，避免 refund <-> intent 互相 import

    match = intent_service.find_order(session, msg, fields, days=days)
    if match.order is None or not match.confident:
        return None
    if exclude_id and match.order.id == exclude_id:
        return None
    # 兜底命中的「同一发送人唯一在途订单」不算证据：消息里的收货信息都没对上
    if match.hits and all(h == "同一发送人的在途订单" for h in match.hits):
        logger.info(
            "退单匹配只靠同一发送人兜底（订单 %s）：不认，留人工核对", match.order.order_no
        )
        return None
    return match.order


def initial_refund_status(order: Order) -> str:
    """刚识别到退单时，处理状态按「推到哪一步了」定（口径同 refund_path）。

    **还没推给供应商的单不存在「拦截」**：供应商那边压根没下过单，自然没有包裹要拦，
    直接不推、把钱退给客户就完了。以前一律记成「待拦截」，页面会催着运营去拦截一个
    根本不存在的包裹（2026-10-02 修：订单 3317081124078045869 就是这么显示的）。
    """
    path = refund_path(order)
    if path == PATH_NOT_PUSHED:
        return REFUND_STATUS_NOT_PUSHED
    if path == PATH_RECEIVED:
        # 客户已经收到货：包裹拦不住了，走收货退单（等客户寄回）
        return REFUND_STATUS_RECEIVED
    # ② 已推单未发货 -> 联系供应商撤回发货；③ 已发货未收货 -> 物流拦截：都还要拦
    return REFUND_STATUS_PENDING


def mark_refund(
    session,
    order: Order,
    msg: RawMessage | None = None,
    keyword: str = "",
    reason: str = "",
    actor: str = "",
    commit: bool = True,
) -> tuple[Order, bool]:
    """给订单打上退单标记，返回 (订单, 是否受理)。

    逆向流程第一步：订单落到「**买家申请退单**」，并把退单理由记下来；
    之后由运营同步供应商（「已同步供应商退单」）、退款完成才落到「已退款」，
    最后走收尾（已付款的标记供应商已退款 / 未付款的不付款）落到「关闭完成」。

    **已发货也受理**（2026-09-29 口径）：
      · 已发货、用户还没收到货 -> 同步物流拦截（推单状态写明「同步物流拦截」）；
      · 用户已收到货          -> 收货退单，运营维护退回的快递单号。
    只有已撤单（只留档）的订单不受理。
    退单受理时按**成交价**记退款金额，运营可按实际退款额改。
    """
    ts = now()
    reason = (reason or "").strip()
    if not reason and msg is not None:
        reason = extract_reason(msg.raw_text, keyword)
    if reason:
        order.refund_reason = reason
    first_time = not (order.refund_status or "").strip()
    accepted = True
    # 处理状态按「推到哪一步了」定（见 initial_refund_status）；已经记过状态的（含人工改的）不动
    if first_time:
        order.refund_status = initial_refund_status(order)
    # 退款金额：默认按成交价退（运营可在详情页按实际退款额改）；
    # 没填成交价的老数据退回售价 —— 否则退款金额是 0，资金页那条退款流水就建不出来
    if not (order.refund_amount or 0):
        # 符号口径（2026-10-03）：退给客户的钱存负值（-abs 保证重复执行不会翻符号）
        order.refund_amount = -abs(float(order.deal_amount or order.sale_amount or 0))
    order.refund_at = order.refund_at or ts
    if keyword:
        order.refund_keyword = keyword
    if msg is not None:
        order.refund_source_message_id = msg.message_id
        if msg.msg_time is not None and order.msg_time is None:
            order.msg_time = msg.msg_time
        if not order.chat_id:
            order.chat_id = msg.chat_id
    # 订单状态：买家申请退单（等运营同步供应商）；已发货的同样受理（拦截 / 收货退单）
    order_state_service.set_state(
        session, order, order_state_service.ORDER_STATE_REFUND_REQUEST, commit=False
    )
    if not (order.refund_reason or "").strip():
        # 群里没写为什么退：挂「待确认」，页面提示 + 回群催一句，运营也能手工补
        order.needs_review = True
        order.review_reason = (
            "缺退单理由：请在群里补充（回复「退单理由：xxx」），运营也可在订单详情手工维护"
        )
    order.updated_at = ts
    # 资金结算：退单不再结算（流水作废）
    try:
        from . import finance as finance_service

        finance_service.sync_order(session, order)
    except Exception as exc:  # noqa: BLE001  结算流水失败不影响退单处理
        logger.warning("退单后同步结算流水失败 %s: %s", order.order_no, exc)
    if first_time:
        # 订单处理日志：首次识别到退单留一条（重复同步只更新来源，不刷屏）
        from . import order_log as order_log_service  # 局部导入，避免循环依赖
        from . import push as push_service

        order_log_service.log(
            session,
            order,
            order_log_service.ACTION_REFUND_DETECT,
            detail=(
                f"群里同步退单（关键词：{keyword or order.refund_keyword or '退单'}）："
                f"{order.refund_status_label}"
                f"；推单状态：{push_service.refund_push_status(order)}"
                f"；退款金额（按成交价）：{abs(order.refund_amount or 0):.2f}"
                + (
                    f"；退单理由：{order.refund_reason}"
                    if (order.refund_reason or "").strip()
                    else "；没填退单理由，已提醒补充"
                )
            ),
            commit=False,
        )
    if commit:
        session.commit()
    return order, accepted


def ask_reason(
    session,
    order: Order,
    msg: RawMessage | None = None,
    client=None,
    commit: bool = True,
) -> bool:
    """退单消息里没写理由：回群里 @ 一句让发送人补充（同一订单只提醒一次）。"""
    if msg is None or client is None or not getattr(client, "enabled", False):
        return False
    if order.refund_reason_asked_at is not None:
        return False
    text = (
        f"收到退单（订单 {order.order_no}），但没看到退单理由。\n"
        "请补充退单理由，例如回复：「退单理由：客户拍错了」。\n"
        "没填理由的退单会挂待确认，运营也可以在后台补。"
    )
    try:
        # 按消息中心里「催补充退单理由」勾的方式发（默认只在群里回一句，不 @ 人）
        from .notify import deliver  # 局部导入，避免 refund <-> notify 循环依赖

        deliver(client, msg, text, key="refund_reason", auto_mention=False)
    except Exception as exc:  # noqa: BLE001  提醒失败不阻断归档
        logger.warning("提醒补充退单理由失败 %s: %s", order.order_no, exc)
        return False
    order.refund_reason_asked_at = now()
    logger.info("订单 %s 缺退单理由：已回群提醒补充", order.order_no)
    if commit:
        session.commit()
    return True


REGISTER_KEYWORD = "退单登记"


def register_return(
    session,
    msg,
    result,
    client=None,
    *,
    commit: bool = True,
) -> tuple[Order | None, list[str], str]:
    """处理一条**退单登记**消息（2026-10-06 口径）。

    群里专门的退单模板，写的不是新订单而是给已有订单补退单信息：

        原单号：YT1134546055340
        退回单号：73729433121621
        买家姓名电话地址：黄仕宏 17396748268 …
        产品名称：倍思Q2 …
        备注：无3c。顾客承担运费退回

    做的事：按「原单号」找到原订单 -> 退回单号写成订单的退单编号（return_tracking_no）
    -> 备注**累加**（历史备注不动）-> 顺带把这单标记退单（下一步走收件 / 退款）；
    跟 match_original 一样，匹配不上**不新建订单**，回群让对方补信息。

    返回 (订单 | None, 本次写进去的内容, 处理说明)。
    """
    from . import intent as intent_service  # 局部导入，避免 refund <-> intent 互相 import
    from . import order_note as order_note_service

    fields = dict(getattr(result, "fields", {}) or {})

    # 「买家姓名电话地址：黄仕宏 …」这行里 receiver_name 会把标签的标点一并吃进来
    # （取到「电话地址：黄仕宏」）：打分前先扔掉这种明显不成样子的值，别把别的单认错。
    scoring = dict(fields)
    maybe_name = str(scoring.get("receiver_name") or "").strip()
    if "：" in maybe_name or ":" in maybe_name:
        scoring.pop("receiver_name", None)

    original_no = str(fields.get("original_order_no") or "").strip()
    return_no = str(fields.get("return_no") or "").strip()
    note = str(fields.get("note") or "").strip()

    # 1) 找订单：原单号（订单号 -> 物流单号）> 手机号 / 产品 / 地址打分
    order: Order | None = None
    if original_no:
        order = intent_service.find_by_order_no(session, original_no)
        if order is None:
            # 群里常把闲鱼订单号写成 YT… / SF… 这种像快递号的样子
            order = intent_service.find_by_tracking_no(session, original_no)
    if order is None:
        scoring["order_no"] = original_no or str(fields.get("order_no") or "").strip()
        match = intent_service.find_order(session, msg, scoring)
        if match.order is not None and match.confident:
            hits = match.hits or []
            # 「同一发送人的在途订单」不做证据：收货信息一条都没对上
            if not (hits and all(h == "同一发送人的在途订单" for h in hits)):
                order = match.order

    if order is None:
        logger.info("退单登记未匹配到订单：%s（消息 %s）", original_no or return_no, msg.message_id)
        return None, [], (
            "这条是退单登记（"
            + (f"原单号 {original_no}" if original_no else f"退回单号 {return_no or '未提供'}")
            + "），但没匹配到订单：没有新建订单，请在群里带上订单号 / 收货人手机号再发一次，"
            "或到订单详情页手工维护退单编号与备注"
        )

    actor = str(getattr(msg, "sender_name", "") or "").strip()
    changes: list[str] = []

    # 2) 退回单号 -> 退单编号（已维护过的返回「没有变化」，不算写进去）
    if return_no:
        ok, why = set_return_tracking_no(session, order, return_no, actor=actor, commit=False)
        if ok:
            changes.append(
                f"退单编号 {return_no}" if "没有变化" not in why else f"退单编号没变（{return_no}）"
            )
        else:
            changes.append(why)

    # 3) 备注：多次备注累加，不覆盖历史
    if note:
        ok, why = order_note_service.append_note(
            session, order, note, actor=actor, source="群消息", commit=False
        )
        changes.append("备注已累计" if ok else why)

    # 4) 还没进退单流程的顺手标记退单：有退回单号说明客户已经在退了
    if not order.is_refund:
        try:
            mark_refund(session, order, msg, keyword=REGISTER_KEYWORD, reason=note, commit=False)
            changes.append("已标记退单")
            voice_service.refund_event(session, order)
        except Exception as exc:  # noqa: BLE001  播报失败不影响登记
            logger.warning("退单登记标记退单失败 %s: %s", order.order_no, exc)

    order.updated_at = now()
    msg.order_id = order.id
    msg.kind = "template"
    msg.parse_status = "parsed"
    if commit:
        session.commit()

    summary = "、".join(changes) or "退单编号与备注都已经是最新，没有变化"
    logger.info("退单登记 %s：%s", order.order_no, summary)
    return order, changes, f"已登记到订单 {order.order_no}（{summary}）"


def handle(
    session,
    msg: RawMessage,
    result,
    client,
    keyword: str,
) -> tuple[Order, bool, str]:
    """处理一条退单消息，返回 (订单 | None, 是否新建, 处理说明)。

    匹配到原订单 -> 给原订单打退单标记；**匹配不到就返回 None，不新建订单** ——
    退单是要对存量订单做处理，凭空建一笔只会多一笔要人工清理的脏数据，
    消息留痕在飞书消息页（可人工标「其实是退单」或带订单号再发一次）。
    """
    fields = dict(getattr(result, "fields", {}) or {})
    original = match_original(
        session, msg, fields, platform=getattr(result, "platform", "xianyu")
    )

    if original is not None:
        changes = dedupe.diff_fields(original, fields)
        changed = dedupe.apply_changes(original, changes, fields)
        if msg.raw_text:
            original.raw_text = msg.raw_text
        if not original.fingerprint:
            original.fingerprint = dedupe.order_fingerprint(original)
        _, accepted = mark_refund(session, original, msg, keyword)
        if accepted and not (original.refund_reason or "").strip():
            # 没写为什么退：回群里催一句，避免运营来回问
            ask_reason(session, original, msg, client)
        msg.order_id = original.id
        msg.kind = "template"
        msg.parse_status = "parsed"
        session.commit()
        order, is_new = original, False
        note = (
            "已匹配到原订单并标记退单"
            if accepted
            else "已匹配到原订单，但该单已发货：退单请求未受理，请让用户收到货后再退"
        )
        if changed:
            note += "（信息变更：" + "、".join(changed) + "）"
    else:
        # 没匹配到原订单：**不新建订单**（退单是对存量订单的处理，凭空建单只会留脏数据）
        order, is_new = None, False
        note = (
            "识别到退单（关键词：" + (keyword or "退单") + "），但按手机号 / 收货人 / 地址 / 产品名"
            "都没匹配到原订单：没有新建订单，请在飞书消息页人工核对（可标「其实是退单」），"
            "或在群里带上订单号 / 手机号再发一次"
        )
        session.commit()

    if order is not None:
        try:
            voice_service.refund_event(session, order)
        except Exception as exc:  # noqa: BLE001  播报失败不影响归档
            logger.warning("退单语音播报失败 %s: %s", order.order_no, exc)
        logger.info("退单处理 %s：%s（关键词：%s）", order.order_no, note, keyword)
    else:
        logger.info("退单未匹配到原订单：%s（关键词：%s，消息 %s）", note, keyword, msg.message_id)
    return order, is_new, note


# —— 运营手工标记（订单详情页「标记退单」「标记拦截」两个按钮）——
# 标记退单：用户收到货之后退回（退货退款）；标记拦截：供应商已发货、用户退款但货还没收到，
# 需要把快递拦下来退回。两者资金口径一致（这单不再结算），只是处理状态不同。
MARK_REFUNDED = "refunded"
MARK_INTERCEPTED = "intercepted"
MANUAL_KEYWORDS = {MARK_REFUNDED: "手工标记退单", MARK_INTERCEPTED: "手工标记拦截"}
MANUAL_ACTIONS = {MARK_REFUNDED: "标记退单", MARK_INTERCEPTED: "标记拦截"}


def mark_manual(
    session,
    order: Order,
    kind: str,
    actor: str = "",
    note: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """运营手工标记退单 / 拦截，返回 (是否成功, 说明)。

    · 标记退单（refunded）：用户**收到货后**退回，走退货退款流程；
    · 标记拦截（intercepted）：供应商**已发货、用户退款但货还没收到**，把快递拦下退回。
    两者都会把订单落到「已退单」并作废结算流水（这单不再跟供应商结算）；
    已撤单的订单不允许再标记，重复标记是幂等的。
    """
    from . import finance as finance_service  # 局部导入，避免循环依赖
    from . import order_log as order_log_service

    if kind not in MANUAL_KEYWORDS:
        return False, "不支持的标记类型"
    if order.is_recalled:
        return False, "该订单已撤单（只留档、不参与流程），无需再标记退单 / 拦截"

    ts = now()
    action = MANUAL_ACTIONS[kind]
    target_label = REFUND_STATUS_LABELS.get(kind, action)
    if order.refund_status == kind and order.order_state == order_state_service.ORDER_STATE_REFUNDED:
        return True, f"该订单已经是「{target_label}」，无需重复标记"

    # 状态机不允许时（例如已撤单）要把改到一半的字段还原，避免留下半成品
    fields = (
        "refund_status", "refund_at", "refund_keyword", "refund_by",
        "refund_handled_at", "order_status", "refund_note", "order_state", "updated_at",
    )
    snapshot = {name: getattr(order, name) for name in fields}
    before = REFUND_STATUS_LABELS.get((order.refund_status or "").strip(), "非退单")

    order.refund_status = kind
    order.refund_at = order.refund_at or ts
    # 退款金额：按成交价记（运营可在详情页按实际退款额改）
    if not (order.refund_amount or 0):
        # 同上：存负值（钱出去）
        order.refund_amount = -abs(float(order.deal_amount or 0))
    if not order.refund_keyword:
        order.refund_keyword = MANUAL_KEYWORDS[kind]
    if actor:
        order.refund_by = actor
    order.refund_handled_at = ts
    if note:
        order.refund_note = note
    order.updated_at = ts

    ok, reason = order_state_service.set_state(
        session, order, order_state_service.ORDER_STATE_REFUNDED, actor=actor, commit=False
    )
    if not ok:
        for name, value in snapshot.items():
            setattr(order, name, value)
        return False, reason

    # 资金：退单 / 拦截的订单不再结算给供应商（流水置为已作废）
    finance_service.sync_order(session, order)
    order_log_service.log(
        session,
        order,
        action,
        detail=(
            f"退单处理：{before} → {target_label}" if before != target_label else target_label
        )
        + (f"；备注：{note}" if note else ""),
        operator=actor,
    )
    if commit:
        session.commit()
    logger.info("订单 %s 手工%s（处理人 %s）", order.order_no, action, actor or "-")
    return True, f"已标记为「{action}」：该单不再结算给供应商"


def intercept_hint(order: Order) -> str:
    """退单该干什么：按「推到哪一步了」给运营一句话指引。"""
    if order.is_received:
        return (
            "用户已收到货：请让用户把货寄回，收到退回件后把**退单物流单号**维护进来，"
            "再走退款与关闭完成"
        )
    if order.tracking_no:
        return (
            f"该订单已发货（物流单号 {order.tracking_no}）、用户还没收到货："
            "请立即联系供应商 / 快递做**物流拦截**，把包裹拦下来退回"
        )
    if order.supplier_status in ("pushed", "done") or order.pushed_at is not None:
        return "该订单已推给供应商、还没出物流单号：请立即联系供应商**撤回发货**，不要发货"
    return "该订单还没推给供应商：直接不推，走退款与关闭完成即可"


def set_refund_amount(
    session,
    order: Order,
    amount,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """维护退款金额（默认按成交价，运营可按实际退款额改）。"""
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        return False, "退款金额请填数字"
    if value < 0:
        return False, "退款金额不能为负"
    # 符号口径（2026-10-03）：入参是「要退多少钱」的正数，订单上存它的负值
    before = float(order.refund_amount or 0)
    if abs(abs(before) - value) < 1e-6:
        return True, "退款金额没有变化"
    order.refund_amount = -abs(value)
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_REFUND_AMOUNT,
        detail=f"退款金额：{abs(before):.2f} → {value:.2f}（成交价 {float(order.deal_amount or 0):.2f}）",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"退款金额已改为 {value:.2f}"


def set_return_tracking_no(
    session,
    order: Order,
    tracking_no: str,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """维护**退单物流单号**：用户已收到货、把货寄回来时的退回快递单号。

    只有「已收货退单」这一步才用得上（推单状态 = 用户收货退单，同步退单物流单号）。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    value = (tracking_no or "").strip()
    if value == (order.return_tracking_no or "").strip():
        return True, "退单物流单号没有变化"
    before = (order.return_tracking_no or "").strip() or "（空）"
    order.return_tracking_no = value
    # 填了退回单号说明用户确实收到货了：顺手把「已收货」补上，口径不用再点一次
    if value and not order.is_received:
        order.is_received = True
        order.received_at = order.received_at or now()
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_RETURN_TRACKING,
        detail=f"退单物流单号：{before} → {value or '（清空）'}",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"退单物流单号已保存：{value or '（已清空）'}"


def set_received(
    session,
    order: Order,
    received: bool,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """标记「用户已收到货」：区分退单该拦截快递（未收货）还是该等退回件（已收货）。"""
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    if bool(order.is_received) == bool(received):
        return True, f"该订单已经是「{'已收货' if received else '未收货'}」"
    order.is_received = bool(received)
    order.received_at = now() if received else None
    order.updated_at = now()
    label = "已收货" if received else "未收货"
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_RECEIVED,
        detail=f"是否已收货 → {label}（退单口径：{'收货退单，维护退单物流单号' if received else '同步物流拦截'}）",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"已标记为「{label}」"


def mark_supplier_refunded(
    session,
    order: Order,
    actor: str = "",
    note: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """运营线下跟供应商对完账后，标记「供应商已退款」 -> 订单自动落到「关闭完成」。

    只有**已经付款给供应商**（transfer_status=paid）的退单才需要这一步：
    钱付过去了，得让供应商退回来，核对完在这里标记，订单自动关闭完成。
    没付款的单不用等供应商退款（也不许再付款），直接点「关闭完成」。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    if not order.is_refund:
        return False, "该订单不是退单，无需标记供应商退款"
    if (order.transfer_status or "") != "paid":
        return (
            False,
            "该订单还没付款给供应商：不用等供应商退款（也不要再付款），直接点「关闭完成」即可",
        )
    if order.supplier_refunded == "paid":
        return True, "该订单供应商已退款（无需重复标记）"

    order.supplier_refunded = "paid"
    order.supplier_refunded_at = now()
    if actor:
        order.refund_by = actor
    order.refund_handled_at = now()
    if note:
        order.refund_note = (f"{order.refund_note}\n{note}" if order.refund_note else note)[:2000]
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_SUPPLIER_REFUNDED,
        detail="供应商已退款（线下核对完成）" + (f"；备注：{note}" if note else ""),
        operator=actor,
        commit=False,
    )
    # 供应商钱退回来了 -> 这单收尾：自动落到「关闭完成」
    ok, reason = order_state_service.set_state(
        session, order, order_state_service.ORDER_STATE_CLOSED, actor=actor, commit=False
    )
    if not ok:
        session.rollback()
        return False, reason
    # 关完了就把处理状态落到终态，别再挂着「待拦截」
    if (order.refund_status or "").strip():
        order.refund_status = REFUND_STATUS_CLOSED
    if commit:
        session.commit()
    return True, "供应商已退款：订单已自动关闭完成"


def close_refund_order(
    session,
    order: Order,
    actor: str = "",
    note: str = "",
    force: bool = False,
    commit: bool = True,
) -> tuple[bool, str]:
    """退单收尾：订单落到「关闭完成」。

    按当前退单路径的**必需步骤**卡一道：该退给客户的钱退了、该追的供应商退款追了、
    该寄回的货有着落了，才允许关闭。缺哪一步就提示缺哪一步（见 missing_steps）。

    线下已经处理完、只是系统里没记的（老数据、微信直接退的），走 `force=True`
    的「线下已退，强制关闭」：必须写明原因，留痕里会记清楚是强制关闭、缺的是哪几步。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    if not (order.is_refund or order.is_recalled):
        return False, "该订单是正常订单：不用关闭（要留档就点「标记撤回」）"
    if order.order_state == order_state_service.ORDER_STATE_CLOSED:
        return True, "该订单已经是「关闭完成」"

    blocks = blocking_reasons(order)
    if blocks:
        if not force:
            return (
                False,
                "还不能关闭："
                + "；".join(blocks)
                + "。确实都在线下处理完了的，用「线下已退，强制关闭」并写明原因",
            )
        if len((note or "").strip()) < 4:
            return False, "强制关闭必须写明原因（如：已微信退款给客户，2026-09-30，单号 xxx）"

    ok, reason = order_state_service.set_state(
        session, order, order_state_service.ORDER_STATE_CLOSED, actor=actor, commit=False
    )
    if not ok:
        return False, reason
    if note:
        order.refund_note = (f"{order.refund_note}\n{note}" if order.refund_note else note)[:2000]
    if actor:
        order.refund_by = actor
    # 处理状态一并落到终态：以前关完了还挂着「待拦截」，页面显示「已退单 · 待拦截」自相矛盾
    if (order.refund_status or "").strip():
        order.refund_status = REFUND_STATUS_CLOSED
    order.refund_handled_at = now()
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_CLOSE,
        detail=(
            "退单收尾：关闭完成（"
            + (
                "供应商已退款，钱已退回"
                if (order.transfer_status or "") == "paid"
                else "未付款给供应商，不用付款也不用追退款"
            )
            + "）"
            + (f"｜**强制关闭**，未满足：{'；'.join(blocks)}" if blocks else "")
        )
        + (f"；备注：{note}" if note else ""),
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return (
        True,
        ("已强制关闭完成（留痕已记明原因）：" if blocks else "已关闭完成：")
        + "该订单不再参与统计、结算与后续流程",
    )


# —— 退单主路径（2026-10 口径）——
# 退单只有三条主路径（已收货是第三条的后半段），路径不存库、按订单推到哪一步派生；
# 进度也不再靠 refund_status 一个枚举猜，而是看「这几件事做完了没」。
# 这样「已拦截还显示已退款」「货没寄回也能关」这类对不上的情况就不会再出现。
PATH_NOT_PUSHED = "not_pushed"
PATH_PUSHED = "pushed"
PATH_SHIPPED = "shipped"
PATH_RECEIVED = "received"

PATH_LABELS = {
    PATH_NOT_PUSHED: "① 还没推给供应商 · 直接不推",
    PATH_PUSHED: "② 已推单未发货 · 撤回发货",
    PATH_SHIPPED: "③ 已发货未收货 · 物流拦截",
    PATH_RECEIVED: "④ 已收货 · 等客户寄回",
}

PATH_ACTION = {
    PATH_NOT_PUSHED: "不用推给供应商，直接退款给客户",
    PATH_PUSHED: "立刻联系供应商撤回发货，别发出去",
    PATH_SHIPPED: "立刻联系供应商 / 快递做物流拦截，把包裹拦下退回",
    PATH_RECEIVED: "让客户把货寄回，收到后把退回快递单号填进来",
}

# 各路径下「已叫停」这一步的说法：不推 / 撤回发货 / 物流拦截
PATH_STOP_LABEL = {
    PATH_NOT_PUSHED: "不推给供应商",
    PATH_PUSHED: "已撤回发货",
    PATH_SHIPPED: "已物流拦截",
    PATH_RECEIVED: "已物流拦截 / 客户已寄回",
}

# 各路径默认的叫停动作（运营不指定时按路径自动选）
PATH_STOP_ACTION = {
    PATH_NOT_PUSHED: "no_push",
    PATH_PUSHED: "withdraw",
    PATH_SHIPPED: "intercept",
    PATH_RECEIVED: "intercept",
}

STOP_ACTION_LABELS = {
    "no_push": "不推给供应商",
    "withdraw": "已撤回发货",
    "intercept": "已物流拦截",
}

# 物流轨迹状态：接入物流查询后由接口回写（见 apply_tracking_status）
TRACKING_STATUS_LABELS = {
    "": "未查询",
    # 正向（2026-10-13 定稿的六态）
    "pending_pickup": "待揽收",
    "accepted": "已揽收",
    "transit": "运输中",
    "delivering": "派送中",
    "waiting_pickup": "待取件",
    "signed": "已签收",
    # 异常分支
    "delivery_failed": "派送失败",
    "returning": "退回中",
    "returned": "已退回",
    "intercepted": "拦截成功",
    "failed": "异常",
}

# 轨迹状态 -> 能自动确认的事实（只补空缺，人工标记永远优先）
AUTO_FACTS_FROM_TRACKING = {
    "signed": ("已签收", "系统按物流轨迹自动标记已收货"),
    "returned": ("已退回", "系统按物流轨迹自动标记已收到退回件"),
    "intercepted": ("拦截成功", "系统按物流轨迹自动标记已叫停（物流拦截）"),
}


def refund_path(order: Order) -> str:
    """当前走哪条退单路径：按「订单推到哪一步了」派生，不单独存字段。"""
    if order.is_received:
        return PATH_RECEIVED
    if (order.tracking_no or "").strip():
        return PATH_SHIPPED
    if order.pushed_at is not None or (order.supplier_status or "") in ("pushed", "done"):
        return PATH_PUSHED
    return PATH_NOT_PUSHED


def _step_stopped(order: Order) -> bool:
    """「已叫停」做完了没：优先看事实字段 stopped_at，没有再按老口径推断（历史数据兼容）。"""
    if order.stopped_at is not None:
        return True
    if order.refund_supplier_synced_at is not None:
        return True
    if (order.order_state or "") in (
        ORDER_STATE_REFUND_SYNCED,
        ORDER_STATE_REFUNDED,
        ORDER_STATE_CLOSED,
    ):
        return True
    return (order.refund_status or "") in ("intercepted", "shipped", "refunded", "closed")


def _stopped_at(order: Order):
    """已叫停的时间：事实字段优先，老数据按同步时间近似（页面显示用）。"""
    return order.stopped_at or order.refund_supplier_synced_at


def refund_steps(order: Order) -> list[dict]:
    """这条路径要做的几件事 + 各自做完了没 + 没做完时卡人的话（block）。

    required=False 的步骤（比如没付款给供应商就不用等供应商退款）在页面上显示为「不需要」，
    不参与关闭校验 —— 见 blocking_reasons（直接从这里取 block，不另写一套 if）。
    """
    path = refund_path(order)
    paid_supplier = (order.transfer_status or "") == "paid"
    received = bool(order.is_received)
    stop_label = PATH_STOP_LABEL[path]
    defs = (
        {
            "key": "stopped",
            "label": f"已叫停（{stop_label}）",
            "done": _step_stopped(order),
            "required": path != PATH_NOT_PUSHED,
            "block": f"还没叫停（{stop_label}）：先联系供应商把货停下来",
            "at": _stopped_at(order),
            "by": order.stopped_by or "",
            "hint": f"立即联系供应商 / 快递：{stop_label}，把包裹拦下来",
            "action": "stopped",
            "input": "note",
            "input_label": "叫停备注",
            "placeholder": "如：已电话通知供应商别发货",
            "button": f"标记「{stop_label}」",
            "confirm": "确认已经叫停了？会记上时间与你这个操作人",
        },
        {
            "key": "returned",
            "label": "客户已寄回（退回单号）",
            "done": bool((order.return_tracking_no or "").strip()),
            "required": received,
            "block": "客户还没寄回：把退回快递单号填进来",
            "at": None,
            "by": "",
            "hint": "问客户要退回的快递单号并填进来，后面查件、收件都用它",
            "action": "return-tracking",
            "input": "return_tracking_no",
            "input_label": "退回快递单号",
            "placeholder": "如：SF1234567890",
            "button": "保存退回单号",
            "confirm": "",
        },
        {
            "key": "return_received",
            "label": "已收到退回件",
            "done": order.return_received_at is not None,
            "required": received,
            "block": "退回件还没收到：查退回件物流，收到后标记（收不到要跟客户确认）",
            "at": order.return_received_at,
            "by": "",
            "hint": "查一下退回件物流：签收了就点确认（收不到要跟客户对一下）",
            "action": "return-received",
            "input": "",
            "input_label": "",
            "placeholder": "",
            "button": "标记「已收到退回件」",
            "confirm": "确认已经收到客户寄回的货？收到后才可以退款给客户",
        },
        {
            "key": "refunded",
            "label": "已退款给客户",
            "done": (order.refund_paid or "unpaid") == "paid",
            "required": True,
            "block": "还没退款给客户：退款后把「是否退款」改成已退款",
            "at": order.refund_paid_at,
            "by": "",
            "hint": "把钱退给客户（支付宝 / 微信原路退回），退完点确认",
            "action": "pay",
            "input": "refund_amount",
            "input_label": "退款金额",
            "placeholder": "默认按成交价",
            "button": "标记「已退款给客户」",
            "confirm": "确认钱已经退给客户了？",
        },
        {
            "key": "supplier_refunded",
            "label": "供应商已退款",
            "done": (order.supplier_refunded or "unpaid") == "paid",
            "required": paid_supplier,
            "block": "还没追回供应商的钱：收到后标记「供应商已退款」",
            "at": order.supplier_refunded_at,
            "by": "",
            "hint": "跟供应商对完账、钱退回来了就点确认（标记后订单自动关闭）",
            "action": "supplier-refunded",
            "input": "note",
            "input_label": "线下核对备注",
            "placeholder": "如：供应商已把钱退回，微信已确认",
            "button": "标记「供应商已退款」",
            "confirm": "确认供应商已把钱退回来了？标记后订单自动关闭完成",
        },
    )
    return list(defs)


def current_step(order: Order) -> dict | None:
    """现在该做的那一步：第一个还没做的必需步骤。

    页面只把这一步展开（说明 + 要填的 + 按钮），其余步骤收成一行进度 ——
    流程性的东西一次全铺开，运营反而不知道该点哪里。
    全做完了返回 None，这时候该做的就是收尾关单。
    """
    if order.is_closed:
        return None
    for step in refund_steps(order):
        if step["required"] and not step["done"]:
            return step
    return None


def missing_steps(order: Order) -> list[str]:
    """还没做完、且这条路径必须做的步骤（关闭前要补齐的）。"""
    return [s["label"] for s in refund_steps(order) if s["required"] and not s["done"]]


def blocking_reasons(order: Order) -> list[str]:
    """关闭完成卡住的原因（人话），空列表 = 可以关。

    唯一真源就是上面的步骤清单 —— 缺哪一步就卡哪一步，不另写一套 if（改步骤时不用改两处）。
    """
    return [
        s["block"] for s in refund_steps(order) if s["required"] and not s["done"]
    ]


def next_step_hint(order: Order) -> str:
    """下一步该干什么：路径动作 + 第一个缺的必需步骤。"""
    if order.is_closed:
        return "已关闭完成：这单走完了，不再参与统计与结算"
    missing = missing_steps(order)
    if not missing:
        return "该做的都做完了 —— 点「关闭完成」收尾"
    return f"{PATH_ACTION[refund_path(order)]}；下一步：{missing[0]}"


def mark_stopped(
    session,
    order: Order,
    action: str = "",
    actor: str = "",
    note: str = "",
    source: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """标记「已叫停」：不推给供应商 / 撤回发货 / 物流拦截，写下时间与动作。

    这是退单第一步的事实记录 —— 以前靠 refund_status 推断，说不清到底做没做、什么时候做的。
    action 不传就按当前路径自动选（已推单 -> 撤回发货，已发货 -> 物流拦截）。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    if not (order.is_refund or order.is_recalled):
        return False, "该订单不是退单：不用叫停"
    action = action or PATH_STOP_ACTION[refund_path(order)]
    if action not in STOP_ACTION_LABELS:
        return False, "未知的叫停动作"
    order.stopped_at = order.stopped_at or now()
    order.stopped_action = action
    order.stopped_by = source or actor or "系统自动"
    if note:
        order.refund_note = (f"{order.refund_note}\n{note}" if order.refund_note else note)[:2000]
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_STOPPED,
        detail=(
            f"已叫停：{STOP_ACTION_LABELS[action]}"
            + (f"（来源：{source}）" if source else "")
            + (f"；备注：{note}" if note else "")
        ),
        operator=source or actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"已标记「已叫停：{STOP_ACTION_LABELS[action]}」"


def mark_return_received(
    session,
    order: Order,
    actor: str = "",
    note: str = "",
    source: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """标记「已收到退回件」：客户寄回的货我们签收了。

    填了退回单号只说明客户寄了，不代表我们收到 —— 收到货是退款给客户的前提，单独记一笔。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    if not (order.is_refund or order.is_recalled):
        return False, "该订单不是退单：不用记退回件"
    if order.return_received_at is not None:
        return True, "已经标记过「已收到退回件」"
    if not (order.return_tracking_no or "").strip():
        return False, "还没有退回快递单号：先填单号，再标记收到"
    order.return_received_at = now()
    if note:
        order.refund_note = (f"{order.refund_note}\n{note}" if order.refund_note else note)[:2000]
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_RETURN_RECEIVED,
        detail="已收到退回件" + (f"（来源：{source}）" if source else "")
        + (f"；备注：{note}" if note else ""),
        operator=source or actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, "已标记「已收到退回件」：可以退款给客户了"


def apply_tracking_status(
    session,
    order: Order,
    status: str,
    on_return: bool = False,
    source: str = "物流轨迹",
    commit: bool = True,
) -> tuple[bool, str]:
    """物流轨迹接入点：把查到的轨迹状态落到订单上，并自动补上能确认的事实。

    接入物流查询后，接口拿到轨迹状态调这里即可（on_return=True 表示查的是退回件单号）。
    原则：
    · 轨迹只写**事实字段**（已收货 / 已收到退回件 / 已叫停），**不改订单状态、不自动关单**；
    · 只补空缺 —— 人工已经标记过的以人工为准，不会被轨迹覆盖；
    · 每一步都留痕，写明「来源：物流轨迹」，事后能分清是运营点的还是系统自动补的。
    """
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    status = (status or "").strip()
    if status not in TRACKING_STATUS_LABELS:
        return False, f"未知的物流状态：{status or '（空）'}"

    if on_return:
        order.return_tracking_status = status
        order.return_tracking_status_at = now()
    else:
        order.tracking_status = status
        order.tracking_status_at = now()
    label = TRACKING_STATUS_LABELS[status]
    filled: list[str] = []

    # 正向件已签收 -> 客户已收货（没收货的退单路径因此自动切到「已收货，等寄回」）
    if not on_return and status == "signed" and not order.is_received:
        order.is_received = True
        order.received_at = order.received_at or now()
        filled.append("已收货")
    # 正向件拦截成功 -> 已叫停（物流拦截）
    if not on_return and status == "intercepted" and order.stopped_at is None:
        mark_stopped(
            session, order, action="intercept", source=source, commit=False
        )
        filled.append("已叫停（物流拦截）")
    # 退回件已签收 / 已退回 -> 我们收到退回件
    if on_return and status in ("signed", "returned") and order.return_received_at is None:
        order.return_received_at = now()
        filled.append("已收到退回件")

    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_TRACKING_STATUS,
        detail=(
            f"{'退回件' if on_return else '正向件'}物流状态：{label}"
            + (f"；自动补记：{'、'.join(filled)}" if filled else "")
        ),
        operator=source,
        commit=False,
    )
    if commit:
        session.commit()
    return True, (
        f"{'退回件' if on_return else '正向件'}物流状态已更新为「{label}」"
        + (f"；自动补记：{'、'.join(filled)}" if filled else "")
    )


def backfill_refund_facts(session) -> int:
    """回填「已叫停 / 已收到退回件」两个事实字段（老数据一次性补，之后由操作与轨迹写入）。

    · 已叫停：老数据按旧口径（同步过供应商 / 状态已推进 / 处理状态是拦截之后）推断，
      时间取当时能取到的最早时间；
    · 已收到退回件：只回填**已关闭完成**且已收货且有退回单号的单 —— 这类单流程已走完，
      货没收到不可能关掉。未关闭的老单不猜（填了单号不等于收到），交给体检提醒运营核对。
    """
    n = 0
    for order in session.scalars(select(Order).where(is_refund_order())).all():
        if order.stopped_at is None and _step_stopped(order):
            order.stopped_at = (
                order.refund_supplier_synced_at or order.refund_handled_at or order.refund_at
            )
            order.stopped_action = PATH_STOP_ACTION[refund_path(order)]
            order.stopped_by = "历史数据回填"
            n += 1
        if (
            order.return_received_at is None
            and order.is_closed
            and order.is_received
            and (order.return_tracking_no or "").strip()
        ):
            order.return_received_at = order.refund_paid_at or order.refund_handled_at or order.closed_at
            n += 1
    if n:
        session.commit()
    return n


def fix_intercept_status(session) -> int:
    """老数据修正：处理状态与「推到哪一步了」对不上的，按路径改回来（幂等，可重跑）。

    两类要改：
      · 从没推给供应商却记成拦截类的（待拦截 / 已拦截 / 已发货无法拦截）——
        没下过单就没有包裹，这三个都谈不上 -> 无需拦截（未推单）；
      · 已经关闭完成却还挂着「待拦截」的 -> 已关闭。
    已经推过单的、以及人工改成的非拦截类状态（已退款等）一律不动。
    """
    intercept_like = (
        REFUND_STATUS_PENDING,
        REFUND_STATUS_INTERCEPTED,
        REFUND_STATUS_SHIPPED,
    )
    n = 0
    for order in session.scalars(select(Order).where(is_refund_order())).all():
        current = (order.refund_status or "").strip()
        if not current:
            continue  # 没记状态的按派生文案展示（见 Order.refund_status_label），不硬写
        if order.is_closed:
            want = REFUND_STATUS_CLOSED
        elif current in intercept_like and refund_path(order) == PATH_NOT_PUSHED:
            want = REFUND_STATUS_NOT_PUSHED
        else:
            continue
        if want != current:
            order.refund_status = want
            n += 1
    if n:
        session.commit()
    return n


def order_issues(order: Order) -> list[dict]:
    """单笔退单的「状态与事实对不上」体检（历史数据兼容用，只报不改）。"""
    issues: list[dict] = []
    paid_customer = (order.refund_paid or "unpaid") == "paid"
    paid_supplier = (order.transfer_status or "") == "paid"
    st = (order.refund_status or "").strip()

    def add(kind: str, level: str, label: str, fix: str) -> None:
        issues.append({"kind": kind, "level": level, "label": label, "fix": fix})

    if order.is_closed and not paid_customer:
        add(
            "closed_unpaid",
            "high",
            "已关闭完成，但「是否退款」还是未退款",
            "确认线下到底退没退：没退要补退；已退的不用处理（当时是强制 / 直接关闭的）",
        )
    if order.is_closed and paid_supplier and (order.supplier_refunded or "unpaid") != "paid":
        add(
            "closed_no_supplier_refund",
            "high",
            "已关闭完成，但已付款给供应商、却没标记「供应商已退款」",
            "找供应商追退款，收到后标记「供应商已退款」",
        )
    if order.is_closed and order.is_received and not (order.return_tracking_no or "").strip():
        add(
            "closed_no_return",
            "high",
            "已关闭完成，但客户已收货、没有退回物流单号",
            "确认货有没有寄回：没寄回要追回，寄回了把单号补上",
        )
    if st and (order.order_state or "") not in REFUND_ORDER_STATES:
        add(
            "state_mismatch",
            "high",
            "有退单处理状态，但订单状态不在退单流程里（两套口径打架）",
            "在退单处理里重新保存一次，让订单状态按事实回算",
        )
    if st == "closed":
        add(
            "dead_status",
            "mid",
            "处理状态是已废弃的「closed」（现在关闭完成看订单状态）",
            "不用管，展示已按订单状态走；下次维护这单会自动覆盖",
        )
    if st == "intercepted" and (order.order_state or "") == ORDER_STATE_REFUNDED and not paid_customer:
        add(
            "intercept_shows_refunded",
            "mid",
            "实际是「已拦截」，但订单状态是已退款 —— 页面上会显示成已退款",
            "按实际进度处理：钱真退给客户后再关闭完成",
        )
    if st == "refunded" and not paid_customer:
        add(
            "status_paid_mismatch",
            "mid",
            "处理状态是「已退款」，但「是否退款」还是未退款",
            "钱真退了就点「标记已退款」；没退就把处理状态改回实际进度",
        )
    if paid_customer and not float(order.refund_amount or 0):
        add(
            "amount_zero",
            "mid",
            "已退款给客户，但退款金额是 0（毛利会算错）",
            "在「编辑订单」里补上退款金额",
        )
    if not order.refund_at:
        add("no_refund_at", "low", "退单没有识别时间（老数据）", "不用管，只影响展示时间")
    if (
        order.is_received
        and (order.return_tracking_no or "").strip()
        and order.return_received_at is None
        and not order.is_closed
    ):
        add(
            "no_return_received",
            "mid",
            "有退回快递单号，但没标记「已收到退回件」（填单号只说明客户寄了）",
            "查一下退回件物流：收到了就标记，收不到要跟客户确认",
        )
    return issues


# 待办分组的顺序与每组该干的事：按「这条路径上第一个还没做的步骤」分
TODO_GROUPS = (
    ("stopped", "还没叫停：联系供应商把货停下来"),
    ("returned", "等客户寄回：催一下，寄了把退回单号填进来"),
    ("return_received", "退回件在途：查退回件物流，收到后标记"),
    ("refunded", "待退款给客户：退款后改成已退款"),
    ("supplier_refunded", "待追回供应商货款：收到后标记"),
    ("closable", "都齐了：可以关闭完成"),
)


def refund_todo(session) -> list[dict]:
    """退单待办：还没关闭的退单，按「现在卡在哪一步」分组 —— 一组一个动作。

    与体检（scan_anomalies）的分工：
      · 体检找的是「历史数据对不上」的老账；
      · 这里列的是「今天还欠一个动作」的活 —— 打开就知道先干哪几票。
    分组取的是这条路径上**第一个还没做的步骤**（先做的排在前面）。
    """
    buckets: dict[str, list[dict]] = {}
    orders = session.scalars(
        select(Order).where(is_refund_order()).order_by(Order.id.desc())
    ).all()
    for order in orders:
        if order.is_closed:
            continue
        steps = refund_steps(order)
        missing = [s for s in steps if s["required"] and not s["done"]]
        key = missing[0]["key"] if missing else "closable"
        buckets.setdefault(key, []).append(
            {
                "id": order.id,
                "order_no": order.order_no or "",
                "customer": order.receiver_name or order.buyer_nick or "-",
                "phone": order.receiver_phone or "",
                "amount": abs(float(order.refund_amount or 0)) or float(order.deal_amount or 0),
                "path_label": PATH_LABELS[refund_path(order)],
                "missing": [s["label"] for s in missing],
                "block": missing[0]["block"] if missing else "",
                "at": order.refund_at or order.msg_time,
                "has_return_no": bool((order.return_tracking_no or "").strip()),
            }
        )
    out: list[dict] = []
    for key, title in TODO_GROUPS:
        rows = buckets.get(key) or []
        if not rows:
            continue
        out.append(
            {
                "key": key,
                "title": title,
                "rows": rows,
                "count": len(rows),
                "amount": round(sum(r["amount"] for r in rows), 2),
            }
        )
    return out


def scan_anomalies(session) -> list[dict]:
    """历史退单体检：把所有「状态与事实对不上」的退单挑出来（只报不改）。

    新口径上线后老数据不会自动改（改数据有风险），这里列清楚每笔单的问题与建议动作，
    运营按单去核对：真有问题的补录，本来就处理完的不用动。
    """
    out: list[dict] = []
    for order in session.scalars(select(Order).where(is_refund_order())).all():
        for issue in order_issues(order):
            out.append(
                {
                    "order_id": order.id,
                    "order_no": order.order_no,
                    "closed": bool(order.is_closed),
                    **issue,
                }
            )
    level_rank = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda r: (level_rank.get(r["level"], 9), r["order_no"]))
    return out


# —— 逆向流程的后续动作（订单详情页操作）——
def mark_supplier_synced(
    session,
    order: Order,
    actor: str = "",
    note: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """运营把退单同步给供应商：状态 -> 「已同步供应商退单」。"""
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    ok, reason = order_state_service.set_state(
        session,
        order,
        order_state_service.ORDER_STATE_REFUND_SYNCED,
        actor=actor,
        commit=False,
    )
    if not ok:
        return False, reason
    if note:
        order.refund_note = (f"{order.refund_note}\n{note}" if order.refund_note else note)[:2000]
    if actor:
        order.refund_by = actor
    order.refund_handled_at = now()
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_REFUND_SYNC,
        detail="已同步供应商退单" + (f"；备注：{note}" if note else ""),
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, "已同步供应商退单：等供应商确认后在这里维护「是否退款」"


def backfill_refund_status(session) -> int:
    """老数据补齐：订单状态进了退单流程、但没记退单处理状态的，按状态补上。

    2026-09 整理状态口径时发现：老版本的退单只落了 order_state，refund_status 是空的，
    于是「这单是不是退单」出现两套判断（一套看状态、一套看处理状态），列表条数、待拦截
    数字、推单组批都对不上。这里补一次（幂等），补完只需要看 refund_status 就够了。

    **补什么状态按「推到哪一步了」派生**（2026-10-02 改）：
      · 已退款     -> refunded
      · 其余退单态 -> 按路径：还没推给供应商的是 not_pushed（没有包裹可拦），
                      已推单 / 已发货的是 pending（待拦截），客户已收货的是 received。
    以前一律补成 pending，于是「从没推过单的退单」也被说成待拦截（订单 3317081124078045869）。
    """
    rows = list(
        session.scalars(
            select(Order).where(
                Order.order_state.in_(REFUND_ORDER_STATES), Order.refund_status == ""
            )
        ).all()
    )
    for order in rows:
        order.refund_status = (
            REFUND_STATUS_REFUNDED
            if order.order_state == ORDER_STATE_REFUNDED
            else initial_refund_status(order)
        )
        order.updated_at = now()
    if rows:
        session.commit()
        logger.info("退单处理状态补齐：%s 笔老退单按订单状态补上了处理状态", len(rows))
    return len(rows)


def set_refund_reason(
    session,
    order: Order,
    reason: str,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """人工维护退单理由（群里没填 / 填错时补上）。"""
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    reason = (reason or "").strip()
    if not reason:
        return False, "请填写退单理由"
    before = (order.refund_reason or "").strip() or "（空）"
    if before == reason:
        return True, "退单理由没有变化"
    order.refund_reason = reason[:2000]
    # 理由补齐后，因缺理由挂的「待确认」自动消掉
    if order.needs_review and "缺退单理由" in (order.review_reason or ""):
        order.needs_review = False
        order.review_reason = ""
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_REFUND_REASON,
        detail=f"退单理由：{before} → {reason[:120]}",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, "已保存退单理由"


def set_refund_paid(
    session,
    order: Order,
    paid: bool,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """维护「是否退款」（与「是否转账」同一套口径：paid / unpaid）。"""
    from . import order_log as order_log_service  # 局部导入，避免循环依赖

    target = "paid" if paid else "unpaid"
    label = "已退款" if paid else "未退款"
    if order.refund_paid == target:
        return True, f"该订单已经是「{label}」"
    before = "已退款" if order.refund_paid == "paid" else "未退款"
    order.refund_paid = target
    order.refund_paid_at = now() if paid else None
    order.updated_at = now()
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_REFUND_PAID,
        detail=f"是否退款：{before} → {label}",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"已标记为「{label}」"


def restore_normal_order(
    session,
    order: Order,
    actor: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """把被误判的订单恢复成正常订单：清掉撤单 / 退单标记，状态按事实重算。

    系统自动判定（群里撤回消息、命中退单词）难免误伤，这是人工补救入口：
    恢复后订单重新参与统计与后续流程，结算流水也跟着回滚。
    """
    from . import finance as finance_service  # 局部导入，避免循环依赖
    from . import order_log as order_log_service
    from . import recall as recall_service

    before: list[str] = []
    if order.is_recalled:
        before.append("已撤单")
        recall_service.undo_recalled(session, order, commit=False)
    if order.is_refund:
        before.append(f"退单（{order.refund_status_label or '待拦截'}）")
        order.refund_status = ""
        order.refund_keyword = ""
        order.refund_reason = ""
        order.refund_at = None
        order.refund_source_message_id = ""
        order.refund_by = ""
        order.refund_note = ""
        order.refund_handled_at = None
        order.refund_supplier_synced_at = None
        order.refund_reason_asked_at = None
        order.refund_paid = "unpaid"
        order.refund_paid_at = None
        # 2026-09-29 逆向口径新增的字段一起清掉，避免恢复后还留着「关闭完成」的痕迹
        order.refund_amount = 0.0
        order.return_tracking_no = ""
        order.is_received = False
        order.received_at = None
        order.supplier_refunded = "unpaid"
        order.supplier_refunded_at = None
        order.closed_at = None
        # 只清「缺退单理由」挂的待确认，解析存疑的提示保留
        if order.needs_review and "缺退单理由" in (order.review_reason or ""):
            order.needs_review = False
            order.review_reason = ""
    if not before:
        return False, "该订单本来就是正常订单，无需恢复"
    order.updated_at = now()
    order_state_service.refresh(session, order, commit=False)
    try:
        finance_service.sync_order(session, order)
    except Exception as exc:  # noqa: BLE001  结算流水失败不影响恢复
        logger.warning("恢复正常订单后同步结算流水失败 %s: %s", order.order_no, exc)
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_RESTORE,
        detail=f"恢复正常订单（原状态：{'、'.join(before)}）：重新参与统计与后续流程",
        operator=actor,
        commit=False,
    )
    if commit:
        session.commit()
    return True, f"已恢复为正常订单：{order.order_state_label}"
