"""Excel 导入建档（2026-10-08）：**批次建好的时候还不知道这批货是什么**。

吃进来的货是「某一家的某一批」，什么类目都有、品牌型号规格在建批次时根本定不了 ——
所以建档不靠手填，靠供应商那张 Excel：

  上传 → 认表头（自动匹配 + 手工改）→ 预览 → 执行

三条口径：

  1. **货号是命根子**：表格里那列「货号 / 款号 / SKU 编码」写进 ``wms_items.sku_code``，
     现场检测拿扫码枪扫货号就能把这台货的档案带出来（系统条码 QC… 是另一套号，
     终身不变；货号是外部编号，允许供应商自己编）；
  2. **认不出来的列沉淀成商品字段**：表格里多出来的列（颜色 / 供应商批次 / 采购单号…）
     登记进 ``wms_item_fields``，值存 ``wms_items.extra`` —— 之后在「商品字段」页
     可以改名、改类型、停用、排序，成为商品表自己的字段；
  3. **混合品类批次**：批次勾了「本批类目混杂」就按表格每行定品类（品类列空着才用
     批次的默认品类），条码里的品类码跟着每行走。

导入是**写库存动作**（发条码 + 记入库流水），所以留痕：每张表记一条 ``wms_import_logs``。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit
from . import intake
from .models import (
    WmsBatch,
    WmsImportLog,
    WmsItem,
    WmsItemField,
    category_node,
    category_options,
    leaf_codes,
)
from .timeutil import now as _now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 标准字段
# 导入能直接落到 wms_items 真列上的字段（扩展字段不在这里，是导入时现登记的）
# 字段口径按供应商清单（河南仓库第一批：货号 / 下单时间 / 京东订单号 / 品牌 / 产品 /
# 一级分类 / 二级分类 / 三级分类 / 下单价格 / 故障原因 / 产品状态 …）对齐。
STANDARD_FIELDS: tuple[dict, ...] = (
    {"key": "sku_code", "label": "货号", "type": "text", "core": True,
     "hint": "扫码枪扫的就是它（款号 / SKU 编码 / 料号都行）"},
    {"key": "product_name", "label": "产品 / 商品名称", "type": "text",
     "hint": "清单里那一长串商品名 —— 展示与检测载入优先用它"},
    {"key": "brand", "label": "品牌", "type": "text"},
    {"key": "category_l1", "label": "一级分类", "type": "text", "hint": "如 家用电器"},
    {"key": "category_l2", "label": "二级分类", "type": "text", "hint": "如 厨房小电"},
    {"key": "category_l3", "label": "三级分类", "type": "text", "hint": "如 破壁机（有它就够）"},
    {"key": "category", "label": "品类（中文 / 三级码）", "type": "text",
     "hint": "中文（电饭煲 / 对讲机）或三级码（HA.KC.DFB）；也可以用上面三级分类三列"},
    {"key": "model", "label": "型号", "type": "text"},
    {"key": "spec", "label": "规格", "type": "text", "hint": "颜色 / 容量 / 配置"},
    {"key": "serial_no", "label": "序列号 / IMEI", "type": "text"},
    {"key": "purchase_cost", "label": "采购 / 回收成本", "type": "number",
     "hint": "清单里叫「下单价格」"},
    {"key": "base_price", "label": "基准价（市场价）", "type": "number"},
    {"key": "qty", "label": "数量", "type": "number", "hint": "空着按 1 台；>1 生成多台（同货号）"},
)

# 表头别名：供应商的表五花八门，先把最常见的写法认出来，认错了解释得改（预览页可改）
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "sku_code": ("货号", "商品货号", "货号编码", "货品编码", "商品编号", "款号", "料号",
                 "sku", "sku编码", "sku code", "skucode", "编码", "编号", "货品编号"),
    "product_name": ("产品", "产品名称", "商品名称", "品名", "货品名称", "产品描述", "商品"),
    "brand": ("品牌", "brand", "厂牌", "商品品牌"),
    "category_l1": ("一级分类", "一级类目", "一级品类", "大类", "一级分类名称", "一级"),
    "category_l2": ("二级分类", "二级类目", "二级品类", "中类", "二级分类名称", "二级"),
    "category_l3": ("三级分类", "三级类目", "三级品类", "小类", "三级分类名称", "三级"),
    "model": ("型号", "机型", "model", "规格型号"),
    "spec": ("规格", "配置", "spec", "颜色", "容量", "规格参数"),
    "serial_no": ("序列号", "imei", "sn", "串号", "机身码", "设备号", "sn码"),
    "category": ("品类", "类目", "类别", "category", "三级品类", "商品类目", "产品类目"),
    "purchase_cost": ("成本", "采购价", "回收价", "进价", "单价", "成本价", "采购成本",
                      "进货价", "回收成本", "cost", "下单价格", "下单金额", "结算价"),
    "base_price": ("基准价", "市场价", "参考价", "建议售价", "售价", "定价", "price"),
    "qty": ("数量", "件数", "台数", "个数", "qty", "quantity", "数量(台)"),
}

# 这些列名自带「产品 / 状态 / 时间」等字样，**不许被包含匹配抢成系统字段** ——
# 它们是清单里的过程信息（故障原因 / 售后状态 / 下单时间…），一律沉淀成商品字段。
SEDIMENT_HEADERS: tuple[str, ...] = (
    "产品状态", "售后状态", "当前状态", "故障原因", "测试说明", "测试原因", "检测说明",
    "建议折扣", "下单时间", "退货时间", "京东订单号", "订单号", "备注",
)

# 一次导入的上限（保护：防止一张几万行的表把号段与事务拖垮）
IMPORT_MAX_ROWS = 2000
IMPORT_MAX_QTY = 50

_FIELD_KEYS = {f["key"] for f in STANDARD_FIELDS}


def field_label(key: str) -> str:
    for f in STANDARD_FIELDS:
        if f["key"] == key:
            return str(f["label"])
    return key


def _norm(value) -> str:
    """表头归一化：去空白、全角冒号括号、小写——「货号(必填)」要认成「货号」。"""
    text = str(value or "").strip()
    text = re.sub(r"[（(][^）)]*[）)]", "", text)          # 去掉括号里的补充说明
    text = text.replace("：", "").replace(":", "").replace("*", "")
    return re.sub(r"\s+", "", text).lower()


def guess_mapping(headers: list[str]) -> dict[int, str]:
    """列 → 标准字段（认不出来的留空，由人在预览页选「沉淀为商品字段」）。

    先精确认别名，再退一步做包含匹配（「商品货号」含「货号」）。
    """
    out: dict[int, str] = {}
    taken: set[str] = set()
    sediments = {_norm(h) for h in SEDIMENT_HEADERS}
    for idx, raw in enumerate(headers):
        norm = _norm(raw)
        if not norm:
            continue
        if norm in sediments:
            # 清单里的过程信息列（产品状态 / 售后状态 / 下单时间 / 故障原因…）：
            # 名字里带「产品」「状态」等字样，绝不能被包含匹配抢走 → 直接留给「沉淀为商品字段」
            continue
        hit = ""
        for key, aliases in HEADER_ALIASES.items():
            if key in taken:
                continue
            for alias in aliases:
                anorm = _norm(alias)
                if norm == anorm:
                    hit = key
                    break
            if hit:
                break
        if not hit:
            for key, aliases in HEADER_ALIASES.items():
                if key in taken:
                    continue
                for alias in aliases:
                    anorm = _norm(alias)
                    if len(anorm) >= 2 and (anorm in norm or norm in anorm):
                        hit = key
                        break
                if hit:
                    break
        if hit:
            taken.add(hit)
            out[idx] = hit
    return out


def slugify(header: str, used: set[str]) -> str:
    """表头 → 扩展字段的 key（中文原样留着更好认，重复的加数字后缀）。"""
    text = re.sub(r"[（(][^）)]*[）)]", "", str(header or "")).strip()
    text = re.sub(r"[\s/\\:：*?\"'#\[\]{}]+", "_", text)
    key = text or "col"
    base = key
    n = 2
    while key in used or key in _FIELD_KEYS:
        key = f"{base}_{n}"
        n += 1
    return key


# ---------------------------------------------------------------- 品类识别
def _match_single(text: str) -> str:
    """一小段文字（整串，或「家用电器 厨房小电 破壁机」里切出来的一段）→ 三级品类码。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    up = raw.upper().replace(" ", "")
    for code, path in category_options():
        if up == code.upper():
            return code
    norm = _norm(raw)
    if not norm:
        return ""
    # ① 最后一段（三级名）精确命中，最可信
    for code, path in category_options():
        segs = [s for s in str(path).split(" › ")]
        if segs and _norm(segs[-1]) == norm:
            return code
    # ② 二级名精确（写「对讲机」没说模拟还是公网）：归到这个二级的兜底叶（「其它 XX」）
    parent_hit = ""
    for code in leaf_codes():
        parts = str(code).split(".")
        if len(parts) != 3:
            continue
        node2 = category_node(f"{parts[0]}.{parts[1]}")
        if not node2 or _norm(node2.get("label")) != norm:
            continue
        if str((category_node(code) or {}).get("label") or "").startswith("其它"):
            return code
        parent_hit = parent_hit or code
    if parent_hit:
        return parent_hit
    # ③ 退一步：路径里含这段文字
    for code, path in category_options():
        if norm in _norm(path):
            return code
    return ""


def match_category(text: str) -> str:
    """表格里写的品类 → 三级品类码；认不出返回空串。

    三种写法都认：三级码（``HA.KC.DFB``）、中文名（「破壁机」）、
    **分类三列拼起来的一串**（「家用电器 厨房小电 破壁机」）。
    2026-10-08：清单（河南仓库第一批）把分类拆成一级 / 二级 / 三级三列，拼起来按分隔符
    切开**从后往前**认（三级最可信；三级认不出退二级名 → 归到该二级的「其它 XX」兜底叶）。
    """
    segs = [s for s in re.split(r"[\s/›>＞、,，|｜;；]+", str(text or "").strip()) if s.strip()]
    for seg in reversed(segs):
        code = _match_single(seg)
        if code:
            return code
    return _match_single(text)


# ---------------------------------------------------------------- 读表
def read_table(path: Path, suffix: str = "") -> dict:
    """读 xlsx / csv：返回 ``{"headers": [...], "rows": [[...]]}``。

    空行整行跳过；单元格 None 当空串。xlsx 只认第一个工作表（供应商的表都是一张）。
    """
    suffix = (suffix or path.suffix or "").lower()
    rows: list[list] = []
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook  # 局部导入：openpyxl 是可选依赖

        wb = load_workbook(filename=str(path), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        for line in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else v for v in line])
        wb.close()
    elif suffix == ".csv":
        raw = Path(path).read_bytes()
        for encoding in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV 编码认不出来（试过 UTF-8 / GBK），另存为 xlsx 再来")
        rows = [list(line) for line in csv.reader(io.StringIO(text))]
    else:
        raise ValueError("只支持 .xlsx / .xlsm / .csv 三种表")

    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        raise ValueError("这张表是空的")

    def _filled(line) -> int:
        return sum(1 for c in line if str(c or "").strip())

    # 供应商的表常常第一行是标题（「河南仓库第一批清单」这种大字），真正的表头在第二行 ——
    # 认出来就往下顺延一行（特征：首行只有一个格子有字，而下一行有 3 列以上都写着字）。
    head_at = 0
    if len(rows) > 1 and _filled(rows[0]) <= 1 and _filled(rows[1]) >= 3:
        head_at = 1
    headers = [str(c or "").strip() for c in rows[head_at]]
    body = [
        [("" if c is None else str(c).strip()) for c in r] for r in rows[head_at + 1:]
    ]
    return {"headers": headers, "rows": body, "header_row": head_at + 1}


# ---------------------------------------------------------------- 上传暂存
def _import_dir() -> Path:
    from .config import config as _cfg

    folder = Path(_cfg.media_path) / "wms_imports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_upload(data: bytes, filename: str) -> str:
    """上传的文件先落盘（预览与执行分两步，不能靠浏览器回传整张表）。"""
    from uuid import uuid4

    suffix = Path(filename or "").suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".csv"):
        raise ValueError("只支持 .xlsx / .xlsm / .csv 三种表")
    token = f"{uuid4().hex[:16]}{suffix}"
    (_import_dir() / token).write_bytes(data)
    return token


def upload_path(token: str) -> Path | None:
    """token → 落盘路径（只认本目录里的文件名，防目录穿越）。"""
    name = str(token or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = _import_dir() / name
    return path if path.is_file() else None


def build_template(batch: WmsBatch | None = None) -> bytes:
    """下载用的导入模板：标准表头 + 两行示例（照着填就不会认错列）。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "货品清单"
    ws.append([f["label"] for f in STANDARD_FIELDS])
    ws.append(["A-1001", "美的（Midea）电饭煲 3L 家用多功能智能电饭煲", "美的",
               "家用电器", "厨房小电", "电饭煲", "", "MB-FB30", "3L 白色", "", 88, 199, 1])
    ws.append(["A-1002", "九阳豆浆机 1.2L 家用全自动免滤", "九阳",
               "家用电器", "厨房小电", "豆浆机", "", "DJ12E", "1.2L", "", 120, 260, 2])
    ws.append(["A-1003", "海能达对讲机 数字手持台", "海能达",
               "数码", "对讲设备", "对讲机", "", "PD980", "黑色", "SN123456", 260, 520, 1])
    note = ws.max_row + 2
    ws.cell(row=note, column=1, value="填写说明")
    ws.cell(row=note + 1, column=1,
            value="① 货号必填（后面检测扫的就是它）；② 分类可以写一级 / 二级 / 三级三列，"
                  "也可以只填「品类（中文 / 三级码）」一列；③ 数量>1 会生成多台同货号货品；"
                  "④ 表里多出来的列（故障原因 / 产品状态 / 下单时间…）会被登记成商品字段，"
                  "值跟着货品一起存。")
    ws.column_dimensions["A"].width = 16
    for col in "BCDEFGHIJKLM":
        ws.column_dimensions[col].width = 18
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 扩展字段
def ensure_field(db: Session, key: str, label: str, field_type: str = "text",
                 source: str = "") -> WmsItemField:
    """登记 / 复用一个扩展字段（同 key 只建一次，改名由「商品字段」页负责）。

    2026-10-08 用户拍板：**商品字段以导入表格的表头为准** —— 所以新建时
    ① 排序排在现有字段末尾（同一张表按列序依次登记，顺序就是表格里的列顺序）；
    ② 记下来源（哪张表带来的），老字段没记来源的在被这张表用到时补上。
    """
    row = db.scalar(select(WmsItemField).where(WmsItemField.key == key))
    if row is None:
        top = db.scalar(select(func.max(WmsItemField.sort))) or 0
        row = WmsItemField(
            key=key, label=str(label or key), field_type=str(field_type or "text"),
            enabled=True, sort=int(top or 0) + 10, source=str(source or ""),
            created_at=_now(), updated_at=_now(),
        )
        db.add(row)
        db.flush()
    else:
        if not str(row.label or ""):
            row.label = str(label or key)
        if source and not str(row.source or ""):
            row.source = str(source)
    return row


def move_field(db: Session, field_id: int, direction: str = "up") -> None:
    """商品字段排序（2026-10-08）：往上 / 往下挪一位，顺序整体重排成 10、20、30…

    页面上「上移 / 下移」按钮走这里 —— 比手填排序数字省事，也不会撞号。
    """
    rows = list_fields(db)
    idx = next((i for i, f in enumerate(rows) if f.id == field_id), -1)
    if idx < 0:
        raise ValueError("字段不存在")
    swap = idx - 1 if str(direction or "").lower() == "up" else idx + 1
    if swap < 0 or swap >= len(rows):
        return
    rows[idx], rows[swap] = rows[swap], rows[idx]
    for i, f in enumerate(rows):
        f.sort = (i + 1) * 10
        f.updated_at = _now()


def list_fields(db: Session, only_enabled: bool = False) -> list[WmsItemField]:
    stmt = select(WmsItemField)
    if only_enabled:
        stmt = stmt.where(WmsItemField.enabled.is_(True))
    return list(db.scalars(stmt.order_by(WmsItemField.sort.asc(), WmsItemField.id.asc())).all())


def field_map(db: Session) -> dict[str, str]:
    """key → 展示名（页面展示 extra 里的值用）。"""
    return {f.key: (f.label or f.key) for f in list_fields(db)}


# ---------------------------------------------------------------- 执行导入
def _to_number(text: str) -> float:
    raw = str(text or "").strip().replace(",", "").replace("¥", "").replace("元", "")
    if not raw:
        return 0.0
    try:
        return round(float(raw), 2)
    except ValueError:
        return 0.0


def _to_int(text: str, default: int = 1) -> int:
    raw = str(text or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def validate_rows(
    db: Session,
    batch: WmsBatch,
    *,
    headers: list[str],
    rows: list[list[str]],
    mapping: dict[int, str],
    extra_specs: list[dict] | None = None,
    line_base: int = 2,
    cat_fallback: str = "",
) -> tuple[list[dict], list[str], dict[str, int], list[dict]]:
    """逐行翻译成建档入参，并挑出毛病（**只读，不写库**）。

    预览页的「先预检一遍」和真正的导入共用这一段 —— 毛病清单在**导入前**就摆出来，
    别让人导完了才知道「整批没导进来」。行号从 2 开始（1 是表头），跟表格对得上。

    返回 ``(payload, errors, sku_lines, unknown)``：payload 可直接喂 ``intake.register``，
    errors 是给人看的毛病清单，sku_lines 是 货号 → 行号，unknown 是**认不出来的分类**
    （``[{"raw": 原文, "lines": [行号]}]``，预览页据此让人当场指定一个兜底类目）。

    ``cat_fallback`` 非空时：分类认不出来的行**按它建档**（不再算毛病）——
    清单里那几行的分类格常填着备注/标题文本，与其让人回去改几百行的表，不如当场指定。

    **不短路**：有毛病的行进 errors，合法的行照样回在 payload 里 ——
    要不要「跳过坏行、先导其余」由调用方（``apply_import`` 的 skip_bad_rows）定。
    """
    extra_specs = list(extra_specs or [])
    fallback = str(cat_fallback or "").strip().upper()
    errors: list[str] = []
    payload: list[dict] = []
    seen_sku: dict[str, int] = {}
    unknown: dict[str, dict] = {}  # 认不出来的分类：原文 → 出现在哪几行
    for offset, row in enumerate(rows):
        line_no = offset + int(line_base or 2)  # 表头在第 1 行 → 数据从第 2 行开始
        line: dict = {"extra": {}}
        for idx, key in mapping.items():
            if not key or idx >= len(headers):
                continue
            value = str(row[idx] if idx < len(row) else "" or "").strip()
            if key.startswith("extra:"):
                line["extra"][key[6:]] = value
            elif key in _FIELD_KEYS:
                line[key] = value
        for spec in extra_specs:
            idx = int(spec.get("col") or -1)
            if 0 <= idx < len(row):
                value = str(row[idx] or "").strip()
                if str(spec.get("type") or "text") == "number":
                    value = _to_number(value) or value
                line["extra"][str(spec.get("key"))] = value

        sku = str(line.get("sku_code") or "").strip()
        if not sku:
            errors.append(f"第 {line_no} 行没有货号 —— 货号是扫码检测认货的凭据，必须填")
            continue
        if sku in seen_sku:
            errors.append(f"第 {line_no} 行货号与第 {seen_sku[sku]} 行重复：{sku}")
            continue
        seen_sku[sku] = line_no
        line["sku_code"] = sku

        qty = _to_int(line.get("qty") or "", 1)
        if qty < 1:
            errors.append(f"第 {line_no} 行数量不合法：{line.get('qty')}")
            continue
        if qty > IMPORT_MAX_QTY:
            errors.append(f"第 {line_no} 行数量 {qty} 超过单台上限 {IMPORT_MAX_QTY}")
            continue

        # 品类：优先「品类」那一列；清单常把分类拆成一级 / 二级 / 三级三列 → 拼起来一起认
        parts = [str(line.get(f"category_l{i}") or "").strip() for i in (1, 2, 3)]
        raw_cat = str(line.get("category") or "").strip() or " ".join(p for p in parts if p)
        if raw_cat:
            cat = match_category(raw_cat)
            if not cat and fallback:
                cat = fallback  # 人指定了兜底类目：这几行按它建档，不再算毛病
            if not cat:
                errors.append(
                    f"第 {line_no} 行的分类「{raw_cat}」认不出来 —— "
                    f"写中文（如 破壁机）或三级码（如 HA.KC.DFB）"
                )
                unknown.setdefault(raw_cat, {"raw": raw_cat, "lines": []})["lines"].append(line_no)
                continue
            line["category"] = cat
        else:
            line["category"] = str(batch.category or "")

        line["purchase_cost"] = _to_number(line.get("purchase_cost") or "")
        line["base_price"] = _to_number(line.get("base_price") or "")
        # 数量（2026-10-10 改口径）：这一条货就是 N 台 —— **同一个条码**，不再按数量拆成
        # N 条货品（以前拆开后扫码会变成「到底扫哪一台」，台账也数不清台数）
        line["qty"] = qty
        payload.append({**line, "_line": line_no})  # _line 只是内部记号，出口会摘掉

    # 库里已有的货号不能再导一次（跨批次重复会让扫货号变成「到底是哪台」）
    exist = {
        str(v)
        for v in db.scalars(
            select(WmsItem.sku_code).where(WmsItem.sku_code.in_(list(seen_sku)))
        ).all()
    }
    bad_lines: set[int] = set()
    for sku, line_no in seen_sku.items():
        if sku in exist:
            errors.append(f"第 {line_no} 行货号已建档：{sku}")
            bad_lines.add(line_no)
    kept: list[dict] = []
    for item in payload:
        if int(item.pop("_line", 0) or 0) in bad_lines:
            continue
        kept.append(item)
    return kept, errors, seen_sku, list(unknown.values())


def _write_failed_log(
    db: Session,
    batch: WmsBatch,
    *,
    file_name: str,
    row_count: int,
    mapping: dict[int, str],
    headers: list[str],
    fields_added: list[str],
    errors: list[str],
    operator: str,
) -> None:
    """导入失败也要留一条记录（2026-10-08）。

    以前失败只在页面顶部闪一次提示条（还只显示前 3 条），回头查「上次那张表为什么没导进来」
    什么痕迹都没有 —— 现在失败也写日志，**原因一字不漏存进 errors**，页面能翻出来看。
    """
    db.rollback()  # 回滚这一批里已经登记的字段，别留半截
    log = WmsImportLog(
        batch_id=batch.id,
        batch_no=batch.batch_no,
        file_name=str(file_name or "")[:255],
        row_count=int(row_count or 0),
        ok_count=0,
        fail_count=len(errors),
        mapping={str(headers[i]): v for i, v in mapping.items() if i < len(headers)},
        fields=list(fields_added),
        # 一张几千行的表可能条条都错：只留前 20 条，够定位问题了
        errors=[str(e) for e in errors[:20]],
        operator=str(operator or ""),
        created_at=_now(),
    )
    db.add(log)
    db.commit()


def apply_import(
    db: Session,
    batch_id: int,
    *,
    headers: list[str],
    rows: list[list[str]],
    mapping: dict[int, str],
    extra_specs: list[dict] | None = None,
    operator: str = "",
    file_name: str = "",
    line_base: int = 2,
    skip_bad_rows: bool = False,
    cat_fallback: str = "",
) -> dict:
    """按映射把表格灌成货品：逐行建档（发条码 + 入库流水），多出来的列沉淀成字段。

    ``cat_fallback``：分类认不出来的行按这个类目建档（预览页让人当场指定的兜底）——
    清单里那几行的分类格常常填着备注/标题文本，回去改几百行的表太折磨人。

    返回 ``{"ok": 台数, "errors": [文案], "fields": [key], "log_id": n, "skipped": 跳过行数}``。

    默认**整批一个事务**：任何一行不合法就整批不导（导一半的库存比不导更难收拾）。
    ``skip_bad_rows=True``（人在预览页勾了「跳过有毛病的行」）时只导合法行，
    坏行照样记进导入记录的 errors 里 —— 几十行脏数据不该把几百行好货一起挡在门外。
    """
    batch = db.get(WmsBatch, batch_id)
    if batch is None:
        raise ValueError("批次不存在")
    if str(batch.status or "") == "closed":
        raise ValueError(f"批次 {batch.batch_no} 已关闭，不能继续建档")
    if not rows:
        raise ValueError("表里没有数据行")
    if len(rows) > IMPORT_MAX_ROWS:
        raise ValueError(f"一张表最多 {IMPORT_MAX_ROWS} 行，这份有 {len(rows)} 行（拆成几张表导）")

    extra_specs = list(extra_specs or [])
    # 字段来源：记下这张表（「商品字段」页按它交代出处 —— 字段以导入的表头为准）
    field_source = " ".join(
        p for p in (str(file_name or "").strip(), f"（{batch.batch_no}）" if batch else "") if p
    )
    # ① 先登记扩展字段（这次要用到的列，按表格列序依次登记 → 顺序就是表头顺序）
    used_keys: set[str] = set()
    fields_added: list[str] = []
    for spec in extra_specs:
        key = str(spec.get("key") or "").strip()
        if not key:
            continue
        ensure_field(db, key, str(spec.get("label") or key),
                     str(spec.get("type") or "text"), source=field_source)
        used_keys.add(key)
        fields_added.append(key)

    # ② 逐行翻译 + 挑毛病（跟预览页的预检是同一段代码，不会出现「预览说没事、导入才报错」）
    payload, errors, seen_sku, unknown = validate_rows(
        db, batch, headers=headers, rows=rows, mapping=mapping,
        extra_specs=extra_specs, line_base=line_base, cat_fallback=cat_fallback,
    )
    if (errors and not skip_bad_rows) or not payload:
        if not errors:
            errors = ["表里没有一行能导入的（先看「货号」那一列有没有映射对）"]
        _write_failed_log(
            db, batch, file_name=file_name, row_count=len(rows), mapping=mapping,
            headers=headers, fields_added=fields_added, errors=errors, operator=operator,
        )
        return {"ok": 0, "errors": errors, "fields": fields_added, "log_id": 0, "skipped": 0}

    # ④ 建档（发条码 + 入库流水），留一条导入记录
    log = WmsImportLog(
        batch_id=batch.id,
        batch_no=batch.batch_no,
        file_name=str(file_name or "")[:255],
        row_count=len(rows),
        ok_count=len(payload),
        fail_count=len(errors),
        mapping={str(headers[i]): v for i, v in mapping.items() if i < len(headers)},
        fields=fields_added,
        errors=[str(e) for e in errors[:20]],
        operator=str(operator or ""),
        created_at=_now(),
    )
    db.add(log)
    db.flush()
    try:
        created = intake.register(
            db, batch.id, payload, operator=operator, import_id=log.id
        )
    except ValueError as exc:
        _write_failed_log(
            db, batch, file_name=file_name, row_count=len(rows), mapping=mapping,
            headers=headers, fields_added=fields_added, errors=[str(exc)], operator=operator,
        )
        return {"ok": 0, "errors": [str(exc)], "fields": fields_added, "log_id": 0, "skipped": 0}

    log.ok_count = len(created)
    note = ("沉淀字段：" + "、".join(fields_added)) if fields_added else ""
    if cat_fallback:
        # 兜底建档的行留句话，回头好查「这几台的类目是当时拍的」
        note = (note + "；" if note else "") + f"分类认不出的按 {cat_fallback} 建档"
    if errors:
        note = (note + "；" if note else "") + f"跳过 {len(errors)} 行有毛病的"
    audit.log(db, "batch", batch.id, "import", after=f"{file_name}（{len(created)} 台）",
              operator=operator, note=note)
    db.commit()
    return {"ok": len(created), "errors": errors, "fields": fields_added,
            "log_id": log.id, "skipped": len(errors)}


def logs_of_batch(db: Session, batch_id: int, limit: int = 10) -> list[WmsImportLog]:
    return list(
        db.scalars(
            select(WmsImportLog)
            .where(WmsImportLog.batch_id == batch_id)
            .order_by(WmsImportLog.id.desc())
            .limit(limit)
        ).all()
    )
