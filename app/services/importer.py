"""历史订单导入：把运营手上的登记表（xlsx）按系统结构导入成订单。

业务口径（2026-09）：登记表里通常只有「供应商 / 产品 / 成本价 / 出货价 / 出单账号 /
带教归属人 / 付款金额」这类账目字段，没有订单号也没有收货信息 —— 导入时补成系统认得的结构：

  · 订单号：登记表没有截图可补，落 ``HIS-年月日-序号``（**不是** TMP- 临时号，
    否则系统会一直把它当「等截图补单号」而去群里催图、回执里也不显示单号）；
  · 业务日：用表格里的日期写 ``ordered_at``，统计趋势与资金结算都按这一天归；
  · 状态：已经付款给供应商 -> 「已推单」+「已转账」，不占待办列表，也不重复催转账；
    付款金额为 0 -> 「未转账」，留给运营按实际付款情况核销；
  · 资金：导入后按系统口径生成「公司自营应收 / 公司自营应付」两条结算流水，
    已付款的那笔应付直接核销 —— 和群里归档出来的订单口径完全一致。

同一个文件重复导入不会重复建单：按「日期 + 出单账号 + 产品 + 售价 + 成本」判重。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ORDER_STATE_PUSHED,
    Order,
)
from ..services import finance as finance_service
from ..services import order_log as order_log_service
from ..services import supplier as supplier_service
from ..services import shop as shop_service
from ..timeutil import beijing_now, now

logger = logging.getLogger(__name__)

# 历史订单单号前缀（区别于 TMP- 临时号：登记表没有订单截图可补，也不用催图）
HISTORY_PREFIX = "HIS-"

ACTION_CREATE = "create"
ACTION_SKIP = "skip"
ACTION_ERROR = "error"

# 表头别名：登记表列名换说法也能认（键=系统字段，值=可能出现的列名）
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "day": ("日期", "出单日期", "下单日期", "业务日期"),
    "supplier": ("供应商", "所属供应链", "供应链", "上游供应商"),
    "product": ("产品", "产品名称", "商品", "商品名称"),
    "model": ("型号", "规格"),
    "color": ("颜色", "配色"),
    "cost": ("成本价", "成本", "采购价"),
    "sale": ("出货价", "售价", "销售价", "卖出价"),
    "account": ("出单账号", "出货闲鱼号", "闲鱼号", "出单号"),
    "owner": ("带教归属人", "归属人", "回款支付宝归属人", "回款归属"),
    "paid": ("付款金额", "已付金额", "付款", "实付"),
    "note": ("备注", "说明"),
}

_NOISE_MODEL = {"·", "-", "—", "/", "无", ""}


def _clean(value: object) -> str:
    """单元格 -> 字符串（换行压成空格，去空白）。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _money(value: object) -> float:
    """单元格 -> 金额（拿不到就 0）。"""
    if value is None or isinstance(value, str) and not value.strip():
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return round(float(text), 2) if text not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def _day(value: object) -> date | None:
    """单元格 -> 日期（支持 datetime / date / 2026-09-24 / 2026/9/24 / Excel 序列号）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:
        # Excel 日期序列号（2020 年之后的常见范围）
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _map_columns(header: list[str]) -> dict[str, int]:
    """表头 -> {系统字段: 列下标}（按别名匹配，找不到就缺该字段）。"""
    mapping: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = _clean(name)
        for field, aliases in _COLUMN_ALIASES.items():
            if field in mapping:
                continue
            if key in aliases or any(key.startswith(a) for a in aliases):
                mapping[field] = idx
                break
    return mapping


def read_sheet(path: str | Path, sheet: str = "") -> tuple[list[dict], str]:
    """读登记表的一个工作表，返回 (记录列表, 实际用的表名)。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    name = sheet or (wb.sheetnames[0] if wb.sheetnames else "")
    ws = wb[name] if name in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], name

    header = [_clean(c) for c in rows[0]]
    mapping = _map_columns(header)
    if "product" not in mapping:
        return [], name

    out: list[dict] = []
    for row in rows[1:]:
        if row is None or not any(c is not None and str(c).strip() for c in row):
            continue

        def cell(field: str) -> object:
            idx = mapping.get(field)
            return row[idx] if idx is not None and idx < len(row) else None

        product = _clean(cell("product"))
        if not product:
            continue
        model = _clean(cell("model"))
        color = _clean(cell("color"))
        spec = " ".join(x for x in (model, color) if x and x not in _NOISE_MODEL)
        day = _day(cell("day"))
        out.append(
            {
                "day": day,
                "supplier": _clean(cell("supplier")),
                "product": product,
                "spec": spec,
                "cost": _money(cell("cost")),
                "sale": _money(cell("sale")),
                "account": _clean(cell("account")),
                "owner": _clean(cell("owner")),
                "paid": _money(cell("paid")),
                "note": _clean(cell("note")),
            }
        )
    return out, name


def _norm_product(text: str) -> str:
    """产品名归一（判重用）：只留中英文数字，去掉空格与标点差异。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text or "")).lower()


def _next_order_no(session: Session, day: date) -> str:
    """生成当天不重复的历史订单号 HIS-年月日-序号。"""
    prefix = f"{HISTORY_PREFIX}{day.strftime('%Y%m%d')}-"
    used = {
        str(x)
        for x in session.scalars(
            select(Order.order_no).where(Order.order_no.like(f"{prefix}%"))
        ).all()
    }
    seq = 1
    while f"{prefix}{seq:02d}" in used:
        seq += 1
    return f"{prefix}{seq:02d}"


def _find_same(session: Session, row: dict) -> Order | None:
    """判重：同一天 + 同一个出单账号 + 同一个产品 + 售价成本都一致，就是同一笔。"""
    if row.get("day") is None:
        return None
    start = datetime.combine(row["day"], datetime.min.time())
    end = start.replace(hour=23, minute=59, second=59)
    candidates = session.scalars(
        select(Order).where(
            Order.ordered_at.is_not(None),
            Order.ordered_at >= start,
            Order.ordered_at <= end,
        )
    ).all()
    wanted = _norm_product(row["product"])
    for order in candidates:
        if (order.xianyu_account or "").strip() != row["account"]:
            continue
        if _norm_product(order.product_name) != wanted:
            continue
        if round(float(order.sale_amount or 0), 2) != row["sale"]:
            continue
        if round(float(order.cost_amount or 0), 2) != row["cost"]:
            continue
        return order
    return None


def plan(session: Session, rows: list[dict]) -> list[dict]:
    """逐条算出要做什么（create / skip / error），不写库。"""
    plans: list[dict] = []
    for row in rows:
        item = dict(row)
        if not row.get("product"):
            item.update(action=ACTION_ERROR, reason="没有产品名")
            plans.append(item)
            continue
        same = _find_same(session, row)
        if same is not None:
            item.update(action=ACTION_SKIP, reason=f"已存在同一笔订单 #{same.id} {same.order_no}")
            plans.append(item)
            continue
        item.update(action=ACTION_CREATE, reason="")
        plans.append(item)
    return plans


def _build_order(session: Session, row: dict) -> Order:
    day: date = row["day"] or beijing_now().date()
    day_dt = datetime.combine(day, datetime.min.time())
    paid = row["paid"] > 0
    order = Order(
        platform="xianyu",
        order_no=_next_order_no(session, day),
        ordered_at=day_dt,
        product_name=row["product"],
        spec=row.get("spec") or "",
        quantity=1,
        sale_amount=row["sale"],
        deal_amount=row["sale"],
        cost_amount=row["cost"],
        supplier_name=supplier_service.ensure_order_supplier(
            session, row.get("supplier") or "", source="登记表导入"
        ),
        xianyu_account=row.get("account") or "",
        payee_name=row.get("owner") or "",
        # 已经付过款给供应商：算「已推单 + 已转账」，不占待办也不重复催转账
        supplier_status="pushed",
        pushed_at=day_dt,
        transfer_status=finance_service.TRANSFER_PAID if paid else "unpaid",
        transfer_paid_at=day_dt if paid else None,
        order_state=ORDER_STATE_PUSHED,
        note=row.get("note") or "",
        raw_text=(
            f"历史订单导入（付款登记表 {day.strftime('%Y-%m-%d')}）："
            f"{row['product']} / 成本 {row['cost']} / 出货价 {row['sale']}"
            + (f" / 已付款 {row['paid']}" if paid else " / 未付款")
        ),
        parse_confidence=1.0,
        created_at=day_dt,
        updated_at=day_dt,
    )
    session.add(order)
    session.flush()
    return order


def import_rows(
    session: Session,
    rows: list[dict],
    *,
    actor: str = "",
    apply: bool = False,
    commit: bool = True,
) -> dict:
    """导入一批记录（apply=False 只算计划不动库），返回汇总。"""
    plans = plan(session, rows)
    summary = {
        "total": len(plans),
        "created": 0,
        "skipped": 0,
        "failed": 0,
        "applied": bool(apply),
        "items": [],
    }
    for item in plans:
        action = item["action"]
        if action != ACTION_CREATE or not apply:
            # 跳过 / 出错 / 只预览（预览也算进「计划新建」，方便先看清单再决定）
            key = {
                ACTION_SKIP: "skipped",
                ACTION_ERROR: "failed",
            }.get(action, "created")
            summary[key] += 1
            summary["items"].append(
                {
                    "action": action,
                    "product": item.get("product", ""),
                    "day": item["day"].strftime("%Y-%m-%d") if item.get("day") else "",
                    "reason": item.get("reason", ""),
                }
            )
            continue
        try:
            order = _build_order(session, item)
            # 按系统口径生成 / 更新两条结算流水（已付款的应付直接核销）
            finance_service.sync_order(session, order)
            # 会话关闭了 autoflush：先落库，再让「已转账」把应付流水核销掉
            session.flush()
            finance_service.sync_transfer(session, order)
            # 出货账号（店铺）：店铺管理里还没有这个账号就自动补一条
            shop_service.ensure_shop(
                session, order.xianyu_account, operator="", source="登记表导入"
            )
            order_log_service.log(
                session,
                order,
                order_log_service.ACTION_IMPORT,
                f"历史订单导入：{item['product']}（登记表 {item['day']}）",
                operator=actor,
            )
        except Exception as exc:  # noqa: BLE001  一条失败不影响其它行
            logger.warning("历史订单导入失败 %s: %s", item.get("product"), exc)
            summary["failed"] += 1
            summary["items"].append(
                {
                    "action": ACTION_ERROR,
                    "product": item.get("product", ""),
                    "day": item["day"].strftime("%Y-%m-%d") if item.get("day") else "",
                    "reason": str(exc),
                }
            )
            continue
        summary["created"] += 1
        summary["items"].append(
            {
                "action": ACTION_CREATE,
                "product": item.get("product", ""),
                "day": item["day"].strftime("%Y-%m-%d") if item.get("day") else "",
                "order_no": order.order_no,
                "order_id": order.id,
            }
        )
    if apply and commit:
        session.commit()
    return summary
