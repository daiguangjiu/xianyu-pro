"""打印设置：云账号 / 打印机档案 / 标签模板，全部在页面上改，改完立刻生效。

为什么单独一层（2026-10-08）：

· 我们的服务在**云上**，现场那台 XP-420B 在仓库里 —— 服务碰不到它的网口，
  所以打印只能走「芯烨条码云平台」中转：我们按官方 API 把标签 XML 发出去，
  打印机联网自己取单打印。**现场不用装驱动、不用开内网端口**。
· 账号、模板、纸张这些都是「会变的现场事实」：换账号、换打印机、标签改版式，
  都不该改代码重启服务 —— 所以它们存在库里（``wms_print_configs`` / ``wms_printers``），
  在本页上改。

模板是**官方要求的 XML**（``<PAGE>`` / ``<LABEL>``），用花括号占位符填值：
``{barcode}`` ``{grade}`` ``{grade_label}`` ``{display_code}`` ``{category}`` ``{brand}``
``{action}`` ``{title}``（放不下自动加 …）``{date}`` ``{w}`` ``{h}`` ``{gap}`` ``{copies}``
``{cx}``（纸宽一半，居中要用）``{rx}``（纸宽减 2mm，靠右要用）。

**字号只能这样用（官方文档原文，2026-10-10 按它修掉线上乱码）**：
``f`` 取值 [1,8]，默认 7，其中 **1~6 是英数字体**（中文喂进去会一个字节一个字母
印成拉丁乱码）、**7 = 汉字 24×24 点（3mm）**、**8 = 更小的中文字体**；
想放大就用 ``xw`` / ``yh``（横向 / 纵向倍数，取值 [1,10]）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import cloud_print
from .label_preview import FONT_DOTS
from .labeling import GRADE_ACTION, label_data
from .models import (
    WmsItem,
    WmsLabelPrint,
    WmsPrintConfig,
    WmsPrinter,
    grade_label,
)
from .timeutil import now as _now

# ---------------------------------------------------------------- 模板
# 版式（2026-10-10 改，起因是线上打出来中文全乱码）：
# 第一行「等级码 + 等级中文」靠左、**品牌靠右**；下面依次是显示编码 / 品名
# （放不下自动加 ...）/ 品类·日期 / 处置指令，最底下条码。
#
# **字号全部改成中文字体 f=7 / f=8** —— 官方文档：f 取值 [1,8]，默认 7，
# 「英数字体可以设置 [1,6]」；一个汉字在 f=7 下是 3mm×3mm，f=8 更小。
# 以前这里写的是 f=4/5/6（英数字体），所以英文数字条码都正常、中文全变成
# 一串拉丁字母（ÊÃ ä Ð），现场照片就是这么来的。要更大只能靠 xw / yh 放大。
# 行距按点阵算过：等级 24×2=48 点≈6mm，正文 24 点≈3mm / 16 点≈2mm，
# 条码 60 点≈7.5mm 还带一行可读字符，40mm 纸装得下。
DEFAULT_TEMPLATE = """<PAGE w="{w}" l="{h}" ld="{gap}" pt="1" dpi="203">
  <LABEL id="1" c="{copies}">
    <TEXT x="2" y="2" xw="2" yh="2" f="7">{grade}</TEXT>
    <TEXT x="16" y="4" xw="1" yh="1" f="8">{grade_label}</TEXT>
    <TEXT x="{rx}" y="4" xw="1" yh="1" f="8" a="right">{brand}</TEXT>
    <TEXT x="2" y="10" xw="1" yh="1" f="7">{display_code}</TEXT>
    <TEXT x="2" y="14" xw="1" yh="1" f="8">{title}</TEXT>
    <TEXT x="2" y="17" xw="1" yh="1" f="8">{category} · {date}</TEXT>
    <TEXT x="2" y="21" xw="1" yh="1" f="7">{action}</TEXT>
    <BARC x="{cx}" y="26" h="60" s="2" a="center" ct="code128">{barcode}</BARC>
  </LABEL>
</PAGE>"""

# 老模板（2026-10-09 及以前）：中文行用了 f=1~6 的英数字体 —— 打出来就是一串
# 拉丁字母。留着原文好把库里那份**自动换成新版**（用户没自己改过的话）。
LEGACY_TEMPLATES: tuple[str, ...] = (
    """<PAGE w="{w}" l="{h}" ld="{gap}" pt="1" dpi="203">
  <LABEL id="1" c="{copies}">
    <TEXT x="2" y="2" xw="2" yh="2" f="6">{grade}</TEXT>
    <TEXT x="16" y="3" xw="1" yh="1" f="5">{grade_label}</TEXT>
    <TEXT x="{rx}" y="3" xw="1" yh="1" f="5" a="right">{brand}</TEXT>
    <TEXT x="2" y="10" xw="1" yh="1" f="6">{display_code}</TEXT>
    <TEXT x="2" y="15" xw="1" yh="1" f="4">{title}</TEXT>
    <TEXT x="2" y="19" xw="1" yh="1" f="4">{category} · {date}</TEXT>
    <TEXT x="2" y="23" xw="1" yh="1" f="5">{action}</TEXT>
    <BARC x="{cx}" y="28" h="60" s="2" a="center" ct="code128">{barcode}</BARC>
  </LABEL>
</PAGE>""",
)

# 模板版本号：库里记着这个键，老版才会被自动升级（不记就分不清「用户自己没改过」
# 和「用户改过但我们不知道」）
TEMPLATE_VERSION = "2"

# 模板里能用的占位符（页面上是这份说明，改模板照着抄）
TEMPLATE_VARS: tuple[tuple[str, str], ...] = (
    ("{barcode}", "条码内容（Code128 印的就是它，扫码复核认的也是它）"),
    ("{display_code}", "显示编码（人读的那串，如 QC-HA-2610-0003）"),
    ("{grade}", "等级码 S / A / B / C / D / W"),
    ("{grade_label}", "等级中文（S 级准新机 …）"),
    ("{brand}", "品牌（货品档案里的「品牌」，空就不印）"),
    ("{action}", "处置指令（直接上架 / 厂内维修 / 线上报修 / 拆机件）"),
    ("{category}", "品类（HA 家用电器 …）"),
    ("{title}", "品名 —— **按这行的字号算能放几个字**，放不下自动加 …（不会顶出纸边）"),
    ("{date}", "打印日期 YYYY-MM-DD"),
    ("{w}", "纸宽 mm（取自打印机档案）"),
    ("{h}", "纸长 mm"),
    ("{gap}", "标签间隙 mm（连续纸填 0）"),
    ("{copies}", "打印份数"),
    ("{cx}", "纸宽的一半 —— 条码 / 文字设 a=center 居中时 x 必须填这个"),
    ("{rx}", "纸宽减 2mm —— 文字设 a=right 靠右时 x 填这个（如品牌放右上角）"),
)

# ---------------------------------------------------------------- 配置
CONFIG_KEYS: tuple[str, ...] = (
    "cloud_user", "cloud_key", "base_url", "timeout",
    "enabled", "auto_push", "mode", "copies", "template",
    # 回调（2026-10-09）：公网域名填进芯烨后台的那个地址由它拼出来
    "public_base_url", "callback_token",
    # 一键自检的结果（2026-10-09）：存着，刷新页面也看得到，不用盯着提示条
    "last_test", "last_test_ok", "last_test_at",
)

DEFAULTS: dict[str, str] = {
    "cloud_user": "",
    "cloud_key": "",
    "base_url": cloud_print.BASE_URL,
    "timeout": "10",
    "enabled": "0",
    "auto_push": "0",
    "mode": "1",          # 1 = 不检查在线也生成订单（离线先缓存，联网自动打）
    "copies": "1",
    "template": DEFAULT_TEMPLATE,
    "public_base_url": "",
    "callback_token": "",
    "last_test": "",
    "last_test_ok": "",
    "last_test_at": "",
}

# 页面上这几个键是「开关 / 数字」，存 0 / 1 与整数
_BOOL_KEYS = ("enabled", "auto_push", "mode")
_INT_KEYS = ("timeout", "copies")

MASKED = "••••••••"


def _get(session: Session, key: str) -> str:
    row = session.scalar(select(WmsPrintConfig).where(WmsPrintConfig.key == key))
    return str(row.value or "") if row else ""


def config(session: Session, mask: bool = True) -> dict[str, str]:
    """当前打印配置（缺的键给默认值）；``mask`` 为真时密钥只给掩码。"""
    out: dict[str, str] = {}
    for key in CONFIG_KEYS:
        raw = _get(session, key)
        out[key] = raw if raw else DEFAULTS.get(key, "")
    if mask and out.get("cloud_key"):
        out["cloud_key"] = MASKED
    return out


def save_config(session: Session, values: dict[str, Any]) -> None:
    """保存配置：空字符串表示「没填 / 不改」（密钥尤其如此，页面上不回显）。"""
    for key in CONFIG_KEYS:
        if key not in values:
            continue
        val = str(values.get(key) or "").strip()
        # 回调令牌例外：空 = 「不校验」，得能清掉（其余键空串一律当「不改」）
        if not val and key != "callback_token":
            continue
        if key == "cloud_key" and val == MASKED:
            continue  # 页面原样回传掩码 = 不改
        if key in _INT_KEYS:
            try:
                val = str(max(1, int(val)))
            except ValueError:
                continue
        if key in _BOOL_KEYS:
            val = "1" if val not in ("", "0", "false", "False") else "0"
        row = session.scalar(select(WmsPrintConfig).where(WmsPrintConfig.key == key))
        if row is None:
            row = WmsPrintConfig(key=key, value=val)
            session.add(row)
        else:
            row.value = val
    session.commit()


def auth_of(cfg: dict[str, str]) -> dict[str, Any]:
    """把配置转成 ``cloud_print`` 要的鉴权参数。"""
    try:
        timeout = max(1, int(cfg.get("timeout") or 10))
    except ValueError:
        timeout = 10
    return {
        "user": cfg.get("cloud_user") or "",
        "key": cfg.get("cloud_key") or "",
        "base_url": cfg.get("base_url") or "",
        "timeout": float(timeout),
    }


def has_key(cfg: dict[str, str]) -> bool:
    """账号密钥齐不齐（**不看 enabled 开关**）：齐了就能向云端发请求。

    急停要用它，不能用 ready() —— 见 :func:`emergency_stop` 的说明。
    """
    return bool(
        str(cfg.get("cloud_user") or "").strip()
        and str(cfg.get("cloud_key") or "").strip()
        and cfg.get("cloud_key") != MASKED
    )


def ready(cfg: dict[str, str]) -> bool:
    """能不能发：启用 + 有账号 + 有密钥。"""
    return bool(cfg.get("enabled") == "1" and has_key(cfg))


# ---------------------------------------------------------------- 打印机
def printers(session: Session) -> list[WmsPrinter]:
    return list(
        session.scalars(
            select(WmsPrinter).order_by(
                WmsPrinter.is_default.desc(), WmsPrinter.id.asc()
            )
        ).all()
    )


def default_printer(session: Session) -> WmsPrinter | None:
    """默认打印机：先找标了默认的，没有就拿第一台启用的。"""
    rows = printers(session)
    for row in rows:
        if row.is_default and row.enabled:
            return row
    return next((r for r in rows if r.enabled), None)


def save_printer(
    session: Session,
    *,
    printer_id: int = 0,
    sn: str = "",
    name: str = "",
    kind: str = "label",
    width_mm: int = 60,
    height_mm: int = 40,
    gap_mm: int = 2,
    copies: int = 1,
    enabled: bool = True,
    note: str = "",
    verify: bool = False,
    cfg: dict[str, str] | None = None,
) -> tuple[WmsPrinter, str]:
    """登记 / 改一台打印机。``verify`` 为真时先调云接口把设备号绑到开发者ID下。

    绑定是官方要求的**第一步**（没绑的机器打印接口会回 1002「打印机未绑定」）。
    """
    sn = str(sn or "").strip()
    if not sn:
        raise ValueError("打印机编号（SN）不能为空 —— 在机身顶部的标签上")
    row = session.get(WmsPrinter, printer_id) if printer_id else None
    if row is None:
        exists = session.scalar(select(WmsPrinter).where(WmsPrinter.sn == sn))
        if exists:
            raise ValueError(f"编号 {sn} 已经登记过了（{exists.name or '未命名'}）")
        row = WmsPrinter(sn=sn, created_at=_now())
        session.add(row)
        action = "已添加"
    else:
        if row.sn != sn:
            dup = session.scalar(select(WmsPrinter).where(WmsPrinter.sn == sn))
            if dup:
                raise ValueError(f"编号 {sn} 已经登记过了")
        row.sn = sn
        action = "已保存"

    if verify:
        if not ready(cfg or {}):
            raise ValueError("先把云账号填好并启用，才能把设备号绑到开发者ID下")
        try:
            # 注意是 auth_of(cfg)：接口要的是 user / key，配置里存的是 cloud_user /
            # cloud_key，直接把配置字典撒进去会缺参数（2026-10-09 修：勾「同时绑定」必 500）
            cloud_print.verify_printer(
                sn, name or row.name or "检测仓", **auth_of(cfg or {})
            )
        except cloud_print.CloudPrintError as exc:
            raise ValueError(f"绑定失败：{exc}（{exc.hint}）") from exc

    row.name = str(name or "").strip() or sn
    row.kind = "receipt" if str(kind or "").lower().startswith("r") else "label"
    row.width_mm = max(1, int(width_mm or 60))
    row.height_mm = max(1, int(height_mm or 40))
    row.gap_mm = max(0, int(gap_mm or 0))
    row.copies = max(1, int(copies or 1))
    row.enabled = bool(enabled)
    row.note = str(note or "")
    row.updated_at = _now()
    session.flush()
    if not session.scalar(select(WmsPrinter).where(WmsPrinter.is_default.is_(True))):
        row.is_default = True  # 第一台自动当默认，省一步操作
    session.commit()
    return row, f"{action}「{row.name}」（{row.sn}）"


def set_default(session: Session, printer_id: int) -> str:
    rows = printers(session)
    hit = next((r for r in rows if r.id == printer_id), None)
    if hit is None:
        raise ValueError("打印机不存在")
    for row in rows:
        row.is_default = row.id == hit.id
    session.commit()
    return f"「{hit.name}」已设为默认打印机"


def toggle(session: Session, printer_id: int) -> str:
    row = session.get(WmsPrinter, printer_id)
    if row is None:
        raise ValueError("打印机不存在")
    row.enabled = not row.enabled
    row.updated_at = _now()
    session.commit()
    return f"「{row.name}」已{'启用' if row.enabled else '停用'}"


def delete_printer(session: Session, printer_id: int) -> str:
    row = session.get(WmsPrinter, printer_id)
    if row is None:
        raise ValueError("打印机不存在")
    name = row.name
    session.delete(row)
    session.commit()
    if not default_printer(session):
        first = next((r for r in printers(session)), None)
        if first:
            first.is_default = True
            session.commit()
    return f"已删除「{name}」"


# ---------------------------------------------------------------- 模板渲染
def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def label_vars(item: WmsItem | None = None, row: WmsLabelPrint | None = None) -> dict:
    """模板要的值：优先用货品现算（字段最全），没有货品就从打印记录上取。

    **档位以打印记录为准**（2026-10-13 用户三催）：预览与检测台「提交前即时打标」
    传进来的 row.grade 是引擎建议档，货品本身的 grade 还停在 W 待测 ——
    等级、等级中文、处置动作必须跟着 row 的档走，否则纸上永远是「W 待测」，
    跟引擎建议、跟实际评级对不上（截图里引擎建议 S、纸上 W 就是这条）。
    """
    if item is not None:
        data = label_data(item)
        row_grade = str(getattr(row, "grade", "") or "").strip().upper() if row else ""
        grade = row_grade or data["grade"]
        if row_grade and row_grade != data["grade"]:
            grade_label_text = grade_label(grade)
            action_text = GRADE_ACTION.get(grade, data["action"])
        else:
            grade_label_text = data["grade_label"]
            action_text = data["action"]
        return {
            "barcode": data["barcode"],
            "display_code": data["display_code"],
            "grade": grade,
            "grade_label": grade_label_text,
            "brand": data["brand"],
            "action": action_text,
            "category": data["category"],
            "title": data["title"],
            "date": data["date"],
        }
    grade = str(getattr(row, "grade", "") or "W").upper() if row else "W"
    return {
        "barcode": str(getattr(row, "barcode", "") or ""),
        "display_code": str(getattr(row, "display_code", "") or ""),
        "grade": grade,
        "grade_label": grade_label(grade),
        "brand": "",
        "action": str(getattr(row, "action_text", "") or GRADE_ACTION.get(grade, "待检测")),
        "category": "",
        "title": "",
        "date": _now().strftime("%Y-%m-%d"),
    }


def _num(text: Any, default: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def fit_text(text: Any, budget: float) -> str:
    """按「这一行能放几个字」截断，放不下就末尾加 … 。

    中文按 1 格、英数按 0.55 格估宽（203dpi 点阵字的实际比例）—— 品名是从
    供应商清单里带进来的一长串（品牌 + 卖点 + 规格），不截就会顶出纸边、
    甚至把条码挤到下一张（2026-10-09）。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    # 末尾留三个英文点的位置：**不用 …（U+2026）** —— 标签机点阵字库不一定有这个
    # 符号，打出来就是个方块或乱码（2026-10-09）
    room = max(1.0, float(budget)) - 1.8
    used = 0.0
    out: list[str] = []
    for ch in s:
        w = 0.55 if ch.isascii() else 1.0
        if used + w > room:
            return "".join(out).rstrip() + "..."
        out.append(ch)
        used += w
    return s


def title_budget(template: str, width: int = 60, dpi: int = 203) -> float:
    """品名那一行能放几个字：按**模板里那行的字号 f 和横向放大 xw** 算。

    字号换成毫米 = 点阵高 / dpi × 25.4 × xw，再用「纸宽 - 左边距 - 右边距」除以它。
    认不出那行就给 14（60mm 纸、4mm 字的经验值）。
    """
    found = re.search(r"<TEXT\b[^>]*>[^<]*\{title\}[^<]*</TEXT>",
                      str(template or ""), re.I)
    if not found:
        return 14.0
    attrs = dict(re.findall(r"(\w+)\s*=\s*\"([^\"]*)\"", found.group(0)))
    pts = FONT_DOTS.get(int(_num(attrs.get("f"), 7)), 24)
    xw = _num(attrs.get("xw"), 1) or 1
    font_mm = pts * xw / (dpi or 203) * 25.4
    if font_mm <= 0:
        return 14.0
    left = _num(attrs.get("x"), 0)
    usable = max(4.0, float(width or 60) - left - 2.0)   # 右边留 2mm
    return max(4.0, usable / font_mm)


def render(template: str, values: dict, *, width: int = 60, height: int = 40,
           gap: int = 2, copies: int = 1) -> str:
    """把占位符换成真值（值统一做 XML 转义，免得品名里的 & 把标签打废）。"""
    out = str(template or DEFAULT_TEMPLATE)
    merged = {
        "w": int(width or 60),
        "h": int(height or 40),
        "gap": int(gap or 0),
        "copies": max(1, int(copies or 1)),
        # 纸宽的一半：**必须取整** —— 云端不认 x="30.0" 这种小数坐标，
        # 带小数点会被判 REQUEST_PARAM_INVALID（code -2），整单打不出来（2026-10-09）
        "cx": int(round(int(width or 60) / 2)),
        # 右边界（靠右时要贴着纸边再留 2mm，不然最后一笔会被切掉）
        "rx": int(width or 60) - 2,
        **{k: v for k, v in values.items()},
    }
    # 兜底：模板里写了、这回数据里没有的占位符（比如这台货没填品牌）一律印空，
    # 绝不能把 "{brand}" 这种花括号原样印到标签上
    for key, _hint in TEMPLATE_VARS:
        merged.setdefault(key.strip("{}"), "")
    # 品名按这一行真实能放下几个字截断（放不下加 …）
    if "title" in merged:
        merged["title"] = fit_text(merged.get("title"), title_budget(out, int(width or 60)))
    for key, val in merged.items():
        out = out.replace("{" + key + "}", _esc(val))
    return out


# 一张标签单最多印几张（2026-10-12 加，血的教训）：
# 那天一台 20 台数的货，打印机吐了几百张 —— 份数在「云 API 的 copies 参数」和
# 模板 ``<LABEL c="{copies}">`` 里各写了一遍，两边是**相乘**关系，20 变成了 400。
# 现在份数只由模板出一次，这里再兜一道上限：数量录错也不至于打一整卷纸。
MAX_COPIES = 50


def copies_of(row: WmsLabelPrint, printer: WmsPrinter | None) -> int:
    """这条打印任务印几张（2026-10-10）：**份数跟货品数量走** —— 3 台就打 3 张一样的。

    任务是入队那一刻按数量记下来的（``WmsLabelPrint.copies``）；只有数量 = 1（或老数据
    没这个值时）才退回打印机档案上的默认份数，老行为不变。

    **这个数只喂给模板的 ``{copies}``**（2026-10-12 改）：芯烨云 API 的 ``copies``
    参数与报文里 ``<LABEL c="N">`` 是叠加（相乘）的，两边都传 N 会打出 N×N 张。
    所以下发时 API 那一侧固定写 1，份数只在这里出一次，再按 ``MAX_COPIES`` 封顶。
    """
    qty = int(getattr(row, "copies", 0) or 0)
    if qty > 1:
        return min(qty, MAX_COPIES)
    return max(1, int(printer.copies if printer else 1))


def render_for(session: Session, row: WmsLabelPrint, printer: WmsPrinter | None,
               cfg: dict[str, str] | None = None) -> str:
    """按这台打印机的纸张渲染某条打印任务的 XML（份数与打印时用的是同一个口径）。"""
    item = session.get(WmsItem, row.item_id) if row.item_id else None
    paper = printer or default_printer(session)
    return render(
        (cfg or config(session)).get("template") or DEFAULT_TEMPLATE,
        label_vars(item, row),
        width=int(paper.width_mm if paper else 60),
        height=int(paper.height_mm if paper else 40),
        gap=int(paper.gap_mm if paper else 2),
        copies=copies_of(row, paper),
    )


# ---------------------------------------------------------------- 下发与回查
def send(session: Session, row: WmsLabelPrint, printer: WmsPrinter | None = None) -> tuple[bool, str]:
    """把一条打印任务发到云打印机。返回 ``(是否成功, 人话)`` —— 不抛异常，
    失败原因写进 ``row.error``，页面上看得到。
    """
    cfg = config(session, mask=False)
    if not ready(cfg):
        return False, "云打印没开（去「打印设置」填开发者ID / UserKEY 并启用）"
    target = printer or default_printer(session)
    if target is None:
        return False, "还没登记打印机（去「打印设置」加一台）"
    if not target.enabled:
        return False, f"「{target.name}」已停用，换一台或先启用"
    xml = render_for(session, row, target, cfg)
    # 幂等因子：同一条打印任务 5 分钟内只打一次（防重复点、防网络重试打出两张）
    idempotent = f"WMS{row.id}-{row.label_type}"[:50]
    try:
        order_no = cloud_print.print_label(
            target.sn,
            xml,
            # 份数**只能出现在一处**：XML 里的 <LABEL c="{copies}"> 已经带上了数量，
            # 云 API 这个 copies 再写 N 就是 N×N（20 台 → 400 张，2026-10-12 事故现场）。
            # 所以这里恒为 1 = 「这份内容发一次」，印几张由报文自己说。
            copies=1,
            mode=int(cfg.get("mode") or 1),
            idempotent=idempotent,
            **auth_of(cfg),
        )
    except cloud_print.CloudPrintError as exc:
        row.error = f"{exc}｜{exc.hint}".strip("｜")
        row.printer_sn = target.sn
        row.transport = "CLOUD"
        row.status = "queued"
        session.commit()
        return False, row.error
    row.order_no = order_no
    row.printer_sn = target.sn
    row.transport = "CLOUD"
    row.status = "sent"
    row.error = ""
    row.sent_at = _now()
    session.commit()
    return True, f"已下发到「{target.name}」（订单号 {order_no}）"


def refresh(session: Session, row: WmsLabelPrint) -> tuple[bool, str]:
    """回查云端：这张到底打出来没有（打出来就标 printed）。"""
    if not row.order_no:
        return False, "还没下发到云端（先点「下发」）"
    cfg = config(session, mask=False)
    if not ready(cfg):
        return False, "云打印没开"
    try:
        done = cloud_print.query_order_state(row.order_no, **auth_of(cfg))
    except cloud_print.CloudPrintError as exc:
        return False, f"{exc}｜{exc.hint}".strip("｜")
    if done:
        row.status = "printed"
        row.printed_at = row.printed_at or _now()
        row.error = ""
        session.commit()
        return True, "云端确认已打印"
    return False, "云端还没打出这张（打印机会在联网后自动补打）"


def printer_status(session: Session, printer: WmsPrinter) -> tuple[bool, str]:
    """查一台机器在不在（顺带把型号与固件版本带回来）。"""
    cfg = config(session, mask=False)
    if not ready(cfg):
        return False, "云打印没开"
    try:
        info = cloud_print.query_printer_status(printer.sn, **auth_of(cfg))
    except cloud_print.CloudPrintError as exc:
        return False, f"{exc}｜{exc.hint}".strip("｜")
    tail = f"（{info['model']} · {info['version']}）" if info.get("model") else ""
    return True, f"「{printer.name}」{info['status_text']}{tail}"


def test_print(session: Session, printer: WmsPrinter) -> tuple[bool, str]:
    """打一张测试页：确认「账号 + 模板 + 这台机器」这条链路是通的。

    **打的就是页面预览里那张**（当前模板 + 样例数据，队列里有真货就用真的）——
    以前这里是另一套假数据，于是「预览里看到的」和「打出来的」不是一回事，
    照着预览调坐标也对不上（2026-10-09 改：两者同源，所见即所得）。
    """
    cfg = config(session, mask=False)
    if not ready(cfg):
        return False, "云打印没开（先填开发者ID / UserKEY 并启用）"
    xml = sample_xml(session)
    try:
        order_no = cloud_print.print_label(
            printer.sn, xml, copies=1, mode=int(cfg.get("mode") or 1), **auth_of(cfg)
        )
    except cloud_print.CloudPrintError as exc:
        return False, f"测试打印失败：{exc}｜{exc.hint}".strip("｜")
    return True, f"测试页已下发（订单号 {order_no}），看打印机出不出纸"


def clear_queue(session: Session, printer: WmsPrinter) -> tuple[bool, str]:
    """清空这台机器在云端的待打印队列（打错一堆时用）。"""
    cfg = config(session, mask=False)
    if not ready(cfg):
        return False, "云打印没开"
    try:
        cloud_print.clear_queue(printer.sn, **auth_of(cfg))
    except cloud_print.CloudPrintError as exc:
        return False, f"{exc}｜{exc.hint}".strip("｜")
    return True, f"「{printer.name}」的云端队列已清空"


def emergency_stop(session: Session) -> tuple[bool, str]:
    """强制停止（2026-10-12 加）：一次动作把还在路上的打印全掐掉。

    起因：那天一台 "20 台数" 的货一次吐了几百张，现场只能靠拔电源收场。
    三件事必须一起做，少一件就停不干净：

    1. **关掉「入队即下发」** —— 不关的话队列里每排进一张就自动往外发，
       前面清得再快也追不上它发得快；
    2. **清云端待打印队列** —— 已经发给云平台、机器还没打的那批，全撤；
    3. **本地「待打印」全部作废** —— 免得手滑再点一次「全部下发」，纸又哗哗出来。

    已经出了的纸收不回来，这条保证的是「**不会再多打一张**」。

    **注意 2 不看 ``enabled`` 开关**（2026-10-12 现场踩过）：现场第一反应是把「云打印」
    开关关掉想让它停，结果云端队列还在、机器一联网接着吐 —— 开关只管我们发不发新的，
    管不了已经在云端排着的那批。所以这里只要账号密钥在就去清，不看开关。
    """
    cfg = config(session, mask=False)
    parts: list[str] = []

    # 1. 关掉自动下发（否则停了也白停）
    _set(session, "auto_push", "0")
    parts.append("已关闭「入队即下发」")

    # 2. 清云端队列（只要账号密钥在就清，**不看 enabled** —— 见 docstring）
    target = default_printer(session)
    if target is None:
        parts.append("没登记打印机（云端队列没动）")
    elif not has_key(cfg):
        parts.append("还没配开发者ID / UserKEY（云端队列没动，本地已停）")
    else:
        try:
            cloud_print.clear_queue(target.sn, **auth_of(cfg))
            parts.append(f"「{target.name}」云端队列已清空")
        except cloud_print.CloudPrintError as exc:
            parts.append(f"云端队列没清掉：{exc}")

    # 3. 本地待打印全部作废（sent / printed 不动，那是已经发出去的事实）
    try:
        n = int(
            session.execute(
                update(WmsLabelPrint)
                .where(WmsLabelPrint.status == "queued")
                .values(status="cancelled")
            ).rowcount
            or 0
        )
    except Exception:
        n = 0
    session.commit()
    parts.append(f"本地作废 {n} 张待打印" if n else "本地没有待打印任务")
    return True, "强制停止完成：" + "；".join(parts)


def _set(session: Session, key: str, value: str) -> None:
    """直接写一个配置键（不走 ``save_config`` —— 它把空串当「不改」，自检结果要能清空）。"""
    row = session.scalar(select(WmsPrintConfig).where(WmsPrintConfig.key == key))
    if row is None:
        session.add(WmsPrintConfig(key=key, value=value))
    else:
        row.value = value


# ---------------------------------------------------------------- 模板体检 / 升级
# 芯烨文档：``f`` 取值 [1,8]，默认 7，**「英数字体可以设置 [1,6]」**。
# f=1~6 是纯英数字体，中文喂进去会一个字节一个字母地印成拉丁字母 ——
# 2026-10-10 现场那张乱码标签就是这么来的（英文数字条码全对、中文全变 ÊÃ ä Ð）。
_TEXT_TAG_RE = re.compile(r"<TEXT\b([^>]*)>(.*?)</TEXT>", re.I | re.S)
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")


def template_warnings(template: str) -> list[str]:
    """模板体检：把「一眼能看出来会打坏」的地方点出来（现在只查最要命的字体那条）。

    页面上拿到非空列表就飘黄条 —— 比让现场打一张废标签再回来问快得多。
    """
    out: list[str] = []
    for attrs_raw, inner in _TEXT_TAG_RE.findall(str(template or "")):
        body = _PLACEHOLDER_RE.sub("", str(inner or "")).strip()
        if not body or body.isascii():
            continue                      # 占位符与英文数字随便用哪个字体
        attrs = dict(re.findall(r"(\w+)\s*=\s*\"([^\"]*)\"", attrs_raw or ""))
        try:
            font = int(str(attrs.get("f") or 7).strip() or 7)
        except ValueError:
            font = 7
        if font <= 6:
            out.append(
                f"这行的文字「{body[:12]}」用了 f=\"{font}\" —— f=1~6 是英数字体，"
                "中文会一个字节一个字母地印成拉丁乱码。带中文的行 f 要用 7（汉字 3mm）"
                "或 8（更小），想放大改用 xw / yh（倍数 1~10）。"
            )
    return out


def ensure_template(session: Session) -> str:
    """把库里那份老模板升到「中文用 f=7 / f=8」的新版。

    返回 ``upgraded`` / ``kept`` / ``current``：
    · 库里没存过模板、或存的就是老默认模板 → 换成新默认（upgraded）；
    · 用户自己改过（跟老默认不一样）→ **一个字都不动**（kept），
      只在页面上按 ``template_warnings`` 飘黄条 —— 改坏模板是用户的自由，
      但我们不能悄悄按自己的意思覆盖他写的东西，也不能让乱码无声无息。
    """
    if _get(session, "template_v") == TEMPLATE_VERSION:
        return "current"
    current = _get(session, "template").strip()
    if not current or current in {one.strip() for one in LEGACY_TEMPLATES}:
        _set(session, "template", DEFAULT_TEMPLATE)
        _set(session, "template_v", TEMPLATE_VERSION)
        session.commit()
        return "upgraded"
    if not template_warnings(current):
        # 自写模板但没毛病：记个版本号就不再重复看
        _set(session, "template_v", TEMPLATE_VERSION)
        session.commit()
    return "kept"


def _store_test(session: Session, steps: list[dict], started: float) -> dict:
    ok = all(s["ok"] is not False for s in steps)
    result = {
        "ok": ok,
        "steps": steps,
        "ms": int((time.perf_counter() - started) * 1000),
        "at": now_text(),
    }
    _set(session, "last_test", json.dumps(result, ensure_ascii=False))
    _set(session, "last_test_ok", "1" if ok else "0")
    _set(session, "last_test_at", result["at"])
    session.commit()
    return result


def self_test(session: Session) -> dict:
    """一键自检：开关 → 账号 → 云接口通不通 → 机器在不在 → 打一张测试页。

    一步一步走，**在哪一步断了就说到哪一步**（账号错 / 机器离线 / 模板不合法
    是三种完全不同的处理办法，混成一句「失败」没法动手）。结果存进配置，
    页面刷新也看得到。
    """
    started = time.perf_counter()
    cfg = config(session, mask=False)
    steps: list[dict] = []

    def add(ok: bool | None, text: str) -> None:
        steps.append({"ok": ok, "text": text})

    enabled = cfg.get("enabled") == "1"
    add(enabled, "云打印已启用" if enabled else "云打印没启用 —— 勾上「启用云打印」再保存")
    user = str(cfg.get("cloud_user") or "").strip()
    key = str(cfg.get("cloud_key") or "").strip()
    cred_ok = bool(user and key and key != MASKED)
    add(cred_ok, f"开发者ID {user} 与 UserKEY 都已填" if cred_ok
        else "开发者ID / UserKEY 没填全（在芯烨平台「个人中心 → 我的资料」里）")
    target = default_printer(session)
    add(target is not None, f"默认打印机「{target.name}」（{target.sn}）" if target
        else "还没登记打印机 —— 在下面「加一台打印机」填机身顶部的编号（首次要勾「同时绑定」）")
    if not (enabled and cred_ok and target):
        return _store_test(session, steps, started)

    try:
        info = cloud_print.query_printer_status(target.sn, **auth_of(cfg))
    except cloud_print.CloudPrintError as exc:
        add(False, f"云接口没调通：{exc}｜{exc.hint}".strip("｜"))
        return _store_test(session, steps, started)
    tail = f"（{info.get('model')} · {info.get('version')}）" if info.get("model") else ""
    add(True, f"云接口通了（签名已通过）· 接口 {cfg.get('base_url')}")
    add(int(info.get("status") or 0) != 0,
        f"「{target.name}」{info.get('status_text')}{tail}")

    ok_p, msg_p = test_print(session, target)
    add(ok_p, msg_p)
    return _store_test(session, steps, started)


def last_test_of(session: Session) -> dict | None:
    """上次一键自检的结果（没跑过就是 None）。"""
    raw = str(_get(session, "last_test") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def paper_of(printer: WmsPrinter | None) -> dict[str, int]:
    """纸张三件套（模板预览用）。"""
    return {
        "w": int(printer.width_mm if printer else 60),
        "h": int(printer.height_mm if printer else 40),
        "gap": int(printer.gap_mm if printer else 2),
        "copies": int(printer.copies if printer else 1),
    }


def sample_xml(session: Session) -> str:
    """模板预览：拿队列里最近的一条（没有就造一台假货）渲染一遍。"""
    printer = default_printer(session)
    cfg = config(session)
    row = session.scalar(
        select(WmsLabelPrint).order_by(WmsLabelPrint.id.desc())
    )
    paper = paper_of(printer)
    if row is not None:
        return render_for(session, row, printer, cfg)
    # 队列还空着：**拿一台真货渲染**（2026-10-13）—— 以前这里造的是假货，
    # 等级取兜底的 W，看着像「打出来都是 W 待测」，纯粹误导人。
    item = session.scalar(select(WmsItem).order_by(WmsItem.id.desc()))
    return render(
        cfg.get("template") or DEFAULT_TEMPLATE,
        label_vars(item, None) if item is not None else label_vars(None, None),
        width=paper["w"], height=paper["h"], gap=paper["gap"], copies=paper["copies"],
    )


def now_text() -> str:
    return _now().strftime("%Y-%m-%d %H:%M")


__all__ = [
    "DEFAULT_TEMPLATE", "TEMPLATE_VARS", "MASKED", "LEGACY_TEMPLATES",
    "TEMPLATE_VERSION", "template_warnings", "ensure_template",
    "config", "save_config", "ready", "auth_of",
    "printers", "default_printer", "save_printer", "set_default", "toggle",
    "delete_printer", "render", "render_for", "label_vars", "paper_of",
    "send", "refresh", "printer_status", "test_print", "clear_queue",
    "self_test", "last_test_of", "sample_xml",
    "fit_text", "title_budget",
]
