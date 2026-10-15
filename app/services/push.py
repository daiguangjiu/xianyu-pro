"""单笔推单：生成发给供应商的推单文案，并支持「复制即标记已推单」。

业务口径（2026-09 现行）：
  - 供应链发货先按单笔推单（一单一推），批量推单后续再开放；
  - 推单信息在订单详情页顶部单独成块，一键复制；
  - 复制即视为已推单：订单的推单状态置为 pushed 并记录 pushed_at；
  - 「今日第 x 单」按供应商分别计数：同一家供应商「当天产生的订单」，
    按订单发送时间（群里消息时间）先后排队，与「推没推」无关；
  - 撤回的订单不占号：撤了中间一单，后面订单的单号自动往前补，
    由撤回处理即时重排 + 后台定时任务兜底修复（见 repair_daily_indexes）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import String, case, cast, func, select, text
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    ORDER_STATE_CLOSED,
    Order,
    awaiting_tracking,
    not_recalled,
    not_refund_in_progress,
    pending_push_order,
    pushed_order,
)
from ..db import dialect_name
from ..timeutil import beijing_now, now, to_beijing
from .notify import mask_phone, notify_context, pending_check_names, render_notify
from . import audit as audit_service
from . import finance as finance_service
from . import supplier as supplier_service
from . import order_state as order_state_service

logger = logging.getLogger(__name__)

# 全站时间口径是东八区（北京时间），这里只保留时间常量供旧调用方引用
CN_OFFSET = timedelta(hours=8)
TRANSFER_LABELS = {"paid": "已转账", "unpaid": "未转账"}

# 物流单号填好后，**最多只能再改 1 次**（首次填写「空 -> 有值」不计入）。
# 改单号会在该订单的飞书群里 @ 发送人，来回改会刷屏，也给对账留下多个版本；
# 填错一次还能救回来，之后就锁死（要再改找管理员）。
TRACKING_MAX_EDIT = 1

# 渔货栈自营供应链的识别口径（2026-10-15 用户拍板）：
# 供应商档案编码 YS001，或名称带「渔货栈」——这类订单视为渔货栈自营商品，
# 推单走「同步订单到渔集发货」（发货单 → 渔集仓库拣货出库），不推给外部供应商。
SELF_SUPPLY_CHAIN_CODES = ("YS001",)
SELF_SUPPLY_CHAIN_NAME_KEY = "渔货栈"


def is_self_supply_chain(session: Session, order: Order) -> bool:
    """这张订单是否走渔集发货：渔货栈自营单（origin=self）或供应链是渔货栈（YS001）。"""
    if (order.origin or "") == "self":
        return True
    name = (order.supplier_name or "").strip()
    if not name:
        return False
    if SELF_SUPPLY_CHAIN_NAME_KEY in name:
        return True
    from ..models import Supplier

    row = session.scalar(select(Supplier).where(Supplier.name == name))
    return bool(row and (row.code or "").strip().upper() in SELF_SUPPLY_CHAIN_CODES)
# 推单状态（order.supplier_status）的展示文案
SUPPLIER_STATUS_LABELS = {
    "pending": "待推单发货",
    "pushed": "已推单",
    "done": "已完成",
    "cancelled": "已取消",
}
# —— 逆向订单的推单状态文案（2026-09-29 口径）——
# 推单状态按「订单上的事实」推出来（撤没撤单、退没退单、推没推、有没有物流单号、收没收货），
# 不另外存字段 —— 事实变了，列表与详情上看到的话就跟着变，不会出现两处说法不一致。
PUSH_STATUS_CANCELED = "用户撤单，无需推单"
PUSH_STATUS_REFUND_NO_PUSH = "用户退单，无需推单"
PUSH_STATUS_RECALL_SHIP = "用户退单，同步撤回发货"
PUSH_STATUS_INTERCEPT = "用户退单，同步物流拦截"
PUSH_STATUS_RETURN = "用户收货退单，同步退单物流单号"
# 老名字（撤单不推单）保留：老脚本 / 测试可能引用，值改成新文案
SUPPLIER_STATUS_RECALLED = PUSH_STATUS_CANCELED
# 单号修正进操作日志，便于事后追「供应商那边收到的号」
ACTION_FIX_INDEX = "单号修正"
# 定时修复只回看最近几天（撤回来的单都是当天的单）
REPAIR_DAYS = 3

# —— 时间筛选 / 时间维度统计（2026-10-06 新增）——
# 供应链发货页要能回答「这段时间发了多少单、花了多少成本」，所以列表、统计、趋势
# 三处共用同一份时间条件（见 pushed_filters），口径不会一个跟筛选、一个不跟。
# 时间口径三选一：**默认推单时间**（这批单是什么时候推给供应商的），
# 发货时间 = 供应商把货发出、单号回填的时间（订单 shipped_at），下单时间 = 群里下单那一刻。
TIME_FIELDS: dict[str, tuple[str, object]] = {
    "pushed": ("推单时间", Order.pushed_at),
    "shipped": ("发货时间", Order.shipped_at),
    "ordered": ("下单时间", Order.ordered_at),
}
DEFAULT_TIME_FIELD = "pushed"
# 快捷区间：页面上给链接，点了直接把 date_from / date_to 填好（也能自己在日期框里改）
RANGE_PRESETS: dict[str, str] = {
    "today": "今天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    "month": "本月",
    "last_month": "上月",
}
# 趋势最多给多少行（按天看两个月够翻账了；按月看则是 60 个月）
TREND_LIMIT = 60


def apply_transfer(order: Order, transfer_status: str) -> bool:
    """更新「是否转账」并维护转账时间，返回转账状态是否发生变化。

    标记「已转账」的时刻要留痕：资金结算里这笔的「公司自营应付」流水
    就以这个时间作为结算时间（见 finance.sync_transfer）。
    """
    changed = (order.transfer_status or "") != transfer_status
    order.transfer_status = transfer_status
    if transfer_status == finance_service.TRANSFER_PAID:
        if changed or order.transfer_paid_at is None:
            order.transfer_paid_at = now()
    else:
        order.transfer_paid_at = None
    order.updated_at = now()
    return changed


def is_pushed(order: Order) -> bool:
    """是否已推给供应商（推单时间 / 推单状态为准）。"""
    return order.pushed_at is not None or (order.supplier_status or "") in ("pushed", "done")


def refund_push_status(order: Order) -> str:
    """逆向订单（撤单 / 退单）的推单状态文案（""= 不是逆向订单）。

    按订单上的事实推，四种场景：
      · 未推单 + 用户撤单    -> 用户撤单，无需推单
      · 未推单 + 用户退单    -> 用户退单，无需推单
      · 已推单、供应商还没回物流单号（视作未发货）+ 退单 -> 用户退单，同步撤回发货
      · 已推单、供应商已回单、用户还没收到货 + 退单      -> 用户退单，同步物流拦截
      · 已推单、用户已收到货 + 退单                     -> 用户收货退单，同步退单物流单号
    """
    if order.is_recalled or (order.order_state or "") == order_state_service.ORDER_STATE_CANCELED:
        if not order.is_refund:
            return PUSH_STATUS_CANCELED
        # 既是撤单又是退单：按「退单」走（用户退单优先，撤单只是留档动作）
    if not order.is_refund:
        return ""
    if not is_pushed(order):
        return PUSH_STATUS_REFUND_NO_PUSH
    if not (order.tracking_no or "").strip():
        return PUSH_STATUS_RECALL_SHIP
    if order.is_received:
        return PUSH_STATUS_RETURN
    return PUSH_STATUS_INTERCEPT


def supplier_status_label(order: Order) -> str:
    """推单状态的展示文案（逆向订单按场景给话术，正常订单按 supplier_status）。

    已撤单 / 退单的订单不会再推给供应商，所以不显示「待推单发货」，
    而是直接写明「用户撤单，无需推单」这类话术，免得运营以为还漏了一单没推。
    """
    refund_status = refund_push_status(order)
    if refund_status:
        return refund_status
    value = (order.supplier_status or "").strip()
    return SUPPLIER_STATUS_LABELS.get(value, value or SUPPLIER_STATUS_LABELS["pending"])


def supplier_status_tone(order: Order) -> str:
    """推单状态的配色（列表 badge 用）：refund（逆向）| recalled | pending | pushed | done。"""
    if refund_push_status(order):
        return "refund"
    if order.is_recalled or (order.order_state or "") == order_state_service.ORDER_STATE_CANCELED:
        return "recalled"
    value = (order.supplier_status or "").strip()
    return value or "pending"


def can_pay_supplier(order: Order) -> tuple[bool, str]:
    """还能不能付款给供应商（未付款的退单：忽略、且这单不要再付款）。

    逆向订单里「未付款」的收尾规则就是：**不用付了** —— 所以这里挡一道，
    免得运营照着老习惯把钱转出去，回头还要找供应商追退款。
    """
    if order.is_refund or order.is_recalled:
        return (
            False,
            "该订单已退单 / 撤单：未付款的不用再付款（也不该付款），走退单收尾关闭即可",
        )
    return True, ""


def _fmt_money(value: float | None) -> str:
    """金额：整数不带小数，其余保留两位。"""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.2f}"


def cn_day_start(value: datetime | None) -> tuple[datetime, datetime]:
    """返回某时刻所在「东八区自然日」的起止区间。

    库里的时间本身就是北京时间（naive），所以直接用日期切分，不再做时区换算。
    """
    base = value or now()
    if base.tzinfo is not None:
        base = to_beijing(base)
    start = datetime(base.year, base.month, base.day)
    return start, start + timedelta(days=1)


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def index_day(order: Order) -> datetime:
    """「今日第 x 单」计数所用的自然日基准时刻：按订单产生时间（群里消息发送时间）。

    订单在当天推、还是隔天才推，都算在它「产生」的那一天里，
    这样供应商对账（今天第几单）不会因为推单跨天而错位。
    """
    return order.msg_time or order.ordered_at or order.created_at or now()


def _order_day_expr():
    """SQL 侧的「订单产生时间」：与 index_day 保持同一优先级，用于按天切片与排序。"""
    return func.coalesce(Order.msg_time, Order.ordered_at, Order.created_at)


def day_label(base: datetime | None = None) -> str:
    """日期的中文短标签：「（9/25）周五」（推单文案与详情页 chip 共用，便于对账）。"""
    value = base or now()
    if value.tzinfo is not None:
        value = to_beijing(value)
    return f"（{value.month}/{value.day}）{WEEKDAYS[value.weekday()]}"


def supplier_daily_index(
    session: Session, order: Order, index: supplier_service.SupplierIndex | None = None
) -> int:
    """该订单是「该供应商今天第几单」（供应商维度，按订单发送时间排队）。

    2026-09 口径：
      1 只数同一家供应商当天的订单（编码 / 名称 / 名称（编码）各种写法算一家）；
      2 自然日按订单产生时间（群里消息发送时间）切分，跨天不累计；
      3 排队顺序按订单产生时间先后（同一时刻用 id 兜底），与「推没推」无关：
        还没推的订单也占号，所以同一个号不会重复发给供应商；
      4 撤回的订单不占号：撤了中间一单，后面订单的单号自动往前补（无需人工改号）。
    """
    # 同一家供应商可能被记成编码 / 名称 / 名称（编码），计数要把这些写法都算上
    idx = index or supplier_service.build_index(session)
    supplier_keys = idx.keys(order.supplier_name or "")
    if not supplier_keys:
        # 没指定供应商（或写了占位符）：只在「未指定」这一堆里排队；详情页会提醒先补全
        supplier_keys = [order.supplier_name or ""]
    base = index_day(order)
    start, end = cn_day_start(base)
    day_expr = _order_day_expr()
    stmt = select(func.count(Order.id)).where(
        not_recalled(),
        Order.supplier_name.in_(supplier_keys),
        day_expr >= start,
        day_expr < end,
    )
    if order.id is None:  # 还没落库：只数它前面产生的订单
        stmt = stmt.where(day_expr < base)
    else:
        stmt = stmt.where((day_expr < base) | ((day_expr == base) & (Order.id <= order.id)))
    return max(int(session.scalar(stmt) or 0), 1)


def repair_daily_indexes(session: Session, days: int = REPAIR_DAYS) -> dict:
    """定时修复「今日第 x 单」：把撤回 / 撤销撤回导致的单号变化修回去。

    单号本身是实时算出来的（撤了中间一单，后面的单号立刻往前补），这里负责把
    「报给供应商的号」与「现在的号」对照清楚，并留下可追溯的记录：

      1 老数据回填：本版本之前推的单没有记录报给供应商的号，按当前口径补一次；
      2 记下修正：撤回导致重排（推单时报的号 != 现在的号）时更新 push_index_fixed
        并写一条操作日志；重复执行不会重复记账，撤回被撤销后自动复位（回到 0）。

    调用点：撤回处理（即时）、后台定时同步循环与启动（兜底）。
    """
    start = (now() - timedelta(days=max(int(days), 1))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_expr = _order_day_expr()
    index = supplier_service.build_index(session)
    rows = list(
        session.scalars(
            select(Order)
            .where(not_recalled(), day_expr >= start)
            .order_by(day_expr.asc(), Order.id.asc())
        ).all()
    )
    checked = filled = fixed = reset = 0
    for order in rows:
        # 还没推给供应商的单：没有「报给供应商的号」要对账
        if order.pushed_at is None and order.supplier_status not in ("pushed", "done"):
            continue
        checked += 1
        current = supplier_daily_index(session, order, index)
        if not order.push_index:
            order.push_index = current
            filled += 1
            continue
        if current == order.push_index:
            if order.push_index_fixed:
                order.push_index_fixed = 0  # 与推单时报的一致（撤回被撤销：单号回到原位）
                reset += 1
            continue
        if order.push_index_fixed != current:
            order.push_index_fixed = current
            fixed += 1
            audit_service.log_action(
                session,
                None,
                ACTION_FIX_INDEX,
                order.order_no,
                f"推单时报第 {order.push_index} 单 → 撤回后修正为第 {current} 单",
            )
    if filled or fixed or reset:
        session.commit()
        logger.info(
            "「今日第 x 单」校验：检查 %s 笔，回填 %s 笔，修正 %s 笔，复位 %s 笔",
            checked,
            filled,
            fixed,
            reset,
        )
    return {"checked": checked, "filled": filled, "fixed": fixed, "reset": reset}


def push_text(order: Order, index: int | None = None) -> str:
    """生成推单文案（直接复制发给供应商）。

    「今日第 x 单」带上日期与周几（如今日（9/25）周五 第1单），
    供应商一眼能看出是哪天的第几单，避免跨天对账歧义。
    """
    index = index if index is not None else 1
    transfer = TRANSFER_LABELS.get(order.transfer_status, "未转账")
    day = day_label(index_day(order))
    # 设置页配了推单模板就按模板渲染（{{ 产品 }} {{ 收货人 }} {{ 第几单 }} …），
    # 没配（或写错）用下面这套内置格式
    text = render_notify(
        settings.push_template,
        notify_context(
            order,
            # 推给供应商要真号，覆盖掉默认脱敏的「收货电话」
            收货电话=order.receiver_phone or "",
            收货地址=order.receiver_address or "",
            备注=order.note or "",
            成本价=_fmt_money(order.cost_amount),
            是否转账=transfer,
            第几单=str(index),
            今日日期=day,
        ),
    )
    if text:
        return text
    lines = [
        f"产品：{order.product_name or ''}",
        f"收货人：{order.receiver_name or ''}",
        f"手机号码：{order.receiver_phone or ''}",
        f"地址信息: {order.receiver_address or ''}",
    ]
    # 收货信息不确定时，推单文案里也要说一声：供应商照着地址发货，错了就是售后问题
    pending = pending_check_names(order)
    if pending:
        lines += ["", f"⚠️ {'、'.join(pending)}待核对：截图识别不确定或与群里对不上，请先确认再发货"]
    lines += [
        "",
        f"备注：{order.note or ''}",
        "",
        f"成本价：{_fmt_money(order.cost_amount)}",
        f"是否转账：{transfer}",
        "",
        f"今日{day} 第{index}单，烦请及时发货！",
    ]
    return "\n".join(lines)


def tracking_notice(order: Order, *, is_update: bool = False) -> str:
    """物流单号同步到群里的文案（@ 发送人在 notify 层补）。

    is_update=True 表示这次是**改单号**后再通知一遍：开头先说明「单号有更新，以本条为准」，
    否则群里看的人会以为又发了一单。第一次填写走默认话术。

    设置页配了发货模板就按模板渲染（{{ 物流单号 }} {{ 订单号 }} {{ 产品 }} {{ 是否更新 }} …），
    没配（或写错）用内置格式。
    """
    text = render_notify(
        settings.tracking_template,
        notify_context(order, 是否更新="是" if is_update else ""),
    )
    if text:
        return text
    company = f"（{order.tracking_company}）" if (order.tracking_company or "").strip() else ""
    lines = []
    if is_update:
        lines.append("♻️ 物流单号有更新，请以本条为准")
    lines.append(f"🚚 {'物流单号更新' if is_update else '已发货，物流单号'}：{order.tracking_no or '-'}{company}")
    if order.order_no and not order.order_no.startswith(("AUTO-", "TMP-")):
        lines.append(f"订单号：{order.order_no}")
    if order.product_name:
        lines.append(f"产品：{order.product_name}")
    if order.receiver_name or order.receiver_phone:
        lines.append(f"收货：{order.receiver_name or '-'} {mask_phone(order.receiver_phone)}")
    if order.supplier_name:
        lines.append(f"供应商：{order.supplier_name}")
    return "\n".join(lines)


def build_push_payload(session: Session, order: Order) -> dict:
    """详情页推单模块需要的全部数据。"""
    index = supplier_service.build_index(session)
    order_index = supplier_daily_index(session, order, index)
    return {
        "text": push_text(order, order_index),
        "index": order_index,
        # 推单时报给供应商的号（0 = 还没推过）：撤回重排后与 index 对照出变化
        "reported_index": int(order.push_index or 0),
        "index_fixed": int(order.push_index_fixed or 0),
        # 详情页 chip 用：今日（9/25）周五 第 x 单
        "day_label": day_label(index_day(order)),
        # 统一展示「供应商名称（供应商编码）」，历史数据里只记了编码的也能对上档案
        "supplier": index.label(order.supplier_name or ""),
        "supplier_missing": supplier_service.is_placeholder(order.supplier_name),
        "pushed": order.supplier_status in ("pushed", "done") or order.pushed_at is not None,
        "pushed_at": order.pushed_at,
        "transfer": order.transfer_status or "unpaid",
        "transfer_label": TRANSFER_LABELS.get(order.transfer_status, "未转账"),
        # 标记「已转账」的时间：资金结算里这笔应付流水按它记结算时间
        "transfer_paid_at": order.transfer_paid_at,
        "tracking_no": order.tracking_no or "",
        "tracking_synced_at": order.tracking_synced_at,
    }


def set_transfer(session: Session, order: Order, transfer_status: str) -> Order:
    """只更新「是否转账」（不标记已推单），用于推单文案实时刷新。

    「已转账」= 采购款已经付给供应商：这笔的应付结算流水跟着核销（见 finance.sync_transfer），
    结算时间取标记已转账的时间（order.transfer_paid_at）。
    """
    if transfer_status not in TRANSFER_LABELS:
        raise ValueError("转账状态不合法")
    if transfer_status == finance_service.TRANSFER_PAID:
        ok, reason = can_pay_supplier(order)
        if not ok:
            raise ValueError(reason)
    apply_transfer(order, transfer_status)
    session.commit()
    finance_service.sync_transfer(session, order, commit=True)
    return order


def time_column(time_field: str = DEFAULT_TIME_FIELD):
    """时间口径 → 订单上的时间列（认不到就回落推单时间）。"""
    return TIME_FIELDS.get(time_field, TIME_FIELDS[DEFAULT_TIME_FIELD])[1]


def _day_expr(column):
    """取该时间列的「北京时间那一天」YYYY-MM-DD（库里存 UTC，必须 +8 再切）。

    按方言分支：SQLite 的 date(col,'+8 hours') 在 PostgreSQL 上不存在，直接写会
    UndefinedFunction（线上见过首页 500），所以两边各写各的。
    """
    if dialect_name() == "sqlite":
        return func.substr(cast(func.date(column, "+8 hours"), String), 1, 10)
    return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD")


def _parse_day(value: str):
    """YYYY-MM-DD → date；格式不对返回 None（页面上手填歪了不至于 500）。"""
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.strptime(text_value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def day_bounds(date_from: str = "", date_to: str = "") -> tuple[datetime | None, datetime | None]:
    """北京日期区间 → 库里能直接比的 UTC naive 半开区间 [start, end)。

    存库是 UTC：北京 10-06 00:00 落在库里的 10-05 16:00，所以边界要减 8 小时；
    用半开区间（结束日 +1 天的 00:00）而不是拼 23:59:59，秒级精度不会漏单。
    """
    start: datetime | None = None
    end: datetime | None = None
    first = _parse_day(date_from)
    if first:
        start = datetime.combine(first, time.min) - CN_OFFSET
    last = _parse_day(date_to)
    if last:
        end = datetime.combine(last + timedelta(days=1), time.min) - CN_OFFSET
    # 只填了结束日、或起止填反了：给个能用的区间，别让页面空着报错
    if start and end and start >= end:
        start, end = None, None
    return start, end


def range_days(preset: str, today: date | None = None) -> tuple[str, str]:
    """快捷区间 → (date_from, date_to) 北京日期字符串，页面拿它拼链接 / 回填日期框。

    today 不传就按北京时间今天（后台定时任务跑在 UTC 日切点附近也不会串天）。
    """
    day = today or beijing_now().date()
    iso = day.isoformat()
    if preset == "today":
        return iso, iso
    if preset == "7d":
        return (day - timedelta(days=6)).isoformat(), iso
    if preset == "30d":
        return (day - timedelta(days=29)).isoformat(), iso
    if preset == "month":
        return day.replace(day=1).isoformat(), iso
    if preset == "last_month":
        first_this = day.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        return last_month_end.replace(day=1).isoformat(), last_month_end.isoformat()
    return "", ""


def pushed_filters(
    session: Session,
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    date_from: str = "",
    date_to: str = "",
    time_field: str = DEFAULT_TIME_FIELD,
) -> list:
    """「已推单记录」的公共筛选条件：列表、待填回单计数、统计、趋势共用一份。

    加了时间维度以后，统计与趋势也走这里 —— 页面上的数字跟列表是同一批单，
    不会出现「列表 12 条、统计 93 条」这种两处对不上的情况。

    退单 / 撤回的单不进这里：它们不发货、也不用供应商回单（退单该在退单页跟进）。
    以前没排掉，页面上会混着几条「没单号的退单」，看着像漏催的回单。
    注意用的是 not_refund_in_progress()（只排「还在退单流程里」的），不是 not_refund_order()
    —— 后者把「关闭完成」也算退单，会把正常签收完成的单一起排掉。
    """
    conds: list = [not_recalled(), pushed_order(), not_refund_in_progress()]
    if q:
        like = f"%{q.strip()}%"
        # 关键词覆盖 订单号 / 产品名称 / 收货人 / 手机号 / 物流单号
        # （物流单号也进搜索：拿到一个单号想反查是哪一单时，不用再去订单页翻）
        conds.append(
            Order.order_no.like(like)
            | Order.product_name.like(like)
            | Order.receiver_name.like(like)
            | Order.receiver_phone.like(like)
            | Order.tracking_no.like(like)
        )
    if supplier:
        conds.append(
            Order.supplier_name.in_(supplier_service.build_index(session).keys(supplier))
        )
    if transfer in TRANSFER_LABELS:
        conds.append(Order.transfer_status == transfer)
    if tracking == "none":
        conds.append((Order.tracking_no == "") | (Order.tracking_no.is_(None)))
    elif tracking == "has":
        conds.append(Order.tracking_no != "")
    # 时间维度：起止都是「北京时间那一天」，边界换算见 day_bounds
    start, end = day_bounds(date_from, date_to)
    if start is not None or end is not None:
        col = time_column(time_field)
        # 挑了时间段就只认「这个时间有值」的单：还没推的单没有推单时间，
        # 不该出现在「10-06 推了几单」里（列为 NULL 会被排掉）
        conds.append(col.is_not(None))
        if start is not None:
            conds.append(col >= start)
        if end is not None:
            conds.append(col < end)
    return conds


def pushed_orders(
    session: Session,
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    limit: int = 500,
    include_closed: bool = False,
    date_from: str = "",
    date_to: str = "",
    time_field: str = DEFAULT_TIME_FIELD,
) -> list[Order]:
    """已推单记录（按推单时间倒序），供应链发货页维护物流单号用。

    include_closed=False（默认）：不列「已关闭完成」的订单 —— 这些单子钱货都处理完了，
    留在推单列表里只会让人以为还要催回单、填单号。真要翻旧账，页面上点「显示已关闭 / 已完成」。
    """
    conds = list(
        pushed_filters(
            session,
            q,
            supplier,
            transfer,
            tracking,
            date_from=date_from,
            date_to=date_to,
            time_field=time_field,
        )
    )
    if not include_closed:
        conds.append(Order.order_state != ORDER_STATE_CLOSED)
    return list(
        session.scalars(
            select(Order)
            .where(*conds)
            .order_by(Order.pushed_at.desc().nullslast(), Order.id.desc())
            .limit(limit)
        ).all()
    )


def closed_pushed_count(
    session: Session,
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    date_from: str = "",
    date_to: str = "",
    time_field: str = DEFAULT_TIME_FIELD,
) -> int:
    """当前筛选下「已关闭完成」的已推单单量：列表默认收起，页面用这个数给展开入口。"""
    conds = list(
        pushed_filters(
            session,
            q,
            supplier,
            transfer,
            tracking,
            date_from=date_from,
            date_to=date_to,
            time_field=time_field,
        )
    )
    conds.append(Order.order_state == ORDER_STATE_CLOSED)
    return int(
        session.scalar(select(func.count()).select_from(Order).where(*conds)) or 0
    )



def pending_tracking_count(
    session: Session,
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    date_from: str = "",
    date_to: str = "",
    time_field: str = DEFAULT_TIME_FIELD,
) -> int:
    """待填回单的单数（当前筛选下）：与首页「待供应商回单」、左侧菜单徽标同一个口径。

    退单 / 撤回的订单不算 —— 它们不发货，自然不用供应商回单。
    """
    conds = pushed_filters(
        session,
        q,
        supplier,
        transfer,
        tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=time_field,
    )
    return int(
        session.scalar(select(func.count()).select_from(Order).where(*conds, awaiting_tracking()))
        or 0
    )


def pushed_overview(session: Session) -> dict[str, int]:
    """供应链发货页顶部三个数字（全局口径，**不随页面筛选变**）。

    为什么不用筛选后的行数：这三个数是给运营看「整体进度」的。以前卡片用当前列表行数，
    点了「待填回单」以后「已推单」跟着从 93 掉到 21，看着像数据丢了（也解释了
    「为什么有两个已推单数量」的困惑）。筛选出多少条，单独在列表上方说。

    三个数能对上：已推单记录 = 供应商已发货 + 待填回单。
    三个数都不含「已关闭完成」（钱货都处理完了，不参与跟进），也不含退单 / 撤回的
    单（它们不发货、没有回单的事，在退单页跟进）。
    """
    base = [
        not_recalled(),
        pushed_order(),
        not_refund_in_progress(),
        Order.order_state != ORDER_STATE_CLOSED,
    ]
    pushed = int(session.scalar(select(func.count()).select_from(Order).where(*base)) or 0)
    shipped = int(
        session.scalar(
            select(func.count()).select_from(Order).where(*base, Order.tracking_no != "")
        )
        or 0
    )
    pending = int(
        session.scalar(
            select(func.count()).select_from(Order).where(*base, awaiting_tracking())
        )
        or 0
    )
    return {"pushed": pushed, "shipped": shipped, "pending": pending}


def pushed_stats(session: Session, conds: list) -> dict:
    """当前筛选下的**单量与金额**（成本口径）：与列表同一份 conds，所以数字一定对得上。

    为什么单开一个函数、不用 len(rows)：列表有 500 条上限，筛选结果超过 500 时
    行数就不再是单量了；金额更是得让数据库去 sum（拉回 Python 算会漏掉截断的部分）。

    成本相关四个数：
      cost    = 采购成本合计（Order.cost_amount，付给供应商的进货价）
      sale    = 销售额合计（卖出去的价，Order.sale_amount）
      freight = 运费合计（Order.shipping_fee）
      fee     = 平台技术服务费合计（Order.platform_fee）
    毛利 profit = 销售额 − 成本 − 运费 − 平台技术服务费，是**这一批发货单的账面口径**
    （不含退款：退了的钱在资金 / 退单页按单看，混进来会让「这个月发货赚了多少」忽高忽低）。
    """
    row = (
        session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.cost_amount), 0.0),
                func.coalesce(func.sum(Order.sale_amount), 0.0),
                func.coalesce(func.sum(Order.shipping_fee), 0.0),
                func.coalesce(func.sum(Order.platform_fee), 0.0),
            ).where(*conds)
        ).one()
        or (0, 0.0, 0.0, 0.0, 0.0)
    )
    count = int(row[0] or 0)
    cost = round(float(row[1] or 0.0), 2)
    sale = round(float(row[2] or 0.0), 2)
    freight = round(float(row[3] or 0.0), 2)
    fee = round(float(row[4] or 0.0), 2)
    # 已发货（有物流单号）/ 待回单（推了还没单号）在同一个筛选里各数一次
    shipped = int(
        session.scalar(
            select(func.count()).select_from(Order).where(*conds, Order.tracking_no != "")
        )
        or 0
    )
    pending = int(
        session.scalar(
            select(func.count()).select_from(Order).where(*conds, awaiting_tracking())
        )
        or 0
    )
    return {
        "count": count,
        "shipped": shipped,
        "pending": pending,
        "cost": cost,
        "sale": sale,
        "freight": freight,
        "fee": fee,
        "profit": round(sale - cost - freight - fee, 2),
        "avg_cost": round(cost / count, 2) if count else 0.0,
        # 成本占销售额：越高说明这批货进价压得越狠，页面用一句话提示
        "cost_rate": round(cost / sale * 100, 1) if sale else 0.0,
    }


def pushed_trend(
    session: Session,
    conds: list,
    time_field: str = DEFAULT_TIME_FIELD,
    group: str = "day",
    limit: int = TREND_LIMIT,
) -> list[dict]:
    """按时间维度（按天 / 按月）统计「这段时间发了多少单、成本多少」。

    group=day 取北京时间那一天 YYYY-MM-DD，group=month 截前 7 位 YYYY-MM。
    **只统计时间列有值的单**：还没推的单没有推单时间，按天分组它们会掉进空桶里，
    看着像「某天有 0 单但其实是有单没推」。
    """
    col = time_column(time_field)
    day = _day_expr(col)
    # 按天 = YYYY-MM-DD；按月 = 取前 7 位 YYYY-MM（先 +8 切日再截，两种库都成立）
    period = func.substr(day, 1, 7) if group == "month" else day
    rows = (
        session.execute(
            select(
                period.label("period"),
                func.count(Order.id).label("count"),
                func.sum(case((Order.tracking_no != "", 1), else_=0)).label("shipped"),
                func.coalesce(func.sum(Order.cost_amount), 0.0).label("cost"),
                func.coalesce(func.sum(Order.sale_amount), 0.0).label("sale"),
            )
            .where(*conds, col.is_not(None))
            .group_by(period)
            .order_by(period.desc())
            .limit(limit)
        )
        .all()
    )
    out: list[dict] = []
    for row in rows:
        count = int(row[1] or 0)
        cost = round(float(row[3] or 0.0), 2)
        sale = round(float(row[4] or 0.0), 2)
        out.append(
            {
                "period": str(row[0] or ""),
                "count": count,
                "shipped": int(row[2] or 0),
                "pending": max(count - int(row[2] or 0), 0),
                "cost": cost,
                "sale": sale,
                "avg_cost": round(cost / count, 2) if count else 0.0,
            }
        )
    return out


def mark_pushed(session: Session, order: Order, transfer_status: str | None = None) -> Order:
    """复制推单信息即视为已推单（已完成/已取消的订单不动）。

    推单时一并标记了「已转账」的话，这笔的应付结算流水同步核销。
    """
    if order.supplier_status not in ("done", "cancelled"):
        order.supplier_status = "pushed"
    if order.pushed_at is None:
        order.pushed_at = now()
    if transfer_status in TRANSFER_LABELS:
        if transfer_status == finance_service.TRANSFER_PAID:
            ok, _reason = can_pay_supplier(order)
            # 退单 / 撤单的单不再付款：只推单，转账状态保持未转账
            transfer_status = transfer_status if ok else "unpaid"
        apply_transfer(order, transfer_status)
    # 记下这次报给供应商的是「今日第 x 单」：撤回后单号会重排，靠它对照出变化
    order.push_index = supplier_daily_index(session, order)
    order.push_index_fixed = 0
    order.updated_at = now()
    # 订单状态：新建 -> 已推单（已发货 / 已撤单 / 已退单的不动）
    order_state_service.advance(session, order, order_state_service.ORDER_STATE_PUSHED)
    session.commit()
    # 推单时若标了「已转账」，应付流水跟着核销
    if transfer_status == "paid":
        finance_service.sync_transfer(session, order, commit=True)
    return order


def pending_orders(session: Session, limit: int = 50) -> list[Order]:
    """待推单发货的订单（按消息时间正序，先来的先推；撤回订单不参与）。"""
    return list(
        session.scalars(
            select(Order)
            .where(pending_push_order())
            .order_by(Order.msg_time.asc(), Order.id.asc())
            .limit(limit)
        ).all()
    )


def batch_mark_pushed(session: Session, orders: Iterable[Order]) -> int:
    count = 0
    for order in orders:
        mark_pushed(session, order)
        count += 1
    return count


def apply_tracking_from_wms(
    session: Session, order: Order, tracking_no: str, operator: str = ""
) -> dict:
    """渔集出库发货后**自动回填物流单号**（2026-10-15）。

    渔货栈的订单不走供应商推单 —— 货从渔集仓发。渔集出库核销填了运单号后，
    由这里把单号写回订单，与 /batches/tracking 手工回填走**同一套事实**：
    单号 / 物流公司 / 订单状态（已发货）/ 批次明细同步更新、群里 @ 订单发送人、
    语音播报。返回报告 dict 供渔集审计留痕；任何一步失败不抛异常。
    """
    from .notify import sync_tracking_to_group
    from . import logistics as logistics_service
    from . import voice as voice_service

    tracking_no = str(tracking_no or "").strip()
    if not tracking_no:
        return {"ok": False, "reason": "empty_tracking"}
    before = (order.tracking_no or "").strip()
    if before == tracking_no:
        return {"ok": True, "noop": True, "tracking_no": tracking_no}

    order.tracking_no = tracking_no
    # 物流公司按单号规则自动认（顺丰速运 / 中通快递 …），与手工回填同一口径
    order.tracking_company = logistics_service.guess_company(tracking_no)
    order_state_service.advance(session, order, order_state_service.ORDER_STATE_SHIPPED)
    # 批次明细的单号一起更新：批次详情 / 导出与订单保持一致
    from ..models import PushBatchItem

    for item in session.scalars(
        select(PushBatchItem).where(PushBatchItem.order_id == order.id)
    ).all():
        item.tracking_no = tracking_no

    from . import order_log as order_log_service

    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_TRACKING,
        detail=(
            f"渔集出库自动回填物流单号：{tracking_no}"
            + (f"（{order.tracking_company}）" if order.tracking_company else "")
        ),
        operator=operator or "渔集出库",
        commit=True,
    )
    voice_service.tracking_event(session, order)
    # 单号 / 状态 / 批次明细先落库 —— 群通知挂了也不能把回填吞掉
    session.commit()

    try:
        status, error = sync_tracking_to_group(order, session=session, is_update=bool(before))
        session.commit()
    except Exception:  # noqa: BLE001  通知失败只降级：单号已经保存了
        session.rollback()
        logger.warning("渔集回填单号后的群通知失败（order=%s）", order.order_no, exc_info=True)
        status, error = "failed", "群通知异常（单号已保存）"
    return {
        "ok": True,
        "tracking_no": tracking_no,
        "company": order.tracking_company,
        "notified": status,
        "notify_error": error,
    }
