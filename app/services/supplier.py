"""供应链发货：按供应商组批、状态流转、导出。"""
from __future__ import annotations

import csv
import io
import logging
import re

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    Order,
    PushBatch,
    PushBatchItem,
    Supplier,
    not_recalled,
    not_refund_order,
    pending_push_order,
)
from ..timeutil import now
from . import voice as voice_service
from . import order_state as order_state_service

logger = logging.getLogger(__name__)

EXPORT_HEADERS = [
    ("batch_no", "批次号"),
    ("order_no", "订单号"),
    ("xianyu_account", "出货闲鱼号"),
    ("product_name", "产品"),
    ("spec", "规格"),
    ("quantity", "数量"),
    ("receiver_name", "收货人"),
    ("receiver_phone", "手机号码"),
    ("receiver_address", "地址信息"),
    ("buyer_nick", "买家"),
    ("sale_amount", "售价"),
    ("cost_amount", "成本"),
    ("payee_name", "回款支付宝归属人"),
    ("note", "备注"),
    ("supplier_order_no", "供应商单号"),
    ("tracking_no", "运单号"),
]


def _new_batch_no(supplier: str) -> str:
    ts = now()
    stamp = ts.strftime("%Y%m%d%H%M%S")
    short = "".join(ch for ch in supplier if ch.isascii() and ch.isalnum())[:6] or "SUP"
    return f"PB{ts.strftime('%Y%m%d')}-{short}-{stamp[-6:]}"


def create_batch(
    session: Session, supplier_name: str, order_ids: list[int], note: str = ""
) -> PushBatch:
    orders = session.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    if not orders:
        raise ValueError("没有可推单的订单")

    batch = PushBatch(
        batch_no=_new_batch_no(supplier_name),
        supplier_name=supplier_name,
        status="draft",
        note=note,
    )
    session.add(batch)
    session.flush()

    total_sale = 0.0
    total_cost = 0.0
    existed = set()
    for order in orders:
        if order.id in existed:
            continue
        existed.add(order.id)
        # 退单订单不再推给供应商（群里已同步退单，避免继续发货）
        if order.is_refund:
            logger.info("退单订单 %s 不参与推单", order.order_no)
            continue
        # 已在本批次之外的其它草稿批次中则跳过
        dup = session.scalar(
            select(PushBatchItem).where(PushBatchItem.order_id == order.id)
        )
        if dup:
            continue
        session.add(PushBatchItem(batch_id=batch.id, order_id=order.id))
        total_sale += order.sale_amount or 0
        total_cost += order.cost_amount or 0

    batch.item_count = len(existed)
    batch.total_sale = round(total_sale, 2)
    batch.total_cost = round(total_cost, 2)
    session.commit()
    return batch


def auto_batch_by_supplier(session: Session, limit_suppliers: int | None = None) -> list[PushBatch]:
    """把所有待推单订单按供应商自动组批。"""
    rows = (
        session.execute(
            select(Order.supplier_name)
            .where(pending_push_order())
            .group_by(Order.supplier_name)
        )
        .all()
    )
    batches = []
    for (supplier,) in rows:
        orders = session.scalars(
            select(Order).where(
                pending_push_order(),
                Order.supplier_name == supplier,
            )
        ).all()
        pending_ids = [
            o.id
            for o in orders
            if session.scalar(
                select(PushBatchItem).where(PushBatchItem.order_id == o.id)
            )
            is None
        ]
        if not pending_ids:
            continue
        batches.append(create_batch(session, supplier or "未指定供应商", pending_ids))
        if limit_suppliers and len(batches) >= limit_suppliers:
            break
    return batches


def set_batch_status(session: Session, batch_id: int, status: str) -> PushBatch:
    batch = session.get(PushBatch, batch_id)
    if not batch:
        raise ValueError("批次不存在")
    batch.status = status
    if status == "pushed":
        batch.pushed_at = now()
    items = session.scalars(select(PushBatchItem).where(PushBatchItem.batch_id == batch_id)).all()
    order_status = {"pushed": "pushed", "done": "done", "cancelled": "pending"}.get(status)
    for item in items:
        if order_status and item.order:
            item.order.supplier_status = order_status
            item.order.updated_at = now()
            if order_status == "pushed":
                # 批次推给供应商 -> 订单状态「已推单」
                order_state_service.advance(
                    session, item.order, order_state_service.ORDER_STATE_PUSHED
                )
    session.commit()
    return batch


def update_item(
    session: Session, batch_id: int, order_id: int, supplier_order_no: str = "", tracking_no: str = ""
) -> None:
    item = session.scalar(
        select(PushBatchItem).where(
            PushBatchItem.batch_id == batch_id, PushBatchItem.order_id == order_id
        )
    )
    if not item:
        raise ValueError("批次明细不存在")
    if supplier_order_no:
        item.supplier_order_no = supplier_order_no
    changed_order = None
    if tracking_no:
        item.tracking_no = tracking_no
        if item.order:
            # 供应商回单物流更新 -> 语音播报「供应商已发货」
            if (item.order.tracking_no or "").strip() != tracking_no.strip():
                item.order.tracking_no = tracking_no.strip()
                item.order.updated_at = now()
                changed_order = item.order
                # 上游返回物流单号 -> 订单状态「已发货」
                order_state_service.advance(
                    session, item.order, order_state_service.ORDER_STATE_SHIPPED
                )
    session.commit()
    if changed_order is not None:
        voice_service.tracking_event(session, changed_order)


def batch_rows(session: Session, batch: PushBatch) -> list[dict]:
    items = session.scalars(
        select(PushBatchItem).where(PushBatchItem.batch_id == batch.id)
    ).all()
    rows = []
    for item in items:
        o = item.order
        if not o:
            continue
        rows.append(
            {
                "batch_no": batch.batch_no,
                "order_no": o.order_no,
                "xianyu_account": o.xianyu_account,
                "product_name": o.product_name,
                "spec": o.spec,
                "quantity": o.quantity,
                "receiver_name": o.receiver_name,
                "receiver_phone": o.receiver_phone,
                "receiver_address": o.receiver_address,
                "buyer_nick": o.buyer_nick,
                "sale_amount": o.sale_amount,
                "cost_amount": o.cost_amount,
                "payee_name": o.payee_name,
                "note": o.note,
                "supplier_order_no": item.supplier_order_no,
                "tracking_no": item.tracking_no,
            }
        )
    return rows


def export_csv(session: Session, batch: PushBatch) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([h for _, h in EXPORT_HEADERS])
    for row in batch_rows(session, batch):
        writer.writerow([row.get(k, "") for k, _ in EXPORT_HEADERS])
    # BOM 便于 Excel 正确识别 UTF-8
    return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")


def export_xlsx(session: Session, batch: PushBatch) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "推单明细"
    ws.append([h for _, h in EXPORT_HEADERS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in batch_rows(session, batch):
        ws.append([row.get(k, "") for k, _ in EXPORT_HEADERS])
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width * 1.6 + 2, 10), 42)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def count_pending_orders(session: Session) -> int:
    """待推单订单数（撤回订单不参与后续流程，不计入）。"""
    return session.scalar(
        select(func.count(Order.id)).where(pending_push_order())
    ) or 0


# ------------------------------------------------ 订单里的供应商写法 <-> 档案
# 模板消息里的「所属供应链 / 所属供应商」填的是供应商编码（如 GYS001），也可能是名称
# 或「名称（编码）」。下面这套解析/匹配逻辑保证编码与名称都能关联到同一家供应商，
# 并且系统内统一显示成「供应商名称（供应商编码）」。
_PLACEHOLDERS = {
    "", "未指定", "未指定供应商", "未指定供应链", "无", "暂无", "待定", "未知",
    "空", "-", "--", "—", "/", "\\", "?", "？",
}
_REF_RE = re.compile(r"[（(]\s*([^（）()]{0,32}?)\s*[)）]\s*$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


def is_placeholder(value: str) -> bool:
    """「未指定」这类占位写法：等同于没填供应商。"""
    return (value or "").strip() in _PLACEHOLDERS


def looks_like_code(value: str) -> bool:
    """是否像供应商编码：短、纯 ASCII 字母数字，且含数字或全大写（如 GYS001）。"""
    text = (value or "").strip()
    if not (1 < len(text) <= 32) or not _CODE_RE.match(text) or text.isdigit():
        return False
    return any(ch.isdigit() for ch in text) or text.upper() == text


def parse_ref(value: str) -> tuple[str, str]:
    """把订单里的供应商写法拆成 (名称, 编码)。

    支持「名称（编码）」「名称(编码)」「纯编码 GYS001」「纯名称」。
    """
    text = (value or "").strip()
    if not text or is_placeholder(text):
        return "", ""
    match = _REF_RE.search(text)
    if match:
        name = text[: match.start()].strip(" 　-—/|·")
        code = match.group(1).strip()
        if name or code:
            return name, code
    if looks_like_code(text):
        return "", text
    return text, ""


def display_name(name: str, code: str = "") -> str:
    """统一展示写法：有编码时显示「供应商名称（供应商编码）」。"""
    name = (name or "").strip()
    code = (code or "").strip()
    if name and code:
        return f"{name}（{code}）"
    return name or code


def supplier_label(supplier: Supplier) -> str:
    """供应商档案的统一展示名。"""
    return display_name(supplier.name, supplier.code)


class SupplierIndex:
    """订单里的供应商写法 <-> 供应商档案 的对照表与归一化。"""

    def __init__(self, suppliers: list[Supplier]) -> None:
        self.suppliers = list(suppliers)
        self._by_name: dict[str, Supplier] = {}
        self._by_code: dict[str, Supplier] = {}
        self._by_label: dict[str, Supplier] = {}
        for row in self.suppliers:
            name = (row.name or "").strip()
            code = (row.code or "").strip()
            if name:
                self._by_name.setdefault(name.casefold(), row)
            if code:
                self._by_code.setdefault(code.casefold(), row)
            label = display_name(name, code)
            if label:
                self._by_label.setdefault(label.casefold(), row)

    def register(self, row: Supplier) -> None:
        """把新建的档案并入索引：同一批处理里接着用，避免同一家被重复建档。"""
        if row not in self.suppliers:
            self.suppliers.append(row)
        name = (row.name or "").strip()
        code = (row.code or "").strip()
        if name:
            self._by_name.setdefault(name.casefold(), row)
        if code:
            self._by_code.setdefault(code.casefold(), row)
        label = display_name(name, code)
        if label:
            self._by_label.setdefault(label.casefold(), row)

    def resolve(self, value: str) -> Supplier | None:
        """把订单里的写法（编码 / 名称 / 名称（编码））定位到供应商档案。"""
        text = (value or "").strip()
        if not text or is_placeholder(text):
            return None
        folded = text.casefold()
        hit = (
            self._by_label.get(folded)
            or self._by_name.get(folded)
            or self._by_code.get(folded)
        )
        if hit is not None:
            return hit
        name, code = parse_ref(text)
        if code:
            hit = self._by_code.get(code.casefold())
        if hit is None and name:
            hit = self._by_name.get(name.casefold())
        return hit

    def label(self, value: str) -> str:
        """订单里的原始写法 -> 统一展示值；认不出档案时保留（规整后的）原文。"""
        text = (value or "").strip()
        if not text or is_placeholder(text):
            return ""
        hit = self.resolve(text)
        if hit is not None:
            return supplier_label(hit)
        name, code = parse_ref(text)
        return display_name(name, code) if (name or code) else text

    def keys(self, value: Supplier | str) -> list[str]:
        """该供应商在订单表里可能出现的所有等价写法（用于 IN 查询）。"""
        texts: list[str] = []
        row = value if isinstance(value, Supplier) else self.resolve(str(value or ""))
        if row is not None:
            texts += [row.name, row.code, supplier_label(row)]
        if not isinstance(value, Supplier):
            texts.append(str(value or ""))
            name, code = parse_ref(str(value or ""))
            texts += [name, code, display_name(name, code)]
        values = {
            text.strip()
            for text in texts
            if text and text.strip() and not is_placeholder(text)
        }
        return sorted(values)

    def group_key(self, value: str) -> str:
        """统计聚合键：同一家供应商的不同写法合并成一行。"""
        text = (value or "").strip()
        if not text:
            return ""
        return self.label(text) or text


def build_index(session: Session) -> SupplierIndex:
    return SupplierIndex(list(session.scalars(select(Supplier)).all()))


def normalize_order_supplier(
    session: Session, value: str, index: SupplierIndex | None = None
) -> str:
    """订单落库/保存时统一供应商写法：能对上档案就存「名称（编码）」，否则存原文。"""
    text = (value or "").strip()
    if not text:
        return ""
    return (index or build_index(session)).label(text)


def _similar(index: SupplierIndex, name: str) -> Supplier | None:
    """名字互相包含的算同一家（「梦想电商」与「安徽梦想电商」），避免重复建档。"""
    folded = (name or "").strip().casefold()
    if len(folded) < 3:
        return None
    for row in index.suppliers:
        other = (row.name or "").strip().casefold()
        if not other:
            continue
        short, long = (folded, other) if len(folded) <= len(other) else (other, folded)
        if len(short) >= 3 and short in long:
            return row
    return None


def ensure_supplier(
    session: Session,
    value: str,
    index: SupplierIndex | None = None,
    source: str = "",
) -> Supplier | None:
    """订单 / 登记表里出现的供应商：档案里有就复用，没有才自动建档。

    业务口径（2026-09）：供应商档案由后台维护，订单页只能选；群里识别出来的、
    登记表导入进来的供应商常常还没建档 —— 这时按订单里的写法自动补一条档案
    （名称必填、编码留空，note 记下来源方便运营补资料），订单随后统一成展示名。

    判重：名称 / 编码 / 「名称（编码）」精确对上，或名字互相包含（「梦想电商」
    与「安徽梦想电商」）都算同一家，不重复建档。不提交事务，由调用方统一提交。
    """
    text = (value or "").strip()
    if not text or is_placeholder(text):
        return None
    idx = index or build_index(session)
    hit = idx.resolve(text)
    if hit is not None:
        return hit
    name, code = parse_ref(text)
    key = (name or code).strip()
    if not key:
        return None
    hit = _similar(idx, key)
    if hit is not None:
        return hit
    if session.scalar(
        select(Supplier).where(func.lower(Supplier.name) == key.lower())
    ) is not None:
        return idx.resolve(key)
    row = Supplier(name=key, code="")
    row.note = f"订单自动建档（{source or '订单/导入'}）：请先补全编码与联系方式"
    session.add(row)
    session.flush()
    # 档案可能是订单先到：把订单里的旧写法统一成最新展示名
    relabel_orders(session, row)
    idx.register(row)
    logger.info("供应商自动建档：%s（来源：%s）", key, source or "订单/导入")
    return row


def ensure_order_supplier(
    session: Session,
    value: str,
    index: SupplierIndex | None = None,
    source: str = "",
) -> str:
    """订单落库时的供应商写法：档案里没有的自动建档，返回统一展示名「名称（编码）」。"""
    text = (value or "").strip()
    if not text:
        return ""
    idx = index or build_index(session)
    row = ensure_supplier(session, text, index=idx, source=source)
    if row is not None:
        return supplier_label(row)
    return idx.label(text) or text


def backfill_from_orders(session: Session, commit: bool = True) -> dict:
    """按现有订单里的供应商写法补齐档案 / 统一写法（历史数据对齐，可反复执行）。

    老订单里的写法（「梦想电商」「BH001」）对不上 newly 建的档案时：
    能对上的归一到「名称（编码）」，确实没有的补一条档案。返回变动统计。
    """
    idx = build_index(session)
    before = len(idx.suppliers)
    stats = {"suppliers_before": before, "created": 0, "changed": 0, "names": []}
    rows = session.scalars(
        select(Order).where(Order.supplier_name.is_not(None), Order.supplier_name != "")
    ).all()
    for order in rows:
        label = ensure_order_supplier(session, order.supplier_name, index=idx, source="历史订单补齐")
        if label and label != (order.supplier_name or ""):
            stats["names"].append(f"{order.supplier_name} → {label}")
            order.supplier_name = label
            order.updated_at = now()
            stats["changed"] += 1
    stats["created"] = len(idx.suppliers) - before
    if commit:
        session.commit()
    logger.info(
        "供应商档案补齐：新建 %s 家、订单写法归一 %s 笔",
        stats["created"],
        stats["changed"],
    )
    return stats


def supplier_options(session: Session) -> list[dict]:
    """订单页「供应商」下拉的可选项（值 = 落库写法「名称（编码）」）。"""
    rows = list_suppliers(session)
    return [
        {"value": supplier_label(row), "text": supplier_label(row), "code": row.code}
        for row in rows
    ]


def relabel_orders(session: Session, supplier: Supplier) -> int:
    """把订单里该供应商的旧写法（编码 / 旧名称）统一为最新的「名称（编码）」。

    供应商改名或补填编码后，历史订单的「所属供应商」跟着一起更新，避免同一家
    在列表里出现多种写法、统计被拆成多行。不提交事务，由调用方统一提交。
    """
    label = supplier_label(supplier)
    conditions = [Order.supplier_name == label]
    name = (supplier.name or "").strip()
    code = (supplier.code or "").strip()
    if name:
        conditions.append(Order.supplier_name == name)
    if code:
        conditions.append(Order.supplier_name == code)
        # 之前按旧名称记录过的「旧名（编码）」一并改过来
        conditions.append(Order.supplier_name.like(f"%（{code}）"))
        conditions.append(Order.supplier_name.like(f"%({code})"))
    changed = (
        session.query(Order)
        .filter(or_(*conditions), Order.supplier_name != label)
        .update(
            {Order.supplier_name: label, Order.updated_at: now()},
            synchronize_session=False,
        )
    )
    if changed:
        logger.info("供应商 %s 关联订单写法已统一：%s 条", label, changed)
    return int(changed or 0)


# ------------------------------------------------------------------ 供应商档案
_TRUTHY = ("1", "on", "true", "yes", "y")


def _clean(data, key: str) -> str:
    """从表单（dict 或 Starlette FormData）里取字符串并去掉首尾空白。"""
    value = data.get(key, "") if hasattr(data, "get") else ""
    return str(value or "").strip()


def _apply_fields(row: Supplier, data) -> None:
    # 供应商编码由后台自行维护（对账/沟通时指认用），允许留空
    row.code = _clean(data, "code")
    row.city = _clean(data, "city")
    row.category = _clean(data, "category")
    row.contact_name = _clean(data, "contact_name")
    row.contact_info = _clean(data, "contact_info")
    has_group = str(data.get("has_group", "") or "").strip().lower() in _TRUTHY
    row.has_group = has_group
    row.group_name = _clean(data, "group_name") if has_group else ""
    row.note = _clean(data, "note")


def list_suppliers(
    session: Session,
    q: str = "",
    city: str = "",
    category: str = "",
    group: str = "",
) -> list[Supplier]:
    stmt = select(Supplier)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Supplier.name.like(like)
            | Supplier.code.like(like)
            | Supplier.category.like(like)
            | Supplier.contact_name.like(like)
            | Supplier.contact_info.like(like)
            | Supplier.group_name.like(like)
        )
    if city:
        stmt = stmt.where(Supplier.city == city)
    if category:
        stmt = stmt.where(Supplier.category == category)
    if group == "1":
        stmt = stmt.where(Supplier.has_group.is_(True))
    elif group == "0":
        stmt = stmt.where(Supplier.has_group.is_(False))
    # 已维护编码的排前面（按编码升序），没编码的按名称升序：列表里的系统序号据此稳定排序
    return list(
        session.scalars(
            stmt.order_by(
                case((Supplier.code == "", 1), else_=0),
                Supplier.code.asc(),
                Supplier.name.asc(),
            )
        ).all()
    )


def distinct_values(session: Session, column) -> list[str]:
    return [
        r[0]
        for r in session.execute(
            select(column).where(column != "").group_by(column).order_by(column)
        ).all()
    ]


def get_supplier(session: Session, supplier_id: int) -> Supplier | None:
    return session.get(Supplier, supplier_id)


def _assert_code_free(session: Session, code: str, exclude_id: int = 0) -> None:
    """编码是订单关联的关键字，不能重复（忽略大小写）。"""
    if not code:
        return
    owner = session.scalar(
        select(Supplier).where(
            func.lower(Supplier.code) == code.lower(), Supplier.id != exclude_id
        )
    )
    if owner is not None:
        raise ValueError(f"供应商编码「{code}」已被「{owner.name}」使用")


def create_supplier(session: Session, data) -> Supplier:
    name = _clean(data, "name")
    if not name:
        raise ValueError("供应商名称不能为空")
    if session.scalar(select(Supplier).where(Supplier.name == name)):
        raise ValueError(f"供应商「{name}」已存在")
    _assert_code_free(session, _clean(data, "code"))
    row = Supplier(name=name)
    _apply_fields(row, data)
    session.add(row)
    session.flush()
    # 档案可能是订单先到、档案后建：把订单里用编码/名称记下的写法统一成展示名
    relabel_orders(session, row)
    session.commit()
    return row


def update_supplier(session: Session, supplier_id: int, data) -> Supplier:
    row = session.get(Supplier, supplier_id)
    if not row:
        raise ValueError("供应商不存在")
    name = _clean(data, "name")
    if not name:
        raise ValueError("供应商名称不能为空")
    dup = session.scalar(
        select(Supplier).where(Supplier.name == name, Supplier.id != supplier_id)
    )
    if dup:
        raise ValueError(f"供应商「{name}」已存在")
    _assert_code_free(session, _clean(data, "code"), exclude_id=supplier_id)
    row.name = name
    _apply_fields(row, data)
    row.updated_at = now()
    session.flush()
    # 改名 / 补填编码后，关联订单里的写法跟着更新
    relabel_orders(session, row)
    session.commit()
    return row


def delete_supplier(session: Session, supplier_id: int) -> None:
    row = session.get(Supplier, supplier_id)
    if not row:
        return
    session.delete(row)
    session.commit()


def order_stats_by_name(
    session: Session, index: SupplierIndex | None = None
) -> dict[str, dict]:
    """按订单里的供应商汇总：单数、销售额、成本、待推单数。

    键统一成展示名（名称（编码））：同一家用编码、名称、名称（编码）记下的订单
    会合并到一行，不会被拆散。
    """
    idx = index or build_index(session)
    stats: dict[str, dict] = {}

    def _row(key: str) -> dict:
        return stats.setdefault(
            key or "未指定", {"orders": 0, "sale": 0.0, "cost": 0.0, "pending": 0}
        )

    rows = session.execute(
        select(
            Order.supplier_name,
            func.count(Order.id),
            func.coalesce(func.sum(Order.sale_amount), 0.0),
            func.coalesce(func.sum(Order.cost_amount), 0.0),
        )
        .where(Order.supplier_name != "")
        .where(not_recalled())
        .group_by(Order.supplier_name)
    ).all()
    for name, count, sale, cost in rows:
        item = _row(idx.group_key(name))
        item["orders"] += int(count or 0)
        item["sale"] += float(sale or 0)
        item["cost"] += float(cost or 0)
    pending_rows = session.execute(
        select(Order.supplier_name, func.count(Order.id))
        .where(
            Order.supplier_name != "",
            pending_push_order(),
        )
        .group_by(Order.supplier_name)
    ).all()
    for name, count in pending_rows:
        _row(idx.group_key(name))["pending"] += int(count or 0)
    for item in stats.values():
        item["sale"] = round(item["sale"], 2)
        item["cost"] = round(item["cost"], 2)
    return stats


def supplier_stat(
    session: Session, supplier: Supplier, index: SupplierIndex | None = None
) -> dict:
    """单个供应商的订单统计：编码或名称写法都算上。"""
    return order_stats_by_name(session, index).get(supplier_label(supplier)) or {
        "orders": 0,
        "sale": 0.0,
        "cost": 0.0,
        "pending": 0,
    }


def supplier_orders(
    session: Session, supplier: Supplier | str, limit: int = 10
) -> list[Order]:
    """该供应商关联的订单：订单里写编码、名称或「名称（编码）」都能命中。"""
    keys = build_index(session).keys(supplier)
    if not keys:
        return []
    return list(
        session.scalars(
            select(Order)
            .where(Order.supplier_name.in_(keys), not_recalled())
            .order_by(Order.id.desc())
            .limit(limit)
        ).all()
    )
