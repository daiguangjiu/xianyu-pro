"""渔货栈「订单管理」与渔枢系统订单之间的桥（只读）。

渔货栈订单的主体是渔货栈自己的两张表：`downstream_orders`（渠道下游单）与
`purchase_orders`（1688 采购 / 代发单）—— 渔货栈出的单先在渔货栈落地、能在渔货栈看，
同时由 `app/services/dso_sync.py` 在渔枢系统建一笔「平台订单」（`orders` 表，
`origin=self` 就是渔货栈出单自动建档的），运营在那边审单 / 推单 / 发货 / 结算。

渔货栈订单页的字段要求与渔枢系统端保持一致（金额口径、收货信息、推单状态），
所以这里按单号把渔枢系统侧的事实批量取回来，页面合并展示：

- **只读**：金额（售价 / 采购成本 / 运费 / 退款 / 平台技术服务费）与推单状态
  都由渔枢系统端维护（那边是订单中台），渔货栈只看不改；
- 查不到（这单还没同步到渔枢系统、或渔货栈单独部署没有渔枢系统那套）返回空，页面显示「未建档」；
- 任何异常都吞掉返回空 dict —— 桥断了也不能把渔货栈订单页打挂。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于部署形态（合并部署才有 app 包）
    from app.db import SessionLocal as _hub_session
    from app.models import Order as _HubOrder
except Exception:  # noqa: BLE001  渔货栈单独部署：桥整体降级
    _hub_session = None
    _HubOrder = None

# 展示用字段（与渔枢系统端订单列表 / 详情页一致的口径）
_VIEW_FIELDS = (
    "id",
    "order_no",
    "origin",
    "order_state",
    "supplier_status",
    "product_name",
    "spec",
    "quantity",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    # 飞书发送人（运营姓名）——「我要录单」按它跟渔货栈对账（2026-10-14）
    "sender_name",
    "xianyu_account",
    "sale_amount",
    "deal_amount",
    "cost_amount",
    "shipping_fee",
    "platform_fee",
    "refund_amount",
    "supplier_refund_amount",
    "net_profit",
    "msg_time",
    "ordered_at",
    "tracking_no",
)

# 渔枢系统订单详情路径（合并部署时同一域名，绝对路径即可）
HUB_ORDER_PATH = "/orders/{order_id}"

# sync_last 动作：这条单在渔枢系统那边是什么状态（页面用来说明「推送」结果）
HUB_STATE_LABELS = {
    "new": "新建（待审）",
    "pushed": "已推单",
    "shipped": "已发货",
    "done": "已完成",
    "cancelled": "已撤单",
}
SUPPLIER_STATUS_LABELS = {
    "pending": "未推供应商",
    "pushed": "已推供应商",
    "done": "已完成",
    "cancelled": "已取消",
}


def available() -> bool:
    """当前部署里有没有渔枢系统那套订单库可读。"""
    return _hub_session is not None and _HubOrder is not None


def base_order_no(value: str | None) -> str:
    """下游单号 → 平台订单单号。

    渔货栈一笔单多 SKU 时 DSO 单号会带 `::sku#<idx>` 后缀（见 `src/order/service.py`），
    渔枢系统那边同一笔单只有一个单号，比对前先把后缀去掉。
    """
    return (value or "").split("::sku#")[0].strip()


def hub_state_text(value: str | None) -> str:
    return HUB_STATE_LABELS.get((value or "").strip(), (value or "").strip() or "—")


def supplier_status_text(value: str | None) -> str:
    return SUPPLIER_STATUS_LABELS.get((value or "").strip(), (value or "").strip() or "—")


def _brief(row) -> dict[str, Any]:
    """把渔枢系统订单拍平成 dict（模板不摸 ORM 对象，也不需要 session）。"""
    data = {field: getattr(row, field, None) for field in _VIEW_FIELDS}
    data["url"] = HUB_ORDER_PATH.format(order_id=data.get("id"))
    data["state_text"] = hub_state_text(data.get("order_state"))
    data["supplier_status_text"] = supplier_status_text(data.get("supplier_status"))
    data["is_self_origin"] = (data.get("origin") or "") == "self"
    # 售价口径与渔枢系统端一致：有成交价取成交价，否则取售价
    data["paid"] = float(data.get("deal_amount") or 0) or float(data.get("sale_amount") or 0)
    # 退款金额在库里存负值（2026-10-03 符号口径），页面展示 / 回填用绝对值
    data["refund_amount_abs"] = round(abs(float(data.get("refund_amount") or 0)), 2)
    return data


def by_order_nos(order_nos: Iterable[str]) -> dict[str, dict[str, Any]]:
    """按单号批量取渔枢系统订单（只读）；桥不可用 / 查询失败一律返回空 dict。"""
    nos = {base_order_no(n) for n in order_nos if base_order_no(n)}
    if not nos or not available():
        return {}
    try:
        with _hub_session() as db:
            rows = db.scalars(select(_HubOrder).where(_HubOrder.order_no.in_(nos))).all()
            return {str(r.order_no).strip(): _brief(r) for r in rows if (r.order_no or "").strip()}
    except Exception as exc:  # noqa: BLE001  桥断了也要能看渔货栈自己的订单
        logger.warning("取渔枢系统订单失败（渔货栈订单页按「未建档」显示）：%s", exc)
        return {}


def one(order_no: str) -> dict[str, Any] | None:
    """按单号取一笔渔枢系统订单（找不到返回 None）。"""
    return by_order_nos([order_no]).get(base_order_no(order_no))


def push_to_hub(db, dso) -> tuple[Any, str]:
    """把一笔渔货栈下游订单推到渔枢系统（补建档 / 补空字段，幂等）。

    直接复用收单时那条同步链路（`app/services/dso_sync.py`），所以：
    渔枢系统已有同单号就只补空字段（运营填过的不动），没有才新建一条「新建」订单。
    返回 `(order, action)`，`action` 见 dso_sync.ACTION_*。
    """
    from app.services.dso_sync import sync_downstream_order

    return sync_downstream_order(db, dso)


# ---------------------------------------------------------------- 飞书群报单 → 渔货栈录单
# 2026-10-14「我要录单」（用户口径）：内部自营同学在飞书群发的单与渔货栈要一致 ——
# 群里报了，录单列表里也有，按「运营姓名」（飞书发送人）对齐。
GROUP_SYNC_DAYS = 7      # 只回看最近几天的群报单（再老的属于历史数据，不补建）
GROUP_SYNC_LIMIT = 200   # 单轮最多搬多少条，防首屏查询被历史大表拖死


def sync_group_orders(db, *, days: int = GROUP_SYNC_DAYS, limit: int = GROUP_SYNC_LIMIT) -> int:
    """把飞书群报进渔枢系统的单同步成渔货栈录单（幂等，返回本次新建笔数）。

    单号落成 ``FS-<渔枢系统单号>``，按它查重 —— 群里同一单同步多少遍都只建一条；
    运营姓名取渔枢系统订单的 ``sender_name``（飞书发送人，收单时已解析）。
    任何异常都吞掉：这是「锦上添花」的同步，不能把订单列表页打挂。
    """
    if not available() or _HubOrder is None:
        return 0
    from datetime import timedelta

    from ..models import DownstreamOrder

    created = 0
    try:
        from app.timeutil import now as _bj_now  # noqa: PLC0415

        with _hub_session() as hub:
            since = _hub_now() - timedelta(days=max(int(days or 7), 1))
            rows = hub.scalars(
                select(_HubOrder)
                .where(_HubOrder.created_at >= since)
                # 群里撤回的留档单不参与业务（沿用渔枢侧口径）
                .where(or_(_HubOrder.is_recalled.is_(False), _HubOrder.is_recalled.is_(None)))
                .order_by(_HubOrder.id.desc())
                .limit(max(int(limit or 1), 1))
            ).all()
        for row in rows:
            no = str(getattr(row, "order_no", "") or "").strip()
            if not no:
                continue
            dso_no = f"FS-{no}"
            exists = db.scalar(
                select(DownstreamOrder).where(
                    DownstreamOrder.downstream_order_id == dso_no
                )
            )
            if exists is not None:
                continue
            operator = str(getattr(row, "sender_name", "") or "")
            paid = float(getattr(row, "deal_amount", 0) or 0) or float(
                getattr(row, "sale_amount", 0) or 0
            )
            msg_at = getattr(row, "msg_time", None) or getattr(row, "ordered_at", None)
            note_bits = [f"飞书群报单同步（运营：{operator or '未知'}）"]
            if msg_at is not None:
                note_bits.append(f"报单时间 {msg_at:%Y-%m-%d %H:%M}")
            dso = DownstreamOrder(
                downstream_order_id=dso_no[:64],
                channel="xianyu",
                channel_sku_id="",
                buyer_name=str(getattr(row, "receiver_name", "") or "")[:64],
                buyer_phone=str(getattr(row, "receiver_phone", "") or "")[:32],
                address=str(getattr(row, "receiver_address", "") or ""),
                qty=max(int(getattr(row, "quantity", 0) or 1), 1),
                sale_price=round(paid, 2),
                fish_price=0,
                status="pending",
                raw_json={
                    "source": "feishu_group",
                    "owner": "",
                    "operator": operator[:64],
                    "title": str(getattr(row, "product_name", "") or "")[:255],
                    "spec": str(getattr(row, "spec", "") or ""),
                    "shop_name": str(getattr(row, "xianyu_account", "") or ""),
                    "hub_order_no": no,
                    "note": "；".join(note_bits),
                    "images": [],
                },
                created_at=_bj_now(),
                updated_at=_bj_now(),
            )
            db.add(dso)
            created += 1
        if created:
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("飞书群报单同步渔货栈失败（跳过）：%s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return created


def _hub_now():
    """渔枢系统订单表的 created_at 是 UTC naive（与渔货栈同 clock），比较要同口径。"""
    from app.timeutil import utc_now  # noqa: PLC0415

    return utc_now().replace(tzinfo=None)
