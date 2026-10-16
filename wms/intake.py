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
import re
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
from . import grading
from . import ledger
from . import profile_guess

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


FISH_PREFIX = "YS"
# 认亲指纹里保留的字符：中英文数字，其余（空格 / 标点 / 全角符号）统统压掉
_FISH_KEEP = re.compile(r"[^0-9a-z一-鿿]+")


def fish_name_key(name: str) -> str:
    """商品名指纹：**同一款货要不要认成同一个渔货号，全看它**。

    同一款货在不同批次、不同供应商清单里写出来的名字常常只差一两个符号
    （``飞利浦电动牙刷 HX2431`` 与 ``飞利浦电动牙刷HX2431 `` 必须是一款），
    所以比对前先把大小写、空格、标点、全角符号全去掉，只留字。
    """
    return _FISH_KEEP.sub("", str(name or "").strip().lower())


def next_fish_code(
    session: Session,
    at: datetime | None = None,
    used: "Iterable[str] | None" = None,
) -> str:
    """渔货号：``YS + 建档日期(8 位) + 当天 4 位流水``（例 ``YS202610140001``）。

    ``used`` 是**这次还没落库就已经用掉的号**（批量回填场景）：批量补号时几百行
    要一次发几百个号，库里 read 出来的最大流水还是 0 —— 不把它们算进来，
    一整批货会全挤在同一个渔货号上。

    三套号各管一段，互不干涉：
      ``barcode``   条码 ``QC…``     入库顺序 + 品类，终身不变（物理的那台货）
      ``sku_code``  进货货号         供应商清单上的外部编号（一台一个，去重不了）
      ``fish_code`` 渔货号 ``YS…``   渔集自己的**商品款**（同款多台挂一个）
    """
    moment = at or _now()
    prefix = f"{FISH_PREFIX}{moment.strftime('%Y%m%d')}"
    top = 0
    for code in session.scalars(
        select(WmsItem.fish_code).where(WmsItem.fish_code.like(f"{prefix}%"))
    ).all():
        tail = str(code or "")[len(prefix):]
        if tail.isdigit():
            top = max(top, int(tail))
    for code in used or ():
        raw = str(code or "")
        if raw.startswith(prefix):
            tail = raw[len(prefix):]
            if tail.isdigit():
                top = max(top, int(tail))
    return f"{prefix}{top + 1:04d}"


def fish_index(session: Session) -> dict[str, str]:
    """把库里的「商品名指纹 → 渔货号」一次性捞出来（建档批量认亲用）。

    有了它 ``register`` 就一行也不用再查一次全表 —— 直接拿字典比对。
    """
    out: dict[str, str] = {}
    for fish, name in session.execute(
        select(WmsItem.fish_code, WmsItem.product_name).where(WmsItem.fish_code != "")
    ).all():
        key = fish_name_key(name)
        if key and key not in out:
            out[key] = str(fish or "")
    return out


def resolve_fish_code(
    session: Session,
    name: str,
    index: dict[str, str] | None = None,
    at: datetime | None = None,
) -> str:
    """给一行新货定渔货号：**先认亲**（库里有同款就挂进去），认不到才发新号。

    ``index`` 传 ``fish_index(session)`` 的结果以避免逐行查库（建档批量场景）。
    没有商品名的货（老数据 / 手录）认不了亲，一律发新号 —— 事后可以在台账页合并。
    """
    pool = fish_index(session) if index is None else index
    key = fish_name_key(name)
    if key:
        hit = pool.get(key)
        if hit:
            return hit
    code = next_fish_code(session, at=at)
    if key:
        pool[key] = code
    return code


def set_fish_code(session: Session, item: WmsItem, code: str, operator: str = "",
                  note: str = "") -> WmsItem:
    """把一台货挂到某个渔货号上 —— **台账页的合并 / 拆分就靠它**。

    ``code`` 传一个已存在的渔货号 = 并进去；传空串 = **拆出来**单独发一个新渔货号
    （同一款货其实是两台不同的东西，比如成色差得多不想放在一起卖）。
    渔货号只认现有 / 新发两种，不校验存在性（挂到不存在的号上没有意义，但也不至于出错）。

    ``note`` 给调用方定制审计说明（如检测台的「独立渔货号重新建档」）；不传就按
    有原号 / 没原号自动写成「渔货号换绑」「补发渔货号」。
    """
    raw = str(code or "").strip().upper()
    if not raw:
        raw = next_fish_code(session)
    before = str(item.fish_code or "")
    if raw == before:
        return item
    item.fish_code = raw
    item.updated_at = _now()
    audit.log(session, "item", item.id, "fish_code", before=before, after=raw,
              operator=operator,
              note=note or ("渔货号换绑" if before else "补发渔货号"))
    session.commit()
    return item


def split_fish(session: Session, code: str, operator: str = "") -> list[str]:
    """**多条码拆分**（2026-10-15 用户拍板）：一个渔货号只该对应一个条码商品。

    一款下面挂了多个条码时，把**最早建档那台**留在原号上（渔枢商品档案的商家编码
    就是它，别动），其余每台各自**拆出去发一个新渔货号**（同款但成色 / 配置不同
    本来就不该挤在一个号里卖）。返回发出去的新渔货号清单。

    审计每台一条（action=fish_code，note=多条码拆分），来源可查。
    """
    rows = find_items_by_fish(session, code)
    if len(rows) <= 1:
        raise ValueError(
            f"{code or '（空号）'} 名下只有 {len(rows)} 个条码商品，不用拆"
        )
    keep, extras = rows[0], rows[1:]
    new_codes: list[str] = []
    for row in extras:
        row_text = row.display_code or row.barcode or f"#{row.id}"
        set_fish_code(session, row, "", operator=operator,
                      note=f"多条码拆分：{row_text} 从 {code} 拆出，独立发新渔货号")
        new_codes.append(str(row.fish_code or ""))
    audit.log(session, "item", keep.id, "fish_code", before=code, after=code,
              operator=operator,
              note=f"多条码拆分：保留 {len(extras)} 台拆出（{'、'.join(new_codes)}）")
    session.commit()
    return new_codes


def find_items_by_fish(session: Session, code: str) -> list[WmsItem]:
    """一个渔货号下挂着的所有条码商品（台账详情 / 检测台挑货用）。"""
    raw = str(code or "").strip().upper()
    if not raw:
        return []
    return list(
        session.scalars(
            select(WmsItem).where(WmsItem.fish_code == raw).order_by(WmsItem.id)
        ).all()
    )


def fish_groups(session: Session) -> dict[str, dict]:
    """**每个渔货号一行概览**：``{渔货号: {name, brand, items, qty, keys}}``。

    一次扫全表攒出来（几千台也就一遍），检测台的「同款候选」与台账都靠它，
    免得逐款回库再查一次。``keys`` 是这个号下面出现过的**商品名指纹**集合 ——
    同一款下面几台货的名字可能写得略有出入（多个空格、多一个后缀），都得认。
    """
    out: dict[str, dict] = {}
    for row in session.scalars(
        select(WmsItem).where(WmsItem.fish_code != "").order_by(WmsItem.id)
    ).all():
        code = str(row.fish_code or "").strip().upper()
        slot = out.get(code)
        if slot is None:
            slot = out[code] = {
                "fish_code": code,
                "name": str(row.product_name or ""),
                "brand": str(row.brand or ""),
                "items": 0, "qty": 0, "keys": set(),
            }
        slot["items"] += 1
        slot["qty"] += int(row.qty or 0) or 1
        key = fish_name_key(row.product_name)
        if key:
            slot["keys"].add(key)
    return out


def fish_candidates(session: Session, item: WmsItem, limit: int = 5) -> list[dict]:
    """「这台货是不是跟库里某一款其实是同一个东西」—— 检测台的归属弹窗靠它。

    导入是**按单品一行一台**导进来的，同一款货写了几个略有出入的名字就会被发成
    好几个渔货号。扫到这种货时给检测员一句提醒：库里还有 N 个号跟它同名，
    要不要并进去（并进去 = 那一款下面多一台、它的库存跟着加）。

    只认**别的**渔货号（自己现在挂的那个不算），按那一款下面已有的台数倒序。
    """
    if item is None:
        return []
    key = fish_name_key(getattr(item, "product_name", "") or "")
    if not key:
        return []   # 没有商品名的货认不了亲，别乱提示
    cur = str(getattr(item, "fish_code", "") or "").strip().upper()
    hits = []
    for code, group in fish_groups(session).items():
        if code == cur or key not in group["keys"]:
            continue
        hits.append({"fish_code": code, "name": group["name"],
                     "brand": group["brand"], "items": group["items"],
                     "qty": group["qty"]})
    hits.sort(key=lambda x: (-int(x["items"]), x["fish_code"]))
    return hits[:max(1, int(limit or 1))]


# 「这台货的商品资料齐不齐」就看这几个核心档案字段（2026-10-15 用户拍板）：
# 型号 / 规格决定检测定级与上架定价，品牌与商品名称决定详情页能不能看明白。
# 顺序即页面与提示里的展示顺序。
PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("product_name", "商品名称"),
    ("brand", "品牌"),
    ("model", "型号"),
    ("spec", "规格"),
)

# 型号 / 规格是「同一款里区分两台货」的关键：缺了它，详情页上就是一排「—」，
# 定价也无从下手 —— 所以只要这两项有空的，检测台就提醒人去维护。
KEY_PROFILE_KEYS: tuple[str, ...] = ("model", "spec")


def profile_missing(item: object) -> list[str]:
    """这台货「商品名称 / 品牌 / 型号 / 规格」里还空着哪几个（返回中文标签）。

    给检测台用：缺了就在「② 商品入库信息」卡顶上写清缺哪几项，让人去补全。
    """
    if item is None:
        return []
    out = []
    for key, label in PROFILE_FIELDS:
        if not str(getattr(item, key, "") or "").strip():
            out.append(label)
    return out


def need_profile(item: object) -> bool:
    """**要不要提醒**检测员去维护商品资料：型号 / 规格只要有一个空着就要提醒。

    建档时常常只认到货号与商品名，型号规格得等拆箱看铭牌才补得齐 ——
    所以这里不看品牌（无品牌的货太多了），只看这两个区分字段。
    """
    if item is None:
        return False
    return any(not str(getattr(item, key, "") or "").strip()
               for key in KEY_PROFILE_KEYS)


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
    fish_pool = fish_index(session)  # 商品名指纹 → 渔货号（本批认亲用，同款挂同一个渔货号）
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
        # 渔货号：行里显式带了就认它（历史数据迁移 / 手工指定），否则按商品名认亲
        product_name = str(raw.get("product_name") or "").strip()
        # 型号 / 规格（2026-10-16 用户要求）：清单里没写就按商品名认一次 ——
        # 认不出来的留空（绝不猜一个凑数），人填过的原样保留。
        guess = profile_guess.guess_of(
            product_name,
            str(raw.get("brand") or "").strip(),
            extra=dict(raw.get("extra") or {}),
            model=str(raw.get("model") or "").strip(),
            spec=str(raw.get("spec") or "").strip(),
        )
        item = WmsItem(
            barcode=barcode,
            display_code=display_code_of(barcode),
            batch_id=batch.id,
            batch_no=batch.batch_no,
            inbound_order_id=inbound_order_id,
            category=category,
            brand=str(raw.get("brand") or "").strip(),
            product_name=product_name,
            fish_code=str(raw.get("fish_code") or "").strip().upper()
            or resolve_fish_code(session, product_name, index=fish_pool),
            model=guess["model"],
            spec=guess["spec"],
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
            # 一律向下取整到元（2026-10-16 用户拍板）：¥184.06 → ¥184.00。
            selling_price=grading.floor_price(raw.get("selling_price")),
            suggest_price=grading.floor_price(raw.get("suggest_price")),
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
    fish_code: str = "",
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
            # 渔货号（2026-10-14）：整批同型号 = 同一款，指定/认亲都走 register
            "fish_code": str(fish_code or "").strip().upper(),
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
    # 渔货号（2026-10-14）：扫的是「这一款」而不是某一台 —— 认到里面挂的第一台，
    # 检测台要靠 ``find_items_by_fish`` 把同款的几台列出来让人挑（与扫货号同一条规矩）。
    row = session.scalar(
        select(WmsItem).where(WmsItem.fish_code == raw.upper()).order_by(WmsItem.id)
    )
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
    # 价格同步要跟「改之前」比，先抄下来（提交成功后推给渔枢 / 渔货栈，2026-10-15）
    old_selling = round(float(item.selling_price or 0.0), 2)
    old_suggest = round(float(item.suggest_price or 0.0), 2)
    text_fields = (
        ("sku_code", "货号"), ("product_name", "产品名"), ("brand", "品牌"),
        ("model", "型号"), ("spec", "规格"), ("serial_no", "序列号"), ("category", "品类"),
    )
    for key, label in text_fields:
        if key not in fields:
            # 部分更新（整款改商品资料只传商品名称 / 品牌 / 型号 / 规格四项）——
            # 没传进来的字段一律不动。以前这里会把「没传」当成空值写回，
            # 结果整款一改就把每台货的货号与品类清空、市场价归零。
            continue
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
        if key not in fields:
            continue
        # 建议售价是人在表单里填的 → 打回「人工值」（系统算的那台会跟着定级重算）
        if key == "suggest_price":
            item.suggest_auto = False
        new_cost = round(float(fields.get(key) or 0.0), 2)
        # 渔货价 / 建议售价向下取整到元（2026-10-16 用户拍板），成本价与市场价保留原值
        if key in ("selling_price", "suggest_price"):
            new_cost = grading.floor_price(new_cost)
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
        # 价格同步（2026-10-15 用户拍板）：编辑把渔货价 / 建议售价改了就推给
        # 渔枢商品档案 + 渔货栈货源，真变了在渔货栈「通知」里落一条。桥断只降级。
        if item.fish_code and (
            round(float(item.selling_price or 0.0), 2) != old_selling
            or round(float(item.suggest_price or 0.0), 2) != old_suggest
        ):
            try:
                from . import bridge

                bridge.push_price_change(
                    item.fish_code,
                    name=str(item.product_name or item.title or ""),
                    selling_price=float(item.selling_price or 0.0),
                    suggest_price=float(item.suggest_price or 0.0),
                    base_price=float(item.base_price or 0.0),
                )
            except Exception:  # noqa: BLE001  同步失败不影响编辑保存
                import logging

                logging.getLogger("wms.intake").warning(
                    "编辑改价同步渔枢 / 渔货栈失败", exc_info=True
                )
    return changed


def update_goods_profile(
    session: Session,
    code: str,
    *,
    fields: dict,
    operator: str = "",
) -> tuple[int, list[str]]:
    """改**一款货**的商品资料（2026-10-15）：渔货号下面每一台条码商品一起改。

    货品台账详情页（``/wms/goods/{fish_code}``）的「编辑商品信息」走这里 ——
    商品名称 / 品牌 / 型号 / 规格是**一款货的公共资料**，同款多台各存一份；
    改一次要整款同步，否则台账里会出现「同一款货两台型号不一样」。

    **条码终身不变**（见 :func:`update_item`），改品类也不重发条码。
    返回 ``(同步了几台, 改动摘要)`` —— 整款同改，摘要取第一台的看一份就够。
    """
    rows = find_items_by_fish(session, code)
    if not rows:
        raise ValueError(f"没有这个渔货号：{code or '（空）'}")
    picked = {k: v for k, v in (fields or {}).items() if v is not None}
    # 进货货号是「供应商清单上那一列」、一台一个，整款同步必然撞号 —— 款级编辑不带这一项
    picked.pop("sku_code", None)
    if not picked:
        return 0, []
    total = 0
    summary: list[str] = []
    for row in rows:
        changed = update_item(
            session, row, fields=picked, operator=operator,
            remark="整款改商品资料（商品名称 / 品牌 / 型号 / 规格）",
        )
        if changed:
            total += 1
            if not summary:
                summary = changed
    # 商品资料同步（2026-10-16 用户要求）：型号 / 规格在渔集改了（含系统补出来的），
    # 渔枢商品档案与渔货栈货源的「型号 / 规格」两行要跟着显示 —— 桥断只降级。
    head = rows[0]
    try:
        from . import bridge

        bridge.push_profile_change(
            code,
            brand=str(head.brand or ""),
            model=str(head.model or ""),
            spec=str(head.spec or ""),
        )
    except Exception:  # noqa: BLE001  同步是账，保存是现场 —— 失败不拖整款保存
        logger.warning("商品资料同步渔枢 / 渔货栈失败", exc_info=True)
    return total, summary


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
                WmsItem.fish_code.like(like),
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
