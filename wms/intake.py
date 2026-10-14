"""批次 / 入库单 / 建档：库存的起点。

两条硬约束（方案 6.1）：
  · **条码生成必须服务端**，客户端不参与（防重号）；
  · **号段按段领用**（batch.serial_start ~ serial_end），禁止边入库边现算导致重号。

条码口径（与方案「条码终身不变」对齐）：
  barcode      ``QC`` + 品类(PH) + 年月(2610) + 4 位流水 → ``QCPH26100012``
               不含等级（等级是可变字段，写进条码就得换码）
  display_code ``QC-PH-2610-0012``（带横杠，标签上印这个给人看）
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    BATCH_DRAFT,
    BATCH_OPEN,
    CATEGORY_CHOICES,
    STATUS_PENDING,
    WH_PENDING,
    WmsBatch,
    WmsInboundOrder,
    WmsItem,
    WmsLabelPrint,
    WmsOutboundItem,
    WmsPartsItem,
    WmsRepairOrder,
    WmsStockLedger,
    WmsStocktakeLine,
    WmsTestRecord,
    l1_code_of,
)
from . import audit
from . import ledger

logger = logging.getLogger(__name__)

BARCODE_PREFIX = "QC"
# 号段默认长度：一批最多 500 台（与方案示例 2603-01-0001~0500 一致）
DEFAULT_SERIAL_SPAN = 500


# 系统货号前缀（2026-10-12）：货上没贴清单号那种非扫码场景，由系统发号，YC = 渔集
SKU_PREFIX = "YC"


def barcode_of(category: str, yymm: str, seq: int) -> str:
    """条码本体：QC + 品类（**只取一级码**）+ 年月 + 4 位流水。

    品类现在是三级码（``HA.KC.DFB``），条码只认第一段 —— 历史条码 ``QCPH…`` 的规则不变，
    扫出来的码也还是 12 位，不会因为品类拆细了就变长。
    """
    cat = l1_code_of(category or "OT")[:2].ljust(2, "O")
    return f"{BARCODE_PREFIX}{cat}{str(yymm or '0000')}{int(seq):04d}"


def display_code_of(barcode: str) -> str:
    """显示编码：QC-PH-2610-0012（带横杠，人看）。"""
    code = str(barcode or "")
    if len(code) < 10:
        return code
    return f"{code[:2]}-{code[2:4]}-{code[4:8]}-{code[8:]}"


def next_sku_code(session: Session, at: datetime | None = None) -> str:
    """系统货号：``YC + 年月 + 4 位流水``（2026-10-12，非扫码场景用）。

    手工建档时货上没贴清单号（旧货 / 回收货常见），由系统发一个唯一货号，
    检测台照样能扫能搜。号按「YC + 年月」前缀取库里已用的最大流水 +1 ——
    与条码流水是两套（条码管入库顺序，货号管货本身），互不影响。
    """
    moment = at or _now()
    yymm = moment.strftime("%y%m")
    prefix = f"{SKU_PREFIX}{yymm}"
    top = 0
    for code in session.scalars(
        select(WmsItem.sku_code).where(WmsItem.sku_code.like(f"{prefix}%"))
    ).all():
        tail = str(code or "")[len(prefix):]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"{prefix}{top + 1:04d}"


def next_batch_no(session: Session, at: datetime | None = None) -> str:
    """批次号：YYMM-NN（同月递增）。"""
    moment = at or _now()
    yymm = moment.strftime("%y%m")
    same = [
        str(row.batch_no or "")
        for row in session.scalars(
            select(WmsBatch).where(WmsBatch.batch_no.like(f"{yymm}-%"))
        ).all()
    ]
    idx = 1
    while f"{yymm}-{idx:02d}" in same:
        idx += 1
    return f"{yymm}-{idx:02d}"


def next_inbound_no(session: Session, at: datetime | None = None) -> str:
    moment = at or _now()
    prefix = "RK" + moment.strftime("%y%m%d")
    cnt = int(
        session.scalar(
            select(func.count()).select_from(WmsInboundOrder).where(
                WmsInboundOrder.order_no.like(f"{prefix}%")
            )
        ) or 0
    )
    return f"{prefix}{cnt + 1:03d}"


def create_batch(session: Session, data, operator: str = "") -> WmsBatch:
    """建批次：预留号段（serial_start ~ serial_end）。

    2026-10-08 口径：**建批次的时候还不知道这批货是什么**（某一家的某一批，什么类目都有），
    所以批次不选品类 —— ``mixed`` 恒真，品类一律按导入表格的每行定（见 ``register``）。
    ``category`` 只留给老数据 / 脚本用，页面不再传。
    """
    category = str(data.get("category") or "").strip().upper()
    source = str(data.get("source_type") or "").strip().lower()
    try:
        qty = int(data.get("qty_expected") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        raise ValueError("预计数量必须大于 0")
    span = max(qty, DEFAULT_SERIAL_SPAN)
    row = WmsBatch(
        batch_no=next_batch_no(session),
        category=category,
        # 品类按导入表格每行定（页面上已经没有品类选择器，这里恒真）
        mixed=True,
        source_type=source or "purchase",
        supplier_id=int(data.get("supplier_id") or 0) or None,
        supplier_name=str(data.get("supplier_name") or "").strip(),
        qty_expected=qty,
        serial_start=1,
        serial_end=span,
        serial_next=1,
        warehouse_code=str(data.get("warehouse_code") or WH_PENDING).upper(),
        status=BATCH_OPEN,
        remark=str(data.get("remark") or "").strip(),
        created_by=str(operator or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    audit.log(session, "batch", row.id, "batch_create", after=row.batch_no, operator=operator)
    session.commit()
    return row


def list_batches(session: Session, q: str = "", status: str = "",
                 only_disabled: bool = False) -> list[WmsBatch]:
    """批次列表：默认只给**启用中**的（禁用的是「不再用了」，但历史货品还在，不删）。

    ``only_disabled=True`` 反过来只看被禁用的 —— 列表页的「已禁用」筛选项用它找回来。
    """
    stmt = select(WmsBatch).where(WmsBatch.enabled.is_(not only_disabled))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(WmsBatch.batch_no.like(like), WmsBatch.supplier_name.like(like))
        )
    if status:
        stmt = stmt.where(WmsBatch.status == status)
    return list(session.scalars(stmt.order_by(WmsBatch.id.desc())).all())


def batch_stats(session: Session, batch_id: int) -> dict:
    """批次进度：已建档 / 已定级 / 各档分布（分级分布报表按批次对比用）。"""
    rows = list(
        session.scalars(select(WmsItem).where(WmsItem.batch_id == batch_id)).all()
    )
    grades: dict[str, int] = {}
    for row in rows:
        key = str(row.grade or "W").upper()
        grades[key] = grades.get(key, 0) + 1
    graded = sum(1 for r in rows if str(r.grade or "W").upper() != "W")
    return {
        "total": len(rows),
        "graded": graded,
        "pending": len(rows) - graded,
        "grades": grades,
        "cost": round(sum(float(r.purchase_cost or 0) for r in rows), 2),
    }


def create_inbound(session: Session, batch_id: int, warehouse_code: str = "",
                   checker: str = "", remark: str = "") -> WmsInboundOrder:
    batch = session.get(WmsBatch, batch_id)
    if not batch:
        raise ValueError("批次不存在")
    row = WmsInboundOrder(
        order_no=next_inbound_no(session),
        batch_id=batch.id,
        batch_no=batch.batch_no,
        warehouse_code=str(warehouse_code or WH_PENDING).upper(),
        total_qty=0,
        checked_qty=0,
        status="draft",
        checker=str(checker or ""),
        remark=str(remark or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    session.commit()
    return row


def _next_seq(session: Session, category: str, yymm: str) -> int:
    """这个「一级品类 + 年月」下一个能用的条码流水号（库里已用的最大号 + 1）。

    条码是 ``QC + 一级品类 + 年月 + 4 位流水``，所以流水号必须**按品类与月份全局唯一**。
    2026-10-08 的坑：批次不再定品类（品类按导入表格**每行**定），可流水还按批次各自从
    0001 起 —— 两个批次的同一品类在同一个月必然撞码，导入时报「条码重复」**整批退回**。
    现在改成按品类查库里已用的最大流水 +1。
    """
    prefix = f"{BARCODE_PREFIX}{l1_code_of(category or 'OT')[:2].ljust(2, 'O')}{yymm}"
    top = 0
    for code in session.scalars(
        select(WmsItem.barcode).where(WmsItem.barcode.like(f"{prefix}%"))
    ).all():
        tail = str(code or "")[len(prefix):]
        if tail.isdigit():
            top = max(top, int(tail))
    return top + 1


def _allocate(session: Session, batch: WmsBatch) -> None:
    """记一笔「这一批又收了一台」，号段上限按需撑开。

    条码流水自 2026-10-08 起由 ``_next_seq`` 按品类 + 年月全局分配，号段退化成
    「这批收了多少台」的进度（页面照旧显示「已用 N / 号段 A~B」）—— 所以这里**不再拦**
    超过号段的情况，只把 serial_end 撑到够（清单一来就是几百行，不该被预计数量挡住）。

    并发下的最后一道防线仍是条码的唯一索引：真撞了报 IntegrityError 由调用方回滚重来。
    """
    batch.serial_next = int(batch.serial_next or 1) + 1
    if int(batch.serial_end or 0) < int(batch.serial_next or 1):
        batch.serial_end = int(batch.serial_next or 1)


def norm_qty(value, hi: int = 1000) -> int:
    """数量：不填就是 1 台；上限由调用方给（Excel 导入 50、手工建档 200）。

    一台货品**可以带 N 台**（同一个条码，不再按数量拆成 N 个码）——
    供应商清单写「数量 3」就是这一条货 3 台，条码终身唯一、数量单独记。
    """
    try:
        qty = int(str(value if value is not None else "1").strip() or 1)
    except (TypeError, ValueError):
        qty = 1
    return max(min(qty, int(hi)), 1)


def register(
    session: Session,
    batch_id: int,
    rows: list[dict],
    operator: str = "",
    inbound_order_id: int | None = None,
    import_id: int | None = None,
) -> list[WmsItem]:
    """一码一条建档：逐条生成唯一条码，写货品 + 入库流水。

    状态一律 PENDING、等级一律 W；入库即写入库流水（账从这一刻开始）。

    行里可以带（Excel 导入用）：
      ``category``   混合品类批次按行定品类（空 = 用批次的品类）
      ``sku_code``   货号（供应商清单里的编号，检测时扫它）
      ``extra``      沉淀下来的扩展字段（dict，存 wms_items.extra）
      ``qty``        这一条货有几台（2026-10-10，默认 1；不再拆成多台多个码）
    """
    batch = session.get(WmsBatch, batch_id)
    if not batch:
        raise ValueError("批次不存在")
    if not rows:
        raise ValueError("至少要有一台货品")

    yymm = (_now()).strftime("%y%m")
    created: list[WmsItem] = []
    seq_pool: dict[str, int] = {}  # 一级品类 → 下一个流水（本批内自己往上加，不重复查库）
    for raw in rows:
        # 品类按行定（混合批次）：条码里的品类码跟着这一行走，不是整批一个
        category = str(raw.get("category") or "").strip().upper() or batch.category
        _allocate(session, batch)  # 记一笔这批又收了一台（号段按需撑开）
        cat_key = l1_code_of(category or "OT")[:2]
        if cat_key not in seq_pool:
            seq_pool[cat_key] = _next_seq(session, category, yymm)
        seq = seq_pool[cat_key]
        seq_pool[cat_key] = seq + 1
        barcode = barcode_of(category, yymm, seq)
        if session.scalar(select(WmsItem).where(WmsItem.barcode == barcode)):
            raise ValueError(f"条码重复（{barcode}），请重试")
        item = WmsItem(
            barcode=barcode,
            display_code=display_code_of(barcode),
            batch_id=batch.id,
            batch_no=batch.batch_no,
            inbound_order_id=inbound_order_id,
            category=category,
            brand=str(raw.get("brand") or "").strip(),
            product_name=str(raw.get("product_name") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            spec=str(raw.get("spec") or "").strip(),
            serial_no=str(raw.get("serial_no") or "").strip(),
            # 数量（2026-10-10）：这一条货有几台，同一条码、整台流转
            qty=norm_qty(raw.get("qty")),
            sku_code=str(raw.get("sku_code") or "").strip(),
            own_barcode=str(raw.get("own_barcode") or "").strip(),
            extra=dict(raw.get("extra") or {}),
            import_id=import_id,
            grade="W",
            status=STATUS_PENDING,
            warehouse_code="",
            location_code="",
            purchase_cost=float(raw.get("purchase_cost") or 0.0),
            # 渔货价 / 建议售价（2026-10-12）：建档时人工填的初始值（照渔枢的价格口径）。
            # 留空（0）就等检测定级时按「市场价 × 该档系数 / 建议售价系数」算出来。
            selling_price=float(raw.get("selling_price") or 0.0),
            suggest_price=float(raw.get("suggest_price") or 0.0),
            base_price=float(raw.get("base_price") or 0.0),
            photos=list(raw.get("photos") or []),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(item)
        session.flush()
        ledger.inbound(
            session, item,
            biz_id=batch.batch_no,
            operator=operator,
            remark=f"批次 {batch.batch_no} 入库建档",
        )
        audit.log(session, "item", item.id, "inbound", after=barcode,
                  operator=operator, note=f"批次 {batch.batch_no}")
        created.append(item)

    # 台数口径：按数量累加（一条货带 N 台就是 N 台，不是 1 条）
    total = sum(int(row.qty or 0) or 1 for row in created)
    batch.qty_actual = int(batch.qty_actual or 0) + total
    batch.updated_at = _now()
    if inbound_order_id:
        order = session.get(WmsInboundOrder, inbound_order_id)
        if order:
            order.total_qty = int(order.total_qty or 0) + total
            order.status = "done"
            order.updated_at = _now()
    session.commit()
    return created


def register_bulk(
    session: Session,
    batch_id: int,
    *,
    brand: str,
    model: str,
    spec: str = "",
    product_name: str = "",
    serials: list[str],
    purchase_cost: float = 0.0,
    selling_price: float = 0.0,
    suggest_price: float = 0.0,
    base_price: float = 0.0,
    operator: str = "",
    inbound_order_id: int | None = None,
    category: str = "",
    sku_code: str = "",
    own_barcode: str = "",
    extra: dict | None = None,
) -> list[WmsItem]:
    """按型号批量：同型号多台，只有序列号不同（Excel 导入的序列号走这里）。

    导入时校验空号与重复号，并把行号写进提示，方便 3 秒内定位到具体行。

    ``category`` / ``sku_code`` / ``own_barcode`` / ``extra``（2026-10-08 补）：
    手工建档页跟 Excel 导入**字段口径一致**，批量这条路也不能少字段
    —— 以前这四项只有单条路径才有，批量建出来的货缺货号、缺品类、缺商品字段。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for idx, raw in enumerate(serials or [], start=1):
        value = str(raw or "").strip()
        if not value:
            raise ValueError(f"第 {idx} 行序列号为空白，请补全后再导入")
        if value in seen:
            raise ValueError(f"第 {idx} 行序列号与前面重复：{value}")
        seen.add(value)
        cleaned.append(value)
    existed = {
        str(row.serial_no or "")
        for row in session.scalars(select(WmsItem).where(WmsItem.serial_no.in_(cleaned))).all()
    }
    for idx, value in enumerate(cleaned, start=1):
        if value in existed:
            raise ValueError(f"第 {idx} 行序列号已建档：{value}")
    rows = [
        {
            "brand": brand, "model": model, "spec": spec, "serial_no": value,
            "product_name": product_name,
            "purchase_cost": purchase_cost, "base_price": base_price,
            "selling_price": selling_price, "suggest_price": suggest_price,
            "category": category, "sku_code": sku_code, "own_barcode": own_barcode,
            "extra": dict(extra or {}),
        }
        for value in cleaned
    ]
    return register(session, batch_id, rows, operator=operator,
                    inbound_order_id=inbound_order_id)


def find_item(session: Session, code: str) -> WmsItem | None:
    """扫码枪扫什么都能认：**系统条码 / 显示编码 / 检修后条码 / 货号 / 序列号**。

    现场最常见的两种扫法：进仓时扫供应商货号（Excel 建档写进 sku_code），
    检修规整完自己贴了码之后扫自有码（own_barcode）—— 都在这里认。
    """
    raw = str(code or "").strip()
    if not raw:
        return None
    row = session.scalar(select(WmsItem).where(WmsItem.barcode == raw.upper()))
    if row:
        return row
    compact = raw.replace("-", "").upper()
    if compact != raw.upper():
        row = session.scalar(select(WmsItem).where(WmsItem.barcode == compact))
        if row:
            return row
    row = session.scalar(select(WmsItem).where(WmsItem.own_barcode == raw))
    if row:
        return row
    row = session.scalar(select(WmsItem).where(WmsItem.sku_code == raw))
    if row:
        return row
    return session.scalar(select(WmsItem).where(WmsItem.serial_no == raw))


OWN_BARCODE_PREFIX = "WMS"


def next_own_barcode(session: Session, at: datetime | None = None) -> str:
    """检修后自有码的生成规则：``WMS`` + 年月 + 4 位流水（全库累计，不按天重置）。

    现场多数是拿现成的码贴上去再扫进来（手填 / 扫码录入），这个规则只是「没有码可贴」时的兜底。
    """
    moment = at or _now()
    prefix = f"{OWN_BARCODE_PREFIX}{moment.strftime('%y%m')}"
    used: set[int] = set()
    for value in session.scalars(
        select(WmsItem.own_barcode).where(WmsItem.own_barcode.like(f"{prefix}%"))
    ).all():
        tail = str(value or "")[len(prefix):]
        if tail.isdigit():
            used.add(int(tail))
    seq = 1
    while seq in used:
        seq += 1
    return f"{prefix}{seq:04d}"


def set_own_barcode(session: Session, item: WmsItem, code: str, operator: str = "") -> WmsItem:
    """贴 / 换 / 清「检修后条码」：一码一台，贴过就不能贴到第二台；空串 = 清掉（贴错了）。"""
    raw = str(code or "").strip()
    if raw:
        dup = session.scalar(
            select(WmsItem).where(WmsItem.own_barcode == raw, WmsItem.id != item.id)
        )
        if dup:
            raise ValueError(f"这个码已经贴在 {dup.display_code or dup.barcode} 上了（一码一台）")
    before = str(item.own_barcode or "")
    item.own_barcode = raw
    item.own_barcode_at = _now() if raw else None
    item.updated_at = _now()
    audit.log(session, "item", item.id, "own_barcode", before=before, after=raw,
              operator=operator, note="检修后自有条码" if raw else "清除自有条码")
    session.commit()
    return item


def update_item(
    session: Session,
    item: WmsItem,
    *,
    fields: dict,
    extra: dict | None = None,
    operator: str = "",
    remark: str = "",
    labels: dict | None = None,
) -> list[str]:
    """改一台货品的商品字段（建档 / 导入进来的都能改），返回改动摘要供页面播报。

    **条码不动** —— ``barcode`` 是终身凭据（等级、品类都不写进码的换码逻辑，见方案 6.1），
    改品类也不会重发条码；要换码只能删了重录（走 ``delete_item``）。

    货号要查重：货号是扫码检测认货的凭据，两台货挂同一个货号，扫出来就不知道是哪台了。
    """
    changed: list[str] = []
    text_fields = (
        ("sku_code", "货号"), ("product_name", "产品名"), ("brand", "品牌"),
        ("model", "型号"), ("spec", "规格"), ("serial_no", "序列号"), ("category", "品类"),
    )
    for key, label in text_fields:
        new = str(fields.get(key) or "").strip()
        old = str(getattr(item, key) or "").strip()
        if key == "category":
            new, old = new.upper(), old.upper()
        if new == old:
            continue
        if key == "sku_code" and new:
            dup = session.scalar(
                select(WmsItem).where(WmsItem.sku_code == new, WmsItem.id != item.id)
            )
            if dup:
                raise ValueError(
                    f"货号 {new} 已经被 {dup.display_code or dup.barcode} 用了 —— "
                    f"货号是扫码认货的凭据，两台货不能挂同一个"
                )
        setattr(item, key, new)
        changed.append(f"{label}：{old or '（空）'} → {new or '（空）'}")
    # 价格（2026-10-12 补齐渔货价 / 建议售价）：成本价 / 渔货价 / 建议售价 / 市场价（基准价）
    for key, label in (
        ("purchase_cost", "成本价"), ("selling_price", "渔货价"),
        ("suggest_price", "建议售价"), ("base_price", "市场价"),
    ):
        new_cost = round(float(fields.get(key) or 0.0), 2)
        old_cost = round(float(getattr(item, key) or 0.0), 2)
        if new_cost != old_cost:
            setattr(item, key, new_cost)
            changed.append(f"{label}：¥{old_cost:g} → ¥{new_cost:g}")
    # 数量（2026-10-10）：这台货到底有几台 —— 改数量要写差额流水（余额由流水说得清）
    if fields.get("qty") is not None:
        new_qty = norm_qty(fields.get("qty"))
        old_qty = int(item.qty or 0) or 1
        if new_qty != old_qty:
            ledger.change_qty(session, item, new_qty, operator=operator,
                              remark=f"改数量 {old_qty} → {new_qty} 台")
            changed.append(f"数量：{old_qty} 台 → {new_qty} 台")
    if extra is not None:
        merged = dict(item.extra or {})
        for key, value in extra.items():
            label = str((labels or {}).get(key) or key)
            text = str(value if value is not None else "").strip()
            if text:
                if str(merged.get(key) or "") != text:
                    old_text = str(merged.get(key) or "").strip()
                    merged[key] = text
                    changed.append(f"{label}：{old_text or '（空）'} → {text}")
            elif key in merged:
                merged.pop(key)
                changed.append(f"{label} 已清空")
        item.extra = merged
    if changed:
        item.updated_at = _now()
        audit.log(session, "item", item.id, "edit", before=item.barcode,
                  after="；".join(changed[:6]), operator=operator,
                  note=remark or "改商品字段（条码终身不变）")
        session.commit()
    return changed


def delete_item(session: Session, item: WmsItem, *, operator: str = "",
                remark: str = "") -> dict:
    """删一台货品（错录清理）：**有业务痕迹的不许删**，没痕迹的连账面一起清干净。

    拦下来的情形（这些说明货已经真流动过，删了流水就对不上账，改用核销 / 报废）：
    做检测、上出库单、有报修工单、转过拆机件、进过盘点单、正被锁货。

    允许删时：删掉这台自己的库存流水与标签打印记录 → 删货品 → ``ledger.rebuild()``
    按剩下的流水重算余额（**严禁直接改余额表**）。
    """
    blockers: list[str] = []
    checks = (
        (WmsTestRecord, WmsTestRecord.item_id, "已经做过检测"),
        (WmsOutboundItem, WmsOutboundItem.item_id, "上过出库单"),
        (WmsRepairOrder, WmsRepairOrder.item_id, "有报修工单"),
        (WmsPartsItem, WmsPartsItem.item_id, "已转成拆机件"),
    )
    for model, column, why in checks:
        hit = session.scalar(select(func.count()).select_from(model).where(column == item.id))
        if hit:
            blockers.append(why)
    if item.barcode:
        take = session.scalar(
            select(func.count()).select_from(WmsStocktakeLine)
            .where(WmsStocktakeLine.barcode == item.barcode)
        )
        if take:
            blockers.append("进过盘点单")
    if str(item.locked_by or "").strip():
        blockers.append(f"正被 {item.locked_by} 锁着")
    if blockers:
        raise ValueError(
            f"这台货{'、'.join(blockers)}，删不得（删了流水与账就对不上）—— "
            f"要清账请走核销 / 报废流程"
        )

    snapshot = {
        "barcode": item.barcode,
        "display_code": item.display_code,
        "sku_code": str(item.sku_code or ""),
        "title": item.title,
        "id": item.id,
    }
    # ① 清掉这台留下的账面痕迹（库存流水 + 标签打印记录），余额随后按剩下的流水重算
    session.query(WmsStockLedger).filter(WmsStockLedger.item_id == item.id).delete(
        synchronize_session=False
    )
    session.query(WmsLabelPrint).filter(WmsLabelPrint.item_id == item.id).delete(
        synchronize_session=False
    )
    # ② 批次计数跟着退一台
    batch = session.get(WmsBatch, item.batch_id) if item.batch_id else None
    if batch:
        batch.qty_actual = max(0, int(batch.qty_actual or 0) - (int(item.qty or 0) or 1))
        batch.updated_at = _now()
    audit.log(session, "item", item.id, "delete", before=item.barcode, after="",
              operator=operator,
              note=remark or f"删除货品 {snapshot['sku_code'] or snapshot['barcode']}（错录清理）")
    session.delete(item)
    session.flush()
    # ③ 按剩下的流水重算余额（rebuild 内部会 commit）
    ledger.rebuild(session)
    return snapshot


def find_items_by_sku(session: Session, code: str) -> list[WmsItem]:
    """同一个货号可能有多台（表格里数量 > 1）：扫到重号时给检测员挑一台。"""
    raw = str(code or "").strip()
    if not raw:
        return []
    return list(
        session.scalars(
            select(WmsItem).where(WmsItem.sku_code == raw).order_by(WmsItem.id.asc())
        ).all()
    )


def _items_query(
    session: Session,
    q: str = "",
    status: str = "",
    grade: str = "",
    warehouse: str = "",
    batch_id: int | None = None,
    category: str = "",
):
    """台账的筛选条件（列表与计数共用一份，省得两处写法走样）。"""
    stmt = select(WmsItem)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                WmsItem.barcode.like(like),
                WmsItem.display_code.like(like),
                WmsItem.brand.like(like),
                WmsItem.product_name.like(like),
                WmsItem.model.like(like),
                WmsItem.spec.like(like),
                WmsItem.serial_no.like(like),
                WmsItem.sku_code.like(like),
                WmsItem.own_barcode.like(like),
                WmsItem.batch_no.like(like),
            )
        )
    if status:
        stmt = stmt.where(WmsItem.status == status)
    if grade:
        stmt = stmt.where(WmsItem.grade == grade)
    if warehouse:
        stmt = stmt.where(WmsItem.warehouse_code == warehouse)
    if batch_id:
        stmt = stmt.where(WmsItem.batch_id == batch_id)
    if category:
        stmt = stmt.where(WmsItem.category == category)
    return stmt


def list_items(
    session: Session,
    q: str = "",
    status: str = "",
    grade: str = "",
    warehouse: str = "",
    batch_id: int | None = None,
    category: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[WmsItem]:
    """台账列表：按 id 倒序（新的在前），``offset`` 给分页用。"""
    stmt = _items_query(session, q=q, status=status, grade=grade, warehouse=warehouse,
                        batch_id=batch_id, category=category)
    stmt = stmt.order_by(WmsItem.id.desc()).limit(int(limit))
    if offset:
        stmt = stmt.offset(int(offset))
    return list(session.scalars(stmt).all())


def count_items(
    session: Session,
    q: str = "",
    status: str = "",
    grade: str = "",
    warehouse: str = "",
    batch_id: int | None = None,
    category: str = "",
) -> int:
    """符合条件的货品总数（2026-10-11 用户：867 台只看到 200 条 —— 台账要能翻页）。
    以前页面标题写的是「共 len(rows) 台」，行数被 limit 截断就跟着骗人，改成真 count。"""
    stmt = _items_query(session, q=q, status=status, grade=grade, warehouse=warehouse,
                        batch_id=batch_id, category=category)
    return int(session.scalar(stmt.with_only_columns(func.count(WmsItem.id))) or 0)
