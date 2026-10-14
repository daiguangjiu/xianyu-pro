"""按最新解析规则回填历史订单：只动「没被人工改过」的字段。

背景：解析规则一直在修（无标签收货信息、闲鱼买家编号「[4673]」、备注漏打冒号……），
但修完只有新单受益 —— 库里已经归档的老订单还留着当时解析错的值
（地址被「备注蓝色」污染、收货人带买家编号之类）。这个脚本按新规则重算一遍老订单。

⚠️ 口径（2026-09-29 用户拍板）：**人工改过的字段一律不动**。
   parse_corrections 里留过档的（订单 + 字段）直接跳过，绝不能让机器值把运营
   改对的值冲掉。没被人工改过的字段才按新解析结果回填。

用法（服务器上，app 容器内）：

    cd /app && PYTHONPATH=/app python scripts/reparse_history.py                 # 只看差异（不写库）
    cd /app && PYTHONPATH=/app python scripts/reparse_history.py --apply         # 真正写库
    cd /app && PYTHONPATH=/app python scripts/reparse_history.py --apply --empty-only   # 最保守：只补空字段
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_CANCELED,
    ORDER_STATE_CLOSED,
    Order,
    ParseCorrection,
)
from app.parser.engine import looks_like_address, parse_text  # noqa: E402
from app.services import order_log as order_log_service  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402

# 与 app/services/ingest.py::_apply_parsed 的 mapping 保持一致
FIELDS: dict[str, str] = {
    "order_no": "order_no",
    "buyer_nick": "buyer_nick",
    "product_name": "product_name",
    "spec": "spec",
    "quantity": "quantity",
    "sale_amount": "sale_amount",
    "deal_amount": "deal_amount",
    "total_amount": "total_amount",
    "cost_amount": "cost_amount",
    "shipping_fee": "shipping_fee",
    "platform_fee": "platform_fee",
    "receiver_name": "receiver_name",
    "receiver_phone": "receiver_phone",
    "receiver_address": "receiver_address",
    "supplier_name": "supplier_name",
    "xianyu_account": "xianyu_account",
    "payee_name": "payee_name",
    "note": "note",
}
# 涉及钱的字段：改了必须让人复核一眼（宁可多标一单，也不能悄悄改错金额）
MONEY_FIELDS = frozenset(
    {"sale_amount", "deal_amount", "total_amount", "cost_amount", "shipping_fee", "platform_fee"}
)
# 逆向流程已经走完的单（已撤单 / 关闭完成）：不再动它已有的值，只补空
FINAL_STATES = frozenset({ORDER_STATE_CANCELED, ORDER_STATE_CLOSED})
# 允许「覆盖已有值」的字段：只限收货信息。本轮修的解析 bug 就在这一块
# （无标签三行、买家编号、备注漏冒号污染地址），纯文本解析出来的收货信息比老值准。
# 其余字段（商品名 / 备注 / 金额 / 收款人……）一律只补空：干跑实测过，覆盖会把
# 「Wilson威尔胜小熊网球拍」换成「小熊拍（白色）」、「周玉萍」换成闲鱼昵称，越改越差。
OVERWRITABLE = frozenset({"receiver_name", "receiver_phone", "receiver_address"})


def _same(cur: object, new: object) -> bool:
    """值没变就不用写（金额 141 与 141.0 算一样）。"""
    if isinstance(cur, (int, float)) and isinstance(new, (int, float)):
        return float(cur) == float(new)
    return str(cur or "").strip() == str(new or "").strip()


def _protected(session) -> set[tuple]:
    """人工改过的（订单, 字段）：这些字段一个都不许动。"""
    out: set[tuple] = set()
    for row in session.scalars(select(ParseCorrection)).all():
        if row.order_id:
            out.add((f"id:{int(row.order_id)}", row.field_key))
        if row.order_no:
            out.add((f"no:{str(row.order_no)}", row.field_key))
    return out


def scan(session, *, empty_only: bool, with_ocr: bool, limit: int = 0) -> list[dict]:
    """算出所有「该改但还没改」的字段，不写库。

    默认**不喂截图 OCR**：归档那一步已经融合过一次截图，重解析再喂一遍只会把
    OCR 噪声带进来（2026-09-29 干跑实测：商品名被改成「才思Q2 200002 230WEst」、
    地址变成「和苯谷街道」、金额被拉成 0）。这一轮要修的都是**群消息文本**的解析
    问题（无标签收货信息、备注漏冒号、买家编号），纯文本解析才是对的口径。
    """
    protect = _protected(session)
    stmt = select(Order).order_by(Order.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    changes: list[dict] = []
    skipped_human = 0
    for order in session.scalars(stmt).all():
        text = (order.raw_text or "").strip()
        if not text:
            continue
        ocr_texts = (
            [img.ocr_text for img in order.images if getattr(img, "ocr_text", "")]
            if with_ocr
            else []
        )
        result = parse_text(text, ocr_texts, platform_key=(order.platform or None))
        final = (order.order_state or "") in FINAL_STATES
        for src_key, attr in FIELDS.items():
            if src_key not in result.fields:
                continue
            new = result.fields[src_key]
            cur = getattr(order, attr)
            if new is None or str(new).strip() == "":
                continue
            # 解析出的地址不像地址（截图营销文案之类的噪声）：宁缺勿错，不写
            if attr == "receiver_address" and not looks_like_address(str(new)):
                continue
            if attr == "supplier_name":
                new = supplier_service.ensure_order_supplier(
                    session, str(new), source="群消息订单识别"
                )
            if _same(cur, new):
                continue
            # 人工改过的字段：一律保留人工值
            if (f"id:{order.id}", src_key) in protect or (
                f"no:{order.order_no}",
                src_key,
            ) in protect:
                skipped_human += 1
                continue
            if cur not in (None, "", 0) and (
                empty_only or final or attr not in OVERWRITABLE
            ):
                continue  # 已有值：只补空 / 终态单 / 非收货信息 —— 都不覆盖
            changes.append(
                {
                    "order": order,
                    "attr": attr,
                    "field_key": src_key,
                    "old": cur,
                    "new": new,
                }
            )
    print(f"人工纠错保护：跳过 {skipped_human} 处（这些字段保留运营改的值）")
    return changes


def apply_changes(session, changes: list[dict]) -> int:
    by_order: dict[int, list[dict]] = {}
    for item in changes:
        by_order.setdefault(item["order"].id, []).append(item)
    touched = 0
    for order_id, items in by_order.items():
        order = session.get(Order, order_id)
        parts: list[str] = []
        money_changed = False
        for item in items:
            setattr(order, item["attr"], item["new"])
            parts.append(f"{item['attr']}：{item['old']!r} → {item['new']!r}")
            if item["field_key"] in MONEY_FIELDS:
                money_changed = True
        if money_changed and not order.needs_review:
            order.needs_review = True
            order.review_reason = "历史重解析：金额字段被自动修正，请核对后清除待复核"
        order_log_service.log(
            session,
            order,
            "重解析补录",
            detail="按最新解析规则回填历史订单：" + "；".join(parts),
            operator=order_log_service.SYSTEM_OPERATOR,
        )
        touched += 1
    session.commit()
    return touched


def main() -> int:
    ap = argparse.ArgumentParser(description="按最新解析规则回填历史订单")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报差异）")
    ap.add_argument("--empty-only", action="store_true", help="只补空字段，不覆盖已有值")
    ap.add_argument(
        "--with-ocr", action="store_true", help="连截图 OCR 一起喂（噪声大，默认不开）"
    )
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 笔订单（调试用）")
    ap.add_argument("--show", type=int, default=40, help="最多打印多少条明细")
    args = ap.parse_args()

    init_db()
    with SessionLocal() as session:
        order_count = session.scalar(select(func.count(Order.id)))
        changes = scan(
            session, empty_only=args.empty_only, with_ocr=args.with_ocr, limit=args.limit
        )
        print(f"扫描订单 {order_count} 笔，待回填：{len(changes)} 处")
        for item in changes[: args.show]:
            o = item["order"]
            print(
                f"  #{o.id} {o.order_no} | {item['attr']}: "
                f"{str(item['old'])!r} → {str(item['new'])!r}"
            )
        if len(changes) > args.show:
            print(f"  …… 还有 {len(changes) - args.show} 处")
        if not args.apply:
            print("\n（干跑：没有写库。加 --apply 才会真正回填）")
            return 0
        touched = apply_changes(session, changes)
        print(f"\n已回填 {touched} 笔订单、{len(changes)} 个字段")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
