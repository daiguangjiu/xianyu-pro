"""订单资金数据统计。"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import String, case, cast, func, select, text
from sqlalchemy.orm import Session

from ..models import (
    ORDER_STATE_CHOICES,
    ORDER_STATE_NEW,
    ORDER_STATE_PUSHED,
    ORDER_STATE_SHIPPED,
    SHOP_NATURE_STUDENT,
    Order,
    ProductMerge,
    PushBatch,
    RawMessage,
    awaiting_tracking,
    is_refund_order,
    not_recalled,
    pending_push_order,
)
from ..db import dialect_name
from ..timeutil import beijing_now, now

logger = logging.getLogger(__name__)


def margin(profit: float, sale: float) -> float:
    """毛利率（%）：销售额为 0 时返回 0，避免除零。"""
    if not sale:
        return 0.0
    return round(profit / sale * 100, 1)


def _day_expr(column):
    """取**北京时间那一天**的日期字符串（YYYY-MM-DD）。

    库里存 UTC（2026-10-03 时间口径），按天统计必须按北京自然日切，否则北京
    凌晨 0–8 点出的单会被算到前一天。

    两种库写法不同（以前统一写成 SQLite 那套，线上 PostgreSQL 直接报
    ``function date(timestamp without time zone, character varying) does not exist``
    → 首页 500）：
      · SQLite     → ``date(col, '+8 hours')`` 截前 10 位（不用 CAST AS DATE：
                      SQLite 没有真正的 DATE 类型，会退化成 NUMERIC 得到 0）
      · PostgreSQL → ``to_char(col + 8 小时, 'YYYY-MM-DD')``
    """
    if dialect_name() == "sqlite":
        return func.substr(cast(func.date(column, "+8 hours"), String), 1, 10)
    return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD")


# 公开别名：订单列表的「今日新增」筛选也按北京自然日切（2026-10-12 修：
# 那边以前直接截 UTC 日期，北京凌晨 0–8 点的单会被算到「昨天」而查不出来）
day_expr = _day_expr


def _profit_expr():
    return (
        Order.sale_amount - Order.cost_amount - Order.shipping_fee - Order.platform_fee
    )


def summary(session: Session) -> dict:
    """汇总统计：撤回订单只留档，不参与任何统计。"""
    active = not_recalled()
    total_orders = session.scalar(select(func.count(Order.id)).where(active)) or 0
    sale = (
        session.scalar(
            select(func.coalesce(func.sum(Order.sale_amount), 0.0)).where(active)
        )
        or 0.0
    )
    cost = (
        session.scalar(
            select(func.coalesce(func.sum(Order.cost_amount), 0.0)).where(active)
        )
        or 0.0
    )
    fee = session.scalar(
        select(
            func.coalesce(func.sum(Order.shipping_fee), 0.0)
            + func.coalesce(func.sum(Order.platform_fee), 0.0)
        ).where(active)
    ) or 0.0
    profit = round(float(sale) - float(cost) - float(fee), 2)
    # 退款金额（消费者退款）：退单的销售额不从总销售额里剔除，退出去的钱在这里单独汇总
    refund = (
        session.scalar(
            select(func.coalesce(func.sum(Order.refund_amount), 0.0)).where(active)
        )
        or 0.0
    )
    # 供应商退款：供应商退给我们的钱；与上面相减就是这一摊自己承担的差额
    supplier_refund = (
        session.scalar(
            select(func.coalesce(func.sum(Order.supplier_refund_amount), 0.0)).where(
                active
            )
        )
        or 0.0
    )
    # 待推单：还没推给供应商、且不是撤单 / 退单（逆向订单的推单状态已派生为「无需推单」）
    pending_push = (
        session.scalar(select(func.count(Order.id)).where(active, pending_push_order()))
        or 0
    )
    needs_review = (
        session.scalar(
            select(func.count(Order.id)).where(active, Order.needs_review.is_(True))
        )
        or 0
    )
    # 待供应商回单：已经推给供应商、但还没回填物流单号的订单数（跟进催单用）
    # 口径统一在 models.awaiting_tracking()：菜单徽标、推单页提醒与这里数出来的是同一个数
    pending_tracking = (
        session.scalar(select(func.count(Order.id)).where(active, awaiting_tracking())) or 0
    )
    # 今日口径：与「今日新增」一致，按「今天归档进系统的订单」算一套数字，
    # 页面卡片上「累计 + 今日」并排展示（运营当天要看的是今天进了多少、卖了多少）
    today = beijing_now().date()
    today_row = session.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.sale_amount), 0.0),
            func.coalesce(func.sum(Order.cost_amount), 0.0),
            func.coalesce(func.sum(Order.shipping_fee), 0.0)
            + func.coalesce(func.sum(Order.platform_fee), 0.0),
        ).where(active, _day_expr(Order.created_at) == today.isoformat())
    ).one()
    today_orders = int(today_row[0] or 0)
    today_sale = round(float(today_row[1] or 0.0), 2)
    today_cost = round(float(today_row[2] or 0.0), 2)
    today_fee = round(float(today_row[3] or 0.0), 2)
    today_profit = round(today_sale - today_cost - today_fee, 2)
    unparsed = (
        session.scalar(
            select(func.count(RawMessage.id)).where(RawMessage.parse_status == "pending")
        )
        or 0
    )
    # 成本没维护（=0）的订单数：页面顶部提醒推单人员补全，否则毛利虚高
    zero_cost = (
        session.scalar(
            select(func.count(Order.id)).where(active, Order.cost_amount <= 0)
        )
        or 0
    )
    # 已发货：订单状态走到「已发货」的单数（撤单 / 退单会改状态，天然不在这里）
    shipped = (
        session.scalar(
            select(func.count(Order.id)).where(
                active, Order.order_state == ORDER_STATE_SHIPPED
            )
        )
        or 0
    )
    # 待发货：已经推给供应商、但还没发货的单数（只算「已推单」）。
    # 「待推单」的单子连供应商都还没发过去，谈不上发货，不计入待发货（之前算进去了，数字虚高）。
    pending_ship = (
        session.scalar(
            select(func.count(Order.id)).where(
                active, Order.order_state == ORDER_STATE_PUSHED
            )
        )
        or 0
    )
    return {
        "total_orders": total_orders,
        "total_sale": round(float(sale), 2),
        "total_cost": round(float(cost), 2),
        "total_fee": round(float(fee), 2),
        "total_profit": profit,
        "total_refund": round(float(refund), 2),
        "total_supplier_refund": round(float(supplier_refund), 2),
        "avg_order": round(float(sale) / total_orders, 2) if total_orders else 0.0,
        "margin": round(profit / float(sale) * 100, 1) if sale else 0.0,
        "pending_push": pending_push,
        "pending_tracking": pending_tracking,
        "needs_review": needs_review,
        "today_orders": today_orders,
        # 今日口径（今天归档的订单）：销售额 / 采购成本 / 运费+手续费 / 毛利
        "today_sale": today_sale,
        "today_cost": today_cost,
        "today_fee": today_fee,
        "today_profit": today_profit,
        "unparsed_messages": unparsed,
        "zero_cost_orders": zero_cost,
        "shipped": shipped,
        # 待发货：新建 + 已推单（还没拿到物流单号的单子）
        "pending_ship": pending_ship,
    }


# 趋势图区间上限：一天一根柱子，跨度再长既看不清也拖页面（选更长的自动从末尾掐）
MAX_TREND_DAYS = 366


def _as_day(value) -> date | None:
    """把 ``"2026-10-01"`` / ``date`` / ``datetime`` 统一解析成 date；认不出返回 None。

    首页趋势图的起止日期来自地址栏（``?start=&end=``），乱填一律回落默认区间。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_range(start=None, end=None, days: int = 30) -> tuple[date, date]:
    """趋势图的时间区间（按**北京自然日**）：给了起止日期就用它，否则近 N 天。

    三条兜底：认不出的日期回落默认；起止写反了自动对调；跨度超过 ``MAX_TREND_DAYS`` 从末尾掐。
    """
    today = beijing_now().date()
    end_day = _as_day(end) or today
    start_day = _as_day(start) or (end_day - timedelta(days=days - 1))
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days > MAX_TREND_DAYS - 1:
        start_day = end_day - timedelta(days=MAX_TREND_DAYS - 1)
    return start_day, end_day


def _day_utc_bounds(day: date) -> datetime:
    """北京某日 00:00 对应的 UTC naive 时刻（库里存 UTC，按天统计要按北京自然日切）。"""
    return datetime.combine(day, time.min) - timedelta(hours=8)


def daily_trend(
    session: Session, days: int = 30, start=None, end=None
) -> list[dict]:
    """按天销售额趋势（默认近 N 天，也可给起止日期 —— 首页趋势图用它选时间段 / 按月）。

    每行：``date`` / ``count``（单数）/ ``qty``（件数）/ ``sale`` / ``cost`` / ``profit``
    / ``unit_price``（**当天件单价** = 当天销售额 ÷ 当天件数，件数按订单的 quantity 累加）。
    区间内没有订单的那天也会给一行 0，柱子才连得起来。
    """
    start_day, end_day = resolve_range(start, end, days)
    lo = _day_utc_bounds(start_day)
    hi = _day_utc_bounds(end_day + timedelta(days=1))
    rows = (
        session.execute(
            select(
                # 不用 CAST(x AS DATE)：SQLite 没有真正的 DATE 类型，会退化成 NUMERIC 转换并得到 0。
                # 这里统一截取日期字符串，SQLite 与 PostgreSQL 行为一致。
                _day_expr(Order.ordered_at).label("d"),
                func.count(Order.id).label("cnt"),
                func.coalesce(func.sum(Order.quantity), 0).label("qty"),
                func.coalesce(func.sum(Order.sale_amount), 0.0).label("sale"),
                func.coalesce(func.sum(Order.cost_amount), 0.0).label("cost"),
                func.coalesce(
                    func.sum(
                        Order.sale_amount - Order.cost_amount - Order.shipping_fee - Order.platform_fee
                    ),
                    0.0,
                ).label("profit"),
            )
            .where(Order.ordered_at.is_not(None))
            .where(Order.ordered_at >= lo)
            .where(Order.ordered_at < hi)
            .where(not_recalled())
            .group_by("d")
            .order_by("d")
        )
        .all()
    )
    data = {
        str(r.d)[:10]: {
            "count": int(r.cnt),
            "qty": int(r.qty or 0),
            "sale": round(float(r.sale), 2),
            "cost": round(float(r.cost), 2),
            "profit": round(float(r.profit), 2),
        }
        for r in rows
    }
    out = []
    for i in range((end_day - start_day).days + 1):
        d = (start_day + timedelta(days=i)).isoformat()
        row = data.get(d) or {"count": 0, "qty": 0, "sale": 0.0, "cost": 0.0, "profit": 0.0}
        qty = int(row["qty"])
        sale = round(float(row["sale"]), 2)
        out.append(
            {
                "date": d,
                "count": int(row["count"]),
                "qty": qty,
                "sale": sale,
                "cost": round(float(row["cost"]), 2),
                "profit": round(float(row["profit"]), 2),
                # 当天件单价：没有件数就是 0，页面显示「—」
                "unit_price": round(sale / qty, 2) if qty else 0.0,
            }
        )
    return out


def _is_refund_expr():
    """退单判定：统一走 models.is_refund_order()，别在这里另写一套口径。"""
    return is_refund_order()


def _group_stats(session: Session, column, key: str, limit: int = 20) -> list[dict]:
    stmt = (
        select(
            column,
            func.count(Order.id).label("cnt"),
            func.coalesce(func.sum(Order.sale_amount), 0.0).label("sale"),
            func.coalesce(func.sum(Order.cost_amount), 0.0).label("cost"),
            func.coalesce(
                func.sum(
                    Order.sale_amount - Order.cost_amount - Order.shipping_fee - Order.platform_fee
                ),
                0.0,
            ).label("profit"),
            # 成本为 0 的订单数（含成本字段没维护的情况）
            func.coalesce(
                func.sum(case((Order.cost_amount <= 0, 1), else_=0)), 0
            ).label("zc"),
            # 退单订单数：群里同步退单 / 已走退单流程的单子
            func.coalesce(
                func.sum(case((_is_refund_expr(), 1), else_=0)), 0
            ).label("rc"),
            # 退款金额：这些退单订单的销售额合计（要退给买家的钱）
            func.coalesce(
                func.sum(case((_is_refund_expr(), Order.sale_amount), else_=0.0)), 0.0
            ).label("ra"),
        )
        .where(not_recalled())
        .group_by(column)
        .order_by(func.count(Order.id).desc())
    )
    # limit <= 0 表示取全部分组（供应商维度要先合并同一家的多种写法再截断）
    if limit and limit > 0:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    return [
        {
            key: r[0] or "未指定",
            "count": int(r.cnt),
            "sale": round(float(r.sale), 2),
            "cost": round(float(r.cost), 2),
            "profit": round(float(r.profit), 2),
            # 毛利率：毛利 / 销售额
            "margin": margin(float(r.profit), float(r.sale)),
            # 该维度里成本没维护（=0）的订单数，用于页面提示
            "zero_cost": int(r.zc or 0),
            # 退单单数与对应退款金额（销售额口径）
            "refund_count": int(r.rc or 0),
            "refund_amount": round(float(r.ra or 0.0), 2),
        }
        for r in rows
    ]


def by_supplier(session: Session, limit: int = 20) -> list[dict]:
    """供应商（发货方）维度。

    订单里的「所属供应链」可能写的是编码、名称或「名称（编码）」，先按档案把同一家
    合并成一行（显示为「名称（编码）」），再按订单数取前 N 名。
    """
    from .supplier import build_index  # 延迟导入，避免与 supplier 服务循环依赖

    index = build_index(session)
    merged: dict[str, dict] = {}
    for row in _group_stats(session, Order.supplier_name, "supplier", limit=0):
        key = index.group_key(row["supplier"]) or "未指定"
        item = merged.setdefault(
            key,
            {"supplier": key, "count": 0, "sale": 0.0, "cost": 0.0, "profit": 0.0,
             "margin": 0.0, "zero_cost": 0, "refund_count": 0, "refund_amount": 0.0},
        )
        item["count"] += row["count"]
        item["sale"] = round(item["sale"] + row["sale"], 2)
        item["cost"] = round(item["cost"] + row["cost"], 2)
        item["profit"] = round(item["profit"] + row["profit"], 2)
        item["zero_cost"] += row["zero_cost"]
        item["refund_count"] += row.get("refund_count", 0)
        item["refund_amount"] = round(
            item["refund_amount"] + row.get("refund_amount", 0.0), 2
        )
    for item in merged.values():
        item["margin"] = margin(item["profit"], item["sale"])
    rows = sorted(merged.values(), key=lambda r: r["count"], reverse=True)
    return rows[:limit] if limit and limit > 0 else rows


def by_account(session: Session, limit: int = 20) -> list[dict]:
    """出货闲鱼号维度。"""
    return _group_stats(session, Order.xianyu_account, "account", limit)


def by_payee(session: Session, limit: int = 20) -> list[dict]:
    """回款支付宝归属人维度（资金归属）。"""
    return _group_stats(session, Order.payee_name, "payee", limit)


def top_products(session: Session, limit: int = 10, merged: bool = True) -> list[dict]:
    rows = (
        session.execute(
            select(
                Order.product_name,
                func.count(Order.id).label("cnt"),
                func.coalesce(func.sum(Order.quantity), 0).label("qty"),
                func.coalesce(func.sum(Order.sale_amount), 0.0).label("sale"),
                func.coalesce(func.sum(Order.cost_amount), 0.0).label("cost"),
                func.coalesce(
                    func.sum(
                        Order.sale_amount - Order.cost_amount - Order.shipping_fee - Order.platform_fee
                    ),
                    0.0,
                ).label("profit"),
                func.coalesce(
                    func.sum(case((Order.cost_amount <= 0, 1), else_=0)), 0
                ).label("zc"),
                func.coalesce(
                    func.sum(case((_is_refund_expr(), 1), else_=0)), 0
                ).label("rc"),
                func.coalesce(
                    func.sum(case((_is_refund_expr(), Order.sale_amount), else_=0.0)),
                    0.0,
                ).label("ra"),
            )
            .where(Order.product_name != "")
            .where(not_recalled())
            .group_by(Order.product_name)
            .order_by(func.count(Order.id).desc())
        )
        .all()
    )
    raw = [
        {
            "product": r[0],
            "count": int(r.cnt),
            "quantity": int(r.qty or 0),
            "sale": round(float(r.sale), 2),
            "cost": round(float(r.cost), 2),
            "profit": round(float(r.profit), 2),
            "margin": margin(float(r.profit), float(r.sale)),
            "zero_cost": int(r.zc or 0),
            "refund_count": int(r.rc or 0),
            "refund_amount": round(float(r.ra or 0.0), 2),
            # 合并后这一行包含哪些原始商品名（没合并时就是自己）
            "members": [r[0]],
        }
        for r in rows
    ]
    if not merged:
        return raw[:limit] if limit and limit > 0 else raw
    # 按合并规则把取名不一致的同一个商品归到一起（只影响统计，不动原始订单）
    alias = product_merge_map(session)
    groups: dict[str, dict] = {}
    for item in raw:
        key = alias.get(item["product"]) or item["product"]
        group = groups.setdefault(
            key,
            {
                "product": key,
                "count": 0,
                "quantity": 0,
                "sale": 0.0,
                "cost": 0.0,
                "profit": 0.0,
                "margin": 0.0,
                "zero_cost": 0,
                "refund_count": 0,
                "refund_amount": 0.0,
                "members": [],
            },
        )
        group["count"] += item["count"]
        group["quantity"] += item["quantity"]
        group["sale"] = round(group["sale"] + item["sale"], 2)
        group["cost"] = round(group["cost"] + item["cost"], 2)
        group["profit"] = round(group["profit"] + item["profit"], 2)
        group["zero_cost"] += item["zero_cost"]
        group["refund_count"] += item["refund_count"]
        group["refund_amount"] = round(group["refund_amount"] + item["refund_amount"], 2)
        group["members"].extend(item["members"])
    for group in groups.values():
        group["margin"] = margin(group["profit"], group["sale"])
    rows = sorted(groups.values(), key=lambda r: r["count"], reverse=True)
    return rows[:limit] if limit and limit > 0 else rows


def state_counts(session: Session) -> dict[str, int]:
    """订单状态（新建 / 已推单 / 已发货 / 已撤单 / 已退单）各自的订单数。

    已撤单的订单同时带撤回标记（不参与统计口径），这里按状态原样统计，
    便于在列表 / 看板里一眼看到撤了多少单。
    """
    return {
        s: (
            session.scalar(select(func.count(Order.id)).where(Order.order_state == s))
            or 0
        )
        for s in ORDER_STATE_CHOICES
    }


def nature_counts(session: Session) -> dict[str, int]:
    """订单性质（自营订单 / 学员订单）各自的订单数：按**出货闲鱼号对应的店铺性质**归。

    口径与订单列表的「订单性质」筛选项完全一致：学员店铺名单来自店铺档案，
    没有对应店铺档案（含空账号）的都按「自营」算。撤回订单不参与统计。
    """
    from ..models import Shop  # 局部 import：避免与服务层互相牵制

    names = [
        str(n)
        for n in session.scalars(
            select(Shop.name).where(Shop.name != "", Shop.shop_nature == SHOP_NATURE_STUDENT)
        ).all()
        if (n or "").strip()
    ]
    base = not_recalled()
    total = session.scalar(select(func.count(Order.id)).where(base)) or 0
    student = (
        session.scalar(
            select(func.count(Order.id)).where(base, Order.xianyu_account.in_(names))
        )
        or 0
        if names
        else 0
    )
    return {"self": max(total - student, 0), "student": student, "total": total}


def status_counts(session: Session) -> dict:
    def _count(col, value):
        return (
            session.scalar(
                select(func.count(Order.id)).where(not_recalled(), col == value)
            )
            or 0
        )

    return {
        "supplier": {
            s: _count(Order.supplier_status, s) for s in ("pending", "pushed", "done", "cancelled")
        },
        "batches": {
            s: session.scalar(select(func.count(PushBatch.id)).where(PushBatch.status == s)) or 0
            for s in ("draft", "pushed", "done", "cancelled")
        },
    }


def sparkline(values: Iterable[float]) -> list[float]:
    vals = list(values)
    if not vals:
        return []
    mx = max(vals) or 1.0
    return [round(v / mx * 100, 2) for v in vals]


# ------------------------------------------------------------------ 商品合并（仅统计口径）
# 商品名里常见的噪音：空白与各类符号（不同人写法不一样）
_NOISE_RE = re.compile(r"[\s\-_/\\（）()【】\[\]{}<>、,，.。:：;；*+~|]+")
# 数字：容量 / 型号差异（如 10000 毫安 与 20000 毫安 常被当成两个商品）
_DIGIT_RE = re.compile(r"\d+")


def product_stem(name: str) -> str:
    """商品名归一化后的主干：去掉空白、符号与数字，用于判断「是不是同一个商品」。"""
    s = _NOISE_RE.sub("", name or "")
    s = _DIGIT_RE.sub("", s)
    return s[:8]


def _direct_product_merges(session: Session) -> dict[str, str]:
    """一层合并规则：被合并的名字 -> 目标展示名（不解析链条）。"""
    rows = session.execute(select(ProductMerge)).scalars().all()
    return {r.member: r.canonical for r in rows if r.member and r.canonical}


def _merge_final(name: str, direct: dict[str, str]) -> str:
    """顺着合并链一路解析到终点（A->B、B->C 时 A 最终也算 C）。

    成环（A->B、B->A）时停在原地，保证统计不会死循环。
    """
    seen = {name}
    current = name
    while True:
        nxt = direct.get(current)
        if not nxt or nxt in seen:
            return current
        seen.add(nxt)
        current = nxt


def _would_loop(member: str, canonical: str, direct: dict[str, str]) -> bool:
    """新加一条 member->canonical 会不会绕回 member（A->B 同时 B->A）。

    从目标名 canonical 顺着已有规则往下走，能走到 member 就是环。
    """
    seen = {canonical}
    current = canonical
    while True:
        if current == member:
            return True
        nxt = direct.get(current)
        if not nxt or nxt in seen:
            return False
        seen.add(nxt)
        current = nxt


def product_merge_map(session: Session) -> dict[str, str]:
    """原始商品名 -> 最终展示名。

    支持反复合并：先把几个写法并成一组 A，之后把 A 和新的雷同商品再并成 C，
    这里的 A 会一路解析到 C，统计口径始终跟着最终那一组。
    """
    direct = _direct_product_merges(session)
    return {name: _merge_final(name, direct) for name in direct}


def list_product_merges(session: Session) -> list[dict]:
    """已配置的合并规则（按合并后名称分组）。

    每个成员还带出它下面最终的原始商品名：运营反复合并时，中间那个组名会显示成
    「（含 N 个写法）」，一眼能看出这一组里到底并了哪些产品。
    """
    direct = _direct_product_merges(session)
    children: dict[str, list[str]] = {}
    for member, canonical in direct.items():
        children.setdefault(canonical, []).append(member)

    def leaves(name: str, seen: frozenset = frozenset()) -> list[str]:
        kids = children.get(name) or []
        if not kids or name in seen:
            return [name]
        out: list[str] = []
        for kid in sorted(kids):
            out.extend(leaves(kid, seen | {name}))
        return out

    grouped: dict[str, list[dict]] = {}
    for member, canonical in direct.items():
        grouped.setdefault(canonical, []).append(
            {
                "member": member,
                "leaves": leaves(member),
                "nested": member in children,  # 自己也是个合并组（反复合并出来的中间层）
            }
        )
    return [
        {
            "canonical": canonical,
            "members": [d["member"] for d in items],
            "details": sorted(items, key=lambda d: d["member"]),
        }
        for canonical, items in sorted(grouped.items())
    ]


def product_merge_leaf_count(session: Session, canonical: str) -> int:
    """某个合并组最终包含多少个原始商品写法（把子组展开后计数）。"""
    for row in list_product_merges(session):
        if row["canonical"] == canonical:
            return sum(len(d["leaves"]) for d in row["details"])
    return 0


def save_product_merge(
    session: Session, canonical: str, members: Iterable[str], actor: str = ""
) -> int:
    """把多个商品名合并到同一个展示名（幂等：已存在的规则直接改目标名）。

    被勾选的名字可能已经是某个合并组（运营把已合并的行再并进新组），
    这里只记一条「组名 -> 新展示名」的规则，统计时顺着链一路归并到新名字；
    会造成互相指（A->B 同时 B->A）的规则直接跳过，避免统计里绕圈。
    """
    canonical = (canonical or "").strip()
    if not canonical:
        return 0
    rows: dict[str, ProductMerge] = {
        r.member: r
        for r in session.execute(select(ProductMerge)).scalars().all()
        if r.member
    }
    direct = {m: r.canonical for m, r in rows.items() if r.canonical}
    count = 0
    for member in members:
        member = (member or "").strip()
        if not member or member == canonical:
            continue
        if _would_loop(member, canonical, direct):
            logger.warning("跳过会造成循环的合并规则：%s -> %s", member, canonical)
            continue
        row = rows.get(member)
        if row:
            row.canonical = canonical
            row.created_by = actor
        else:
            row = ProductMerge(canonical=canonical, member=member, created_by=actor)
            session.add(row)
            rows[member] = row
        direct[member] = canonical
        count += 1
    session.commit()
    return count


def delete_product_merge(session: Session, member: str) -> bool:
    """取消某个商品名的合并（恢复按原名单独统计）。"""
    row = session.scalar(select(ProductMerge).where(ProductMerge.member == member))
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


def suggest_product_merges(session: Session, limit: int = 12) -> list[dict]:
    """推荐合并：归一化后主干相同、但写法不同的商品名（取名不一致导致的重复）。

    已经配置过合并规则的商品不再推荐，避免重复打扰。
    """
    alias = product_merge_map(session)
    buckets: dict[str, list[dict]] = {}
    for row in top_products(session, limit=0, merged=False):
        name = row["product"]
        if name in alias:
            continue
        stem = product_stem(name)
        if len(stem) < 3:
            continue
        buckets.setdefault(stem, []).append(row)
    out: list[dict] = []
    for stem, items in buckets.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda r: r["count"], reverse=True)
        out.append(
            {
                "stem": stem,
                # 默认合并到单量最多的那个名字上
                "canonical": items[0]["product"],
                "names": [i["product"] for i in items],
                "count": sum(i["count"] for i in items),
                "sale": round(sum(i["sale"] for i in items), 2),
            }
        )
    out.sort(key=lambda r: r["count"], reverse=True)
    return out[:limit]
