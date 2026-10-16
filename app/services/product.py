"""商品档案：把订单里的商品名归一成商品，并维护闲鱼上架要用的电商属性。

和店铺 / 供应商档案是一套路子，商品部分参照自营商品的口径做全：

  1. 商品名（含别名）是订单与商品之间的桥 —— 群里怎么写，都能认到同一款商品上；
  2. 订单归档后自动挂到商品（Order.product_id），商品页直接看这款卖了多少、赚了多少；
  3. 订单里出现过、商品库里还没有的名字，可以一键建成「草稿」商品，资料在商品页补；
  4. 上架前要的东西都在商品上：来源（自营 / 三方）、审核状态、成色 / 类目、售价与划线价、
     成本、库存（可按单采购）、规格矩阵与 SKU、图集与图文详情、运费模板 / 发货时效 / 售后。

这一层只做业务，不碰页面。
"""

from __future__ import annotations

import html
import itertools
import json
import logging
import math
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Order, Product, ProductSku, Supplier, not_recalled
from ..timeutil import beijing_now, from_local, now
from . import category as category_service
from . import shipping as shipping_service

# 详情里认得出的 HTML 标签：出现就按 HTML 渲染，否则按纯文本排版（换行 / 空行都保留）
_DETAIL_HTML_RE = re.compile(
    r"<\s*(p|br|div|span|img|a|b|strong|i|em|u|ul|ol|li|h[1-6]|table|thead|tbody|tr|td|th|hr|pre|code)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 选项与标签
PRODUCT_STATUS_DRAFT = "draft"
PRODUCT_STATUS_ON = "on_sale"
PRODUCT_STATUS_OFF = "off_sale"
PRODUCT_STATUS_CHOICES: list[tuple[str, str]] = [
    (PRODUCT_STATUS_ON, "在售"),
    (PRODUCT_STATUS_OFF, "已下架"),
    (PRODUCT_STATUS_DRAFT, "未上架"),
]
PRODUCT_STATUS_LABELS = dict(PRODUCT_STATUS_CHOICES)

# 商品属性：自营商品（自己供货）/ 供应链商品（由某家供应链供货，必须指定是哪家）
SOURCE_CHOICES: list[tuple[str, str]] = [
    ("self_operated", "自营商品"),
    ("third_party", "供应链商品"),
]
SOURCE_LABELS = dict(SOURCE_CHOICES)
# 列表徽标 / 分段器里的短名（全称太长，表格里放不下）
SOURCE_SHORT_LABELS: dict[str, str] = {
    "self_operated": "自营",
    "third_party": "供应链",
}

# 审核：草稿 → 待审核 → 通过 / 驳回
AUDIT_DRAFT = "draft"
AUDIT_PENDING = "pending"
AUDIT_APPROVED = "approved"
AUDIT_REJECTED = "rejected"
AUDIT_CHOICES: list[tuple[str, str]] = [
    (AUDIT_DRAFT, "草稿"),
    (AUDIT_PENDING, "待审核"),
    (AUDIT_APPROVED, "已通过"),
    (AUDIT_REJECTED, "已驳回"),
]
AUDIT_LABELS = dict(AUDIT_CHOICES)

CONDITION_CHOICES: list[tuple[str, str]] = [
    ("new", "全新"),
    ("like_new", "几乎全新"),
    ("light_used", "轻微使用痕迹"),
    ("used", "明显使用痕迹"),
]
CONDITION_LABELS = dict(CONDITION_CHOICES)

# 闲鱼发货方式：邮寄-包邮 / 邮寄-不包邮-按距离付费 / 邮寄-不包邮-固定收费 / 无需邮寄-买家自提。
# 具体套哪一种是「运费模板 / 发货方式」规则说了算（services/shipping.py，支持全局默认 +
# 关联供应商 + 关联类目），商品上存的是这里选的 value。
SHIPPING_CHOICES: list[tuple[str, str]] = list(shipping_service.METHOD_CHOICES)
SHIPPING_LABELS = dict(SHIPPING_CHOICES)
# 老数据兼容（历史商品的 shipping_mode 用过 fee = 按件运费）
SHIPPING_LEGACY_LABELS = dict(shipping_service.METHOD_LEGACY)

# 商品类目：一级 → 二级（新增商品的一级/二级联动、详情页编辑的候选，都用这一份）。
# 二级留空 = 这个一级不再细分（前端会自动给出「手工填写」的输入框）。
CATEGORY_TREE: dict[str, list[str]] = {
    "数码配件": [
        "耳机音响", "手机配件", "电脑外设", "键盘鼠标", "充电线材", "存储卡 / U 盘",
        "摄影摄像", "智能穿戴",
    ],
    "个护电器": [
        "电吹风", "剃须刀", "电动牙刷", "美容仪", "直发卷发", "按摩器材", "剃毛器",
    ],
    "服饰鞋包": [
        "男装", "女装", "运动服", "运动鞋", "女包", "箱包", "内衣袜品", "配饰",
    ],
    "家居家纺": [
        "厨房小电", "锅具餐具", "床上用品", "收纳整理", "清洁电器", "家纺布艺", "家居日用",
    ],
    "食品保健": [
        "休闲零食", "坚果炒货", "冲调饮品", "粮油调味", "保健营养", "生鲜水果", "茶饮咖啡",
    ],
    "美妆个护": [
        "面部护肤", "彩妆", "洗护发", "身体护理", "香水", "美妆工具", "口腔护理",
    ],
    "其他": [],
}

# 二级类目里选它 = 手工填写（前端用的特殊值，不落到商品数据里）
CATEGORY_CUSTOM = "__custom__"

# 发布页用的常用选项（对应参照项目里的下拉框）
TOP_CATEGORIES: list[str] = list(CATEGORY_TREE)

# ---------------------------------------------------------------- 类目规格模板
# 型号 / 成色 / 重量这些**不是**商品的固定字段，而是「不同商品卖法不同」的**规格维度**：
# 数码类目看型号与成色，服饰看尺码与颜色，重货看重量。所以按类目给一套推荐规格项，
# 用户新建商品后自己挑、自己填值（规格名与候选值都能改）。
# 结构：[{name: 规格名, values: 候选值}]，values 为空 = 让用户自己输。
CONDITION_SPEC_VALUES: list[str] = [text for _, text in CONDITION_CHOICES]
# 一级类目名对的是「货源 · 类目管理」里那份（渔货栈货源广场的三级类目），不是商品服务里的旧常量
SPEC_TEMPLATES: dict[str, list[dict]] = {
    "家用电器": [
        {"name": "型号", "values": []},
        {"name": "成色", "values": CONDITION_SPEC_VALUES},
        {"name": "颜色", "values": []},
    ],
    "女装男装": [
        {"name": "尺码", "values": []},
        {"name": "颜色", "values": []},
        {"name": "成色", "values": CONDITION_SPEC_VALUES},
    ],
    "家纺家饰": [
        {"name": "规格", "values": []},
        {"name": "颜色", "values": []},
        {"name": "重量", "values": []},
    ],
    "食品生鲜": [
        {"name": "规格", "values": []},
        {"name": "重量", "values": []},
    ],
    "个护家清": [
        {"name": "规格", "values": []},
        {"name": "容量", "values": []},
    ],
    "餐饮饮具": [
        {"name": "容量", "values": []},
        {"name": "颜色", "values": []},
        {"name": "重量", "values": []},
    ],
    "办公文化": [
        {"name": "规格", "values": []},
        {"name": "颜色", "values": []},
    ],
    "母婴玩具": [
        {"name": "规格", "values": []},
        {"name": "颜色", "values": []},
    ],
    "五金工具": [
        {"name": "规格", "values": []},
        {"name": "重量", "values": []},
    ],
    "汽车用品": [
        {"name": "适配车型", "values": []},
        {"name": "规格", "values": []},
    ],
}
# 二级类目更具体，命中就优先用它（覆盖一级那套）
SPEC_TEMPLATES_CAT2: dict[str, list[dict]] = {
    "耳机音响": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES},
               {"name": "颜色", "values": []}],
    "手机配件": [{"name": "适配机型", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES},
               {"name": "颜色", "values": []}],
    "电脑外设": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "手机充电设备": [{"name": "功率", "values": ["20W", "30W", "65W"]},
                 {"name": "接口", "values": ["Type-C", "Lightning"]},
                 {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "线缆配件": [{"name": "长度", "values": ["1m", "1.5m", "2m"]}, {"name": "接口", "values": []},
               {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "智能设备": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "生活电器": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES},
              {"name": "重量", "values": []}],
    "男装": [{"name": "尺码", "values": ["M", "L", "XL", "2XL"]}, {"name": "颜色", "values": []},
            {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "女装": [{"name": "尺码", "values": ["S", "M", "L", "XL"]}, {"name": "颜色", "values": []},
            {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "鞋靴": [{"name": "鞋码", "values": ["38", "39", "40", "41", "42", "43"]},
            {"name": "颜色", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "箱包皮具": [{"name": "颜色", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "床上用品": [{"name": "规格", "values": ["1.5m 床", "1.8m 床", "2.0m 床"]},
              {"name": "重量", "values": []}, {"name": "颜色", "values": []}],
    "厨房小电": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES},
              {"name": "重量", "values": []}],
    "杯壶水具": [{"name": "容量", "values": ["350ml", "500ml", "750ml"]}, {"name": "颜色", "values": []}],
    "休闲零食": [{"name": "规格", "values": ["单盒", "三盒装", "六盒装"]}, {"name": "重量", "values": []}],
    "营养保健": [{"name": "规格", "values": ["单瓶", "礼盒装"]}, {"name": "重量", "values": []}],
    "美容护肤": [{"name": "容量", "values": ["30ml", "50ml", "100ml"]},
              {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    "玩具": [{"name": "规格", "values": ["基础套装", "豪华套装"]}, {"name": "颜色", "values": []}],
    "五金工具": [{"name": "规格", "values": ["46 件套", "108 件套"]}, {"name": "重量", "values": []}],
}
# 都没命中时的兜底：留一个「规格」维度，让用户自己填
DEFAULT_SPEC_TEMPLATE: list[dict] = [{"name": "规格", "values": []}]


def spec_template_for(category: str) -> list[dict]:
    """按商品的三级类目文本（"一级 / 二级 / 三级"）取推荐规格项。

    命中顺序：三级 → 二级 → 一级 → 兜底。返回的是副本，改它不影响常量。
    """
    parts = [p.strip() for p in str(category or "").replace("＞", "/").split("/") if p.strip()]
    for level in (2, 1, 0):
        key = parts[level] if len(parts) > level else ""
        if not key:
            continue
        hit = SPEC_TEMPLATES_CAT2.get(key)
        if not hit and level == 0:
            hit = SPEC_TEMPLATES.get(key)
        elif not hit and level in (1, 2):
            hit = SPEC_TEMPLATES_CAT2.get(key)
        if hit:
            return [{"name": d.get("name", ""), "values": list(d.get("values") or [])} for d in hit]
    return [{"name": d["name"], "values": list(d["values"])} for d in DEFAULT_SPEC_TEMPLATE]


def category_text(cat1: str, cat2: str = "") -> str:
    """一级 / 二级拼成商品上的类目字段（本项目类目是文本，便于统计与筛选）。"""
    cat1 = (cat1 or "").strip()
    cat2 = (cat2 or "").strip()
    if not cat2 or cat2 == CATEGORY_CUSTOM:
        return cat1
    if not cat1:
        # 只填了二级（理论上前端会拦住）：就用二级，别拼出「 / 耳机音响」这种怪值
        return cat2
    return f"{cat1} / {cat2}"


def category_suggestions(session: Session) -> list[str]:
    """类目候选：类目树里的一级、二级组合，加上库里已经在用的写法（老数据不丢）。"""
    values: list[str] = list(TOP_CATEGORIES)
    for cat1, subs in CATEGORY_TREE.items():
        values.extend(category_text(cat1, sub) for sub in subs)
    for existing in distinct_values(session, Product.category):
        if existing not in values:
            values.append(existing)
    return values
FREIGHT_TEMPLATES: list[str] = ["全国包邮", "按件运费", "偏远地区加收", "自提 / 到付"]
# 售后政策（2026-10-16 口径）：新增商品一律默认「非质量问题不退不换」——
# 我们卖的是尾货 / 二手检测机，只保质量问题，7 天无理由会把退货率抬上去。
# 放在列表第一位 = 新建表单默认选中的那一项（模板按 loop.first 选中）。
DEFAULT_AFTER_SALE = "非质量问题不退不换"
AFTER_SALE_OPTIONS: list[str] = [
    DEFAULT_AFTER_SALE, "7 天无理由退换", "质量问题包退换", "不支持退换",
]

TEXT_FIELDS = (
    "name", "code", "category", "brand", "model", "spec", "condition", "status",
    "source_type", "audit_status", "audit_note", "listing_id", "listing_url",
    "shipping_mode", "freight_template", "delivery_days", "after_sale",
    "supplier_name", "shop_name", "image_path", "selling_points", "detail_html",
    "supplier_tip", "note",
)
MONEY_FIELDS = ("price", "original_price", "cost_amount", "fish_price", "shipping_fee", "weight")
INT_FIELDS = ("stock", "supplier_id")


def _label(mapping: dict[str, str], value: str) -> str:
    return mapping.get(value or "", value or "")


def status_label(product: Product) -> str:
    return _label(PRODUCT_STATUS_LABELS, product.status)


def source_label(product: Product) -> str:
    return _label(SOURCE_LABELS, product.source_type)


def source_short_label(product: Product) -> str:
    """列表徽标用的短名（自营 / 供应链）。"""
    return SOURCE_SHORT_LABELS.get(product.source_type or "self_operated", "自营")


def audit_label(product: Product) -> str:
    return _label(AUDIT_LABELS, product.audit_status)


def condition_label(product: Product) -> str:
    return _label(CONDITION_LABELS, product.condition)


def shipping_label(product: Product) -> str:
    """发货方式文案（兼容历史值 fee）。"""
    value = (product.shipping_mode or "").strip()
    if not value:
        return "—"
    return shipping_service.method_label(value)


def product_label(product: Product) -> str:
    """列表 / 下拉里的显示名：商品名（编码）。"""
    code = (product.code or "").strip()
    return f"{product.name}（{code}）" if code else (product.name or f"#{product.id}")


def product_url(product: Product) -> str:
    """商品详情页地址：用**系统流水号（商品编码）**，不用中文商品名。

    编码没维护的老数据回落内部 id，保证链接永远能打开。
    """
    code = (product.code or "").strip()
    return f"/products/{quote(code, safe='')}" if code else f"/products/{product.id}"


def detail_rich(text: str | None) -> str:
    """商品详情的展示排版：**填的什么格式就显示什么格式**。

    · 纯文本（运营从微信 / 供应商那儿直接粘过来的那种）：转义后按输入渲染 ——
      单换行 → ``<br>``、空行 → 分段（``<p>``），图标 / emoji / 特殊符号原样保留；
    · 已经写了 HTML 标签的：原样输出，交给浏览器渲染。

    以前查看页直接 ``| safe`` 输出，纯文本里的换行与空行全被浏览器折叠成一行，
    看着和录入的完全不一样。
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    if _DETAIL_HTML_RE.search(raw):
        return raw
    blocks = re.split(r"\n\s*\n", raw.strip())
    out = []
    for block in blocks:
        lines = list(block.splitlines()) or [""]
        out.append("<p>" + "<br>".join(html.escape(line) for line in lines) + "</p>")
    return "\n".join(out)


# ---------------------------------------------------------------- 多组详情文案
# 一个商品可以写好几版详情文案（不同渠道 / 不同卖点各来一版），最多 20 组；
# 组的顺序就是排序：新增的排在最后，标题默认「文案一 / 文案二 …」。
DETAIL_BLOCK_MAX = 20
_CN_NUM = "一二三四五六七八九十"


def _cn_index(index: int) -> str:
    """1 → 一，11 → 十一，20 → 二十（只用到 20 组，够用就好）。"""
    if index <= 10:
        return _CN_NUM[index - 1]
    if index < 20:
        return "十" + _CN_NUM[index - 11]
    return "二十"


def detail_block_title(index: int) -> str:
    return f"文案{_cn_index(index)}"


def parse_detail_blocks(raw: str, fallback_html: str = "") -> list[dict]:
    """把表单里的 ``detail_blocks_json`` 洗成组列表（最多 20 组，空组丢掉）。

    老数据 / 没走新表单时（比如接口直接建商品）用 ``fallback_html`` 兜底成一组。
    """
    rows: list = []
    try:
        parsed = json.loads(str(raw or "").strip() or "[]")
    except ValueError:
        parsed = []
    if isinstance(parsed, list):
        rows = [r for r in parsed if isinstance(r, dict)]
    out: list[dict] = []
    for row in rows[:DETAIL_BLOCK_MAX]:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        title = str(row.get("title") or "").strip() or detail_block_title(len(out) + 1)
        out.append({"title": title, "content": content})
    if not out and str(fallback_html or "").strip():
        out.append({"title": detail_block_title(1), "content": str(fallback_html).strip()})
    return out


def detail_blocks_of(product: Product) -> list[dict]:
    """读商品上的多组文案；没有分组数据的老商品按原 detail_html 兜成「文案一」。"""
    raw = getattr(product, "detail_blocks", "") or ""
    try:
        rows = json.loads(raw) if str(raw).strip() else []
    except ValueError:
        rows = []
    if not isinstance(rows, list):
        rows = []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        out.append(
            {
                "title": str(row.get("title") or "").strip() or detail_block_title(len(out) + 1),
                "content": content,
            }
        )
    if not out and (product.detail_html or "").strip():
        out.append({"title": detail_block_title(1), "content": product.detail_html.strip()})
    return out[:DETAIL_BLOCK_MAX]


def detail_blocks_text(blocks: list[dict]) -> str:
    """多组文案拼成一份文本写回 detail_html（闲鱼发布包 / 渔货栈同步读的就是它）。"""
    return "\n\n".join(str(b.get("content") or "").strip() for b in blocks if str(b.get("content") or "").strip())


# ---------------------------------------------------------------- 详情图
# 详情图最终也是拼进 detail_html（闲鱼描述里就是一段段 <p><img></p>），
# 但编辑页要能把它们当「图片」单独改 / 排序 / 删，所以读写时都得跟文案拆开。
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"""src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
# 整段就是图的 <p>：连壳一起拿走，免得留下空段落
_P_ONLY_IMG_RE = re.compile(r"<p>\s*(?:<img\b[^>]*>\s*)+</p>", re.IGNORECASE)


def _img_src(tag: str) -> str:
    found = _IMG_SRC_RE.search(tag)
    if not found:
        return ""
    return str(found.group(1) or found.group(2) or found.group(3) or "").strip()


def strip_detail_images(content: str) -> tuple[str, list[str]]:
    """从一段详情文本里挑出图片：返回（去掉图片后的文本, 图片地址列表，按出现顺序）。"""
    srcs: list[str] = []

    def take(match: re.Match) -> str:
        for tag in _IMG_TAG_RE.findall(match.group(0)) or [match.group(0)]:
            src = _img_src(tag)
            if src:
                srcs.append(src)
        return ""

    text = _P_ONLY_IMG_RE.sub(take, str(content or ""))
    text = _IMG_TAG_RE.sub(take, text)
    # 去掉图片后可能只剩空白行的段落，一起收拾干净
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, srcs


def detail_images_of(product: Product) -> list[str]:
    """商品已有的详情图（去重、保序）。

    以 ``detail_html`` 为准：详情图统一拼在文案后面，所以从它里面捞一遍就够
    （老数据的图也是混在 detail_html 里的）。
    """
    images: list[str] = []
    for src in strip_detail_images(product.detail_html or "")[1]:
        if src and src not in images:
            images.append(src)
    return images


def detail_blocks_without_images(product: Product) -> list[dict]:
    """多组文案（图片已剥掉）：编辑页左半边写文案、右半边管图片，互不打架。"""
    out: list[dict] = []
    for block in detail_blocks_of(product):
        text, _ = strip_detail_images(block["content"])
        if text:
            out.append({"title": block["title"], "content": text})
    return out


def compose_detail_html(blocks: list[dict], images: list[str]) -> str:
    """详情最终形态 = 各组文案 + 详情图（图统一排在文案后面，闲鱼描述里也是这么展示）。"""
    parts = [detail_blocks_text(blocks)]
    for url in images:
        url = str(url or "").strip()
        if url:
            parts.append(f'<p><img src="{url}" alt=""></p>')
    return "\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------- 详情分组展示
# 查看页（渔枢系统与渔货栈）都按「一组文案 / 一组详情图」看：文案分段，详情图每 9 张一组。
DETAIL_IMAGE_GROUP_SIZE = 9


def detail_image_groups(images: list[str], size: int = DETAIL_IMAGE_GROUP_SIZE) -> list[list[str]]:
    """详情图按每 9 张一组切开（一组就是一屏，看的时候一组一组翻）。"""
    size = max(1, int(size or DETAIL_IMAGE_GROUP_SIZE))
    urls = [str(u or "").strip() for u in (images or []) if str(u or "").strip()]
    return [urls[i:i + size] for i in range(0, len(urls), size)]


def split_detail_groups(text: str, max_groups: int = DETAIL_BLOCK_MAX) -> list[dict]:
    """把一整份详情文案按段拆成若干组（渔货栈 / 老数据只有 detail_html、没有分组信息时用）。

    HTML 按 <p> 段拆，纯文本按空行拆；图片不参与（它们单独成组展示）。
    """
    raw, _ = strip_detail_images(str(text or ""))
    if not raw:
        return []
    chunks: list[str] = []
    if "<p" in raw.lower():
        chunks = [c.strip() for c in re.findall(r"<p\b[^>]*>.*?</p>", raw, flags=re.S | re.I) if c.strip()]
        rest = re.sub(r"<p\b[^>]*>.*?</p>", "", raw, flags=re.S | re.I).strip()
        if rest:
            chunks.append(rest)
    if not chunks:
        chunks = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if not chunks:
        chunks = [raw.strip()]
    out: list[dict] = []
    for chunk in chunks[:max_groups]:
        out.append({"title": detail_block_title(len(out) + 1), "content": chunk})
    return out


def detail_groups_of(product: Product) -> list[dict]:
    """查看页要展示的文案分组：存过分组的用分组，老商品（只有一份 detail_html）按段还原。"""
    stored = parse_detail_blocks(getattr(product, "detail_blocks", "") or "")
    if stored:
        return detail_blocks_without_images(product)
    return split_detail_groups(product.detail_html or "")


def is_self_operated(product: Product) -> bool:
    return (product.source_type or "self_operated") == "self_operated"


def is_supply_chain(product: Product) -> bool:
    """供应链商品：由某家供应链供货，必须挂到具体供应链上。"""
    return (product.source_type or "") == "third_party"


# ---------------------------------------------------------------- 名字归一与匹配
def normalize_name(text: str | None) -> str:
    return re.sub(r"[\s\-—_·、,，。]+", "", str(text or "")).lower()


def name_stem(text: str | None) -> str:
    """粗略词干：去掉数字后的名字前缀，用于「同款不同叫法」的兜底匹配。"""
    return re.sub(r"[0-9]+", "", normalize_name(text))[:10]


# ---------------------------------------------------------------- 查询
def get_product(session: Session, product_id: int) -> Product | None:
    return session.get(Product, product_id)


def find_by_code(session: Session, code: str, exclude_id: int | None = None) -> Product | None:
    """按商品编码（系统流水号）找商品，大小写不敏感。"""
    key = str(code or "").strip()
    if not key:
        return None
    row = session.scalar(select(Product).where(func.lower(Product.code) == key.lower()))
    if row is not None and exclude_id and row.id == exclude_id:
        return None
    return row


def get_by_key(session: Session, key: str) -> Product | None:
    """详情页入参：商品编码（对外分享用）优先，老链接的数字 id 继续可用。"""
    text = str(key or "").strip()
    if not text:
        return None
    row = find_by_code(session, text)
    if row is not None:
        return row
    return session.get(Product, int(text)) if text.isdigit() else None


def _product_filters(
    q: str = "",
    category: str = "",
    status: str = "",
    supplier: str = "",
    source: str = "",
    audit: str = "",
) -> list:
    """商品列表的筛选条件（分页取数与计数共用一份，避免两边条件走偏）。"""
    conds = []
    if q:
        like = f"%{q.strip()}%"
        conds.append(Product.name.like(like) | Product.code.like(like))
    if category:
        conds.append(Product.category == category)
    if status:
        conds.append(Product.status == status)
    if supplier:
        conds.append(Product.supplier_name == supplier)
    if source:
        conds.append(Product.source_type == source)
    if audit:
        conds.append(Product.audit_status == audit)
    return conds


def _product_stmt(
    q: str = "",
    category: str = "",
    status: str = "",
    supplier: str = "",
    source: str = "",
    audit: str = "",
):
    stmt = select(Product)
    for cond in _product_filters(
        q=q, category=category, status=status, supplier=supplier, source=source, audit=audit
    ):
        stmt = stmt.where(cond)
    return stmt


def count_products(
    session: Session,
    q: str = "",
    category: str = "",
    status: str = "",
    supplier: str = "",
    source: str = "",
    audit: str = "",
) -> int:
    """符合筛选条件的商品总数（分页用，走 SQL 汇总，不把数据取回来数）。"""
    stmt = select(func.count()).select_from(Product)
    for cond in _product_filters(
        q=q, category=category, status=status, supplier=supplier, source=source, audit=audit
    ):
        stmt = stmt.where(cond)
    return int(session.scalar(stmt) or 0)


def list_products(
    session: Session,
    q: str = "",
    category: str = "",
    status: str = "",
    supplier: str = "",
    source: str = "",
    audit: str = "",
    limit: int = 0,
    offset: int = 0,
) -> list[Product]:
    """商品列表（按筛选条件过滤，名称排序）。

    `limit > 0` 时分页取（配套 `offset`），默认一次性取全部（老调用点行为不变）。
    """
    stmt = _product_stmt(
        q=q,
        category=category,
        status=status,
        supplier=supplier,
        source=source,
        audit=audit,
    ).order_by(Product.name)
    if limit and limit > 0:
        stmt = stmt.limit(limit).offset(max(int(offset), 0))
    return list(session.scalars(stmt))


def find_by_name(session: Session, name: str, exclude_id: int | None = None) -> Product | None:
    target = normalize_name(name)
    if not target:
        return None
    for row in session.scalars(select(Product)):
        if exclude_id and row.id == exclude_id:
            continue
        if normalize_name(row.name) == target:
            return row
    return None


def list_stats(session: Session) -> dict:
    """列表页顶部的分段器计数：全部 / 自营 / 供应链 + 各审核状态。未受筛选影响。"""
    stats = {
        "total": 0, "self": 0, "third": 0,
        "pending": 0, "approved": 0, "rejected": 0, "draft": 0,
        "on_sale": 0, "off_sale": 0,
    }
    for product in session.scalars(select(Product)):
        stats["total"] += 1
        if (product.source_type or "self_operated") == "self_operated":
            stats["self"] += 1
        else:
            stats["third"] += 1
        audit = product.audit_status or AUDIT_DRAFT
        if audit in stats:
            stats[audit] += 1
        if product.status == PRODUCT_STATUS_ON:
            stats["on_sale"] += 1
        elif product.status == PRODUCT_STATUS_OFF:
            stats["off_sale"] += 1
    return stats


def distinct_values(session: Session, column) -> list[str]:
    rows = session.scalars(
        select(column).where(column.is_not(None), column != "").distinct().order_by(column)
    )
    return [str(v) for v in rows]


def supplier_options(session: Session) -> list[dict]:
    """供应链下拉：供应商档案里的「名称（编码）」，新增 / 编辑商品时选具体是哪家。"""
    rows = session.scalars(select(Supplier).order_by(Supplier.name)).all()
    return [
        {"id": int(row.id), "name": row.name or "", "code": row.code or ""}
        for row in rows
        if (row.name or "").strip()
    ]


# 商品流水 ID（商家编码 / SKU 前缀）：XYSK + 日期 + 4 位序号，例 XYSK202610030015
CODE_PREFIX = "XYSK"
CODE_SERIAL_WIDTH = 4
_CODE_RE = re.compile(rf"^{CODE_PREFIX}(\d{{8}})(\d{{{CODE_SERIAL_WIDTH},}})$")


def serial_of(code: str | None) -> int:
    """从「XYSK202610030015」里取序号 15；不是这个格式的返回 0。"""
    hit = _CODE_RE.match(str(code or "").strip())
    return int(hit.group(2)) if hit else 0


def is_serial_code(code: str | None) -> bool:
    """是不是新规则的流水号（老编码 YHZ-…、SPD0001 这种都不是）。"""
    return bool(_CODE_RE.match(str(code or "").strip()))


def next_code(session: Session, category_text: str | None = None, *, on: datetime | None = None) -> str:
    """商品流水 ID（商家编码 / SKU 前缀）自动生成：

        XYSK + 日期(YYYYMMDD) + 4 位序号

    例：`XYSK202610030015`（第 15 个商品，2026-10-03 建档）。

    - XYSK = 闲鱼商品库（前缀固定）；
    - 日期取建档的北京自然日；
    - **序号全局累计、不按天重置**（第 16 个商品就是 …0016），删商品也不回收；
    - 类目只决定「什么时候能生成」（选完类目即可出号），不再进编码；
    - 老编码（YHZ-…、SPD0001）由一次性迁移换成这套规则（见 db.py 的
      _migrate_product_serial_code）。
    """
    day = (on or beijing_now()).strftime("%Y%m%d")  # 编码里的日期按北京自然日
    rows = session.scalars(select(Product.code).where(Product.code.like(f"{CODE_PREFIX}%"))).all()
    used = 0
    for code in rows:
        used = max(used, serial_of(code))
    return f"{CODE_PREFIX}{day}{used + 1:0{CODE_SERIAL_WIDTH}d}"


def sku_code_of(product: Product, index: int) -> str:
    """SKU 编码 = 商家编码 + 两位序号：`XYSK202610030015-01`、`-02`…

    系统强制派生，页面上只读展示，不手填（改商家编码 / 删一行都会自动重排）。
    """
    base = str(getattr(product, "code", "") or "").strip()
    if not base:  # 老数据没编码时兜底，至少同一商品内不撞
        base = f"SKU{int(getattr(product, 'id', 0) or 0)}"
    return f"{base}-{max(int(index), 1):02d}"


def renumber_sku_codes(session: Session, product: Product) -> int:
    """把某商品的 SKU 编码按 id 顺序重排成「商家编码-01 / -02…」。

    规格增删、商家编码改了之后都要跑一遍，保证 SKU 编码始终由商家编码派生。
    """
    changed = 0
    for idx, sku in enumerate(list_skus(session, int(product.id or 0)), start=1):
        want = sku_code_of(product, idx)
        if (sku.sku_code or "").strip() != want:
            sku.sku_code = want
            changed += 1
    if changed:
        session.commit()
    return changed


def sync_stock_from_skus(session: Session, product: Product) -> int:
    """总库存 = 各 SKU 库存之和（库存留空的规格不算）。

    库存跟着规格走：商品级那个「库存」是批量设置用的默认值，
    规格表里填了就以规格表为准；一条规格都没填库存时，才保留商品级填的值。
    """
    stocks = [
        int(sku.stock)
        for sku in list_skus(session, int(product.id or 0))
        if sku.stock is not None and int(sku.stock) >= 0
    ]
    if stocks:
        product.stock = sum(stocks)
    else:
        product.stock = max(int(product.stock or 0), 0)
    return int(product.stock or 0)


def _finalize_skus(session: Session, product: Product) -> None:
    """SKU 动过之后统一收尾：刷盘 → 按商家编码重排编码 → 汇总总库存 → 提交。

    刷盘是必须的：session 关了 autoflush，不刷一遍下面查到的还是删之前的行。
    """
    session.flush()
    renumber_sku_codes(session, product)
    sync_stock_from_skus(session, product)
    product.updated_at = now()
    session.commit()


# ---------------------------------------------------------------- 标题 / 别名推荐
# 标题规则（闲鱼上限 30 字）：品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词，
# 词库来自「类目特性规则」（一级类目维护，见 services/category.py 的 parse_rules）。
TITLE_LIMIT = 30
_TITLE_DEFAULT_MODIFIERS: list[str] = ["全新未拆封", "正品保障"]
# 详情文案重写时给要点统一排的项目符号（轮着用，重写后看着像重新排过版）
_REWRITE_BULLETS: list[str] = ["✔ ", "· ", "• "]
# 这些是「营销词」，认商品核心词时先剔掉，避免别名里带一堆废话
_MARKETING_WORDS: list[str] = [
    "全新未拆封", "全新未开封", "全新", "未拆封", "未开封", "正品保障", "正品", "包邮",
    "现货", "秒发", "官方标配", "国行", "二手", "准新", "特价", "特惠", "清仓", "促销",
    "热卖", "爆款", "原装", "原封", "拆封", "未使用", "仅拆封",
]


def _strip_marketing(text: str) -> str:
    out = str(text or "").strip()
    for word in _MARKETING_WORDS:
        out = out.replace(word, " ")
    return re.sub(r"\s+", " ", out).strip(" -_·|/")


def _product_word(name: str, brand: str = "") -> str:
    """从商品标题里取「产品词」：去掉品牌前缀与营销词。"""
    core = _strip_marketing(name)
    brand = (brand or "").strip()
    if brand and core.startswith(brand):
        core = core[len(brand):].strip(" -_·|/")
    return core or _strip_marketing(name) or str(name or "").strip()


def _truncate_words(text: str, limit: int) -> str:
    """按词边界把长文本裁到 limit 字以内（尽量不留半截词）。"""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    picked: list[str] = []
    for word in re.split(r"\s+", text):
        if not word:
            continue
        if len(" ".join([*picked, word])) > limit:
            break
        picked.append(word)
    out = " ".join(picked)
    # 一个词都放不下（整串没有空格）时只能硬截
    return out if out else text[:limit].strip()


def _join_title(words: list[str], limit: int = TITLE_LIMIT) -> str:
    """按顺序把词拼成标题：去重、去空、不超 limit 字（超了就丢掉最长的那个词）。"""
    picked: list[str] = []
    for word in words:
        word = str(word or "").strip(" -_·|/")
        if not word:
            continue
        if any(word in item for item in picked):
            continue
        if len(" ".join([*picked, word])) > limit:
            continue
        picked.append(word)
    title = " ".join(picked)
    if len(title) > limit:
        title = title[:limit]
    return title


def rewrite_detail_text(
    session: Session,
    *,
    text: str = "",
    name: str = "",
    brand: str = "",
    category: str = "",
    selling_points: str = "",
) -> dict:
    """把一组详情文案重写一版（词库驱动，与「AI 生成标题」同一套路）：

    · 开头补一句「品牌 + 产品词 + 类目热搜词」的引导句；
    · 原来的每条要点统一成带图标的短句（先去掉旧序号 / 符号，再重新断句）；
    · 结尾补一句卖点 / 售后提示。
    原文的分段结构保留，结果可直接粘回发布页（前端会自动放进剪贴板）。
    """
    rule = category_service.rules_for(session, category)
    rules = rule["rules"]
    brand_word = (brand or "").strip()
    product_word = _product_word(name, brand_word)
    points = [p.strip() for p in re.split(r"[\n,，;；/|]+", str(selling_points or "")) if p.strip()]
    hots = [str(w).strip() for w in (rules["hot"] or []) if str(w).strip()]
    longs = [str(w).strip() for w in (rules["longtail"] or []) if str(w).strip()]

    body, _ = strip_detail_images(str(text or ""))
    lines: list[str] = []
    for raw in body.splitlines():
        line = str(raw).strip()
        if not line:
            lines.append("")
            continue
        # 去掉原来带的序号 / 项目符号，重写时统一再排一遍
        line = re.sub(r"^[\s\-—•·*※]+", "", line)
        line = re.sub(r"^[（(]?\d+[)）.、]\s*", "", line)
        line = re.sub(r"</?p\b[^>]*>", "", line, flags=re.I)
        line = re.sub(r"<br\s*/?>", " ", line, flags=re.I)
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        lines.append(line)

    # 连续空行压成一个：重写后按空行分段，跟发布页的排版一致
    packed: list[str] = []
    for line in lines:
        if not line and (not packed or not packed[-1]):
            continue
        packed.append(line)
    while packed and not packed[0]:
        packed.pop(0)
    while packed and not packed[-1]:
        packed.pop()

    out: list[str] = []
    if brand_word and product_word:
        lead = f"{brand_word} {product_word}".strip()
    else:
        lead = (brand_word or product_word).strip()
    if lead:
        lead_tail = hots[0] if hots else (points[0] if points else "")
        out.append(f"{lead}｜{lead_tail}" if lead_tail else lead)
    index = 0
    for line in packed:
        if not line:
            out.append("")
            continue
        out.append(f"{_REWRITE_BULLETS[index % len(_REWRITE_BULLETS)]}{line}")
        index += 1
    tail_bits = [p for p in points[:2]] + list(longs[:1])
    if tail_bits:
        out.append("")
        out.append(f"温馨提示：{'，'.join(tail_bits)}。")
    rewritten = "\n".join(out).strip()
    return {
        "text": rewritten,
        "ok": bool(rewritten),
        "note": "按类目词库重写的一版，已复制；想改再粘回发布页编辑" if rewritten else "这段没有可重写的内容",
    }


def suggest_titles(
    session: Session,
    *,
    name: str = "",
    brand: str = "",
    category: str = "",
    selling_points: str = "",
    limit: int = 5,
) -> dict:
    """按「品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词」给出标题候选（每条 ≤ 30 字）。

    词库优先取类目特性规则；类目没维护过就只用品牌词 + 产品词 + 卖点词，
    保证任何类目点一下都有东西可用。
    """
    rule = category_service.rules_for(session, category)
    rules = rule["rules"]
    brand_word = (brand or "").strip()
    product_word = _product_word(name, brand_word)
    points = [p.strip() for p in re.split(r"[\n,，;；/|]+", str(selling_points or "")) if p.strip()]
    modifiers = rules["modifier"] or points[:2] or list(_TITLE_DEFAULT_MODIFIERS)
    hots = rules["hot"] or []
    longs = rules["longtail"] or []

    titles: list[dict] = []
    seen: set[str] = set()
    for index in range(max(1, limit)):
        modifier = modifiers[index % len(modifiers)] if modifiers else ""
        hot = hots[index % len(hots)] if hots else ""
        long_tail = longs[index % len(longs)] if longs else ""
        # 产品词太长会一个人占满 30 字，后面几个词就塞不进来 —— 按候选序号给产品词让位，
        # 越靠后的候选让得越多，标题就能「填满 30 字」且每条组合都不一样。
        reserve = 5 + 3 * index
        head = len(brand_word) + (1 if brand_word else 0)
        max_product = max(6, TITLE_LIMIT - head - reserve)
        product_part = _truncate_words(product_word, max_product)
        words = [
            brand_word, product_part, modifier,
            hot, *(hots[index + 1:] if hots else []),
            long_tail, *(longs[index + 1:] if longs else []),
            *(points[index:] if points else []),
        ]
        title = _join_title(words)
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(
            {
                "title": title,
                "length": len(title),
                "words": {
                    "品牌词": brand_word, "产品词": product_part, "修饰词": modifier,
                    "热搜词": hot, "长尾词": long_tail,
                },
            }
        )
    return {
        "titles": titles,
        "rule_category": rule.get("category", ""),
        "rule_hint": "品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词",
        "title_limit": TITLE_LIMIT,
        "has_rule": bool(rules["modifier"] or rules["hot"] or rules["longtail"]),
    }


# ---------------------------------------------------------------- 规格定义
def parse_spec_defs(text: str) -> list[dict]:
    """把「颜色: 黑, 白」这种多行文本解析成规格定义。

    一行一个规格维度，冒号后是候选值（逗号 / 竖线分隔），如：
        颜色: 深空黑, 白
        容量: 128G, 256G
    """
    defs: list[dict] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        values = [v.strip() for v in re.split(r"[,，|/、]", parts[1]) if v.strip()]
        if name and values:
            defs.append({"name": name, "values": values})
    return defs


def dump_spec_defs(defs: list[dict]) -> str:
    return json.dumps(defs, ensure_ascii=False)


def spec_defs_text(product: Product) -> str:
    """规格定义回显成可编辑文本。"""
    return "\n".join(f"{d['name']}: {', '.join(d['values'])}" for d in product.spec_def_list)


def combo_specs(defs: list[dict]) -> list[str]:
    """规格组合（笛卡尔积）：颜色[黑,白] × 容量[128G] → 「黑 / 128G」「白 / 128G」。"""
    if not defs:
        return []
    value_groups = [d["values"] for d in defs]
    return [" / ".join(combo) for combo in itertools.product(*value_groups)]


def generate_skus(session: Session, product: Product, defs: list[dict]) -> int:
    """按规格组合补齐 SKU（已存在的规格不重复建，价格 / 库存沿用商品上的默认值）。"""
    existing = {sku.spec.strip() for sku in list_skus(session, product.id)}
    created = 0
    for spec in combo_specs(defs):
        if spec in existing:
            continue
        session.add(
            ProductSku(
                product_id=product.id,
                spec=spec,
                price=float(product.price or 0),
                cost_amount=float(product.cost_amount or 0),
                fish_price=float(product.fish_price or 0),
                market_price=float(product.original_price or 0),
                # 库存留空（-1 = 不单独管，跟着商品级那个值走）
                stock=-1,
            )
        )
        created += 1
    product.spec_defs = dump_spec_defs(defs)
    _finalize_skus(session, product)
    return created


# ---------------------------------------------------------------- 建档 / 编辑
def sync_supplier(session: Session, product: Product) -> None:
    """把「供应链 id」与「供应链名称」对齐：填哪个都能补上另一个。

    另外按商品属性收口：自营商品不挂供应链（清掉），
    供应链商品的 supplier_id / supplier_name 至少有一个能对上档案。
    """
    if is_self_operated(product):
        product.supplier_id = 0
        product.supplier_name = ""
        return
    if product.supplier_id:
        row = session.get(Supplier, product.supplier_id)
        if row is not None:
            product.supplier_name = row.name or ""
            return
        product.supplier_id = 0
    name = (product.supplier_name or "").strip()
    if not name:
        return
    row = session.scalar(select(Supplier).where(Supplier.name == name))
    product.supplier_id = int(row.id) if row is not None else 0


def apply_shipping_defaults(session: Session, product: Product) -> dict:
    """按「运费模板 / 发货方式」规则给商品补默认值。

    只在商品上没选（空）时才套用，不覆盖运营的手工选择。命中顺序由
    services/shipping.py::pick_rule 决定：供应商 + 类目 > 类目 > 供应商 > 全局默认。
    返回实际套上的字段（页面上用来提示「已按规则自动套用」）。
    """
    resolved = shipping_service.resolve_for_product(
        session, category_text=product.category, supplier_id=product.supplier_id or None
    )
    touched: dict[str, str] = {}
    if not (product.freight_template or "").strip() and resolved["freight_template"]:
        product.freight_template = resolved["freight_template"]
        touched["freight_template"] = resolved["freight_template"]
        if not float(product.shipping_fee or 0) and resolved["freight_fee"]:
            product.shipping_fee = float(resolved["freight_fee"])
            touched["shipping_fee"] = f"{float(resolved['freight_fee']):.2f}"
    if not (product.shipping_mode or "").strip() and resolved["shipping_mode"]:
        product.shipping_mode = resolved["shipping_mode"]
        touched["shipping_mode"] = resolved["shipping_mode"]
    return touched


def _parse_datetime(raw: str, *, to_utc: bool = True) -> datetime | None:
    """解析表单里的时间：datetime-local 交的是「2026-09-29T10:30」，也兼容手写空格分隔与只有日期。

    时间口径（2026-10-03）：填进来的是**用户时区**的时间（页面 ``| dt`` 就是按用户时区
    渲染的），默认转成 UTC 再存库 —— 不转的话页面会显示成另一个点。
    """
    text = str(raw or "").strip().replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return from_local(parsed) if to_utc else parsed
    return None


# 价格口径（2026-10-07 定稿）：渔货价 = 成本价 × 1.15，建议售价 = 渔货价 × 1.35。
# 只兜底「没填」的字段 —— 运营手填过的一律不动。
FISH_RATE = 1.15
SALE_RATE = 1.35


def derive_prices(product: Product) -> None:
    """按成本价派生渔货价 / 建议售价：留空的才补，填过的不动。"""
    cost = float(getattr(product, "cost_amount", 0) or 0)
    fish = float(getattr(product, "fish_price", 0) or 0)
    if cost > 0 and fish <= 0:
        fish = round(cost * FISH_RATE, 2)
        product.fish_price = fish
    if fish > 0 and float(getattr(product, "price", 0) or 0) <= 0:
        product.price = round(fish * SALE_RATE, 2)


def _apply_form(session: Session, product: Product, form) -> Product:
    for name in TEXT_FIELDS:
        if name in form:
            setattr(product, name, str(form.get(name, "") or "").strip())
    # 商品标签（固定标签）：发布页勾选的 chips 由前端拼进 hidden 的 tags 字段，
    # 这里统一归一成「一行一个」存库（兼容逗号 / 顿号分隔的老写法）。
    if "tags" in form:
        from .tag import join_tags

        raw = str(form.get("tags") or "").replace("，", ",").replace("、", ",")
        product.tags = join_tags([x for x in raw.replace("\n", ",").split(",")])
    for name in MONEY_FIELDS:
        if name in form:
            raw = (
                str(form.get(name, "") or "")
                .strip()
                .replace(",", "")
                .replace("¥", "")
                .replace("￥", "")
            )
            try:
                setattr(product, name, round(float(raw), 2))
            except ValueError:
                setattr(product, name, 0.0)
    for name in INT_FIELDS:
        if name in form:
            raw = str(form.get(name, "") or "").strip()
            try:
                setattr(product, name, int(float(raw)))
            except ValueError:
                setattr(product, name, 0)
    if "stock_enabled" in form:
        product.stock_enabled = str(form.get("stock_enabled")) in ("1", "on", "true", "yes")
    if "images" in form:
        images = [line.strip() for line in str(form.get("images", "") or "").splitlines() if line.strip()]
        product.images = json.dumps(images, ensure_ascii=False)
    if "spec_input" in form:
        product.spec_defs = dump_spec_defs(parse_spec_defs(str(form.get("spec_input", "") or "")))
    # 详情描述是多组文案：结构化存 detail_blocks，同时把各组按顺序拼回 detail_html
    # （闲鱼发布包与渔货栈同步读的都是 detail_html，老的单份数据也照样兼容）
    if "detail_blocks_json" in form:
        blocks = parse_detail_blocks(
            str(form.get("detail_blocks_json", "") or ""),
            fallback_html=str(form.get("detail_html", "") or ""),
        )
        # 详情图单独提交（页面上可以改 / 排序 / 删），拼回 detail_html 时排在文案后面
        images: list[str] = []
        for line in str(form.get("detail_images", "") or "").splitlines():
            url = line.strip()
            if url and url not in images:
                images.append(url)
        product.detail_blocks = json.dumps(blocks, ensure_ascii=False)
        product.detail_html = compose_detail_html(blocks, images)
    # 最近上架：留空表示不动（上架 / 下架时由系统自己记），填了就按填的来
    if "listed_at" in form:
        raw = str(form.get("listed_at", "") or "").strip()
        if raw:
            parsed = _parse_datetime(raw)
            if parsed is not None:
                product.listed_at = parsed
        elif raw == "" and "listed_at_clear" in form:
            product.listed_at = None
    # 商品属性：「供应链商品」必须挂到具体供应链，「自营商品」不挂供应链
    sync_supplier(session, product)
    # 价格口径兜底：成本价填了但渔货价 / 建议售价留空时按 1.15 / 1.35 派生
    derive_prices(product)
    # 上架状态变化时记时间：用于「最近上架」排序与核对
    if product.status == PRODUCT_STATUS_ON and product.listed_at is None:
        product.listed_at = now()
    product.updated_at = now()
    return product


def create_product(session: Session, form, created_by: str = "") -> Product:
    name = str(form.get("name", "") or "").strip()
    if not name:
        raise ValueError("商品名不能为空")
    if find_by_name(session, name) is not None:
        raise ValueError(f"商品「{name}」已存在")
    product = Product(name=name, code=str(form.get("code", "") or "").strip(), created_by=created_by)
    if not product.code:
        # 编码按「YHZ + 三级类目简拼 + 日期 + 流水号」生成，所以类目要在生成前拿到
        product.code = next_code(session, str(form.get("category", "") or ""))
    # 编码是详情页的对外流水号（URL 用它），必须唯一，否则链接会指到别的商品
    clash_code = find_by_code(session, product.code)
    if clash_code is not None:
        raise ValueError(
            f"商品编码「{product.code}」已被商品「{clash_code.name}」占用，请换一个"
        )
    _apply_form(session, product, form)
    # 售后政策没选（接口建档 / 表单没带这一项）就按默认口径：非质量问题不退不换
    if not (product.after_sale or "").strip():
        product.after_sale = DEFAULT_AFTER_SALE
    apply_shipping_defaults(session, product)
    if is_supply_chain(product) and not (product.supplier_name or "").strip():
        raise ValueError("供应链商品必须选择具体对应的供应链")
    if not product.status:
        product.status = PRODUCT_STATUS_DRAFT
    if not product.audit_status:
        product.audit_status = AUDIT_DRAFT
    # 新建表单里的「提交审核 / 立即上架」：直接把状态定下来
    action = str(form.get("submit_action", "") or "").strip()
    if action == "audit":
        product.audit_status = AUDIT_PENDING
    elif action == "publish":
        product.audit_status = AUDIT_APPROVED
        product.status = PRODUCT_STATUS_ON
        product.listed_at = now()
    session.add(product)
    session.commit()
    # 发布页提交了 SKU 矩阵就按矩阵建；没提交但填了规格定义，就按组合生成默认 SKU
    if not apply_skus_json(session, product, str(form.get("skus_json", "") or "")):
        if product.spec_def_list:
            generate_skus(session, product, product.spec_def_list)
    return product


def _sku_money(row: dict, key: str, fallback: float = 0.0) -> float:
    """SKU 行上的金额字段：留空就回落商品级的批量值。"""
    try:
        value = float(row.get(key) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value or float(fallback or 0)


def _sku_stock(row: dict) -> int:
    """SKU 行上的库存：留空记 -1（不单独管库存）。"""
    raw = row.get("stock")
    try:
        return int(float(raw)) if raw not in (None, "") else -1
    except (TypeError, ValueError):
        return -1


def _sku_values(row: dict, product: Product) -> dict:
    """SKU 行 → 档案字段：成本价 / 渔货价 / 建议售价 / 市场价 / 库存。

    渔货价与建议售价是必填口径（页面上会把批量设置的值套到每一行），
    这里再做一层兜底：行上没填就用商品级的批量值，批量值也没有就回落成本价 ——
    保证渔货栈 / 闲鱼拿到的价格不会恒为 0。
    """
    cost = _sku_money(row, "cost", product.cost_amount)
    # 行上没填 → 商品级批量值 → 按口径派生（成本 ×1.15 / 渔货价 ×1.35）
    fish = _sku_money(row, "fish", product.fish_price) or round(cost * FISH_RATE, 2)
    return {
        "cost_amount": cost,
        "fish_price": fish,
        "price": _sku_money(row, "price", product.price) or round(fish * SALE_RATE, 2),
        "market_price": _sku_money(row, "market", product.original_price),
        "stock": _sku_stock(row),
    }


def apply_skus_json(session: Session, product: Product, raw: str) -> int:
    """发布页的 SKU 矩阵（JSON：[{spec, cost, fish, price, market, stock}]）：逐行建 SKU。

    矩阵里的价格 / 库存是运营在发布页手填的，优先于商品上的批量默认值；
    同样的规格已存在就跳过，避免重复建。
    """
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return 0
    if not isinstance(rows, list):
        return 0
    existing = {sku.spec.strip() for sku in list_skus(session, product.id)}
    created = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        spec = str(row.get("spec") or "").strip()
        if not spec or spec in existing:
            continue
        session.add(
            ProductSku(
                product_id=product.id,
                spec=spec,
                sku_code="",  # 由 _finalize_skus 按商家编码派生
                **_sku_values(row, product),
            )
        )
        existing.add(spec)
        created += 1
    if created:
        _finalize_skus(session, product)
    return created


def sync_skus_json(session: Session, product: Product, raw: str) -> int:
    """编辑页的 SKU 矩阵：**全量覆盖** —— 矩阵里没有的规格删掉，有的更新，新出现的补上。

    与 ``apply_skus_json``（发布页，只加不删）的区别在于「删」：编辑页上看到的
    就是这款商品的全部规格，运营把某一行删掉，保存后库里也该真的没了。
    """
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return 0
    if not isinstance(rows, list):
        return 0
    keep: dict[str, dict] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        spec = str(item.get("spec") or "").strip()
        if spec:
            keep[spec] = item
    # 按规格分组：老数据里同一规格重复建过好几条，这里统一收敛成一条
    existing: dict[str, list[ProductSku]] = {}
    for sku in list_skus(session, product.id):
        existing.setdefault(sku.spec.strip(), []).append(sku)
    changed = 0
    for spec, group in existing.items():
        if spec not in keep:
            for sku in group:  # 页面上删掉的规格：库里一并删掉
                session.delete(sku)
                changed += 1
            continue
        for extra in group[1:]:  # 重复的历史记录只留第一条
            session.delete(extra)
            changed += 1
    for spec, item in keep.items():
        values = _sku_values(item, product)
        group = existing.get(spec) or []
        sku = group[0] if group else None
        if sku is None:
            session.add(
                ProductSku(
                    product_id=product.id,
                    spec=spec,
                    sku_code="",  # 由 _finalize_skus 按商家编码派生
                    **values,
                )
            )
            changed += 1
            continue
        # SKU 编码不比对：它由商家编码派生，收尾那一步统一对齐
        before = (
            sku.price,
            sku.cost_amount or 0.0,
            sku.fish_price or 0.0,
            sku.market_price or 0.0,
            sku.stock,
        )
        after = (
            values["price"],
            values["cost_amount"],
            values["fish_price"],
            values["market_price"],
            values["stock"],
        )
        if before != after:
            for name, value in values.items():
                setattr(sku, name, value)
            changed += 1
    # 无论有没有别的改动都收尾一次：SKU 编码按商家编码重排（规格删掉后序号不留空档）、
    # 总库存按各规格汇总
    _finalize_skus(session, product)
    return changed


def edit_state(product: Product, skus: list[ProductSku]) -> dict:
    """编辑页的前端状态：主图九宫格 / 详情图 / 多组详情文案 / 规格维度 / SKU 价目表。

    详情图与文案分开交回去（图片从 detail_html 里捞出来、并从文案里剥掉），
    这样图片能在「商品详情」板块里单独改 / 排序 / 删，保存时再拼回去，不会重复。
    """
    return {
        "mainImages": gallery_images(product),
        "detailImages": detail_images_of(product),
        # 多组详情文案（图片已剥掉；老商品只有一份 detail_html：兜成「文案一」）
        "detailBlocks": detail_blocks_without_images(product),
        "specDefs": [
            {"name": str(d.get("name") or ""), "values": [str(v) for v in (d.get("values") or [])]}
            for d in product.spec_def_list
            if isinstance(d, dict)
        ],
        "skus": [
            {
                "spec": sku.spec,
                # 价格跟着规格走：成本价 / 渔货价 / 建议售价 / 市场价，留空的交回空串
                "cost": "" if not sku.cost_amount else f"{float(sku.cost_amount):g}",
                "fish": "" if not sku.fish_price else f"{float(sku.fish_price):g}",
                "price": "" if not sku.price else f"{float(sku.price):g}",
                "market": "" if not sku.market_price else f"{float(sku.market_price):g}",
                "stock": "" if sku.stock in (None, -1) else str(sku.stock),
                # SKU 编码由商家编码派生（商家编码-01），页面上只读展示
                "code": sku.sku_code or "",
            }
            for sku in skus
        ],
    }


def edit_fields(product: Product) -> dict:
    """编辑页各输入框的初值（表单 name → 文本值，前端按 name 回填）。

    字段名与「发布商品」表单完全一致，所以同一套模板能同时干发布与编辑。
    """
    def money(value: float | None) -> str:
        return "" if not value else f"{float(value):g}"

    return {
        "name": product.name or "",
        "code": product.code or "",
        # 类目走三级下拉，这里给的是「选不出来时的原值」兜底（见编辑页的 ensureCatOption）
        "category": product.category or "",
        "brand": product.brand or "",
        "model": product.model or "",
        # 规格（2026-10-15）：渔集建档带过来的商品档案字段，详情页「商品信息」里看
        "spec": product.spec or "",
        "selling_points": product.selling_points or "",
        # 商品标签：前端按字典标签名回填勾选状态（勾选的拼进 hidden 的 tags 字段）
        "tags": product.tags or "",
        "detail_html": product.detail_html or "",
        "supplier_tip": product.supplier_tip or "",
        "delivery_days": product.delivery_days or "",
        "cost_amount": money(product.cost_amount),
        "fish_price": money(product.fish_price),
        "price": money(product.price),
        "original_price": money(product.original_price),
        # -1 是「不管理库存」的哨兵值，编辑页留空更直观（留空 = 自动汇总各 SKU）
        "stock": "" if (product.stock or 0) < 0 else str(product.stock),
        "stock_enabled": "1" if product.stock_enabled else "0",
        "freight_template": product.freight_template or "",
        "shipping_mode": product.shipping_mode or "",
        "shipping_fee": money(product.shipping_fee),
        "after_sale": product.after_sale or "",
        "source_type": product.source_type or "self_operated",
        "supplier_id": str(product.supplier_id or ""),
        "status": product.status or "",
    }


def update_product(session: Session, product: Product, form) -> Product:
    new_name = str(form.get("name", product.name) or "").strip()
    if new_name and new_name != product.name:
        clash = find_by_name(session, new_name, exclude_id=product.id)
        if clash is not None:
            raise ValueError(f"商品「{new_name}」已存在")
        product.name = new_name
    old_code = (product.code or "").strip()
    _apply_form(session, product, form)
    apply_shipping_defaults(session, product)
    if is_supply_chain(product) and not (product.supplier_name or "").strip():
        raise ValueError("供应链商品必须选择具体对应的供应链")
    # 编码是详情页 URL 的流水号：不能和别的商品撞
    if (product.code or "").strip():
        clash_code = find_by_code(session, product.code, exclude_id=product.id)
        if clash_code is not None:
            raise ValueError(
                f"商品编码「{product.code}」已被商品「{clash_code.name}」占用，请换一个"
            )
    # 商家编码被改了：SKU 编码是「商家编码-01 / -02」派生的，跟着重排一遍
    # （编辑页没带 SKU 矩阵时也要排，不然 SKU 编码会挂着旧前缀）
    if (product.code or "").strip() != old_code:
        renumber_sku_codes(session, product)
    session.commit()
    return product


def delete_product(session: Session, product: Product) -> None:
    """删除商品：把它挂着的订单解绑（订单本身不动），规格一并删掉。

    2026-10-13 补：**上架到渔货栈的货源一并撤掉** —— 以前渔枢这边删了商品，
    渔货栈（supplier_items）那条 hub-prd-* 还挂着，货源广场照样能看见
    （现场就出现过「渔枢后台已经删了、渔货栈还在卖」）。
    """
    session.query(Order).filter(Order.product_id == product.id).update({Order.product_id: None})
    session.query(ProductSku).filter(ProductSku.product_id == product.id).delete()
    # 渔货栈侧的货源与铺货痕迹一起清（同一份库；分端部署读不到渔货栈模型就跳过）
    try:
        from src.models import ChannelListing, SupplierItem  # noqa: PLC0415
        item_id = f"hub-prd-{product.id}"
        session.query(ChannelListing).filter(
            ChannelListing.item_id == item_id).delete(synchronize_session=False)
        session.query(SupplierItem).filter(
            SupplierItem.item_id == item_id).delete(synchronize_session=False)
    except Exception:  # pragma: no cover - 渔货栈不可用时只删渔枢自己的
        logging.getLogger(__name__).warning(
            "delete_product: supplier_items 联动清理跳过（渔货栈不可用）")
    session.delete(product)
    session.commit()


def set_listing(session: Session, product: Product, listed: bool) -> Product:
    """上架 / 下架（手工标记，闲鱼侧没有开放接口）。"""
    if listed:
        product.status = PRODUCT_STATUS_ON
        product.listed_at = now()
        if product.audit_status in ("", AUDIT_DRAFT):
            product.audit_status = AUDIT_APPROVED
    else:
        product.status = PRODUCT_STATUS_OFF
    product.updated_at = now()
    session.commit()
    return product


def set_audit(session: Session, product: Product, status: str, note: str = "") -> Product:
    """审核：待审核 → 通过 / 驳回。驳回时把理由记下来。"""
    product.audit_status = status
    product.audit_note = (note or "")[:255]
    product.updated_at = now()
    session.commit()
    return product


# 匹配类目时不认的兜底名字（挂上去等于没分类）
_SKIP_CATEGORY_NAMES = {"其他", "其它"}
_MATCH_NOISE_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def _match_text(product: Product) -> str:
    """参与类目匹配的文本：商品名 + 品牌（现有类目不参与，免得原地打转）。"""
    parts = [product.name or "", product.brand or ""]
    return _MATCH_NOISE_RE.sub("", " ".join(parts)).lower()


def suggest_category(
    session: Session, product: Product, rows: list | None = None
) -> tuple[str, str]:
    """按商品名 / 品牌 / 别名在类目树里挑一个最合适的类目。

    返回 `(类目文本, 说明)`；认不出来返回空串 —— 类目错了比空着更麻烦，不硬塞「其他」。
    命中口径：类目名（三 / 二 / 一级）出现在商品文本里，层级越深权重越高、名字越长越可信。
    """
    if rows is None:
        from ..models import ProductCategory

        rows = list(
            session.scalars(
                select(ProductCategory).where(ProductCategory.enabled == True)  # noqa: E712
            ).all()
        )
    if not rows:
        return "", "类目树是空的：请先到「货源 · 类目管理」维护类目"
    by_id = {row.id: row for row in rows}
    text = _match_text(product)
    if not text:
        return "", "商品名是空的，没法匹配类目"

    best: tuple[int, str, str] | None = None  # (分数, 类目文本, 命中的类目名)
    for row in rows:
        name = (row.name or "").strip()
        if not name or name in _SKIP_CATEGORY_NAMES:
            continue
        if name.lower() not in text:  # 只在商品文本里出现过才认
            continue
        weight = {3: 10, 2: 5, 1: 2}.get(int(row.level or 1), 1)
        score = len(name) * weight
        if best is not None and score <= best[0]:
            continue
        path: list[str] = []
        cursor: object | None = row
        while cursor is not None:
            path.append(getattr(cursor, "name", "") or "")
            parent_id = getattr(cursor, "parent_id", None)
            cursor = by_id.get(parent_id) if parent_id else None
        best = (score, " / ".join([p for p in reversed(path) if p]), name)
    if best is None:
        return "", f"「{product.name}」在类目树里没认出合适的类目"
    return best[1], f"按「{best[2]}」匹配到 {best[1]}"


def apply_suggested_category(
    session: Session, product: Product, rows: list | None = None
) -> tuple[bool, str]:
    """给单个商品补类目：已经有类目的不动（手填的优先）。返回 (是否改了, 说明)。"""
    if (product.category or "").strip():
        return False, "已有类目，未改动"
    text, note = suggest_category(session, product, rows)
    if not text:
        return False, note
    product.category = text
    product.updated_at = now()
    return True, note


def bulk_match_category(
    session: Session, product_ids: list[int], rows: list | None = None
) -> dict:
    """批量匹配类目：只补空缺，已填类目的一律不动。

    返回 `{done, skipped, failed, samples}` —— samples 给页面提示用（最多 5 条）。
    """
    if rows is None:
        from ..models import ProductCategory

        rows = list(
            session.scalars(
                select(ProductCategory).where(ProductCategory.enabled == True)  # noqa: E712
            ).all()
        )
    done = skipped = failed = 0
    samples: list[str] = []
    for pid in product_ids:
        product = session.get(Product, pid)
        if product is None:
            continue
        changed, note = apply_suggested_category(session, product, rows)
        if changed:
            done += 1
            if len(samples) < 5:
                samples.append(f"{product.name} → {product.category}")
        elif note == "已有类目，未改动":
            skipped += 1
        else:
            failed += 1
            if len(samples) < 5:
                samples.append(f"{product.name}：{note}")
    if done:
        session.commit()
    return {"done": done, "skipped": skipped, "failed": failed, "samples": samples}


def bulk_action(session: Session, product_ids: list[int], action: str, note: str = "") -> int:
    """批量操作：上架 / 下架 / 审核通过 / 驳回 / 删除。返回实际处理条数。"""
    done = 0
    for pid in product_ids:
        product = session.get(Product, pid)
        if product is None:
            continue
        if action == "list":
            set_listing(session, product, True)
        elif action == "unlist":
            set_listing(session, product, False)
        elif action == "approve":
            set_audit(session, product, AUDIT_APPROVED)
        elif action == "reject":
            set_audit(session, product, AUDIT_REJECTED, note)
        elif action == "delete":
            delete_product(session, product)
        else:
            continue
        done += 1
    return done


# ---------------------------------------------------------------- 与订单联动
def match_product(session: Session, name: str | None, spec: str = "") -> Product | None:
    """按商品名认商品：规范名 → 互相包含 → 词干兜底，逐级放宽、宁缺勿错。"""
    key = normalize_name(name)
    if not key:
        return None
    rows = list(session.scalars(select(Product)))
    if not rows:
        return None
    for row in rows:  # 1) 规范名一致
        if normalize_name(row.name) == key:
            return row
    if len(key) >= 4:  # 2) 互相包含（够长才认，短名容易乱匹配）
        for row in rows:
            other = normalize_name(row.name)
            if len(other) >= 4 and (other in key or key in other):
                return row
    stem = name_stem(name)  # 3) 词干兜底
    if len(stem) >= 5:
        for row in rows:
            if name_stem(row.name) == stem:
                return row
    return None


def attach_order(session: Session, order: Order, commit: bool = False) -> Product | None:
    """把订单挂到商品上（认不出来就不挂，保持 product_id 为空）。"""
    if order is None or not (order.product_name or "").strip():
        return None
    product = match_product(session, order.product_name, order.spec or "")
    if product is not None and order.product_id != product.id:
        order.product_id = product.id
        order.updated_at = now()
        if commit:
            session.commit()
    return product


def set_order_product(session: Session, order: Order, product_id: int | None) -> Product | None:
    """手工把订单挂到某款商品（或清空关联）：订单详情页的下拉用。

    自动匹配认错（或认不出）时，运营一句话就能改过来 —— 这也是一种纠错，
    比在商品页里改别名更快。
    """
    product: Product | None = None
    if product_id:
        product = session.get(Product, product_id)
        if product is None:
            raise ValueError("商品不存在")
    order.product_id = product.id if product else None
    order.updated_at = now()
    session.commit()
    return product


def ensure_product(
    session: Session,
    name: str,
    supplier_name: str = "",
    shop_name: str = "",
    price: float = 0.0,
    cost_amount: float = 0.0,
    created_by: str = "",
    commit: bool = False,
) -> Product:
    """订单里出现过、商品库还没有的名字：建成草稿商品，资料回头在商品页补。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("商品名不能为空")
    found = find_by_name(session, name)
    if found is not None:
        return found
    product = Product(
        name=name,
        code=next_code(session),
        status=PRODUCT_STATUS_DRAFT,
        audit_status=AUDIT_DRAFT,
        supplier_name=supplier_name or "",
        shop_name=shop_name or "",
        price=price or 0.0,
        cost_amount=cost_amount or 0.0,
        created_by=created_by or "",
    )
    session.add(product)
    if commit:
        session.commit()
    return product


def link_orders(session: Session, product: Product, limit: int = 5000) -> int:
    """把商品名能对上、但还没挂到该商品上的订单补挂上（商品详情页手动触发）。"""
    key = normalize_name(product.name)
    linked = 0
    rows = session.scalars(
        select(Order).where(not_recalled(), Order.product_id.is_(None)).limit(limit)
    ).all()
    for order in rows:
        if attach_order(session, order) is product:
            linked += 1
        else:
            name = normalize_name(order.product_name)
            if name and name == key:
                order.product_id = product.id
                linked += 1
    if linked:
        session.commit()
    return linked


def product_orders(session: Session, product: Product, limit: int = 20) -> list[Order]:
    return list(
        session.scalars(
            select(Order)
            .where(Order.product_id == product.id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
    )


def reserved_quantity(session: Session, product: Product) -> int:
    """在途占用：挂在这款商品上、还没发货的订单件数（可售 = 库存 - 占用）。"""
    value = session.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0)).where(
            Order.product_id == product.id,
            not_recalled(),
            Order.order_state.in_(("new", "pushed")),
        )
    )
    return int(value or 0)


def live_shop_stat(session: Session, product: Product, days: int = 90) -> dict:
    """在售闲鱼号：**统计出来的数量**，不是手填字段。

    口径：这款商品在售时，数近 N 天出过它订单（不含已撤回）的不同闲鱼号（xianyu_account）。
    没上架 / 草稿记 0 —— 还没挂单的商品，说不清它铺在哪个号上。
    """
    if product.status != PRODUCT_STATUS_ON:
        return {"count": 0, "names": [], "days": days, "on_sale": False}
    since = now() - timedelta(days=days)
    rows = session.execute(
        select(Order.xianyu_account, func.count())
        .where(
            Order.product_id == product.id,
            not_recalled(),
            Order.created_at >= since,
        )
        .group_by(Order.xianyu_account)
    ).all()
    names = [str(r[0] or "").strip() for r in rows if str(r[0] or "").strip()]
    return {"count": len(names), "names": names[:8], "days": days, "on_sale": True}


def product_stat(session: Session, product: Product) -> dict:
    """这款商品的经营数字：订单数 / 件数 / 销售额 / 成本 / 毛利（不含已撤回的订单）。"""
    orders = list(session.scalars(select(Order).where(Order.product_id == product.id, not_recalled())))
    sale = sum(float(o.sale_amount or 0) for o in orders)
    cost = sum(float(o.cost_amount or 0) for o in orders)
    ship = sum(float(o.shipping_fee or 0) for o in orders)
    reserved = reserved_quantity(session, product)
    stock = int(product.stock or 0)
    profit = round(sale - cost - ship, 2)
    # 单件毛利与利润率：没有订单时按商品的售价与成本估一个（详情页的「价格与利润」要看）
    # 成本口径（2026-09-29 起全站统一）：渔货价 = 对外的成本价，没填渔货价才回落采购价
    unit_cost = float(
        product.fish_price if product.fish_price is not None else (product.cost_amount or 0)
    )
    unit_profit = round(float(product.price or 0) - unit_cost, 2)
    unit_rate = round(unit_profit / float(product.price), 4) if float(product.price or 0) else 0.0
    return {
        "orders": len(orders),
        "quantity": sum(int(o.quantity or 0) for o in orders),
        "sale": round(sale, 2),
        "cost": round(cost, 2),
        "shipping": round(ship, 2),
        "profit": profit,
        "profit_rate": round(profit / sale, 4) if sale else unit_rate,
        "unit_profit": unit_profit,
        "unit_rate": unit_rate,
        "last_at": max((o.created_at for o in orders), default=None),
        "reserved": reserved,
        "available": (stock - reserved) if product.stock_enabled else None,
        # 在售闲鱼号：统计值（近 90 天有出单的闲鱼号数），不是手填字段
        "live_shops": live_shop_stat(session, product),
    }


def backfill_from_orders(session: Session, limit: int = 2000) -> dict:
    """按历史订单的商品名批量建档 + 挂单（只建档与关联，不改订单里的商品名与金额）。"""
    names = session.scalars(
        select(Order.product_name)
        .where(not_recalled(), Order.product_name != "")
        .group_by(Order.product_name)
        .limit(limit)
    ).all()
    created = 0
    for name in names:
        if match_product(session, name) is not None:
            continue
        sample = session.scalars(
            select(Order).where(Order.product_name == name).order_by(Order.created_at.desc())
        ).first()
        ensure_product(
            session,
            name,
            supplier_name=(sample.supplier_name or "") if sample else "",
            shop_name=(sample.xianyu_account or "") if sample else "",
            price=float(sample.sale_amount or 0) if sample else 0.0,
            cost_amount=float(sample.cost_amount or 0) if sample else 0.0,
            created_by="按历史订单补齐",
        )
        created += 1
    session.commit()
    linked = 0
    rows = session.scalars(select(Order).where(not_recalled(), Order.product_id.is_(None))).all()
    for order in rows:
        if attach_order(session, order) is not None:
            linked += 1
    if linked:
        session.commit()
    total = session.scalar(select(func.count()).select_from(Product)) or 0
    return {"created": created, "linked": linked, "total": int(total)}


# ---------------------------------------------------------------- 规格 / SKU
def sku_counts(session: Session, product_ids: list[int]) -> dict[int, int]:
    """一次查出多个商品的 SKU 条数（列表页展示用，避免逐个商品查一遍）。"""
    if not product_ids:
        return {}
    rows = session.execute(
        select(ProductSku.product_id, func.count())
        .where(ProductSku.product_id.in_(product_ids))
        .group_by(ProductSku.product_id)
    ).all()
    return {int(pid): int(count) for pid, count in rows}


def list_skus(session: Session, product_id: int) -> list[ProductSku]:
    return list(
        session.scalars(
            select(ProductSku).where(ProductSku.product_id == product_id).order_by(ProductSku.id)
        )
    )


def add_sku(session: Session, product_id: int, form) -> ProductSku:
    def _money(key: str) -> float:
        raw = str(form.get(key, "") or "").strip().replace(",", "").replace("¥", "")
        try:
            return round(float(raw), 2)
        except ValueError:
            return 0.0

    spec = str(form.get("spec", "") or "").strip()
    if not spec:
        raise ValueError("规格名不能为空")
    sku = ProductSku(
        product_id=product_id,
        spec=spec,
        sku_code="",  # 由 _finalize_skus 按商家编码派生
        price=_money("price"),
        cost_amount=_money("cost_amount"),
        fish_price=_money("fish_price"),
        market_price=_money("market_price"),
        stock=int(_money("stock") or -1),
    )
    session.add(sku)
    session.commit()
    owner = session.get(Product, product_id)
    if owner is not None:
        _finalize_skus(session, owner)
    return sku


def delete_sku(session: Session, sku: ProductSku) -> None:
    product_id = sku.product_id
    session.delete(sku)
    session.commit()
    owner = session.get(Product, product_id)
    if owner is not None:
        _finalize_skus(session, owner)  # 删掉一条后序号不留空档、总库存重算


# ---------------------------------------------------------------- 上架预填与统计
def gallery_images(product: Product) -> list[str]:
    """商品素材图：主图排最前，后面跟图集（同一张只留一次）。

    详情页的素材区、闲鱼发布预填包、复制出去的文本都用这一份，免得主图/图集两处各算各的。
    """
    urls: list[str] = [product.image_path] if product.image_path else []
    for url in product.image_list:
        url = (url or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def listing_package(product: Product, skus: list[ProductSku] | None = None) -> dict:
    """闲鱼发布预填包：发布页要填的字段一次带齐。

    闲鱼没有开放「卖家自动发布」接口，发布动作由人在闲鱼发布页完成 ——
    所以这里把标题 / 描述 / 价格 / 库存 / 图片 / 规格 SKU 打包好，配套两个动作：
      · 带入闲鱼发布页：打开闲鱼发布页 + 把预填文本复制到剪贴板，粘贴即成；
      · 下载 JSON：想脚本化处理时用。
    """
    skus = skus if skus is not None else []
    stock = int(product.stock or 0) if product.stock_enabled else 0
    # 详情的「几组文案 / 多少张详情图」也带进预填包：上架前一眼看出资料全不全
    detail_groups = detail_groups_of(product)
    detail_images = detail_images_of(product)
    return {
        "title": product.name or "",
        "title_length": len(product.name or ""),
        # 闲鱼标题上限 30 字，超了在这里就要提醒
        "title_limit": 30,
        "price": round(float(product.price or 0), 2),
        "original_price": round(float(product.original_price or 0), 2),
        "cost": round(float(product.cost_amount or 0), 2),
        # 渔货价：渔货栈对外的成本价 / 进货价，带入闲鱼发布页时供参考
        "fish_price": round(float(product.fish_price or 0), 2),
        "stock": stock,
        "stock_text": f"{stock} 件" if product.stock_enabled else "按单采购",
        "images": gallery_images(product),
        "category_text": product.category or "",
        "condition": condition_label(product),
        "delivery": product.delivery_days or "",
        "after_sale": product.after_sale or "",
        "freight": product.freight_template or shipping_label(product),
        "shipping_mode": shipping_label(product),
        "selling_points": product.selling_point_list,
        "description": (product.detail_html or product.note or "").strip(),
        # 详情拆开看：文案 N 组 + 详情图 M 张（每 9 张一组 → G 组）
        "detail_block_count": len(detail_groups),
        "detail_image_count": len(detail_images),
        "detail_image_groups": len(detail_image_groups(detail_images)),
        "detail_image_group_size": DETAIL_IMAGE_GROUP_SIZE,
        # 供应商友情提示：渔货栈特色信息，给自营团队选品 / 经营做参考（不改主描述，单独一段）
        "supplier_tip": (product.supplier_tip or "").strip(),
        # 品牌带进发布包（可留空）；型号 / 成色 / 重量属于**规格**，在 spec_defs / skus 里带，
        # 不再作为商品固定字段；闲鱼商品 ID 与链接已下线（不维护、不进发布包）
        "brand": product.brand or "",
        # 规格定义（维度）与 SKU 矩阵，闲鱼发布页的多规格填写用得上
        "spec_defs": product.spec_def_list,
        "skus": [
            {
                "spec_name": sku.spec,
                # 建议售价（闲鱼发布页填的价）
                "price": round(float(sku.price or 0), 2),
                # 渔货价（渔货栈进货价）/ 成本价 / 市场价：留空的按 0 带过去，页面不显示
                "fish_price": round(float(sku.fish_price or 0), 2),
                "cost": round(float(sku.cost_amount or 0), 2),
                "market_price": round(float(sku.market_price or 0), 2),
                "stock": int(sku.stock if sku.stock is not None else -1),
                "sku_code": sku.sku_code or "",
            }
            for sku in skus
        ],
    }


def listing_text(package: dict) -> str:
    """把预填包摊成「按闲鱼发布页顺序」的纯文本，复制后可直接照着填。"""
    lines = [
        f"标题：{package['title']}",
        f"价格：¥{package['price']:.2f}" + (f"（划线价 ¥{package['original_price']:.2f}）" if package["original_price"] else ""),
        f"库存：{package['stock_text']}",
        f"类目：{package['category_text'] or '—'}",
        f"成色：{package['condition'] or '—'}",
    ]
    if package["selling_points"]:
        lines.append("卖点：" + "；".join(package["selling_points"]))
    if package["images"]:
        lines.append("图片：" + f"{len(package['images'])} 张")
        lines.extend(f"  {url}" for url in package["images"])
    if package["skus"]:
        lines.append("规格：")
        for sku in package["skus"]:
            stock = "按单采购" if sku["stock"] < 0 else f"{sku['stock']} 件"
            lines.append(f"  {sku['spec_name']}｜¥{sku['price']:.2f}｜{stock}")
    if package["delivery"] or package["after_sale"] or package["freight"]:
        lines.append(
            "发货与售后："
            + " · ".join(x for x in (package["delivery"], package["freight"], package["after_sale"]) if x)
        )
    if package["description"]:
        lines.append("描述：")
        lines.append(package["description"])
    return "\n".join(lines)


def overview(session: Session) -> dict:
    """商品统计：条数分布、库存与成本合计、类目分布、卖得最好的几款。"""
    rows = list(session.scalars(select(Product)))

    def _rows(values: dict[str, int], labels: dict[str, str], url_of) -> list[dict]:
        """分布行：标签 + 条数 + 占比 + 点进去的链接（页面按占比画条形图）。"""
        total = sum(values.values()) or 1
        out = []
        for key, count in sorted(values.items(), key=lambda kv: kv[1], reverse=True):
            out.append(
                {
                    "label": labels.get(key, key),
                    "count": count,
                    "pct": round(count * 100.0 / total, 1),
                    "url": url_of(key),
                }
            )
        return out

    def _q(url: str, name: str) -> str:
        return f"{url}{quote(str(name))}"

    source_values: dict[str, int] = {}
    audit_values: dict[str, int] = {}
    status_values: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_supplier: dict[str, int] = {}
    stock_total = 0
    cost_total = 0.0
    price_total = 0.0
    for p in rows:
        source_values[p.source_type or ""] = source_values.get(p.source_type or "", 0) + 1
        audit_values[p.audit_status or ""] = audit_values.get(p.audit_status or "", 0) + 1
        status_values[p.status or ""] = status_values.get(p.status or "", 0) + 1
        key = (p.category or "未分类").strip() or "未分类"
        by_category[key] = by_category.get(key, 0) + 1
        if p.supplier_name:
            by_supplier[p.supplier_name] = by_supplier.get(p.supplier_name, 0) + 1
        if p.stock_enabled:
            stock_total += int(p.stock or 0)
        cost_total += float(p.cost_amount or 0)
        price_total += float(p.price or 0)

    best = []
    sale_total = 0.0
    profit_total = 0.0
    order_count = 0
    quantity_total = 0
    for p in rows:
        stat = product_stat(session, p)
        order_count += stat["orders"]
        quantity_total += stat["quantity"]
        sale_total += stat["sale"]
        profit_total += stat["profit"]
        if stat["orders"]:
            best.append({"product": p, "stat": stat})
    best.sort(key=lambda item: item["stat"]["sale"], reverse=True)
    top_sale = best[0]["stat"]["sale"] if best else 0.0
    for row in best:  # 条形图用的相对宽度：以卖得最好的那款为满格
        row["width"] = round(row["stat"]["sale"] * 100.0 / top_sale, 1) if top_sale else 0.0

    return {
        "total": len(rows),
        # 分布（页面直接按 pct 画条形图，点一行就跳到筛好的列表）
        "source_rows": _rows(
            source_values, SOURCE_LABELS, lambda k: f"/products?source={k}" if k else ""
        ),
        "audit_rows": _rows(
            audit_values, AUDIT_LABELS, lambda k: f"/products?audit={k}" if k else ""
        ),
        "status_rows": _rows(
            status_values, PRODUCT_STATUS_LABELS, lambda k: f"/products?status={k}" if k else ""
        ),
        "category_rows": _rows(
            dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:10]),
            {},
            lambda k: _q("/products?category=", k),
        ),
        "supplier_rows": _rows(
            dict(sorted(by_supplier.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            {},
            lambda k: _q("/products?supplier=", k),
        ),
        "stock_total": stock_total,
        "cost_total": round(cost_total, 2),
        "price_total": round(price_total, 2),
        "best": best[:10],
        "pending_audit": audit_values.get(AUDIT_PENDING, 0),
        "on_sale": status_values.get(PRODUCT_STATUS_ON, 0),
        "sale_total": round(sale_total, 2),
        "profit_total": round(profit_total, 2),
        "order_count": order_count,
        "quantity_total": quantity_total,
        "profit_rate": round(profit_total / sale_total, 4) if sale_total else 0.0,
        "moving": len(best),
    }


# ---------------------------------------------------------------- 渔货栈图包导入
# 2026-10-15 数据需求：本地按「渔货号/一组图/二三四组详情图」整理好的商品图包
# 导入渔枢自营商品 —— 渔货号即商家编码（code），资料 / 价格 / 库存按渔货号从
# 渔集（wms）带过来，一组图作商品图片，二三四组作图文详情图片。

# 自营供应链的供应商编码 / 名称（与 push.is_self_supply_chain 同一口径）
YUSHANGZAI_SUPPLIER_CODE = "YS001"
YUSHANGZAI_SUPPLIER_NAME = "渔货栈YS001"
# 一组 = 商品图片，二三四组 = 图文详情图片
IMPORT_IMAGE_MAX_BYTES = 15 * 1024 * 1024


def _save_import_image(fish_code: str, group: str, idx: int,
                       filename: str, data: bytes) -> str:
    """导入图片落盘：文件名带渔货号与组号（重导同款直接覆盖，不攒垃圾）。"""
    from pathlib import Path

    from ..config import settings

    suffix = str(Path(filename or "").suffix or ".jpg").lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        suffix = ".jpg"
    folder = Path(settings.media_path) / "products"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{fish_code.lower()}-g{group}-{idx:02d}{suffix}"
    (folder / name).write_bytes(data)
    return f"/media/products/{name}"


def _yushangzai_supplier(session: Session) -> tuple[str, int]:
    """自营供应链（渔货栈YS001）档案：有档案带 id，没有就只记名字。"""
    from sqlalchemy import select

    row = session.scalar(
        select(Supplier).where(Supplier.code == YUSHANGZAI_SUPPLIER_CODE)
    )
    if row:
        return str(row.name or YUSHANGZAI_SUPPLIER_NAME), int(row.id or 0)
    return YUSHANGZAI_SUPPLIER_NAME, 0


def import_from_yushangzai(
    session: Session,
    fish_code: str,
    groups: dict[str, list[tuple[str, bytes]]],
    operator: str = "",
) -> dict:
    """按渔货号导入一款渔货栈自营商品（2026-10-15 数据需求）。

    导入规则（用户拍板）：
    · 目录名 / fish_code = 渔货号 = 商家编码（Product.code，也即新增页的条码）；
    · 商品标题 / 品牌 / 型号 / 规格 / 三级类目按渔货号从渔集带（存在才带）；
    · 渔货价 = 渔集渔货价，建议售价 = 渔集建议售价，市场价 = 渔集基准价，
      库存 = 该渔货号名下台数合计；渔货价同时是自营成本（成本口径一致）；
    · 一组图 → 商品图片（第一张为主图），二三四组 → 图文详情图片（按组顺序）；
    · 默认「48 小时内发货」，自营商品直接上架（on_sale / approved）；
    · 幂等：同渔货号重导 = 覆盖更新（图片按文件名覆盖，不攒垃圾）。
    """
    import re as _re

    from sqlalchemy import select

    from wms import intake as wms_intake
    from wms import models as wms_models

    fish_code = str(fish_code or "").strip().upper()
    if not _re.fullmatch(r"YS\d{8,12}", fish_code):
        raise ValueError(f"渔货号格式不对：{fish_code}（应为 YS + 日期 + 流水）")
    items = wms_intake.find_items_by_fish(session, fish_code)
    if not items:
        raise ValueError(f"渔货号 {fish_code} 在渔集货品台账里查不到，先确认再导入")

    src = items[0]
    total_qty = sum(int(it.qty or 1) for it in items)
    # 三级类目：优先 1688 对标路径（与渔枢类目管理同名），回落渔集自己的三级路径
    cat = wms_models.category_1688(str(src.category or "")) or wms_models.category_path(
        str(src.category or "")
    )
    category = cat.replace(" › ", " / ") if cat and cat != "—" else ""

    name = str(src.title or "").strip() or fish_code
    clash = session.scalar(select(Product).where(Product.name == name, Product.code != fish_code))
    if clash:
        name = f"{name}（{fish_code}）"

    selling = float(src.selling_price or 0.0)
    suggest = float(src.suggest_price or 0.0)
    base_price = float(src.base_price or 0.0)
    sup_name, sup_id = _yushangzai_supplier(session)

    product = session.scalar(select(Product).where(Product.code == fish_code))
    created = product is None
    if product is None:
        product = Product(code=fish_code, created_by=operator or "渔货栈导入")
        session.add(product)
    product.name = name
    product.brand = str(src.brand or "")
    product.model = str(src.model or "")
    product.spec = str(src.spec or "")
    if category:
        product.category = category
    # 价格口径（2026-10-15 用户拍板方案 B）：渔货价 = 渔集渔货价；
    # 销售价（建议售价）= 渔集建议售价，没填（渔集 864/866 都没填）→
    # **市场价 × 0.5 兜底**（与存量老商品「销售价 = 市场价一半」口径一致），
    # 市场价也没有才回落渔货价 —— 保证导入出来的商品有毛利空间，不再销售价=渔货价
    # 向下取整到元（2026-10-16 用户拍板）：兜底价也算「系统给出的价」，一视同仁
    fallback_price = math.floor(round(base_price * SUGGEST_FALLBACK_RATIO, 6))
    product.fish_price = selling
    product.price = suggest or fallback_price or selling
    product.original_price = base_price
    product.cost_amount = selling
    product.stock = total_qty
    product.stock_enabled = True
    product.delivery_days = "48 小时内发货"
    product.source_type = "self_operated"
    product.supplier_name = sup_name
    if sup_id:
        product.supplier_id = sup_id
    product.status = "on_sale"
    product.audit_status = "approved"
    product.updated_at = now()

    # 图片：一组 = 商品图片（第一张主图）；二三四组 = 详情图按组顺序
    main_groups = sorted(groups.get("1") or [], key=lambda x: x[0])
    detail_groups = [sorted(groups.get(k) or [], key=lambda x: x[0]) for k in ("2", "3", "4")]
    if main_groups:
        urls = [
            _save_import_image(fish_code, "1", i, fn, data)
            for i, (fn, data) in enumerate(main_groups, start=1)
        ]
        product.image_path = urls[0]
        product.images = json.dumps(urls[1:], ensure_ascii=False)
    detail_urls: list[str] = []
    for gi, files in enumerate(detail_groups, start=2):
        detail_urls.extend(
            _save_import_image(fish_code, str(gi), i, fn, data)
            for i, (fn, data) in enumerate(files, start=1)
        )
    if detail_urls:
        product.detail_html = "".join(
            f'<p><img src="{u}" /></p>' for u in detail_urls
        )
    # 同渔货号下多 SKU 默认分拆（2026-10-15 用户拍板）：渔集那边一款多台各一个
    # 进货货号，渔枢这边就按台分出规格行，价格 / 库存各算各的
    sku_count = split_product_skus(session, product, items=items)
    session.commit()

    return {
        "ok": True,
        "created": created,
        "product_id": product.id,
        "code": product.code,
        "name": product.name,
        "category": product.category,
        "brand": product.brand,
        "model": product.model,
        "spec": product.spec,
        "fish_price": product.fish_price,
        "price": product.price,
        "original_price": product.original_price,
        "stock": product.stock,
        "skus": sku_count,
        "main_images": len(main_groups),
        "detail_images": len(detail_urls),
        "url": f"/products/{product.code}",
    }


# 同渔货号下多 SKU 分拆（2026-10-15 用户拍板）：一个渔货号在渔集名下有多个条码商品
# （多个进货货号）时，渔枢商品默认分拆出对应的规格行；一款只一条记录的（哪怕一台
# 带 20 台）不拆。这一款用户点名不拆：一条记录 20 台共用一个条码。
SKU_SPLIT_EXCLUDED = {"YS202610120001"}

# 销售价兜底系数（2026-10-15 用户拍板方案 B）：渔集「建议售价」没填时，
# 销售价 = 市场价（基准价）× 0.5 —— 与存量老商品口径一致，保证有毛利空间
SUGGEST_FALLBACK_RATIO = 0.5


def split_product_skus(session: Session, product: Product,
                       items: list | None = None) -> int:
    """把渔货号名下的多条码商品分拆成渔枢商品的规格行，返回规格数（0 = 没拆）。

    · 渔集里 1 个条码商品 = 1 条规格：规格名取 规格 → 型号 → 显示编码 → 条码；
    · 价格各带各的（渔货价 = 渔集渔货价、建议售价、市场价 = 基准价），库存 = 台数；
    · 一台带 N 台（qty>1）**不拆台**，还是一条规格、库存 N；
    · 幂等：先清掉该商品现有规格再重建，SKU 编码由 ``renumber_sku_codes``
      按「商家编码-01 / -02」重排，总库存回算成各规格之和。
    """
    fish_code = str(product.code or "").strip().upper()
    if not fish_code:
        return 0
    if fish_code in SKU_SPLIT_EXCLUDED:
        return 0
    if items is None:
        from wms import intake as wms_intake

        items = wms_intake.find_items_by_fish(session, fish_code)
    if len(items or []) <= 1:
        return 0
    for sku in list_skus(session, int(product.id or 0)):
        session.delete(sku)
    session.flush()
    for it in items:
        selling = float(it.selling_price or 0.0)
        spec = (str(it.spec or "").strip() or str(it.model or "").strip()
                or str(it.display_code or "").strip() or str(it.barcode or ""))
        session.add(ProductSku(
            product_id=int(product.id or 0),
            spec=spec[:255],
            price=float(it.suggest_price or 0.0)
            or math.floor(round(float(it.base_price or 0.0) * SUGGEST_FALLBACK_RATIO, 6))
            or selling,
            cost_amount=selling,
            fish_price=selling,
            market_price=float(it.base_price or 0.0),
            stock=max(0, int(it.qty or 1)),
        ))
    session.flush()
    renumber_sku_codes(session, product)
    sync_stock_from_skus(session, product)
    return len(items)
