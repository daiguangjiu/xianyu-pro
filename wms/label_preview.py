"""标签可视化预览（2026-10-08）：把芯烨云要的 XML 画成「像真标签」的图。

**为什么要有这个**：模板是尖括号，现场没人愿意啃；改完模板也不知道会不会打歪、
条码会不会顶出纸边、品名会不会盖住条码 —— 所以这里按**毫米坐标**把元素摆到一张纸上，
条码用真 Code128 位串（见 ``barcode128.py``）画，预览与真打出来的版式一一对应。

规则取自芯烨条的指令集（跟官方 API 文档同一套）：

· ``<PAGE w="60" l="40" ld="2" dpi="203">`` —— 纸宽 / 纸长 / 标签间隙（毫米）、dpi；
· ``<TEXT x y xw yh f a>`` —— x/y 单位毫米，f 是字体号 1~8，a 是 left/center/right；
· ``<BARC x y h s a ct>`` —— h 是条码高（点）、s 是窄条宽（点），ct 常见 code128；
· ``<QR x y h s>`` / ``<LINE x y x2 y2 w>`` / ``<RECT x y w h lw>`` / ``<IMAGE x y w h>``。

解析用**正则**而不是 XML 解析器：模板是人手改的，少个引号不该让整页 500（解析不了
就退回「只显示原始 XML」）。
"""
from __future__ import annotations

import html as html_mod
import re
from typing import Any

from . import barcode128

PX_PER_MM = 3.7795          # 浏览器里 1mm 的像素数（96dpi / 25.4）
DEFAULT_ZOOM = 1.7          # 屏上放大一点才看得清 60mm 宽的标签
MM_PER_INCH = 25.4

# 字体号 → 点阵高（预览只用它估视觉大小）。
#
# 口径来自芯烨官方文档原文：「属性 f：标签字体设置，取值[1,8]。默认是 7，更小字体
# 可以设置为 8。**英数字体可以设置[1,6]**」，另有「一个汉字常规（24*24）即 f=7 下
# 是 3mm*3mm」—— 203dpi 下 3mm = 24 点，正好对上。
#
# **关键：带中文的 TEXT 只能用 f=7 / f=8**。f=1~6 是纯英数字体，中文喂进去会
# 「一个字节一个字母」印成一串拉丁字母（2026-10-10 线上实测就是照片里那样：
# 英文数字条码全对、中文全变 ÊÃ ä Ð）。字号大小靠 xw / yh 放大（取值 [1,10]）。
FONT_DOTS = {1: 12, 2: 16, 3: 20, 4: 24, 5: 32, 6: 40, 7: 24, 8: 16}

_TAG_RE = re.compile(
    r"<(TEXT|BARC|BARCODE|QR|QRCODE|LINE|RECT|IMAGE|BITMAP)\b([^>]*?)(?:/>|>(.*?)</\1>)",
    re.I | re.S,
)
_ATTR_RE = re.compile(r"([A-Za-z_][\w:-]*)\s*=\s*\"([^\"]*)\"")
_PAGE_RE = re.compile(r"<PAGE\b([^>]*?)/?>", re.I | re.S)
_LABEL_RE = re.compile(r"<LABEL\b([^>]*?)(?:/>|>(.*?)</LABEL>)", re.I | re.S)

# 元素类型 → 中文名（预览图例与报错说话用）
KIND_LABELS = {
    "TEXT": "文字",
    "BARC": "一维条码",
    "QR": "二维码",
    "LINE": "直线",
    "RECT": "方框",
    "IMAGE": "图片",
}


def _num(raw: Any, default: float = 0.0) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)


def _attrs(text: str) -> dict[str, str]:
    return {k.lower(): v.strip() for k, v in _ATTR_RE.findall(text or "")}


def parse(xml: str) -> dict:
    """XML → ``{w, h, gap, dpi, labels: [{id, copies, elements: [...]}]}``。

    解析不出 ``<PAGE>`` 时返回空结构（页面会退回显示原始 XML）。
    """
    text = str(xml or "")
    page = _attrs(_PAGE_RE.search(text).group(1)) if _PAGE_RE.search(text) else {}
    width = _num(page.get("w"), 60)
    height = _num(page.get("l") or page.get("h"), 40)
    gap = _num(page.get("ld"), 0)
    dpi = int(_num(page.get("dpi"), 203)) or 203
    labels: list[dict] = []
    matches = list(_LABEL_RE.finditer(text))
    if not matches:                       # 没写 LABEL 也要能看（整段当一张）
        matches = [None]                  # type: ignore[list-item]
    for match in matches:
        if match is None:
            body, head = text, {}
        else:
            head = _attrs(match.group(1) or "")
            body = match.group(2) or ""
        elements: list[dict] = []
        for tag, raw_attrs, inner in _TAG_RE.findall(body):
            kind = tag.upper()
            kind = {"BARCODE": "BARC", "QRCODE": "QR", "BITMAP": "IMAGE"}.get(kind, kind)
            item: dict[str, Any] = {"kind": kind, "attrs": _attrs(raw_attrs)}
            item["text"] = html_mod.unescape((inner or "").strip())
            elements.append(item)
        labels.append(
            {
                "id": str(head.get("id") or (len(labels) + 1)),
                "copies": max(1, int(_num(head.get("c"), 1))),
                "elements": elements,
            }
        )
    return {"w": width, "h": height, "gap": gap, "dpi": dpi,
            "labels": labels, "text": text}


def _px(mm: float, zoom: float) -> float:
    return round(float(mm) * PX_PER_MM * zoom, 2)


def _dots_px(dots: float, dpi: int, zoom: float) -> float:
    """点 → 像素（203dpi 下 1 点 = 0.125mm）。"""
    mm = float(dots) / float(dpi or 203) * MM_PER_INCH
    return _px(mm, zoom)


def bars_svg(bits: str, module_px: float, height_px: float, color: str = "#111") -> str:
    """位串 → SVG（把连续的条合成一条 path，几百个模块也只有一个节点）。"""
    total = max(1, len(bits))
    cmds: list[str] = []
    i, n = 0, len(bits)
    while i < n:
        if bits[i] == "1":
            j = i
            while j < n and bits[j] == "1":
                j += 1
            width = (j - i) * module_px
            cmds.append(f"M{i * module_px:.2f} 0h{width:.2f}v{height_px:.2f}h-{width:.2f}z")
            i = j
        else:
            i += 1
    width_px = total * module_px
    return (
        f'<svg class="lbl-bars" width="{width_px:.2f}" height="{height_px:.2f}" '
        f'viewBox="0 0 {width_px:.2f} {height_px:.2f}" preserveAspectRatio="none" '
        f'role="img" aria-label="条码">'
        f'<path d="{" ".join(cmds)}" fill="{color}"/></svg>'
    )


def _align_style(attrs: dict[str, str], axis: str) -> str:
    """对齐：芯烨的 a 是 left / center / right，center 时 x 就是纸宽一半。"""
    align = str(attrs.get("a") or "left").lower()
    if align.startswith("c"):
        return "transform:translateX(-50%);"
    if align.startswith("r"):
        return "transform:translateX(-100%);"
    return ""


def _text_transform(attrs: dict, scale_x: float) -> str:
    """文字的「对齐 + 横向放大」合成一个 transform。

    顺序必须是 ``scaleX() translateX()``：百分比位移按元素**原始**宽度算，
    先缩放再平移，位移量才跟着一起放大 —— 居中 / 右对齐才不会因放大而跑偏。
    """
    align = str(attrs.get("a") or "l").lower()
    shift = "-50%" if align.startswith("c") else ("-100%" if align.startswith("r") else "")
    parts: list[str] = []
    if abs(scale_x - 1) > 0.001:
        parts.append(f"scaleX({scale_x:.3f})")
    if shift:
        parts.append(f"translateX({shift})")
    if not parts:
        return ""
    return f"transform:{' '.join(parts)};transform-origin:left top;"


def _element_html(el: dict, page: dict, zoom: float) -> str:
    kind = el["kind"]
    attrs = el["attrs"]
    dpi = int(page.get("dpi") or 203)
    x = _num(attrs.get("x"), 0)
    y = _num(attrs.get("y"), 0)
    style = [f"left:{_px(x, zoom)}px", f"top:{_px(y, zoom)}px"]
    if kind == "TEXT":
        # f 不写时官方默认是 7（中文字体）
        size = FONT_DOTS.get(int(_num(attrs.get("f"), 7)), 24)
        # xw / yh 是横向 / 纵向放大倍数 —— 打印机真的会把字撑这么大。
        # 以前预览只按 f 算字号，于是「预览看着正好、打出来大字压到下一行」
        # （2026-10-09 修：预览必须跟着放大，不然没法照着预览调坐标）
        xw = _num(attrs.get("xw"), 1) or 1
        yh = _num(attrs.get("yh"), 1) or 1
        if xw <= 0:
            xw = 1
        if yh <= 0:
            yh = 1
        fs = _dots_px(size * yh, dpi, zoom)
        style.append(f"font-size:{fs:.2f}px")
        style.append(f"line-height:{fs:.2f}px")
        if str(attrs.get("b") or "") in ("1", "true", "Y", "y"):
            style.append("font-weight:700")
        style.append(_text_transform(attrs, xw / yh))
        text = html_mod.escape(el["text"]) or "&nbsp;"
        return f'<div class="lbl-el lbl-text" style="{";".join(style)}">{text}</div>'
    if kind == "BARC":
        narrow = max(1.0, _num(attrs.get("s"), 2))          # 窄条宽（点）
        module_px = _dots_px(narrow, dpi, zoom)
        height_px = _dots_px(_num(attrs.get("h"), 60), dpi, zoom)
        code_type = str(attrs.get("ct") or "code128").lower()
        content = el["text"]
        if "128" in code_type or not code_type:
            svg = bars_svg(barcode128.encode(content), module_px, height_px)
        else:
            # 非 128 的码制（ean13 等）不强求：画一个按内容长度估算的方框，标出码制
            svg = (
                f'<div class="lbl-el lbl-fake" style="width:{module_px * 11 * max(4, len(content)):.0f}px;'
                f'height:{height_px:.0f}px">{html_mod.escape(code_type)}</div>'
            )
        style.append(_align_style(attrs, "x"))
        return f'<div class="lbl-el lbl-barc" style="{";".join(style)}">{svg}</div>'
    if kind == "QR":
        size = _dots_px(_num(attrs.get("h"), 40), dpi, zoom)
        style.append(f"width:{size:.2f}px")
        style.append(f"height:{size:.2f}px")
        label = html_mod.escape(el["text"][:24]) or "QR"
        return (f'<div class="lbl-el lbl-qr" style="{";".join(style)}" '
                f'title="{label}"><span>QR</span></div>')
    if kind == "LINE":
        x2 = _num(attrs.get("x2"), x)
        y2 = _num(attrs.get("y2"), y)
        width_px = max(1.0, _dots_px(_num(attrs.get("w"), 2), dpi, zoom))
        left, top = min(_px(x, zoom), _px(x2, zoom)), min(_px(y, zoom), _px(y2, zoom))
        w = abs(_px(x2, zoom) - _px(x, zoom)) + width_px
        h = abs(_px(y2, zoom) - _px(y, zoom)) + width_px
        return (f'<div class="lbl-el" style="left:{left}px;top:{top}px;width:{w:.2f}px;'
                f'height:{h:.2f}px;background:#111"></div>')
    if kind == "RECT":
        w = _px(_num(attrs.get("w"), 10), zoom)
        h = _px(_num(attrs.get("h"), 10), zoom)
        lw = max(1.0, _dots_px(_num(attrs.get("lw") or attrs.get("w"), 2), dpi, zoom))
        style.append(f"width:{w:.2f}px")
        style.append(f"height:{h:.2f}px")
        style.append(f"border:{lw:.2f}px solid #111")
        return f'<div class="lbl-el lbl-rect" style="{";".join(style)}"></div>'
    # IMAGE / 未知：占位框（预览里说明「这里会是一张图」）
    w = _px(_num(attrs.get("w"), 20), zoom)
    h = _px(_num(attrs.get("h"), 20), zoom)
    style.append(f"width:{w:.2f}px")
    style.append(f"height:{h:.2f}px")
    return f'<div class="lbl-el lbl-fake" style="{";".join(style)}">图片</div>'


def render(xml: str, zoom: float = DEFAULT_ZOOM) -> dict:
    """XML → 预览（HTML 片段 + 纸张尺寸 + 元素计数）。

    ``ok=False`` 表示解析不出 PAGE（页面应退回显示原始 XML）。
    """
    page = parse(xml)
    if not sum(len(one["elements"]) for one in page["labels"]):
        # 一个元素都没解析出来（写坏的模板 / 本地链路的 TSPL 指令）：页面退回显示原文
        return {"ok": False, "html": "", "page": page, "counts": {}, "labels": 0,
                "count_text": "", "footer": ""}
    width, height = page["w"], page["h"]
    blocks: list[str] = []
    counts: dict[str, int] = {}
    for label in page["labels"]:
        spans = [_element_html(el, page, zoom) for el in label["elements"]]
        for el in label["elements"]:
            counts[el["kind"]] = counts.get(el["kind"], 0) + 1
        head = ""
        if len(page["labels"]) > 1 or label["copies"] > 1:
            head = (f'<div class="lbl-tag">第 {html_mod.escape(label["id"])} 张'
                    f'{" × " + str(label["copies"]) if label["copies"] > 1 else ""}</div>')
        blocks.append(
            f'<div class="lbl-item">{head}'
            f'<div class="lbl-paper" style="width:{_px(width, zoom)}px;height:{_px(height, zoom)}px">'
            f'{"".join(spans)}</div></div>'
        )
    gap_note = f" · 间隙 {page['gap']:g}mm" if page["gap"] else ""
    footer = (f"{width:g} × {height:g} mm{gap_note} · {page['dpi']}dpi · "
              f"共 {len(page['labels'])} 张")
    return {
        "ok": True,
        "html": "".join(blocks),
        "footer": footer,
        "width_mm": width,
        "height_mm": height,
        "gap_mm": page["gap"],
        "dpi": page["dpi"],
        "labels": len(page["labels"]),
        "counts": counts,
        "count_text": " · ".join(
            f"{KIND_LABELS.get(k, k)} {v}" for k, v in sorted(counts.items())
        ),
        "page": page,
    }


def elements_of(xml: str) -> list[dict]:
    """模板里用到的元素类型清单（页面上给「这个模板画了些什么」）。"""
    page = parse(xml)
    kinds: list[str] = []
    for label in page["labels"]:
        for el in label["elements"]:
            if el["kind"] not in kinds:
                kinds.append(el["kind"])
    return [{"kind": k, "label": KIND_LABELS.get(k, k)} for k in kinds]


def preview_css() -> str:
    """预览块要用的样式（各页面 head_extra 直接吃这段，保证一处口径）。"""
    return """
.lbl-item { display: inline-block; vertical-align: top; margin: 0 14px 10px 0; }
.lbl-tag { font-size: 11.5px; color: var(--c-text-muted, #6b7280); margin-bottom: 4px; }
.lbl-paper { position: relative; background: #fff; border: 1px dashed var(--c-line, #cbd5e1);
  border-radius: 3px; box-shadow: 0 1px 3px rgba(15, 23, 42, .10); overflow: hidden; }
.lbl-el { position: absolute; }
.lbl-text { color: #111; white-space: nowrap; font-family: var(--font-mono, monospace); }
.lbl-barc { line-height: 0; }
.lbl-qr { border: 1.5px solid #111; display: flex; align-items: center; justify-content: center;
  background: repeating-linear-gradient(45deg, #111 0 3px, #fff 3px 6px); }
.lbl-qr span { font-size: 9px; font-weight: 700; background: #fff; padding: 0 2px; }
.lbl-fake { border: 1px dashed #94a3b8; color: #64748b; font-size: 10px;
  display: flex; align-items: center; justify-content: center; background: #f8fafc; }
.lbl-foot { font-size: 11.5px; color: var(--c-text-muted, #6b7280); margin-top: 6px; }
"""


__all__ = ["parse", "render", "elements_of", "bars_svg", "preview_css", "KIND_LABELS",
           "DEFAULT_ZOOM"]
