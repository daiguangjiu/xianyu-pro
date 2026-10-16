"""从商品名 / 沉淀字段里认「型号 / 规格」（2026-10-16 用户要求）。

用户原话：**渔集能不能自己补一轮型号、规格，能补的就补，补不了的保持现状**。

背景：货是从供应商清单吃进来的，清单上只有一串 1688 式的营销标题（「欧克森X100proUltra
擦窗机器人机械臂喷水全自动…」），型号规格一格都没填 —— 检测台天天弹「去补全商品信息」，
渔枢 / 渔货栈的商品详情页也全是「—」。人工一台台填不现实，所以让系统先认一轮。

**只补空缺**：已经有型号 / 规格的一律不动（人填的永远优先于猜的）。
**认不出来就留空**：宁可不填也不填错 —— 猜错的型号比空着更害人（贴到闲鱼上就是错的）。

可信度从高到低：

  1. 清单里专门有「型号 / 款号」那一列（导入沉淀的 ``extra`` 字段）→ 直接用；
  2. 商品名里的**字母 + 数字混合串**（X100proUltra / BF-HL302 / MJ-PB40E254D / KFR-35GW）；
  3. 规格认容量 / 尺寸 / 颜色 / 套装（4L、10 寸、橄榄绿、3 件套）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 型号
# 字母开头、字母数字与 - . 混合（型号里常见的分隔符）
_MODEL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{1,23}")
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[A-Za-z]")
# 营销词 / 单位，不是型号
_MODEL_STOP = {
    "pro", "plus", "max", "mini", "ultra", "lite", "air", "new", "wifi", "ai",
    "usb", "led", "lcd", "hd", "4k", "gb", "tb", "ml", "kg", "cm", "mm", "hz",
    "db", "inch", "inchces", "official", "flagship",
}
# 以这些开头的更像是「Pro 4L」这种营销 + 参数的拼接，不当型号
_MODEL_BAD_PREFIX = (
    "pro", "plus", "max", "mini", "ultra", "lite", "air", "new", "wifi", "ai",
)

# ---------------------------------------------------------------- 规格
_CAPACITY = re.compile(
    r"\d+(?:\.\d+)?\s?(?:L|升|ML|ml|毫升|KG|kg|Kg|G\b|g\b|W|w|瓦|寸|英寸|CM|cm|MM|mm|米)"
)
_PACK = re.compile(r"\d+\s?(?:件套|只装|瓶装|袋装|支装|片装|个装)")
_COLORS = (
    "黑色", "白色", "银色", "金色", "灰色", "蓝色", "红色", "粉色", "紫色",
    "绿色", "米色", "咖啡色", "香槟金", "玫瑰金", "橄榄绿", "复古绿", "天空蓝",
    "象牙白", "月光白", "深空灰", "星空灰", "钛金色", "奶油白", "莫兰迪",
)
# 沉淀字段里哪些列名是「型号」
_MODEL_KEYS = ("型号", "款号", "产品型号", "商品型号")
# 沉淀字段里哪些列名是「规格」
_SPEC_KEYS = ("规格", "容量", "颜色", "尺码", "尺寸", "重量", "套餐", "套装")

_BRAND_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9&'\.\- ]{1,23}")


def _latin_of(brand: str) -> list[str]:
    """「欧克森（OUKESEN）」→ 中文名「欧克森」与英文名「OUKESEN」都要从商品名里抹掉。"""
    out: list[str] = []
    for part in _BRAND_LATIN.findall(str(brand or "")):
        part = part.strip()
        if len(part) >= 2:
            out.append(part)
    return out


def _strip_brand(text: str, brand: str) -> str:
    """把品牌（含英文名）从商品名里剔掉 —— 品牌名里带字母数字，会干扰型号识别。"""
    out = str(text or "")
    for piece in [str(brand or "").strip(), *_latin_of(brand)]:
        if piece and piece in out:
            out = out.replace(piece, " ")
    return out


def _is_model_key(key: str) -> bool:
    name = str(key or "").strip()
    return any(word in name for word in _MODEL_KEYS)


def _is_spec_key(key: str) -> bool:
    name = str(key or "").strip()
    return any(word in name for word in _SPEC_KEYS)


def _dedup(bits: list[str], limit: int = 3) -> list[str]:
    out: list[str] = []
    for bit in bits:
        bit = re.sub(r"\s+", "", str(bit or "")).strip()
        if not bit or bit in out:
            continue
        out.append(bit[:20])
        if len(out) >= limit:
            break
    return out


def guess_model(name: str = "", brand: str = "", extra: dict | None = None) -> str:
    """认型号：认不出来返回空串（**绝不猜一个凑数**）。"""
    for key, value in (extra or {}).items():
        if _is_model_key(key) and str(value or "").strip():
            return str(value).strip()[:64]
    text = _strip_brand(name, brand)
    best = ""
    for token in _MODEL_TOKEN.findall(text):
        low = token.lower()
        if len(token) < 2 or len(token) > 24:
            continue
        # 型号一定是「字母 + 数字」都有的（Pro / ULTRA 这类纯字母是营销词）
        if not _HAS_DIGIT.search(token) or not _HAS_ALPHA.search(token):
            continue
        if low in _MODEL_STOP or low.startswith(_MODEL_BAD_PREFIX):
            continue
        if len(token) > len(best):
            best = token
    return best


def guess_spec(name: str = "", brand: str = "", model: str = "",
               extra: dict | None = None) -> str:
    """认规格：容量 / 尺寸 / 套装 / 颜色，最多 3 段，用「 / 」连起来。"""
    bits: list[str] = []
    for key, value in (extra or {}).items():
        if _is_spec_key(key) and str(value or "").strip():
            bits.append(str(value).strip())
    text = _strip_brand(name, brand)
    if model:
        text = text.replace(model, " ")
    for match in _CAPACITY.finditer(text):
        bits.append(match.group(0))
    for match in _PACK.finditer(text):
        bits.append(match.group(0))
    for color in _COLORS:
        if color in text:
            bits.append(color)
    return " / ".join(_dedup(bits))


def fill_of(item) -> dict:
    """给一台货补型号 / 规格 —— **只补空缺**，已经有值的一律不动。

    返回 ``{"model": 补出来的型号, "spec": 补出来的规格}``（补不出来的那项是空串）。
    """
    name = str(getattr(item, "product_name", "") or getattr(item, "title", "") or "")
    brand = str(getattr(item, "brand", "") or "")
    extra = getattr(item, "extra", None) or {}
    model = guess_model(name, brand, extra)
    return {
        "model": "" if str(getattr(item, "model", "") or "").strip() else model,
        "spec": "" if str(getattr(item, "spec", "") or "").strip()
        else guess_spec(name, brand, model, extra),
    }


def guess_of(name: str = "", brand: str = "", extra: dict | None = None,
             model: str = "", spec: str = "") -> dict:
    """给一组原始字段（建档 / 导入时还没落库）补型号 / 规格，同样是只补空缺。"""
    got_model = guess_model(name, brand, extra)
    return {
        "model": model or got_model,
        "spec": spec or guess_spec(name, brand, got_model, extra),
    }
