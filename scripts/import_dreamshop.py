"""合肥梦想电商「价格表 + 素材」→ 渔枢系统商品档案（products 表）。

用法（默认只生成整理表，不动库）：
    .venv/bin/python scripts/import_dreamshop.py                 # 预览：输出 CSV 整理表
    .venv/bin/python scripts/import_dreamshop.py --apply         # 真的写库（幂等：同名商品跳过）
    .venv/bin/python scripts/import_dreamshop.py --limit 10      # 只处理前 10 个（调试）
    .venv/bin/python scripts/import_dreamshop.py --max-images 12 # 每个商品最多带多少张图

两条素材来源：
  1 价格表（「10.1（所有品类） 价格表 （总表）.xls」）—— 商品名 / 拿货价 / 建议售价 / 发货地 / 产品备注；
  2 素材目录（「（梦想电商）所有素材 10.1（更新）」）—— 品类 / 系列 / 商品文件夹里成组的图片。

导入口径（2026-10-06）：
  · **以素材为准**：有图片素材的商品才导，没素材的一律过滤（闲鱼上架没图卖不动，
    也避免往库里塞一堆只有价格的空壳档案）；
  · 一个素材目录 + 一条价格行 = 一个商品（同款不同容量 / 功率在价格表里是两行，
    素材共用一套图，于是导出两个商品、共用同一批图）；
  · 匹配不上价格行的素材商品**也过滤**（成本和售价是必填口径，缺价的商品上架会出 0 元单）；
  · 图片压缩后落到 media/products/dreamshop/<编号>/，主图 1 张 + 详情图若干（默认共 10 张）；
  · 商品落成「供应链商品 / 待审核」，运营在商品页审核通过后再上架（不自动上架）。

匹配为什么不是精确对账：价格表里的行名常常不带品牌（「10000毫安 22.5w q2电 自带线」
的品牌在上一行的分组标题「倍思 Q2电充电宝」里），素材那边又爱写「1万」「2万」「1w毫安」。
所以匹配前先做归一（万→0000、1w毫安→10000毫安），商品名用「分组标题 + 行名」拼，
再按 品牌 + 容量/功率数字 + 词重叠 打分，低置信的会在整理表里标出来让人工过一眼。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------------ 路径与常量
VOLUME = Path("/Volumes/ORICO/99.闲鱼业务/02.合肥梦想电商")
DEFAULT_SOURCE_ROOT = VOLUME / "梦想电商内部素材（10.1 更新）" / "（梦想电商）所有素材 10.1（更新）"
DEFAULT_PRICE_XLS = (
    VOLUME
    / "梦想电商内部素材（10.1 更新）"
    / "（梦想电商）所有拿货价格表 10.1（更新）"
    / "10.1（所有品类） 价格表 （总表）.xls"
)
DEFAULT_OUT = ROOT / "data" / "import" / "dreamshop_products.csv"

# 供应商：这批货都是安徽（合肥）梦想电商供的
SUPPLIER_NAME = "安徽梦想电商"
SUPPLIER_CITY = "合肥"

# 价格口径（2026-10-07 定稿）：渔货价 = 成本价 × 1.15，建议售价 = 渔货价 × 1.35
# 价格表里也有「建议售价」一列，只留档进备注，定价一律按这条规则算，避免两套价打架
PRICE_FISH_RATE = 1.15
PRICE_SALE_RATE = 1.35

# 图片：单商品素材常有上百张，全搬既占盘也拖慢页面 —— 只取前 N 张，并压到长边 1280
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_MAX_IMAGES = 10
IMAGE_LONG_EDGE = 1280
IMAGE_QUALITY = 82
# 素材里「组图（一）」「组图（二）」是同一商品的多套图，合并到商品目录
MERGE_DIR_WORDS = ("组图", "素材", "专用", "实拍", "图片")

# 价格表的大品类：表里只有「电子产品系列：」一个显式标记，其余按内容分段
SECTIONS: list[tuple[int, int, str]] = [
    (0, 200, "电子"),
    (201, 457, "家用电器"),
    (458, 728, "户外"),
    (729, 887, "饰品"),
    (888, 1134, "生活"),
    (1130, 9999, "潮玩"),
]
# 素材目录的一级品类 → 大品类（用于「品类必须一致」这道硬门槛）
MATERIAL_SECTION = {
    "电子品类（注意看品类解读）": "电子",
    "家用电器品类（注意看品类解读）": "家用电器",
    "户外品类（注意看品类解读）": "户外",
    "生活品类（注意看品类解读）": "生活",
    "（热门）泡泡玛特、jellycat 全系列": "潮玩",
    "（热门）铜钱、银元 系列": "饰品",
}

# 品牌表：从商品名里认品牌，同时用于匹配加分（价格行与素材目录品牌不一致就降权）
BRANDS = [
    "倍思", "小米", "米家", "华为", "苹果", "OPPO", "vivo", "iqoo", "一加", "1+", "三星", "安克",
    "小天才", "华强北", "罗技", "雷蛇", "任天堂", "ps5", "ps4", "xbox", "switch",
    "坚果", "当贝", "极米", "索尼", "戴森", "徕芬", "松下", "ulike", "ghd", "昔又",
    "科沃斯", "追觅", "云鲸", "石头", "添可", "美的", "海尔", "云米", "mova", "友望",
    "捷安特", "喜德盛", "禧玛诺", "达亿瓦", "杰克豹", "万奎士", "路亚", "黑冰", "骆驼", "jeep",
    "尤尼克斯", "威可多", "史丹利", "zippo", "ugg", "亚朵", "山姆", "罗莱", "珑骧", "dw",
    "施华洛世奇", "古驰", "宝格丽", "卡地亚", "迪奥", "lv", "格拉芙", "克罗心", "蔻驰",
    "爱马仕", "梵克雅宝", "纪梵希", "西太后", "miumiu", "圣罗兰", "巴宝莉", "范思哲",
    "迪士尼", "labubu", "jellycat", "泡泡玛特", "zisiga", "mokoko", "棉花娃", "tf家族",
    "时代少年团", "权志龙", "大鹅", "蒙口", "mlb", "迪桑特", "李宁", "阿迪达斯", "adidas",
    "安踏", "耐克", "伯希和", "焦下", "gm", "袋鼠", "sgg", "芬达", "依班娜", "野豹", "蓝图",
    "石家班", "天工墨子", "lucky brand", "欧姆龙", "飞利浦", "博世", "松下",
]

# 品类 → 系统类目（一级 / 二级）：二级按关键词在类目树里实配，配不上就只写一级
# （渔枢商品类目是文本「一级 / 二级 / 三级」，上架渔货栈时再按名字去类目树认领）
CATEGORY_RULES: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = [
    (("充电宝", "移动电源"), "数码、电脑", ("移动电源", "充电器", "数码配件")),
    (("充电器",), "数码、电脑", ("充电器", "数码配件")),
    (("耳机",), "数码、电脑", ("耳机", "数码配件")),
    (("鼠标", "键盘"), "数码、电脑", ("鼠标", "电脑外设")),
    (("手表",), "数码、电脑", ("智能手表", "数码配件")),
    (("手柄", "游戏"), "数码、电脑", ("游戏设备", "数码配件")),
    (("音箱", "音响"), "数码、电脑", ("音箱", "数码配件")),
    (("投影仪",), "家用电器", ("投影仪", "大家电")),
    (("扫地机", "洗地机", "吸尘器"), "家用电器", ("清洁电器", "生活电器")),
    (("吹风机", "直发器", "卷发棒", "直板夹"), "个护家清", ("美发器", "个护电器")),
    (("脱毛仪", "美容仪", "大排灯"), "个护家清", ("美容仪", "个护电器")),
    (("风扇", "净化器", "加湿器"), "家用电器", ("生活电器",)),
    (("自行车", "山地车", "公路车"), "运动户外", ("骑行", "自行车")),
    (("乒乓球", "网球", "羽毛球", "球拍"), "运动户外", ("球类",)),
    (("台球",), "运动户外", ("台球",)),
    (("睡袋", "帐篷", "登山杖", "露营", "桌椅"), "运动户外", ("户外装备", "露营")),
    (("望远镜",), "运动户外", ("户外装备",)),
    (("钓", "路亚", "鱼竿", "纺车轮"), "运动户外", ("钓鱼",)),
    (("滑板",), "运动户外", ("滑板",)),
    (("吉他", "贝斯"), "玩具", ("乐器",)),
    (("保温杯", "水杯"), "日用餐厨饮具", ("水杯",)),
    (("墨镜", "眼镜"), "服饰配件饰品", ("眼镜",)),
    (("打火机",), "家居日用品", ("日用品",)),
    (("被子", "枕头", "夏被", "冬被"), "家纺家饰", ("床上用品",)),
    (("冲锋衣", "防晒", "短袖", "羽绒服", "棉服", "马甲"), "女装", ("外套",)),
    (("雪地靴", "拖鞋", "鞋"), "鞋", ("女鞋",)),
    (("双肩包", "行李箱", "手提包", "包"), "箱包皮具", ("背包",)),
    (("项链", "手链", "手镯", "戒指"), "服饰配件饰品", ("首饰",)),
    (("娃娃", "jellycat", "泡泡玛特", "棉花娃", "吊卡"), "玩具", ("潮玩", "毛绒玩具")),
    (("铜钱", "银元"), "其他", ("其他",)),
    (("打印机", "自拍杆"), "数码、电脑", ("数码配件",)),
]

# 品类锚点：两边都认出了锚点却不是同一组（素材是充电宝、价格行是充电器）直接判 0。
# 这批货最容易串的就是「1+100w 充电宝」和「1+100w 充电器」—— 品牌功率全一样，只差品类词。
ANCHOR_GROUPS: list[set[str]] = [
    # 单筒 / 双筒必须排在「望远镜」前面（分组标题里两个词都有，靠前才能把它们分开）
    {"双筒"}, {"单筒"},
    {"充电宝", "移动电源", "电源"},
    {"充电器"},
    {"扫地机"}, {"洗地机"}, {"吸尘器"},
    {"望远镜", "夜视"},
    {"睡袋"}, {"帐篷"}, {"登山杖"}, {"露营", "桌椅"},
    {"网球", "网球拍"}, {"乒乓球"}, {"羽毛球"}, {"台球"},
    {"吉他", "贝斯"},
    {"耳机"}, {"鼠标"}, {"手表"}, {"手柄"}, {"音箱", "音响"}, {"投影仪"},
    {"脱毛仪", "美容仪", "大排灯"},
    {"吹风机", "直发器", "卷发棒", "直板夹"},
    {"风扇"}, {"净化器", "加湿器"},
    {"自行车", "山地车", "公路车"},
    {"保温杯", "水杯"}, {"墨镜"}, {"打火机"}, {"被子", "枕头", "夏被", "冬被"},
    {"冲锋衣", "防晒", "短袖", "羽绒服", "棉服", "马甲"},
    {"雪地靴", "拖鞋"},
    {"双肩包", "行李箱", "手提包"},
    {"项链", "手链", "手镯"},
    {"滑板"}, {"钓", "路亚", "鱼竿", "纺车轮"},
    {"打印机", "自拍杆"},
    {"定位器", "airtag"}, {"苹果笔", "三代笔", "四代笔", "pro笔"},
]

# 同款不同色的素材目录（「1+100w充电宝（银色）/（蓝色）/（极木色）」）合并成一个商品，
# 图片并在一起 —— 颜色是规格，不是一个新商品
COLOR_WORDS = (
    "银色", "蓝色", "极木色", "黑色", "白色", "粉色", "灰色", "紫色", "绿色", "金色", "红色",
    "黄色", "米色", "深灰", "浅蓝", "蓝灰", "深蓝", "静水蓝", "深睡蓝", "竹青黄", "原木青",
    "夜云紫", "融雾紫", "苔藓绿", "柔云粉", "清澄蓝", "水晶紫", "樱花粉",
)

STOP_WORDS = {
    "系列", "正品", "外贸", "平替", "新款", "老款", "爆款", "热门", "注意", "其它", "其他",
    "所有", "各种", "公司", "自家", "仓库", "发货", "专用", "图片", "品牌", "产品", "建议",
    "价格", "标价", "裸机", "套餐", "版本", "官方", "官网", "适用", "标配",
}


# ------------------------------------------------------------------ 文本归一
def normalize(text: str) -> str:
    """归一：全角转半角、大小写、容量写法（1万 / 1w毫安 → 10000毫安）。"""
    out = (text or "").strip().lower()
    # 全角 → 半角
    out = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in out)
    out = out.replace("＋", "+").replace("（", "(").replace("）", ")")
    # 容量写法归一：1w毫安 / 1万毫安 → 10000毫安
    out = re.sub(r"(\d+)\s*w(?=毫安|mah|ma)", lambda m: str(int(m.group(1)) * 10000), out)
    out = re.sub(r"(\d+)\s*万(?=毫安|mah|ma)?", lambda m: str(int(m.group(1)) * 10000), out)
    out = re.sub(r"(\d+)\s*毫安", r"\1", out)
    out = re.sub(r"(\d+)\s*mah", r"\1", out)
    return out


def tokens(text: str) -> set[str]:
    """词集合：中文 2-gram + 英文数字词（去掉括号里的颜色 / 备注等噪声）。"""
    raw = re.sub(r"\([^)]*\)", " ", normalize(text))
    raw = re.sub(r"[^一-龥a-z0-9]+", " ", raw)
    out: set[str] = set()
    for word in raw.split():
        if len(word) < 2 or word in STOP_WORDS:
            continue
        out.add(word)
        if "一" <= word[0] <= "龥":
            for i in range(len(word) - 1):
                out.add(word[i : i + 2])
    return out


def numbers(text: str) -> set[str]:
    """容量 / 功率这类关键数字（10000、22.5、45…）。"""
    raw = normalize(text)
    return {x for x in re.findall(r"\d+(?:\.\d+)?", raw) if x not in ("1", "2", "3", "4", "5")}


def style_word(name: str) -> str:
    """潮玩的核心款名词：去掉品牌、括号、尺寸数字与口水词（「邦尼兔」「草莓蛋糕」）。

    泡泡玛特 / jellycat 同类商品名字前一半都一样（「Jellycat（爱心兔 18CM、31CM）」
    与「Jellycat（彩虹龙 中号、大号）」词重叠极高），光靠相似度分不出来 ——
    款式词必须对得上才认。
    """
    # 只去掉「（合拍）」「（系列）」这类噪声括号，括号里的款式名要留着
    text = re.sub(r"[（(][^）)]*(?:合拍|系列|尺寸|图片|效果图|专用)[^）)]*[）)]", " ", name or "")
    text = re.sub(r"[（()）\[\]]", " ", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:cm|mm|w|毫安|mah)?", " ", text, flags=re.IGNORECASE)
    for word in ("系列", "合拍", "新款", "老款", "正品", "尺寸", "高", "大号", "中号", "小号"):
        text = text.replace(word, " ")
    brand = brand_of(name or "")
    if brand:
        text = normalize(text).replace(normalize(brand), " ")
    return re.sub(r"\s+", "", text)


def watts_of(text: str) -> set[str]:
    """功率数字（65w / 22.5w 这类）：充电头 / 充电宝的型号就差在这个数字上。"""
    raw = normalize(text or "")
    return {x for x in re.findall(r"(\d+(?:\.\d+)?)\s*w(?![a-z])", raw)}


def anchor_of(text: str) -> int | None:
    """这段文本属于哪个品类锚点组（没有就 None）。"""
    raw = normalize(text)
    for index, group in enumerate(ANCHOR_GROUPS):
        if any(word in raw for word in group):
            return index
    return None


def brand_of(text: str) -> str:
    """品牌：归一后比对，返回品牌表的原写法（OPPO / vivo / 1+ 这样大小写不乱改）。"""
    raw = normalize(text)
    for brand in BRANDS:
        if normalize(brand) in raw:
            return brand
    return ""


def clean_title(name: str, group: str = "") -> str:
    """商品名：行名缺品牌时用分组标题里的品牌补上，顺手去掉口水词。

    价格表里「1+160w（15-20分钟）」的品牌在上一行分组「华强北 充电器 / 安卓 充电器」里，
    直接拼整段会得到「华强北 充电器 安卓 充电器 1+160w」这种废话，所以只补品牌词。
    """
    text = (name or "").strip()
    if not brand_of(text) and group:
        brand = brand_of(group)
        if brand:
            text = f"{brand} {text}"
    text = re.sub(r"\s+", " ", text)
    for word in ("（正品）", "(正品)", "（外贸款、平替款）", "（外贸、平替款）", "（外贸款）", "（华强北）"):
        text = text.replace(word, "")
    return text.strip(" -—·")


def money_of(text: str) -> float:
    """价格：'40（紫色 蓝色 白色）' → 40.0；'55 左右' → 55.0；'149 （缺货）' → 149.0。"""
    raw = normalize(text or "")
    match = re.search(r"\d+(?:\.\d+)?", raw)
    return float(match.group()) if match else 0.0


def condition_of(remark: str) -> str:
    """成色：从产品备注里认（闲鱼口径只有四档）。"""
    raw = normalize(remark or "")
    if "全新" in raw:
        return "new"
    if re.search(r"9[5-9]新|98新|95新|99新", raw):
        return "like_new"
    if re.search(r"9成新|8-9成新|8成新|9\.5成新", raw):
        return "light_used"
    if "明显使用" in raw or "7成新" in raw:
        return "used"
    return ""


def category_of(section: str, title: str) -> tuple[str, str]:
    """品类 + 商品名 → (一级, 二级候选)。二级要用的时候再去类目树里核。"""
    raw = normalize(title)
    for keys, cat1, cat2s in CATEGORY_RULES:
        if any(k in raw for k in keys):
            return cat1, cat2s[0] if cat2s else ""
    fallback = {
        "电子": "数码、电脑",
        "家用电器": "家用电器",
        "户外": "运动户外",
        "饰品": "服饰配件饰品",
        "生活": "家居日用品",
        "潮玩": "玩具",
    }
    return fallback.get(section, "其他"), ""


# ------------------------------------------------------------------ 读素材
def scan_materials(root: Path) -> list[dict]:
    """素材目录 → 商品级条目（合并「组图（一）」这类子目录）。

    返回 [{key, name, path, series, section, images:[相对路径]}]，按路径排序保证稳定。
    """
    bucket: dict[str, list[str]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        files = sorted(
            f
            for f in filenames
            if Path(f).suffix.lower() in IMAGE_SUFFIXES and not f.startswith("._")
        )
        if not files:
            continue
        rel = Path(dirpath).relative_to(root)
        parts = list(rel.parts)
        # 组图 / 素材 这类子层归到商品目录上（它们是同一商品的多套图）
        while len(parts) > 2 and any(word in parts[-1] for word in MERGE_DIR_WORDS):
            parts = parts[:-1]
        key = "/".join(parts)
        bucket[key].extend(str(rel / f) for f in files)

    # 同款不同色合并：目录名去掉末尾的颜色括号后一样，就是同一款（图片并到一起）
    merged: dict[tuple[str, str], dict] = {}
    for key in sorted(bucket):
        parts = key.split("/")
        name = parts[-1]
        stem = re.sub(r"[（(][^）)]*[）)]\s*$", "", name).strip()
        if any(color in name for color in COLOR_WORDS):
            name = stem or name
        ident = ("/".join(parts[:-1]), stem or name)
        if ident in merged:
            merged[ident]["images"].extend(bucket[key])
            merged[ident]["colors"] += 1
            continue
        merged[ident] = {
            "key": key,
            "name": name,
            "series": parts[1] if len(parts) > 1 else "",
            "section": MATERIAL_SECTION.get(parts[0], ""),
            "images": sorted(bucket[key]),
            "colors": 1,
        }
    items = sorted(merged.values(), key=lambda x: x["key"])
    for item in items:
        item["images"] = sorted(item["images"])
    return items


# ------------------------------------------------------------------ 读价格表
def load_price_rows(path: Path) -> list[dict]:
    """价格表 → 商品行（带上最近的两级分组标题，补品牌用）。"""
    import xlrd

    sheet = xlrd.open_workbook(str(path)).sheet_by_name("Sheet1")
    rows = [[str(sheet.cell_value(r, c)).strip() for c in range(5)] for r in range(sheet.nrows)]
    groups: list[str] = []
    out: list[dict] = []
    for idx, row in enumerate(rows):
        name = row[0]
        if not name:
            continue
        if not row[1]:
            # 没有价格 = 分组标题 / 说明行。说明行（「B系列：木乃伊式包裹性更强…」这种
            # 一段话）不能当分组名，否则商品名会被拼成一句说明文
            if name.endswith("系列：") or len(name) > 30 or "：" in name and len(name) > 18:
                groups = []
                continue
            # 只认最近一级分组：价格表里「小米移动电源」「华为移动电源」是同级小类，
            # 保留两级会让商品同时带上两个品牌名，匹配时品牌就白判了
            groups = [name]
            continue
        section = next((s for a, b, s in SECTIONS if a <= idx <= b), "")
        # 潮玩按名字认（jellycat / 泡泡玛特 段在表里没有显式分界，靠行号会认成生活品类）
        blob = normalize(f"{name} {row[4]}")
        if any(w in blob for w in ("jellycat", "泡泡玛特", "棉花娃", "labubu", "哭娃", "星星人", "盲盒")):
            section = "潮玩"
        out.append(
            {
                "row": idx + 1,
                "name": name,
                "group": " ".join(groups),
                "cost": money_of(row[1]),
                "cost_text": row[1],
                "price": money_of(row[2]),
                "price_text": row[2],
                "ship_from": row[3],
                "remark": row[4],
                "section": section,
            }
        )
    return out


# ------------------------------------------------------------------ 匹配
def match_score(item: dict, row: dict) -> float:
    """素材商品 ↔ 价格行：品牌 + 品类锚点 + 数字 + 词重叠。

    三道硬门槛，宁可漏也不乱配（配错的价导入进去比没导更麻烦）：
      1 大品类不同 → 0；
      2 两边都认出品类锚点（充电宝 / 充电器 …）却不是同一组 → 0；
      3 品牌不一致 → 大扣分。
    """
    if item["section"] and row["section"] and item["section"] != row["section"]:
        return 0.0
    left_text = f"{item['name']} {item['series']}"
    right_text = f"{row['name']} {row['group']}"
    anchor_l, anchor_r = anchor_of(left_text), anchor_of(right_text)
    if anchor_l is not None and anchor_r is not None and anchor_l != anchor_r:
        return 0.0
    # 潮玩：款式名的每一段都要对得上（「钻戒」「玫瑰手捧花」两段都得在价格行里出现）
    if row["section"] == "潮玩":
        words = [w for w in re.split(r"[和、\s,/|]+", style_word(item["name"])) if len(w) >= 2]
        if words and not all(normalize(w) in normalize(right_text) for w in words):
            return 0.0
    left, right = tokens(left_text), tokens(right_text)
    if not left or not right:
        return 0.0
    score = len(left & right) / len(left | right)
    # 品牌以「自己的名字」为准，分组标题只作兜底（分组里可能混着上一类的品牌）
    brand_l = brand_of(item["name"]) or brand_of(item["series"])
    brand_r = brand_of(row["name"]) or brand_of(row["group"])
    if brand_l and brand_r:
        # 品牌对不上就是另一款（华为的移动电源不能拿小米的价），直接否掉
        if normalize(brand_l) != normalize(brand_r):
            return 0.0
        score += 0.45
    # 功率必须对得上：「小米口袋版 67w」和「小米移动电源3 22.5w」容量都是 10000 毫安，
    # 但这是两款货，价差 20 块
    watt_l, watt_r = watts_of(item["name"]), watts_of(row["name"])
    if watt_l and watt_r and not (watt_l & watt_r):
        return 0.0
    nums_l, nums_r = numbers(item["name"]), numbers(row["name"])
    if nums_l and nums_r:
        if nums_l & nums_r:
            score += 0.25
        elif anchor_l is not None and anchor_l == anchor_r:
            # 同一品类下容量 / 功率对不上（20000 毫安 vs 10000 毫安、100w vs 20w）＝ 不同型号
            return 0.0
        else:
            score -= 0.3
    return round(score, 3)


def build_entries(materials: list[dict], rows: list[dict], min_score: float = 0.28) -> list[dict]:
    """素材 × 价格 → 待导入商品：全局按分数从高到低配，一个素材 / 一条价格行各只用一次。

    为什么不用「每个素材各自取最高分」：同一条价格行会被好几个素材目录抢
    （「oppo 20w 移动电源（充电效果图）1/2/3」是同一款的三组图），那样会建出重复商品。
    """
    pending: list[tuple[float, dict, dict]] = []
    pairs: list[tuple[float, int, int]] = []
    for mi, item in enumerate(materials):
        for ri, row in enumerate(rows):
            # 没价格的行不参与匹配（成本 / 售价是必填口径，导进去就是 0 元商品）
            if row["cost"] <= 0 and row["price"] <= 0:
                continue
            score = match_score(item, row)
            if score >= min_score:
                pairs.append((score, mi, ri))
    pairs.sort(key=lambda x: -x[0])

    used_mat: set[int] = set()
    used_row: set[int] = set()
    entries: list[dict] = []
    for score, mi, ri in pairs:
        if mi in used_mat or ri in used_row:
            continue
        item, row = materials[mi], rows[ri]
        # 价格行只写了「尺寸」「大小」这种泛称（款式名在分组标题里）→ 用分组名当商品名，
        # 否则导进去就是「jellycat 17cm 尺寸」这种看不出是什么货的名字
        if re.search(r"尺寸|大小|厘米|^[\d\s.]+$", normalize(row["name"])) and row["group"]:
            title = clean_title(row["group"])
        else:
            title = clean_title(row["name"], row["group"]) or clean_title(item["name"])
        # 「戴森 全套（图片上的都有）」这类不是一件具体商品，价格也没法对，不要
        if any(word in normalize(title) for word in ("全套", "图片上的", "其它款式", "私聊客服")):
            continue
        # 名字长得像一段说明文字（价格表里把注意事项写进商品名了）也不当成商品
        if len(title) > 40 or "：" in title or ":" in title:
            continue
        # 「尺寸」「主图」这类素材目录不是一件商品（里面只是尺寸图），配上去图是错的
        if len(normalize(item["name"]).strip()) <= 2 or any(
            word in normalize(item["name"]) for word in ("尺寸", "主图", "详情图", "价格表")
        ):
            continue
        # 潮玩（泡泡玛特 / jellycat）款式名高度相似（「爱心兔 18cm」vs「爱心兔 31cm」），
        # 分数很容易虚高 —— 这一类额外提高门槛，宁可留给人确认
        # 分数偏低的（型号对得不够瓷实）一律留给人确认，整理表里标 N
        if score < 0.6 or (row["section"] == "潮玩" and score < 0.7):
            pending.append((score, item, row))
            continue
        used_mat.add(mi)
        used_row.add(ri)
        title = clean_title(row["name"], row["group"]) or clean_title(item["name"])
        cat1, cat2 = category_of(row["section"], title)
        entries.append(
            {
                "material": item["key"],
                "material_name": item["name"],
                "images": item["images"],
                "image_count": len(item["images"]),
                "price_row": row["row"],
                "score": score,
                "title": title,
                "brand": brand_of(title),
                "section": row["section"],
                "category1": cat1,
                "category2": cat2,
                "cost": row["cost"],
                "price": row["price"],
                "ship_from": row["ship_from"],
                "remark": row["remark"],
                "condition": condition_of(row["remark"]),
                "sold_out": "缺货" in (row["cost_text"] or ""),
            }
        )
    # 待确认的（潮玩这类容易配错的）也一并写进整理表，标成「待确认」，默认不入库
    for score, item, row in pending:
        title = clean_title(row["name"], row["group"]) or clean_title(item["name"])
        cat1, cat2 = category_of(row["section"], title)
        entries.append(
            {
                "material": item["key"],
                "material_name": item["name"],
                "images": item["images"],
                "image_count": len(item["images"]),
                "price_row": row["row"],
                "score": score,
                "title": title,
                "brand": brand_of(title),
                "section": row["section"],
                "category1": cat1,
                "category2": cat2,
                "cost": row["cost"],
                "price": row["price"],
                "ship_from": row["ship_from"],
                "remark": row["remark"],
                "condition": condition_of(row["remark"]),
                "sold_out": "缺货" in (row["cost_text"] or ""),
                "pending": True,
            }
        )
    return entries


def load_csv_entries(path: Path, materials: list[dict], allow_missing: bool = False) -> list[dict]:
    """按**人工核对过的**整理表导入：表里改过什么就按什么导（列见 write_csv）。

    整理表是给人过一遍的：自动匹配在潮玩 / 同款多规格上会配错价，人工把「是否导入」
    改成 N、或者直接改商品名 / 类目 / 价格，再跑 --from-csv 就是最终结果。

    `allow_missing`：离线包导入时没有原始素材盘（materials 为空），但图片已经处理好了，
    这时照样按整理表建条目 —— 素材目录对不上素材盘不拦，图片走 `prepare_images` 的复用分支。
    """
    by_key = {item["key"]: item for item in materials}
    entries: list[dict] = []
    with path.open(encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            flag = (row.get("是否导入") or "Y").strip().upper()
            if flag in ("N", "0", "否", "NO"):
                continue
            item = by_key.get((row.get("素材目录") or "").strip())
            if item is None and not allow_missing:
                continue
            category = (row.get("系统类目") or "").strip()
            parts = [p.strip() for p in category.split("/") if p.strip()]
            title = (row.get("商品名") or "").strip() or item["name"]
            entries.append(
                {
                    "material": item["key"] if item else (row.get("素材目录") or "").strip(),
                    "material_name": item["name"] if item else (row.get("素材商品名") or "").strip(),
                    "images": item["images"] if item else [],
                    "image_count": int(row.get("图片数") or 0) or (len(item["images"]) if item else 0),
                    "price_row": row.get("价格表行") or "",
                    "score": float(row.get("匹配置信") or 0),
                    "title": title,
                    "brand": (row.get("品牌") or "").strip() or brand_of(title),
                    "section": (row.get("品类") or "").strip(),
                    "category1": parts[0] if parts else "",
                    "category2": parts[1] if len(parts) > 1 else "",
                    "cost": float(row.get("拿货价") or 0),
                    "price": float(row.get("建议售价") or 0),
                    "ship_from": (row.get("发货地") or "").strip(),
                    "remark": (row.get("产品备注") or "").strip(),
                    "condition": (row.get("成色") or "").strip(),
                    "sold_out": (row.get("状态") or "").strip() == "缺货",
                }
            )
    return entries


# ------------------------------------------------------------------ 图片
def prepare_images(
    root: Path,
    entry: dict,
    slug: str,
    max_images: int,
    dry_run: bool,
    media_root: Path | None = None,
    long_edge: int = IMAGE_LONG_EDGE,
    quality: int = IMAGE_QUALITY,
) -> list[str]:
    """素材图 → media/products/dreamshop/<slug>/，压缩后返回可访问的 URL 列表。

    素材一个商品常有上百张图（还有视频 / docx），这里只取前 max_images 张：
    第 1 张当主图，其余进图文详情。

    **离线包导入**：给了 `media_root`（已处理好的图片目录）且 `media_root/<slug>/`
    里已经有图，就直接复用这批图 —— 线上没有原始素材盘（素材在本地挂载盘上），
    走这条路不用再啃一遍原始素材，编号顺序与本地导出时一致即可对上。
    """
    from PIL import Image

    from app.config import settings

    target_dir = Path(settings.media_path) / "products" / "dreamshop" / slug
    if media_root is not None:
        target_dir = Path(media_root) / slug

    # 离线包：目录里已经有处理好的图，直接按文件名顺序读出来
    if not dry_run and target_dir.is_dir():
        done = sorted(target_dir.glob("*.jpg"))
        if done:
            return [
                f"/media/products/dreamshop/{slug}/{p.name}" for p in done[:max_images]
            ]

    urls: list[str] = []
    files = entry["images"][:max_images]
    for index, rel in enumerate(files, start=1):
        src = root / rel
        dst = target_dir / f"{index:02d}.jpg"
        url = f"/media/products/dreamshop/{slug}/{index:02d}.jpg"
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                with Image.open(src) as img:
                    img = img.convert("RGB")
                    width, height = img.size
                    scale = min(1.0, long_edge / max(width, height))
                    if scale < 1.0:
                        img = img.resize(
                            (int(width * scale), int(height * scale)), Image.LANCZOS
                        )
                    img.save(dst, "JPEG", quality=quality, optimize=True)
            except Exception as exc:  # noqa: BLE001 单张图坏了不能拖垮整批导入
                print(f"  跳过坏图 {rel}：{exc}")
                continue
        urls.append(url)
    return urls


# ------------------------------------------------------------------ 入库
def resolve_category(session, cat1: str, cat2: str) -> str:
    """把「一级 / 二级」落成类目树里真实存在的写法（树里没有就只留一级）。"""
    from app.services import category as category_service

    text = cat1.strip()
    if not text:
        return ""
    try:
        tree = category_service.tree(session)
    except Exception:  # noqa: BLE001 类目树读不出来也不耽误导入
        return text
    node = next((n for n in tree if n.get("name") == cat1), None)
    if node is None:
        return text
    if cat2:
        child = next(
            (c for c in node.get("children", []) if cat2 in (c.get("name") or "") or (c.get("name") or "") in cat2),
            None,
        )
        if child is not None:
            return f"{cat1} / {child['name']}"
    return text


def apply_entries(
    session,
    entries: list[dict],
    root: Path,
    max_images: int,
    media_root: Path | None = None,
    long_edge: int = IMAGE_LONG_EDGE,
    quality: int = IMAGE_QUALITY,
) -> dict:
    """写库：商品 + 图片 + SKU，同名商品跳过（幂等，重复跑不会建重）。"""
    from app.models import Supplier
    from app.services import product as product_service
    from app.services import supplier as supplier_service

    supplier = session.scalar(
        __import__("sqlalchemy").select(Supplier).where(Supplier.name == SUPPLIER_NAME)
    )
    if supplier is None:
        supplier = supplier_service.ensure_supplier(session, SUPPLIER_NAME, source="梦想电商商品导入")
        if supplier is not None:
            supplier.city = SUPPLIER_CITY
            supplier.category = "全品类供应链"
            session.commit()

    created, skipped, failed = 0, 0, 0
    for index, entry in enumerate(entries, start=1):
        slug = f"ds{index:04d}"
        title = entry["title"]
        if product_service.find_by_name(session, title) is not None:
            skipped += 1
            continue
        urls = prepare_images(
            root,
            entry,
            slug,
            max_images,
            dry_run=False,
            media_root=media_root,
            long_edge=long_edge,
            quality=quality,
        )
        if not urls:
            failed += 1
            print(f"  [{slug}] {title}：图片处理失败，跳过")
            continue
        main, details = urls[0], urls[1:]
        blocks = [
            {
                "title": "商品详情",
                "content": "\n".join(
                    line
                    for line in (
                        f"发货地：{entry['ship_from']}" if entry["ship_from"] else "",
                        f"产品备注：{entry['remark']}" if entry["remark"] else "",
                    )
                    if line
                ),
            }
        ]
        form = {
            "name": title,
            "category": resolve_category(session, entry["category1"], entry["category2"]),
            "brand": entry["brand"],
            "condition": entry["condition"],
            "cost_amount": entry["cost"],
            # 价格口径：渔货价 = 成本 × 1.15，建议售价 = 渔货价 × 1.35
            "fish_price": round(entry["cost"] * PRICE_FISH_RATE, 2),
            "price": round(round(entry["cost"] * PRICE_FISH_RATE, 2) * PRICE_SALE_RATE, 2),
            "stock": -1,
            "source_type": "third_party",
            "supplier_name": SUPPLIER_NAME,
            "supplier_id": supplier.id if supplier else 0,
            "image_path": main,
            "images": "\n".join(urls),
            "detail_blocks_json": json.dumps(blocks, ensure_ascii=False),
            "detail_images": "\n".join(details),
            "supplier_tip": entry["remark"],
            "note": (
                f"梦想电商导入 · 价格表第 {entry['price_row']} 行 · 素材 {entry['material']}"
                f"（价格表建议售价 {entry['price']:.2f}，定价按 1.15 / 1.35 口径）"
            ),
            # 导入的商品一律「待审核」，运营过一遍再上架
            "submit_action": "audit",
        }
        try:
            product = product_service.create_product(session, form, created_by="import:dreamshop")
            created += 1
            print(f"  [{slug}] {product.code} {title}（成本 {entry['cost']} / 售价 {entry['price']} / {len(urls)} 图）")
        except Exception as exc:  # noqa: BLE001 单条失败不影响整批
            failed += 1
            session.rollback()
            print(f"  [{slug}] {title} 导入失败：{exc}")
    return {"created": created, "skipped": skipped, "failed": failed}


# ------------------------------------------------------------------ 整理表
def write_csv(entries: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "是否导入", "素材目录", "素材商品名", "图片数", "价格表行", "匹配置信", "商品名", "品牌",
        "品类", "系统类目", "拿货价", "建议售价", "成色", "发货地", "产品备注", "状态",
    ]
    with out.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(fields)
        for entry in entries:
            # 待确认的（潮玩这类容易配错的）默认不导入，人工核对过再改成 Y
            flag = "N" if entry.get("pending") else "Y"
            writer.writerow(
                [
                    flag,
                    entry["material"],
                    entry["material_name"],
                    entry["image_count"],
                    entry["price_row"],
                    entry["score"],
                    entry["title"],
                    entry["brand"],
                    entry["section"],
                    (entry["category1"] + (" / " + entry["category2"] if entry["category2"] else "")),
                    entry["cost"],
                    entry["price"],
                    entry["condition"],
                    entry["ship_from"],
                    entry["remark"],
                    "缺货" if entry["sold_out"] else "",
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="合肥梦想电商商品导入")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--price-xls", default=str(DEFAULT_PRICE_XLS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只出整理表）")
    parser.add_argument("--from-csv", default="", help="按人工核对过的整理表导入（列里的值优先）")
    parser.add_argument("--include-pending", action="store_true", help="连「待确认」的一起导")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个素材商品")
    parser.add_argument("--max-images", type=int, default=DEFAULT_MAX_IMAGES)
    parser.add_argument("--min-score", type=float, default=0.45)
    parser.add_argument(
        "--media-root",
        default="",
        help="离线包导入：已处理好的图片目录（media/products/dreamshop），"
        "给了它就直接用里面的图，不再需要原始素材盘",
    )
    parser.add_argument("--long-edge", type=int, default=IMAGE_LONG_EDGE, help="图片长边上限")
    parser.add_argument("--quality", type=int, default=IMAGE_QUALITY, help="JPEG 质量")
    args = parser.parse_args()

    media_root = Path(args.media_root) if args.media_root else None
    root = Path(args.source_root)
    if not root.exists() and media_root is None:
        print(f"素材目录不存在：{root}（离线导入请给 --media-root）")
        return 1

    materials = scan_materials(root) if root.exists() else []
    if materials:
        print(f"素材：{len(materials)} 个商品目录（同款不同色已合并）")

    # 两条路：按人工核对过的整理表导，或重新匹配一遍先出整理表
    if args.from_csv:
        csv_path = Path(args.from_csv)
        entries = load_csv_entries(csv_path, materials, allow_missing=bool(media_root))
        print(f"整理表里要导入：{len(entries)} 个商品")
    else:
        if not Path(args.price_xls).exists():
            print(f"价格表不存在：{args.price_xls}")
            return 1
        rows = load_price_rows(Path(args.price_xls))
        print(f"价格表：{len(rows)} 条商品行")
        all_entries = build_entries(materials, rows, min_score=args.min_score)
        if args.limit:
            all_entries = all_entries[: args.limit]
        out = Path(args.out)
        # 整理表写全（待确认的标 N，人工核对时能看到并改成 Y）
        write_csv(all_entries, out)
        pending = [e for e in all_entries if e.get("pending")]
        entries = all_entries if args.include_pending else [e for e in all_entries if not e.get("pending")]
        print(
            f"可导入 {len(entries)} 个；待确认 {len(pending)} 个"
            f"（型号对得不够瓷实，整理表里标 N，人工核对后改成 Y 再用 --from-csv 导入）；"
            f"素材里没匹配上价格的已过滤"
        )
        print(f"整理表：{out}")

    if not args.apply:
        print("（只生成整理表，加 --apply 才写库；人工改过表后用 --from-csv 导入）")
        return 0

    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        result = apply_entries(
            session,
            entries,
            root,
            args.max_images,
            media_root=media_root,
            long_edge=args.long_edge,
            quality=args.quality,
        )
    print(f"入库完成：新建 {result['created']} · 已存在跳过 {result['skipped']} · 失败 {result['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
