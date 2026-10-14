"""标签打印：指令适配器 + 打印队列 + 贴标复核。

三个设计点（方案 7）：
  · **TSPL 不硬写在业务代码里**：抽一层适配器，换机型（TSPL / ZPL）只改这里；
  · **打印走队列而不是直接发**：幂等（重复点不会多打）、可追溯（谁在何时打了多少张）、
    断网时能兜底；
  · **补打必须填原因**：同批补打率 > 1% 要告警排查，别让补打变成常态。

macOS 上标签机基本没有官方驱动，**切勿用 CUPS 通用驱动打标签**（尺寸复位、条码不可扫）。
推荐链路是网口机型 RAW 9100；A4 检测单才走 CUPS。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    GRADE_A,
    GRADE_B,
    GRADE_C,
    GRADE_D,
    GRADE_N,
    GRADE_S,
    LABEL_FORMAL,
    LABEL_REPRINT,
    LABEL_W,
    STATUS_GRADED,
    STATUS_SELLABLE,
    WH_SALE,
    WmsItem,
    WmsLabelPrint,
    grade_label,
)
from . import audit
from . import ledger
from . import state

TSPL = "TSPL"
ZPL = "ZPL"
DIALECTS = (TSPL, ZPL)

# 打印链路：云=芯烨条码云（2026-10-08 起云端部署走这条，服务碰不到现场网口）
# A=网口 RAW 9100 / B=Web Serial（仅 Chromium）/ D=CUPS（只打 A4）
TRANSPORT_CLOUD = "CLOUD"
TRANSPORT_NET = "NET9100"
TRANSPORT_SERIAL = "WEBSERIAL"
TRANSPORT_CUPS = "CUPS"
TRANSPORTS = (TRANSPORT_CLOUD, TRANSPORT_NET, TRANSPORT_SERIAL, TRANSPORT_CUPS)

# 处置指令：印在标签上，仓管不看字就知道这批货该去哪个架
GRADE_ACTION = {
    GRADE_N: "直接上架",
    GRADE_S: "直接上架",
    GRADE_A: "微瑕专区",
    # 2026-10-13：B = 缺件 / 微瑕（非外观）不影响售卖 → 照常上架，不再写「厂内维修」
    GRADE_B: "直接上架",
    GRADE_C: "线上报修",
    GRADE_D: "拆机件",
}

# 补打率告警阈值（同批 > 1% 说明前面流程有问题）
REPRINT_ALERT_RATE = 0.01

# 打印状态的人话（队列页与检测台共用，别各写一套）
# cancelled = 被「强制停止」作废掉的任务（2026-10-12 加，防失控连打）
PRINT_STATUS_LABELS = {
    "queued": "待打印",
    "sent": "已下发云端",
    "printed": "已打印",
    "cancelled": "已取消",
}


def label_data(item: WmsItem) -> dict:
    """业务层只产出这个结构，打印链路与指令集与它无关。"""
    grade = str(item.grade or "W").upper()
    return {
        "size": {"w": 60, "h": 40},
        "grade": grade,
        "grade_label": grade_label(grade),
        "category": item.category,
        "barcode": item.barcode,
        "display_code": item.display_code or item.barcode,
        "action": GRADE_ACTION.get(grade, "待检测"),
        "brand": str(item.brand or "").strip(),
        # 品名**不在这里截** —— 多长算「放不下」要看模板那行的字号和纸宽，
        # 交给打印层算（print_settings.render 会按行宽截断、放不下加 …）
        "title": item.title,
        "date": _now().strftime("%Y-%m-%d"),
    }


def build_tspl(label: dict) -> str:
    """TSPL 指令（芯烨 / 佳博 / 汉印 / TSC 通用）。

    BARCODE 由打印机自己合成 Code128，直接传明文即可 —— 比自绘 SVG 可靠。
    """
    size = label.get("size") or {}
    lines = [
        f"SIZE {int(size.get('w') or 60)} mm, {int(size.get('h') or 40)} mm",
        "GAP 2 mm, 0 mm",
        "DIRECTION 1",
        "CLS",
        f'TEXT 15,25,"3",0,1,1,"{label.get("grade") or "W"}"',
        f'TEXT 80,25,"2",0,1,1,"{label.get("category") or ""}"',
        f'TEXT 15,48,"1",0,1,1,"{label.get("display_code") or ""}"',
        f'TEXT 15,66,"1",0,1,1,"{label.get("action") or ""}"',
        f'BARCODE 15,86,"128",42,1,0,2,2,"{label.get("barcode") or ""}"',
        "PRINT 1,1",
    ]
    return "\n".join(lines)


def build_zpl(label: dict) -> str:
    """ZPL 指令（斑马系）：换机型只换这一套指令，业务逻辑不变。"""
    barcode = label.get("barcode") or ""
    return "\n".join(
        [
            "^XA",
            "^PW480",
            "^LL320",
            f'^FO20,20^A0N,60,60^FD{label.get("grade") or "W"}^FS',
            f'^FO120,20^A0N,28,28^FD{label.get("category") or ""}^FS',
            f'^FO20,90^A0N,22,22^FD{label.get("display_code") or ""}^FS',
            f'^FO20,120^A0N,22,22^FD{label.get("action") or ""}^FS',
            "^FO20,150^BY2^BCN,60,N,N^FD" + barcode + "^FS",
            "^XZ",
        ]
    )


def build_command(label: dict, dialect: str = TSPL) -> str:
    if str(dialect or TSPL).upper() == ZPL:
        return build_zpl(label)
    return build_tspl(label)


def label_copies_of(item: WmsItem) -> int:
    """这台货要打几张标签（2026-10-10）：**张数 = 数量** —— 3 台就打 3 张一模一样的。

    一个条码底下带 N 台，每台身上都得贴一张；所以份数不再写死在打印机档案里，
    而是跟着货品的数量走（打印机档案上的份数只作数量为 1 时的默认值）。
    """
    return max(1, int(getattr(item, "qty", 0) or 0) or 1)


def enqueue(
    session: Session,
    items: list[WmsItem],
    *,
    operator: str = "",
    label_type: str = LABEL_FORMAL,
    reason: str = "",
    printer: str = "",
    dialect: str = TSPL,
    transport: str = TRANSPORT_NET,
    grade: str = "",
) -> list[WmsLabelPrint]:
    """入打印队列（幂等：同一台货同一种标签只排一次，重复点不会多打）。

    ``grade``（2026-10-13 用户三催）：**指定档位** —— 检测台「提交前即时打标」
    传引擎建议档进来，货品本身还没定级（grade 还是 W），纸上必须按指定档出，
    不能永远印「W 待测」。空串 = 按货品当前档（标签页批量入队的老路）。
    """
    if label_type == LABEL_REPRINT and not str(reason or "").strip():
        raise ValueError("补打必须填原因（防止标签乱印）")
    want_grade = str(grade or "").strip().upper()
    created: list[WmsLabelPrint] = []
    for item in items:
        data = label_data(item)
        if want_grade and want_grade != data["grade"]:
            data = {**data, "grade": want_grade,
                    "grade_label": grade_label(want_grade),
                    "action": GRADE_ACTION.get(want_grade, data["action"])}
        exists = session.scalar(
            select(WmsLabelPrint).where(
                WmsLabelPrint.item_id == item.id,
                WmsLabelPrint.label_type == label_type,
                WmsLabelPrint.status == "queued",
            )
        )
        if exists:
            # 已经排过队的：数量可能后来又改了，份数跟着数量走；
            # 档位变了也跟着换（先按 W 排过、检测里又判了 S —— 纸上不能还是 W）
            exists.copies = label_copies_of(item)
            if want_grade and str(exists.grade or "").upper() != want_grade:
                exists.grade = want_grade
                exists.action_text = data["action"]
                exists.command = build_command(data, str(exists.dialect or dialect))
            created.append(exists)
            continue
        row = WmsLabelPrint(
            item_id=item.id,
            barcode=item.barcode,
            display_code=data["display_code"],
            # 份数（2026-10-10）：**这台货有几台就打几张**（一个条码底下 3 台，每台身上贴一张）
            copies=label_copies_of(item),
            label_type=label_type,
            grade=data["grade"],
            action_text=data["action"],
            dialect=str(dialect or TSPL).upper(),
            command=build_command(data, dialect),
            transport=str(transport or TRANSPORT_NET),
            printer=str(printer or ""),
            status="queued",
            operator=str(operator or ""),
            reason=str(reason or ""),
            batch_no=str(item.batch_no or ""),
            created_at=_now(),
        )
        session.add(row)
        session.flush()
        created.append(row)
        audit.log(session, "item", item.id,
                  "reprint" if label_type == LABEL_REPRINT else "label",
                  after=f"{label_type} {data['display_code']}",
                  operator=operator, note=reason)
    session.commit()
    return created


def sync_queue_grade(session: Session, item: WmsItem) -> int:
    """等级变了 → 队列里**还没打出去**的标签跟着改（2026-10-13）。

    标签内容是「入队那一刻」按当时的等级生成的（grade / 处置 / 打印指令都存进了
    wms_label_prints）。后来检测定级或重判，队列里那张还挂着老等级 —— 打出来就跟
    实际分级对不上（现场反馈「打印怎么都是 W 待测」：货是入库就排的队，那会儿还没检测）。

    这里按货品**当前**等级把没打出去的记录刷一遍；**已经打出去的不动** ——
    那是既成事实，要改走补打（队列页会提示「纸上 X 档 ≠ 实判 Y 档，要补打」）。
    返回刷新的条数。
    """
    rows = list(
        session.scalars(
            select(WmsLabelPrint).where(
                WmsLabelPrint.item_id == item.id,
                WmsLabelPrint.status.in_(("queued", "sent")),
            )
        ).all()
    )
    if not rows:
        return 0
    data = label_data(item)
    changed = 0
    for row in rows:
        if str(row.grade or "").upper() == data["grade"]:
            continue
        row.grade = data["grade"]
        row.action_text = data["action"]
        row.command = build_command(data, row.dialect)
        changed += 1
    if changed:
        session.flush()
    return changed


def mark_printed(session: Session, print_id: int, operator: str = "") -> WmsLabelPrint:
    row = session.get(WmsLabelPrint, print_id)
    if not row:
        raise ValueError("打印任务不存在")
    row.status = "printed"
    row.printed_at = _now()
    if operator and not row.operator:
        row.operator = operator
    session.commit()
    return row


def last_print_of(session: Session, item_id: int) -> WmsLabelPrint | None:
    """这台货最近一次打印任务（检测台「打过没有 / 印的是几档」就靠它判断）。"""
    if not item_id:
        return None
    return session.scalar(
        select(WmsLabelPrint)
        .where(WmsLabelPrint.item_id == item_id)
        .order_by(WmsLabelPrint.id.desc())
        .limit(1)
    )


def queue(session: Session, limit: int = 100, only_queued: bool = True) -> list[WmsLabelPrint]:
    stmt = select(WmsLabelPrint)
    if only_queued:
        stmt = stmt.where(WmsLabelPrint.status == "queued")
    return list(
        session.scalars(stmt.order_by(WmsLabelPrint.id.desc()).limit(limit)).all()
    )


def reprint_rate(session: Session, batch_no: str = "") -> dict:
    """补打率：> 1% 说明前面流程有问题，要告警排查（防补打变常态）。"""
    stmt_total = select(func.count()).select_from(WmsLabelPrint).where(
        WmsLabelPrint.label_type != LABEL_REPRINT
    )
    stmt_re = select(func.count()).select_from(WmsLabelPrint).where(
        WmsLabelPrint.label_type == LABEL_REPRINT
    )
    if batch_no:
        stmt_total = stmt_total.where(WmsLabelPrint.batch_no == batch_no)
        stmt_re = stmt_re.where(WmsLabelPrint.batch_no == batch_no)
    total = int(session.scalar(stmt_total) or 0)
    reprinted = int(session.scalar(stmt_re) or 0)
    rate = (reprinted / total) if total else 0.0
    return {"total": total, "reprinted": reprinted, "rate": round(rate, 4),
            "alert": bool(total and rate > REPRINT_ALERT_RATE)}


def verify(session: Session, barcode: str, grade: str, operator: str = "") -> dict:
    """贴标复核（防错最后一道闸）。

    强校验三项：
      ① 扫到的条码存在                     否则「未知条码，拒收」
      ② 标签等级 == 系统等级                否则「等级不符，拒收」→ 要求补打
      ③ 条码 == 该货品的条码（张冠李戴）     否则「检查是否贴错机器」
    全通过才放行入库（S/A → 可售仓）。
    """
    raw = str(barcode or "").strip()
    compact = raw.replace("-", "").upper()
    item = session.scalar(select(WmsItem).where(WmsItem.barcode == compact))
    if not item:
        return {"ok": False, "message": f"未知条码 {raw}，拒收", "item": None}
    if str(item.barcode or "").upper() != compact:
        return {"ok": False, "message": "张冠李戴：条码与货品对不上，检查是否贴错机器",
                "item": item}
    scanned = str(grade or "").strip().upper()
    if not scanned:
        return {"ok": False, "message": "没读到标签等级，请重扫", "item": item}
    if scanned != str(item.grade or "").upper():
        return {
            "ok": False,
            "item": item,
            "message": f"等级不符：系统 {item.grade}，实扫 {scanned}，拒收（需补打标签）",
        }
    if str(item.status or "").upper() not in (STATUS_GRADED, STATUS_SELLABLE):
        return {"ok": False, "item": item,
                "message": f"「{item.status}」状态不能贴标复核（要先检测定级）"}
    if str(item.grade or "").upper() in (GRADE_C, GRADE_D):
        # 2026-10-13 起 B 档 = 缺件 / 微瑕可卖，跟 S / A 一样照常入库可售仓，
        # 不再「开维修工单」；只有 C（送外修）/ D（拆机件）不走入库
        return {
            "ok": True,
            "item": item,
            "message": f"标签核对通过（{grade_label(item.grade)}）："
                       f"下一步开{'报修工单' if item.grade == GRADE_C else '拆机件估值'}",
        }

    warehouse, status = state.route_of(item.grade)
    ledger.apply(
        session, item,
        biz_type="LABEL",
        biz_id=str(item.id),
        to_warehouse=warehouse or WH_SALE,
        to_status=status or STATUS_SELLABLE,
        operator=operator,
        remark=f"贴标复核通过（{grade_label(item.grade)}）",
        idempotent_key=f"LABEL:{item.id}",
    )
    audit.log(session, "item", item.id, "label", after="贴标复核通过",
              operator=operator, note=f"{item.display_code} → {warehouse}")
    session.commit()
    return {"ok": True, "item": item, "message": f"复核通过，已入{warehouse}（{status}）"}
