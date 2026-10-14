"""资金板块：公司与订单发起人、公司与上游供应商之间的结算。

业务口径（2026-09）：
  1 公司自营应收：销售金额（买家实付，有成交价取成交价，否则取售价）先扣掉平台技术服务费
    （0.6%，平台结算给公司时直接扣），余下金额再跟订单发起人结算；
  2 公司自营应付（运营应付）：采购成本 + 运费，需要结算给上游供应商；
  3 应收 - 应付（技术服务费已在应收里扣过）= 这一单的预计毛利 = 这一单的预计毛利（页面上同步展示，便于对账）；
  4 撤单 / 已退单的订单不再结算：对应流水自动置为「已作废」，只留档；
    （「已发货无法拦截」的退单未受理，订单照常结算）
  5 流水金额跟着订单走：订单金额被修正后自动同步；已结算的流水只更新金额、不回退状态；
  6 订单状态里的「已转账」= 运营已经把采购款付给上游供应商 = 应付已结清，
    所以这笔订单的「公司自营应付」流水自动核销为已结算（结算人标「转账自动结算」），
    结算时间取订单标记「已转账」的时刻；改回未转账时只回退这类自动核销，人工核销的不动；
  7 支持按笔 / 按天 / 按月聚合，并可按单笔、按期间一键标记「已结算」。

毛利口径（2026-10-03 定稿，**全站唯一实现**就在本文件的 ``order_amounts``）：
    毛利 = 销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费
  · 成本价 = 自营飞书群同步的成本价（order.cost_amount）；
  · 订单上「退款给客户」存**负值**（钱出去）、「供应商退款」存**正值**（钱进来），
    所以公式里是直接相加；非退单订单两项都是 0，自动退化成「销售价 − 运费 − 成本价 − 手续费」；
  · 页面上的毛利（订单列表 / 详情 / 资金页 / 渔货栈订单页）一律取这里的 ``profit``。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    ORDER_STATE_COMPLETED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_SHIPPED,
    REFUND_SETTLED_STATUSES,
    SETTLEMENT_DIRECTIONS,
    SETTLEMENT_INITIATOR,
    SETTLEMENT_KIND_LABELS,
    SETTLEMENT_PENDING,
    SETTLEMENT_REFUND,
    SETTLEMENT_SETTLED,
    SETTLEMENT_SUPPLIER,
    SETTLEMENT_VOID,
    SERVICE_FEE_RATE_DEFAULT,
    Order,
    Settlement,
)
from ..timeutil import BEIJING_ZONE, beijing_now, now, to_local

logger = logging.getLogger(__name__)

# 平台技术服务费：平台结算给公司（店主）时按销售金额扣除的比例，0.6%
# 这是**兜底默认**（店铺没单独设费率时用，见 Shop.service_fee_rate）。
# 现在订单上会落实际金额（order.platform_fee），有金额就直接用，不再依赖这个比例现算。
TECH_SERVICE_FEE_RATE = SERVICE_FEE_RATE_DEFAULT / 100.0

# 「已转账」表示运营已经把采购款付给上游供应商 = 应付已结清：应付流水自动核销，
# 结算人记这个标记，和人工在资金页点的核销区分开；改回未转账时只回退这类自动核销。
TRANSFER_PAID = "paid"
TRANSFER_AUTO_SETTLED_BY = "转账自动结算"
# 退款流水：订单上记了退款时间 = 钱已经退给买家，这条自动核销，结算人标它（与转账自动结算分开，便于追溯）
REFUND_AUTO_SETTLED_BY = "退款自动核销"


# ------------------------------------------------------------------ 金额口径
def service_fee_rate_for(order: Order, shop=None) -> float:
    """这笔订单适用的平台技术服务费率（%，如 0.6 = 0.6%）。

    取自出货店铺（订单的「出货闲鱼号」= 店铺名）：店铺没建档、或没单独设费率，
    就用默认费率 SERVICE_FEE_RATE_DEFAULT（0.6%）。
    """
    if shop is not None:
        return shop.service_fee_rate_value
    return SERVICE_FEE_RATE_DEFAULT


def order_amounts(
    order: Order, *, rate_percent: float | None = None, shop=None, session=None
) -> dict:
    """一笔订单的资金口径（公司自营应收 / 公司自营应付 / 预计毛利）。

    应收 = 销售金额（买家实付）− 平台技术服务费：平台把钱结算给公司时先扣这一笔，
    所以真正能收到的钱要先把它扣掉。应付 = 采购成本 + 运费（给上游供应商）。

    平台技术服务费就是订单上的 ``platform_fee``（一个东西，不再另算一份）：
      · 订单上记了金额（自动算的 / 人工改的）就以它为准；
      · 没记（老数据）就按费率现算：成交价 × 费率（默认 0.6%，店铺可单独设）。
    毛利按定稿公式算（销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费）；
    发生退单时它会比「应收 − 应付」多退少补，这是对的 —— 退款本来就改变这一单的真实赚亏。

    费率怎么取（2026-10-03）：给了 shop 用它；没给 shop 但给了 session，就按「出货闲鱼号」
    查店铺档案取店铺自己的费率 —— 不查的话所有店铺都按 0.6% 兜底，费率设了等于没设。
    """
    paid = float(order.deal_amount or 0) or float(order.sale_amount or 0)
    cost = float(order.cost_amount or 0)
    shipping = float(order.shipping_fee or 0)
    platform_fee = float(order.platform_fee or 0)
    # 退款两项：给客户的存负值、供应商退我们的存正值（2026-10-03 符号口径）
    refund = float(order.refund_amount or 0)
    supplier_refund = float(order.supplier_refund_amount or 0)
    if shop is None and session is not None and rate_percent is None:
        shop = _shop_of(session, order)
    rate = rate_percent if rate_percent is not None else service_fee_rate_for(order, shop)
    tech_fee = platform_fee if platform_fee > 0 else round(paid * (rate or 0) / 100.0, 2)
    receivable = round(paid - tech_fee, 2)
    payable = round(cost + shipping, 2)
    # 定稿公式：销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费
    profit = round(
        paid - shipping + refund - cost + supplier_refund - tech_fee, 2
    )
    return {
        "paid": round(paid, 2),                      # 销售金额（买家实付）
        "tech_fee": round(tech_fee, 2),              # 平台技术服务费（= 订单 platform_fee）
        "receivable": receivable,                    # 公司自营应收 = 销售金额 - 技术服务费
        "cost": round(cost, 2),
        "shipping": round(shipping, 2),
        "platform_fee": round(tech_fee, 2),          # 与 tech_fee 同一个数：就这一笔费用
        "supplier_payable": payable,                 # 公司自营应付（上游供应商）
        "rate_percent": rate or 0.0,                 # 这笔单用的费率（%，页面可显示）
        "refund": round(refund, 2),                  # 退款给客户（负值：钱出去）
        "supplier_refund": round(supplier_refund, 2),  # 供应商退款（正值：钱进来）
        "profit": profit,                            # 毛利（定稿公式，全站唯一实现）
    }


def sync_service_fee(
    session: Session, order: Order, *, shop=None, force: bool = False
) -> float:
    """把这一单的平台技术服务费金额落到订单上（order.platform_fee）。

    只有成交价 > 0 才落。默认（force=False）沿用「人工优先」：
      · 订单上标了「人工填的」（platform_fee_manual）→ 不动；
      · 否则按当前售价 × 店铺费率重算（force=True 时连人工值也重算）。

    改售价必须重算（2026-10-03）：以前「订单上有值就不动」，导致售价从 100 改成 200、
    手续费还停在 0.60 —— 毛利跟着算错。现在按上面的规则自动跟着售价走。
    返回落下去的金额（没落返回 0）。
    """
    paid = float(order.deal_amount or 0) or float(order.sale_amount or 0)
    if paid <= 0:
        return 0.0
    # 人工填过的不动（force=True 才覆盖）；系统算的一律按当前售价重算
    if not force and order.platform_fee_manual:
        return 0.0
    shop = shop if shop is not None else _shop_of(session, order)
    fee = round(paid * service_fee_rate_for(order, shop) / 100.0, 2)
    order.platform_fee = fee
    order.platform_fee_manual = False
    return fee


def _shop_of(session: Session, order: Order):
    """按「出货闲鱼号」找店铺档案（找不到返回 None，费率走默认）。"""
    name = (order.xianyu_account or "").strip()
    if not name:
        return None
    try:
        from .shop import find_by_name

        return find_by_name(session, name)
    except Exception as exc:  # noqa: BLE001  店铺表不可用时按默认费率算，别把资金页打挂
        logger.warning("取店铺费率失败（按默认 %s%% 算）：%s", SERVICE_FEE_RATE_DEFAULT, exc)
        return None


def backfill_service_fee(session: Session, commit: bool = True) -> int:
    """老数据补技术服务费：成交价 > 0 但订单上还没记金额的单，按店铺费率（默认 0.6%）补上。

    历史订单的技术服务费本来只在资金页现算、订单上没落金额（2026-10-02 前 107 单全是 0），
    订单列表新增「平台技术服务费」列后要看得见这笔钱，所以启动时补一遍。
    已经记了金额的单（人工改过）一律不动。
    """
    rows = session.scalars(
        select(Order).where(
            and_(
                Order.platform_fee.is_(None) | (Order.platform_fee == 0),
                or_(Order.deal_amount > 0, Order.sale_amount > 0),
            )
        )
    ).all()
    filled = 0
    for order in rows:
        if sync_service_fee(session, order):
            filled += 1
    if filled and commit:
        session.commit()
    return filled


def amount_of(order: Order, kind: str, *, session=None) -> float:
    """某一类结算的金额。

    应收 = 销售金额 − 平台技术服务费；应付 = 成本 + 运费；退款 = 订单上记的退款金额。

    退款流水金额取**绝对值**：订单上 refund_amount 按符号口径存的是负值（钱出去），
    流水本身的「出去」由 direction=out 表达，金额字段仍记正数。
    """
    amounts = order_amounts(order, session=session)
    if kind == SETTLEMENT_SUPPLIER:
        return amounts["supplier_payable"]
    if kind == SETTLEMENT_REFUND:
        return round(abs(float(order.refund_amount or 0)), 2)
    return amounts["receivable"]


def counterparty_of(order: Order, kind: str) -> str:
    """结算对象：应收取订单发起人（订单发送人），应付取供应商名称，退款取买家。"""
    if kind == SETTLEMENT_SUPPLIER:
        return (order.supplier_name or "").strip() or "未填供应商"
    if kind == SETTLEMENT_REFUND:
        return (
            (order.receiver_name or "").strip()
            or (order.buyer_nick or "").strip()
            or "买家"
        )
    return (order.sender_name or "").strip() or (order.sender_id or "").strip() or "未指定发起人"


def biz_day_of(order: Order) -> str:
    """业务日 / 业务月：按订单出单时间（没有就退回消息时间 / 归档时间）。

    库里的时间存的是 UTC（2026-10-03 时间口径），业务日按**北京时间**算 ——
    北京凌晨 0–8 点出的单算当天，不然账单日会整体错一天。
    """
    when = order.ordered_at or order.msg_time or order.created_at
    if isinstance(when, datetime):
        local = to_local(when, BEIJING_ZONE) or when
        return local.strftime("%Y-%m-%d")
    return beijing_now().strftime("%Y-%m-%d")


def is_void(order: Order) -> bool:
    """这笔订单还该不该结算：撤单 / 已受理的退单都不再结算。

    「已发货无法拦截」（refund_status=shipped）表示退单请求未受理，订单照常结算。
    """
    if order.is_recalled:
        return True
    status = (order.refund_status or "").strip()
    if status and status != "shipped":
        return True
    return (order.order_state or "") == ORDER_STATE_REFUNDED


# 结算时机（2026-10-03）：正向补了「已完成」终态之后，才分得清在途与可结
SETTLE_TRANSIT = "transit"   # 在途：已发货、买家还没签收，建议先不结
SETTLE_READY = "ready"       # 可结：已完成（签收 / 超时自动确认收货）
SETTLE_NONE = ""             # 不适用：还没发货，或已撤单 / 退单
SETTLE_STATE_LABELS = {
    SETTLE_TRANSIT: "在途",
    SETTLE_READY: "可结",
    SETTLE_NONE: "",
}


def settlement_readiness(order: Order) -> str:
    """这笔单现在该不该结：在途 / 可结 / 不适用。

    · 可结 = 已完成（买家签收或超时自动确认收货），钱已经落袋；
    · 在途 = 已发货但还没签收，货在路上，这时候结给发起人风险在自己；
    · 不适用 = 还没发货（谈不上结），或已撤单 / 已退单（is_void 会作废流水）。
    """
    if is_void(order):
        return SETTLE_NONE
    if (order.order_state or "") == ORDER_STATE_COMPLETED or order.completed_at is not None:
        return SETTLE_READY
    if (order.order_state or "") == ORDER_STATE_SHIPPED or (order.tracking_no or "").strip():
        return SETTLE_TRANSIT
    return SETTLE_NONE


# ------------------------------------------------------------------ 流水同步
def sync_transfer(session: Session, order: Order, actor: str = "", commit: bool = False) -> int:
    """订单「已转账」= 钱已经付给供应商：把这笔订单的「公司自营应付」流水核销掉。

    业务口径（2026-09）：
      - 订单状态里的「已转账」表示运营已经把采购款付给上游供应商，等价于应付已结清，
        所以应付流水跟着变成「已结算」，结算人标记为 TRANSFER_AUTO_SETTLED_BY，
        和人工在资金页点的核销区分开；
      - 结算时间：取订单标记「已转账」的那个时刻（order.transfer_paid_at），
        不是跑同步的时间；拿不到（老数据没记）才退化成当前时间；
      - 改回「未转账」：只把当初由「已转账」自动核销的那条退回待结算，
        人工核销的流水不动（避免误撤销别人手上的操作）；
      - 已作废的流水（撤单 / 退单）不参与：这样订单本来就不结算了。
    返回变动条数。
    """
    if order.id is None:
        session.flush()
    row = session.scalar(
        select(Settlement).where(
            Settlement.order_id == order.id, Settlement.kind == SETTLEMENT_SUPPLIER
        )
    )
    if row is None or row.status == SETTLEMENT_VOID:
        return 0
    paid = (order.transfer_status or "").strip() == TRANSFER_PAID
    # 结算时间 = 订单标记「已转账」的时刻（老数据没记这个时间才退化成当前时间）
    ts = getattr(order, "transfer_paid_at", None) or now()
    if paid:
        if row.status != SETTLEMENT_PENDING:
            return 0
        row.status = SETTLEMENT_SETTLED
        row.settled_at = ts
        row.settled_by = actor or TRANSFER_AUTO_SETTLED_BY
        row.updated_at = ts
        changed = 1
    else:
        # 只回退「转账自动」核销的，人工点结算的不动
        if row.status != SETTLEMENT_SETTLED or row.settled_by != TRANSFER_AUTO_SETTLED_BY:
            return 0
        row.status = SETTLEMENT_PENDING
        row.settled_at = None
        row.settled_by = ""
        row.updated_at = ts
        changed = 1
    if changed and commit:
        session.commit()
    return changed


def sync_refund(session: Session, order: Order, commit: bool = False) -> Settlement | None:
    """订单的退款流水：退单受理后要退给买家的钱（refund_amount）。

    与应收 / 应付正好相反 —— 那两条在退单时作废，这条是退单才出现的；撤单（没受理退单）
    不生成。状态跟着订单走：订单落到「已退款 / 已关闭」= 钱已经退给买家，自动核销为
    「已结算」（结算人标 REFUND_AUTO_SETTLED_BY），否则挂在「待结算」等线下退完再点。
    改回未退款时，只回退自动核销的那条（人工点过结算的不动，与转账那套口径一致）。
    """
    if order.id is None:
        session.flush()
    row = session.scalar(
        select(Settlement).where(
            Settlement.order_id == order.id, Settlement.kind == SETTLEMENT_REFUND
        )
    )
    # 订单上 refund_amount 存负值（钱出去），流水金额仍记正数（方向由 direction=out 表达）
    amount = round(abs(float(order.refund_amount or 0)), 2)
    active = (not order.is_recalled) and order.is_refund and amount > 0
    if not active:
        if row is not None and row.status != SETTLEMENT_VOID:
            row.status = SETTLEMENT_VOID
            row.note = "未发生退款（撤单 / 退单未受理 / 未填退款金额），不结算"
            row.updated_at = now()
            if commit:
                session.commit()
        return row

    # 「钱已退给买家」的判据是处理状态（已退款 / 已关闭），不是 refund_at ——
    # refund_at 记的是退单流程开始时间，受理时就有值，拿它当退款时间会把「还没退」记成「已退」。
    # 结算时间优先取 refund_paid_at（实际退款时间），老数据没有才退回退单时间。
    settled = (order.refund_status or "").strip() in REFUND_SETTLED_STATUSES
    settled_at = order.refund_paid_at or order.refund_at
    day = biz_day_of(order)
    if row is None:
        row = Settlement(
            kind=SETTLEMENT_REFUND,
            order_id=order.id,
            order_no=order.order_no or "",
            counterparty=counterparty_of(order, SETTLEMENT_REFUND),
            direction=SETTLEMENT_DIRECTIONS[SETTLEMENT_REFUND],
            amount=amount,
            biz_day=day,
            biz_month=day[:7],
            status=SETTLEMENT_SETTLED if settled else SETTLEMENT_PENDING,
            settled_at=settled_at if settled else None,
            settled_by=REFUND_AUTO_SETTLED_BY if settled else "",
            note="退单：退款给买家",
        )
        session.add(row)
    else:
        row.order_no = order.order_no or row.order_no
        row.counterparty = counterparty_of(order, SETTLEMENT_REFUND)
        row.amount = amount
        row.biz_day = day
        row.biz_month = day[:7]
        row.note = "退单：退款给买家"
        if settled and row.status != SETTLEMENT_SETTLED:
            # 订单落到「已退款 / 已关闭」= 钱已经退给买家：自动核销（与「已转账自动结算」同一路子）
            row.status = SETTLEMENT_SETTLED
            row.settled_at = settled_at
            row.settled_by = row.settled_by or REFUND_AUTO_SETTLED_BY
        elif (
            not settled
            and row.status == SETTLEMENT_SETTLED
            and row.settled_by == REFUND_AUTO_SETTLED_BY
        ):
            row.status = SETTLEMENT_PENDING
            row.settled_at = None
            row.settled_by = ""
    row.updated_at = now()
    if commit:
        session.commit()
    return row


def sync_order(session: Session, order: Order, commit: bool = False) -> list[Settlement]:
    """按订单事实生成 / 更新结算流水（公司自营应收、应付，以及退单的退款）。

    撤单 / 退单 -> 应收应付置为已作废；已结算的流水只更新金额与对象，不回退状态；
    订单「已转账」-> 应付流水自动核销（见 sync_transfer）；
    退单受理 -> 额外生成一条退款流水给买家（见 sync_refund）。
    """
    if order.id is None:
        session.flush()
    # 先确保订单上落了平台技术服务费金额（老数据没落过），流水才跟订单对得上
    sync_service_fee(session, order)
    void = is_void(order)
    rows: list[Settlement] = []
    # 只走应收 / 应付两条：退款那条方向相反（退单才出现），交给 sync_refund 专门维护
    for kind in (SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER):
        row = session.scalar(
            select(Settlement).where(
                Settlement.order_id == order.id, Settlement.kind == kind
            )
        )
        amount = amount_of(order, kind, session=session)
        counterparty = counterparty_of(order, kind)
        if row is None:
            row = Settlement(
                kind=kind,
                order_id=order.id,
                order_no=order.order_no or "",
                counterparty=counterparty,
                direction=SETTLEMENT_DIRECTIONS[kind],
                amount=amount,
                biz_day=biz_day_of(order),
                biz_month=biz_day_of(order)[:7],
                status=SETTLEMENT_VOID if void else SETTLEMENT_PENDING,
                note="撤单/退单，不再结算" if void else "",
            )
            session.add(row)
        else:
            row.order_no = order.order_no or row.order_no
            row.counterparty = counterparty
            row.amount = amount
            row.biz_day = biz_day_of(order)
            row.biz_month = row.biz_day[:7]
            if void and row.status != SETTLEMENT_VOID:
                row.status = SETTLEMENT_VOID
                row.note = "撤单/退单，不再结算"
            elif not void and row.status == SETTLEMENT_VOID:
                # 撤销撤回 / 退单取消：重新参与结算
                row.status = SETTLEMENT_PENDING
                row.note = ""
                row.settled_at = None
                row.settled_by = ""
        row.updated_at = now()
        rows.append(row)
    # 订单「已转账」= 钱已经付给供应商：应付流水跟着核销 / 回退
    sync_transfer(session, order)
    # 退款给买家的钱：退单受理且记了退款金额才有，退款时间落了就自动核销
    refund_row = sync_refund(session, order)
    if refund_row is not None:
        rows.append(refund_row)
    if commit:
        session.commit()
    return rows


def sync_all(session: Session, commit: bool = True) -> int:
    """按现有订单补全 / 校正所有结算流水（启动时跑一次，历史订单也能有账）。"""
    orders = session.scalars(select(Order).order_by(Order.id.asc())).all()
    for order in orders:
        sync_order(session, order)
    if commit:
        session.commit()
    logger.info("结算流水已同步：%s 笔订单", len(orders))
    return len(orders)


def void_order(session: Session, order: Order, commit: bool = False) -> int:
    """订单撤单 / 退单：把它的结算流水置为已作废（只留档）。

    退款流水不在这里动 —— 它恰好是退单才该有的（撤单则没有），由 sync_refund 按订单事实维护。
    """
    if order.id is None:
        return 0
    rows = session.scalars(
        select(Settlement).where(
            Settlement.order_id == order.id, Settlement.kind != SETTLEMENT_REFUND
        )
    ).all()
    changed = 0
    for row in rows:
        if row.status != SETTLEMENT_VOID:
            row.status = SETTLEMENT_VOID
            row.note = "撤单/退单，不再结算"
            row.updated_at = now()
            changed += 1
    if commit:
        session.commit()
    return changed


# ------------------------------------------------------------------ 汇总
def summary(session: Session) -> dict:
    """资金总览（页面顶部 5 个数字，按此顺序展示）：

      1 待结算 · 公司自营应收   pending_in（还没从平台收回来）
      2 公司自营应收已收         settled_in（已经到公司账上）
      3 待结算 · 公司自营应付   pending_out（还没付给上游供应商）
      4 公司自营应付已付         settled_out（已付给供应商，含「已转账」自动核销的）
      5 公司自营净额             settled_net = 已收 − 已付（实际落到公司口袋的钱，负数=付出去的比收回来的多）
    另附本月已收 / 已付（month_settled_*）与笔数，供卡片副标题展示。
    """

    def _sum(*conditions) -> float:
        return round(
            float(
                session.scalar(
                    select(func.coalesce(func.sum(Settlement.amount), 0.0)).where(*conditions)
                )
                or 0.0
            ),
            2,
        )

    def _count(*conditions) -> int:
        return int(
            session.scalar(select(func.count(Settlement.id)).where(*conditions)) or 0
        )

    month = beijing_now().strftime("%Y-%m")  # 默认月份按北京自然月
    quiet = Settlement.status != SETTLEMENT_VOID
    data = {
        "pending_in": _sum(
            Settlement.kind == SETTLEMENT_INITIATOR, Settlement.status == SETTLEMENT_PENDING
        ),
        "pending_out": _sum(
            Settlement.kind == SETTLEMENT_SUPPLIER, Settlement.status == SETTLEMENT_PENDING
        ),
        "pending_count_in": _count(
            Settlement.kind == SETTLEMENT_INITIATOR, Settlement.status == SETTLEMENT_PENDING
        ),
        "pending_count_out": _count(
            Settlement.kind == SETTLEMENT_SUPPLIER, Settlement.status == SETTLEMENT_PENDING
        ),
        "settled_in": _sum(
            Settlement.kind == SETTLEMENT_INITIATOR, Settlement.status == SETTLEMENT_SETTLED
        ),
        "settled_out": _sum(
            Settlement.kind == SETTLEMENT_SUPPLIER, Settlement.status == SETTLEMENT_SETTLED
        ),
        "settled_count_in": _count(
            Settlement.kind == SETTLEMENT_INITIATOR, Settlement.status == SETTLEMENT_SETTLED
        ),
        "settled_count_out": _count(
            Settlement.kind == SETTLEMENT_SUPPLIER, Settlement.status == SETTLEMENT_SETTLED
        ),
        "month_settled_in": _sum(
            Settlement.kind == SETTLEMENT_INITIATOR,
            Settlement.status == SETTLEMENT_SETTLED,
            Settlement.biz_month == month,
        ),
        "month_settled_out": _sum(
            Settlement.kind == SETTLEMENT_SUPPLIER,
            Settlement.status == SETTLEMENT_SETTLED,
            Settlement.biz_month == month,
        ),
        # 退款（退给买家的钱）：方向也是流出，但对象是买家 —— 单独统计，对账时一眼看得见
        "pending_refund": _sum(
            Settlement.kind == SETTLEMENT_REFUND, Settlement.status == SETTLEMENT_PENDING
        ),
        "settled_refund": _sum(
            Settlement.kind == SETTLEMENT_REFUND, Settlement.status == SETTLEMENT_SETTLED
        ),
        "pending_count_refund": _count(
            Settlement.kind == SETTLEMENT_REFUND, Settlement.status == SETTLEMENT_PENDING
        ),
        "settled_count_refund": _count(
            Settlement.kind == SETTLEMENT_REFUND, Settlement.status == SETTLEMENT_SETTLED
        ),
        "month_settled_refund": _sum(
            Settlement.kind == SETTLEMENT_REFUND,
            Settlement.status == SETTLEMENT_SETTLED,
            Settlement.biz_month == month,
        ),
        "void_count": _count(Settlement.status == SETTLEMENT_VOID),
        "month": month,
    }
    # 净额 = 已收 − 已付 − 已退：退款是真金白银出去的，不扣掉会显得比实际赚得多
    data["pending_net"] = round(
        data["pending_in"] - data["pending_out"] - data["pending_refund"], 2
    )
    data["settled_net"] = round(
        data["settled_in"] - data["settled_out"] - data["settled_refund"], 2
    )
    data["total_count"] = _count(quiet)
    # 成本没维护（¥0）的订单会拉低「公司自营应付」：页面提醒去补成本
    data["zero_cost"] = int(
        session.scalar(
            select(func.count(func.distinct(Settlement.order_id)))
            .join(Order, Order.id == Settlement.order_id)
            .where(Settlement.status != SETTLEMENT_VOID, Order.cost_amount == 0)
        )
        or 0
    )
    return data


# ------------------------------------------------------------------ 聚合统计
def filters(
    kind: str = "",
    status: str = "",
    counterparty: str = "",
    day_from: str = "",
    day_to: str = "",
    keyword: str = "",
) -> list:
    """列表 / 统计共用的筛选口径：默认不把「已作废」的流水算进来。"""
    conditions = []
    if kind:
        conditions.append(Settlement.kind == kind)
    if status:
        conditions.append(Settlement.status == status)
    else:
        conditions.append(Settlement.status != SETTLEMENT_VOID)  # 作废的不进统计
    if counterparty:
        conditions.append(Settlement.counterparty == counterparty)
    if day_from:
        conditions.append(Settlement.biz_day >= day_from)
    if day_to:
        conditions.append(Settlement.biz_day <= day_to)
    if keyword:
        conditions.append(Settlement.order_no.contains(keyword))
    return conditions


def count_rows(session: Session, conditions: list) -> int:
    return int(session.scalar(select(func.count(Settlement.id)).where(*conditions)) or 0)


def _sum_if(status: str = "", direction: str = ""):
    """条件求和片段：按（方向 / 状态）累加金额，不满足条件的记 0。"""
    conds = []
    if direction:
        conds.append(Settlement.direction == direction)
    if status:
        conds.append(Settlement.status == status)
    if not conds:
        return Settlement.amount
    return case((and_(*conds), Settlement.amount), else_=0.0)


def _count_if(direction: str):
    return func.count(case((Settlement.direction == direction, Settlement.id), else_=None))


def _sum_kind(kind: str, status: str = ""):
    """按流水类型求和。

    应付（supplier）与退款（refund）方向都是 out，靠方向区分不出来 —— 它们必须分开算，
    否则「应付给供应商」的合计里会混进退给买家的钱。
    """
    conds = [Settlement.kind == kind]
    if status:
        conds.append(Settlement.status == status)
    return case((and_(*conds), Settlement.amount), else_=0.0)


def _count_kind(kind: str, status: str = ""):
    conds = [Settlement.kind == kind]
    if status:
        conds.append(Settlement.status == status)
    return func.count(case((and_(*conds), Settlement.id), else_=None))


def totals(session: Session, conditions: list) -> dict:
    """当前筛选下的合计：笔数、金额、待结算 / 已结算拆分。

    公司自营应收（direction=in，钱进公司）与公司自营应付（direction=out，钱付给供应商）
    方向相反，直接加在一起没有意义（还会把「待结算」算成一个虚高的数），
    所以按方向分别汇总：*_in / *_out，再看 net = 应收 − 应付。
    """
    row = session.execute(
        select(
            func.count(Settlement.id),
            func.coalesce(func.sum(Settlement.amount), 0.0),
            func.coalesce(func.sum(_sum_if(SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_if(SETTLEMENT_SETTLED)), 0.0),
            # 收 / 支三类分开算：应收（钱进公司）、应付（付给供应商）、退款（退给买家）。
            # 应付与退款方向都是 out，只按方向拆会把两者混在一起，所以这里按流水类型拆。
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR, SETTLEMENT_SETTLED)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER, SETTLEMENT_SETTLED)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND, SETTLEMENT_SETTLED)), 0.0),
            _count_kind(SETTLEMENT_INITIATOR),
            _count_kind(SETTLEMENT_SUPPLIER),
            _count_kind(SETTLEMENT_REFUND),
        ).where(*conditions)
    ).one()
    (
        count, amount, pending, settled,
        amount_in, pending_in, settled_in,
        amount_out, pending_out, settled_out,
        amount_refund, pending_refund, settled_refund,
        count_in, count_out, count_refund,
    ) = row

    def _f(v) -> float:
        return round(float(v or 0.0), 2)

    data = {
        "count": int(count or 0),
        "amount": _f(amount),
        "pending": _f(pending),
        "settled": _f(settled),
        # 按方向拆：应收（+）与应付（-）不能混着加
        "count_in": int(count_in or 0),
        "amount_in": _f(amount_in),
        "pending_in": _f(pending_in),
        "settled_in": _f(settled_in),
        "count_out": int(count_out or 0),
        "amount_out": _f(amount_out),
        "pending_out": _f(pending_out),
        "settled_out": _f(settled_out),
        # 退款（退给买家）：不是付给供应商的钱，单独一列，对账时一眼看得见
        "count_refund": int(count_refund or 0),
        "amount_refund": _f(amount_refund),
        "pending_refund": _f(pending_refund),
        "settled_refund": _f(settled_refund),
    }
    # 净额 = 应收 − 应付 − 退款（退款也是真金白银出去的）
    data["net"] = round(
        data["amount_in"] - data["amount_out"] - data["amount_refund"], 2
    )
    data["pending_net"] = round(
        data["pending_in"] - data["pending_out"] - data["pending_refund"], 2
    )
    data["settled_net"] = round(
        data["settled_in"] - data["settled_out"] - data["settled_refund"], 2
    )

    # 平台技术服务费：只有「公司自营应收」这条流水上才有（0.6%，平台结算时扣）。
    # 逐单四舍五入后再求和，跟订单上的数字对得上（直接对总额乘 0.6% 会差几分钱）。
    tech_rows = session.execute(
        select(Order.deal_amount, Order.sale_amount, Order.platform_fee)
        .join(Settlement, Settlement.order_id == Order.id)
        .where(*conditions, Settlement.kind == SETTLEMENT_INITIATOR)
    ).all()
    data["tech_fee"] = round(
        sum(
            # 订单上记了金额（自动算的 / 人工改的）就直接用，没记才按默认费率现算
            round(float(fee or 0.0), 2)
            if float(fee or 0.0) > 0
            else round((float(deal or 0.0) or float(sale or 0.0)) * TECH_SERVICE_FEE_RATE, 2)
            for deal, sale, fee in tech_rows
        ),
        2,
    )
    return data


def order_rows(
    session: Session, conditions: list, *, offset: int = 0, limit: int | None = None
) -> list[dict]:
    """按笔的明细（一行一条流水，带订单信息），可分页。"""
    stmt = (
        select(Settlement, Order)
        .outerjoin(Order, Order.id == Settlement.order_id)
        .where(*conditions)
        .order_by(Settlement.biz_day.desc(), Settlement.id.desc())
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    out = []
    for item, order in rows:
        amount = round(float(item.amount or 0), 2)
        # 平台技术服务费只在「公司自营应收」这条流水上有意义（应付给供应商不涉及）
        tech_fee = (
            order_amounts(order, session=session)["tech_fee"]
            if order is not None and item.kind == SETTLEMENT_INITIATOR
            else 0.0
        )
        out.append(
            {
                "key": str(item.id),
                "period": item.biz_day,
                "kind": item.kind,
                "kind_label": SETTLEMENT_KIND_LABELS.get(item.kind, item.kind),
                "direction": item.direction,
                "counterparty": item.counterparty,
                "order_id": item.order_id,
                "order_no": item.order_no or (order.order_no if order else ""),
                "product": (order.product_name if order else "") or "",
                "receiver": (order.receiver_name if order else "") or "",
                "amount": amount,
                # 退款金额（该订单退给买家的钱）：明细里单列一列，对账时不用去别处翻。
                # 退款流水就是按订单上的 refund_amount 记的，净额里已经把它扣掉了。
                # 展示列取绝对值：订单上按符号口径存的是负值，这一列是「退了多少」不是方向
                "refund_amount": round(abs(float(getattr(order, "refund_amount", 0.0) or 0.0)), 2),
                "tech_fee": tech_fee,
                "status": item.status,
                "status_label": item.status_label,
                # 结算时机（在途 / 可结）：已发货没签收的在途，已完成的可结 —— 运营据此决定先结哪一单
                "settle_state": (
                    settlement_readiness(order) if order is not None else SETTLE_NONE
                ),
                "settle_state_label": (
                    SETTLE_STATE_LABELS[settlement_readiness(order)]
                    if order is not None
                    else ""
                ),
                "settled_at": item.settled_at,
                "settled_by": item.settled_by,
                # 订单创建（归档）时间：查订单问题时用它定位是哪一单、什么时候进的系统
                "created_at": order.created_at if order else None,
                "note": item.note,
                "ids": [item.id],
                "count": 1,
                "pending": amount if item.status == SETTLEMENT_PENDING else 0.0,
                "settled": amount if item.status == SETTLEMENT_SETTLED else 0.0,
            }
        )
    return out


def aggregate_rows(session: Session, conditions: list, granularity: str = "day") -> list[dict]:
    """按天 / 按月聚合（按笔请看 order_rows）。

    收 / 付 / 退三类**不能混着加**：应收是钱进公司、应付是付给供应商、退款是退给买家，
    以前一律按 amount 求和，「合计金额」会把要付出去的钱、退给买家的钱都算成收入。
    这里按流水类型分开汇总（应付与退款方向都是 out，只能靠 kind 区分），再给净额
    = 应收 − 应付 − 退款，跟页面顶部的口径一致。
    """
    period = (
        Settlement.biz_day
        if granularity == "day"
        else func.substr(Settlement.biz_day, 1, 7)
    )
    rows = session.execute(
        select(
            period.label("period"),
            func.count(Settlement.id),
            func.coalesce(func.sum(Settlement.amount), 0.0),
            func.coalesce(
                func.sum(case((Settlement.status == SETTLEMENT_PENDING, Settlement.amount), else_=0.0)),
                0.0,
            ),
            func.coalesce(
                func.sum(case((Settlement.status == SETTLEMENT_SETTLED, Settlement.amount), else_=0.0)),
                0.0,
            ),
            func.count(func.distinct(Settlement.counterparty)),
            _count_kind(SETTLEMENT_INITIATOR, SETTLEMENT_PENDING)
            + _count_kind(SETTLEMENT_SUPPLIER, SETTLEMENT_PENDING)
            + _count_kind(SETTLEMENT_REFUND, SETTLEMENT_PENDING),
            # 三类金额分别汇总（含待结算 / 已结算拆分），净额靠它们算
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND, SETTLEMENT_PENDING)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_INITIATOR, SETTLEMENT_SETTLED)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_SUPPLIER, SETTLEMENT_SETTLED)), 0.0),
            func.coalesce(func.sum(_sum_kind(SETTLEMENT_REFUND, SETTLEMENT_SETTLED)), 0.0),
        )
        .where(*conditions)
        .group_by(period)
        .order_by(period.desc())
    ).all()
    out = []
    for row in rows:
        (
            period_value, count, amount, pending, settled, parties, count_pending,
            amount_in, amount_out, amount_refund,
            pending_in, pending_out, pending_refund,
            settled_in, settled_out, settled_refund,
        ) = row

        def _f(v) -> float:
            return round(float(v or 0.0), 2)

        out.append(
            {
                "key": str(period_value or ""),
                "period": str(period_value or ""),
                "count": int(count or 0),
                "parties": int(parties or 0),
                # amount / pending / settled 是三类金额的合计（不分方向），
                # 只给「整期结算」按钮用（核销的是这些流水本身）
                "amount": _f(amount),
                "pending": _f(pending),
                "settled": _f(settled),
                "count_pending": int(count_pending or 0),
                "amount_in": _f(amount_in),
                "amount_out": _f(amount_out),
                "amount_refund": _f(amount_refund),
                "pending_in": _f(pending_in),
                "pending_out": _f(pending_out),
                "pending_refund": _f(pending_refund),
                "settled_in": _f(settled_in),
                "settled_out": _f(settled_out),
                "settled_refund": _f(settled_refund),
                # 净额 = 应收 − 应付 − 退款（退款也是真金白银出去的）
                "net": round(_f(amount_in) - _f(amount_out) - _f(amount_refund), 2),
                "pending_net": round(_f(pending_in) - _f(pending_out) - _f(pending_refund), 2),
                "settled_net": round(_f(settled_in) - _f(settled_out) - _f(settled_refund), 2),
            }
        )
    return out


def counterparties(session: Session, kind: str = "") -> list[str]:
    """出现过的结算对象（发起人 / 供应商），用于筛选下拉。"""
    stmt = select(Settlement.counterparty).distinct()
    if kind:
        stmt = stmt.where(Settlement.kind == kind)
    return [c for c in session.scalars(stmt.order_by(Settlement.counterparty)).all() if c]


# ------------------------------------------------------------------ 结算操作
def settle(
    session: Session,
    *,
    ids: list[int] | None = None,
    kind: str = "",
    counterparty: str = "",
    day: str = "",
    month: str = "",
    actor: str = "",
    note: str = "",
    commit: bool = True,
) -> int:
    """把符合条件的待结算流水标记为已结算，返回处理条数。

    至少要给一个范围（选中流水 / 按期间 / 按对象），避免误点一次全量核销。
    """
    if not (ids or day or month):
        raise ValueError("请指定要结算的流水或期间")
    stmt = select(Settlement).where(Settlement.status == SETTLEMENT_PENDING)
    if ids:
        stmt = stmt.where(Settlement.id.in_(ids))
    if kind:
        stmt = stmt.where(Settlement.kind == kind)
    if counterparty:
        stmt = stmt.where(Settlement.counterparty == counterparty)
    if day:
        stmt = stmt.where(Settlement.biz_day == day)
    if month:
        stmt = stmt.where(Settlement.biz_month == month)
    rows = session.scalars(stmt).all()
    ts = now()
    for row in rows:
        row.status = SETTLEMENT_SETTLED
        row.settled_at = ts
        row.settled_by = actor
        if note:
            row.note = note
        row.updated_at = ts
    if commit:
        session.commit()
    logger.info(
        "结算完成：%s 条（kind=%s 对象=%s 期间=%s%s）",
        len(rows),
        kind or "全部",
        counterparty or "全部",
        day or month or "选中",
        f" 操作人={actor}" if actor else "",
    )
    return len(rows)


def unsettle(session: Session, ids: list[int], commit: bool = True) -> int:
    """撤销结算（点错了）：把流水退回待结算。"""
    if not ids:
        return 0
    rows = session.scalars(
        select(Settlement).where(
            Settlement.id.in_(ids), Settlement.status == SETTLEMENT_SETTLED
        )
    ).all()
    for row in rows:
        row.status = SETTLEMENT_PENDING
        row.settled_at = None
        row.settled_by = ""
        row.updated_at = now()
    if commit:
        session.commit()
    return len(rows)
