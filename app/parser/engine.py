"""消息文本 -> 结构化订单字段。

解析策略（由强到弱）：
  1. 字段 patterns 正则（第一个捕获组）
  2. 行级 "标签: 值" 匹配（支持 labels 别名，对模板微调容错最好）
  3. 截图 OCR 文本兜底（同 1、2 两步）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .address import (  # noqa: F401  （对外仍从 engine 导出）
    address_complete,
    best_address_line,
    looks_like_address,
)
from .rules import FieldRule, PlatformRule, RuleSet, get_rules

# 行级 key: value（兼容全角/半角冒号、方括号包围的标签）
LINE_KV_RE = re.compile(r"^\s*[【\[［(（]?\s*([^:：\n]{1,16}?)\s*[】\]］)）]?\s*[:：]\s*(.+?)\s*$")
# 用于标签归一化的噪声字符
NOISE_RE = re.compile(r"[\s_\-·•、,，。.:：/\\()（）\[\]【】“”\"']")
PHONE_RE = re.compile(r"1[3-9]\d{9}")
MONEY_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")
INT_RE = re.compile(r"-?\d+")
# 金额的加法写法：「130+80」「130➕80」「130 加 80」（群里常用来写两件货的成本）
ADDITIVE_SEP_RE = re.compile(r"[+➕加]")
ADDITIVE_MONEY_RE = re.compile(
    r"[\d,]+(?:\.\d{1,2})?(?:[ \t]*[+➕加][ \t]*[¥￥]?[\d,]+(?:\.\d{1,2})?)+"
)

# 群消息里偶尔出现「同一行用大量空格分隔多个字段」的版本（换行被 IM 压成空格）；
# 在「紧接着 标签: 」的位置补一个换行，让两种版式走同一套行解析逻辑。
SPLIT_LABEL_RE = re.compile(r"[\s，,;；]+(?=[^\s:：\n]{2,10}\s*[:：])")
# 取值时若把后面的「标签:」一起吃掉，截掉尾巴；避免 "出货闲鱼号：" 空值吞掉下一行。
TRAILING_LABEL_RE = re.compile(r"[\s，,;；]{2,}[^\s:：\n]{2,10}\s*[:：]")
# 「标签:」本身就是值（说明该字段上行时留空），例如 "回款支付宝归属人："
PURE_LABEL_RE = re.compile(r"^[^\s:：\n]{2,12}[:：]$")
# 行内含冒号：多半是另一个字段 / 时间 / 链接，不能当成上一行的续行
HAS_COLON_RE = re.compile(r"[:：]")

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)


def normalize_label(text: str) -> str:
    return NOISE_RE.sub("", text).lower()


# ---------------------------------------------------------------- 常见人为错误
# 群里都是手打 + 复制粘贴：全角半角混写、人名被换行拆开……
# 取值之前先纠一遍。每条纠错的开关放在解析规则文件的 typos 段，可单独关。
# 注：手机号去空格、订单号去空格、x59 这类货币符号、标签两侧多余空白，
#     引擎在取值那一步已经内在处理了，这里不重复做。
TYPO_NAME_FIELDS = frozenset({"receiver_name", "buyer_nick"})

# 全角字母数字 -> 半角（「１２９」→「129」、「ＡＢ」→「AB」）。
# 注意：不能用 unicodedata.normalize("NFKC")，它连中文标点一起转了 ——
# 备注「紫色，单球拍」会变成「紫色,单球拍」，回群通知里看着就不对。
# 规则里的正则本来就同时接受全角 / 半角冒号（[:：]），标点不需要动。
_FULLWIDTH_ALNUM = {
    **{chr(0xFF10 + i): chr(0x30 + i) for i in range(10)},          # ０-９
    **{chr(0xFF21 + i): chr(0x41 + i) for i in range(26)},          # Ａ-Ｚ
    **{chr(0xFF41 + i): chr(0x61 + i) for i in range(26)},          # ａ-ｚ
    "\uFFE5": "\u00A5",   # ￥ -> ¥（金额匹配认这个）
    "\u3000": " ",        # 全角空格 -> 普通空格
}


def apply_typo_fixes(text: str, rules: RuleSet) -> str:
    """取值之前纠一遍常见手写 / 复制错误：每条都能在规则文件里单独关掉。"""
    if text and rules.typo_on("fullwidth_alnum"):
        text = text.translate(_FULLWIDTH_ALNUM)
    return text


def loose_name_value(fr: FieldRule, text: str) -> str | None:
    """人名被人换行拆开时的补救：合并后是「收货人:张 女士」，而人名本来不带空格。

    只在确实用 replied 合并过行时才用（见调用处），正常的单行取值不会走到这里。
    """
    for label in fr.labels:
        match = re.search(rf"{re.escape(label)}[:：]?[ \t]*(.+)$", text, re.MULTILINE)
        if not match:
            continue
        value = (clean_value(match.group(1)) or "").strip()
        if value and len(value) <= 24:
            return value
    return None


def normalize_lines(text: str) -> str:
    """把「用空白分隔多个 标签: 值」的一行拆成多行；已是多行时保持原样。"""
    if not text:
        return ""
    return SPLIT_LABEL_RE.sub("\n", text)


# ---------------------------------------------------------------- 续行合并
# 群里是复制粘贴的模板，字段值经常被人换行（商品名一行、规格一行），
# 只取「标签: 到行尾」就会丢掉后半截，所以要把紧跟在后面的续行并回同一行。
CONTINUATION_MAX_LINES = 2      # 同一个字段最多并几行（再多就多半不是这个字段的内容了）
CONTINUATION_MAX_LEN = 60       # 单行续行长度上限（避免把大段闲聊并进字段）
LONG_VALUE_LEN = 50             # 合并后的值超过这个长度就提示核对
# 地址末尾常混进来的「下一行标签」：群里复制时这行标签紧跟在地址后面，被一起带了过来
ADDRESS_TAIL_NOISE: tuple[str, ...] = (
    "备注", "收货人", "收件人", "收货地址", "地址信息", "手机号码", "联系电话", "联系买家",
    "订单编号", "订单号", "产品名称", "商品名称", "规格", "数量",
    "售价", "成交价", "商品总价", "成本价", "运费", "手续费", "所属供应链", "出货闲鱼号",
)
# 群消息模板和订单截图里都能取到的字段：这两处互相印证，不一致说明至少一处有误
DUAL_SOURCE_FIELDS: dict[str, str] = {
    "receiver_name": "收货人",
    "receiver_phone": "手机号",
    "receiver_address": "地址",
}
DUAL_MIN_OVERLAP = 0.3   # 两处取值重合度低于这个数，就认为说的不是同一个东西


def _norm_digits(value: object) -> str:
    """只留数字：手机号群里常写成 159 2038 4664 或 159-2038-4664。"""
    return re.sub(r"\D", "", str(value or ""))


def _valid_phone(value: object) -> bool:
    return len(_norm_digits(value)) == 11


def _text_overlap(a: object, b: object) -> float:
    """两段文字的重合度（0~1）：包含关系算 1，否则按字符 3-gram 比。"""
    sa = re.sub(r"\s+", "", str(a or ""))
    sb = re.sub(r"\s+", "", str(b or ""))
    if not sa or not sb:
        return 0.0
    if sa in sb or sb in sa:
        return 1.0
    grams_a = {sa[i : i + 3] for i in range(max(len(sa) - 2, 0))}
    grams_b = {sb[i : i + 3] for i in range(max(len(sb) - 2, 0))}
    if not grams_a or not grams_b:
        return 1.0 if sa == sb else 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


# 地址两处比对：共有的 3 字片段达到这个数就算同一个地址（OCR 认错一两个字是常态）
ADDRESS_MIN_SHARED = 3


def _address_shared(a: object, b: object) -> int:
    """两段地址共有的 3 字片段数（对 OCR 错字容忍，比整串比相似度稳）。"""
    x = re.sub(r"\s+", "", str(a or ""))
    y = re.sub(r"\s+", "", str(b or ""))
    if len(x) < 3 or len(y) < 3:
        return 0
    grams = {x[i : i + 3] for i in range(len(x) - 2)}
    return sum(1 for i in range(len(y) - 2) if y[i : i + 3] in grams)


def _address_same(a: object, b: object) -> bool:
    """两处说的是不是同一个地址：包含关系，或片段重合够多，或整体相似度够高。"""
    x = re.sub(r"\s+", "", str(a or ""))
    y = re.sub(r"\s+", "", str(b or ""))
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    if _address_shared(x, y) >= ADDRESS_MIN_SHARED:
        return True
    return _text_overlap(x, y) >= DUAL_MIN_OVERLAP


# 收货三件套：写错一个就发错货 / 找不到人，所以「不确定」时必须明说，不能悄悄填进去。
# 别的字段不在这里标 —— 标多了运营会当成背景噪声，反而看不见真正要核对的。
_RECEIVER_CHECK_FIELDS: dict[str, str] = {
    "receiver_address": "地址",
    "receiver_name": "收货人",
    "receiver_phone": "手机号",
}
# 收货三件套（无标签写法兜底时按这三个 key 取值，见 unlabeled_contact）
_RECEIVER_CONTACT_KEYS: tuple[str, ...] = (
    "receiver_name",
    "receiver_phone",
    "receiver_address",
)


def _receiver_conflict(key: str, text_val: object, ocr_val: object) -> bool:
    """群里写的和截图里认的是不是两回事（对 OCR 错字留一点容忍）。"""
    if key == "receiver_address":
        return not _address_same(text_val, ocr_val)
    if key == "receiver_phone":
        return _norm_digits(text_val) != _norm_digits(ocr_val)
    return _text_overlap(str(text_val or ""), str(ocr_val or "")) < DUAL_MIN_OVERLAP


def _ocr_value_uncertain(key: str, value: object) -> bool:
    """只有截图这一个来源时，这个值看着可不可信。

    截图上的收货信息是印刷体，认全了就能直接用：地址要到区县一级、手机号要
    是 11 位、人名要像人名。认不全的（OCR 常只认出半截）才叫「不确定」——
    一律都标的话，群里模板消息本来就不含收货信息，等于每单都标，运营会麻木。
    """
    v = re.sub(r"\s+", "", str(value or ""))
    if key == "receiver_address":
        return not address_complete(v)
    if key == "receiver_phone":
        return not _valid_phone(v)
    if key == "receiver_name":
        if not 2 <= len(v) <= 15:
            return True
        return not re.fullmatch(r"[\u4e00-\u9fa5·A-Za-z.。 ]+", v)
    return False


def pending_check_fields(result: ParseResult, dual: dict) -> list[str]:
    """收货信息里哪些字段「不确定」，要运营发货前核对，连原因一起给出。

    只标三种情况（两处一致、或只有群里手打的值都不标 —— 手打的比 OCR 可信）：
      · 群里没写、只有截图里有，而且没认全（地址缺区县、手机号位数不对）；
      · 两处都写了但说法对不上：必有一个是错的。

    两边都没取到的不在这里标：解析层分不清「这单真没有收货信息」和「订单上本来
    就有、只是这一轮没解析到」，一律标的话会大面积误报，反而把真要核对的淹掉；
    缺值在订单页就是空的，运营一眼看得到，走的是「缺字段」那条提示。
    """
    out: list[str] = []
    for key, label in _RECEIVER_CHECK_FIELDS.items():
        value = result.fields.get(key)
        text_val, ocr_val = dual.get(key, (None, None))
        if not value:
            continue  # 缺值走「缺字段」提示，不占「待核对」（见函数说明）
        if ocr_val is None:
            continue  # 截图没取到：用的是群里手打的值，可信
        if text_val is None:
            # 只有截图里有：认全了（地址到区县、手机号 11 位）就能直接用，
            # 认不全的才标 —— 这类多半是 OCR 只认出了半截
            if _ocr_value_uncertain(key, value):
                out.append(f"{label}（截图里只认出一部分）")
        elif _receiver_conflict(key, text_val, ocr_val):
            out.append(f"{label}（与群里写的不一致）")
    return out


def _convert_or_none(raw: object, fr: FieldRule) -> object | None:
    """把一个来源（群消息 / 截图）取到的原始字符串转成字段值，失败算没取到。"""
    if raw is None:
        return None
    if isinstance(raw, str) and fr.normalize == "strip_spaces":
        raw = re.sub(r"\s+", "", raw)
    value, ok = convert(raw, fr.type, sum_additive=fr.sum_additive)
    return value if ok else None
# 并进来的内容里出现这些，说明很可能并错了（把别的字段 / 无关文字一起吃进来）
SUSPICIOUS_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("看着像手机号", re.compile(r"1[3-9]\d{9}")),
    ("看着像金额", re.compile(r"[¥￥]\s*[\d,]+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*元")),
    ("看着像地址", re.compile(r"(?:省|自治区|.{2,8}市|.{2,8}区|.{2,8}县)(?:[^，,。]{0,20}(?:街道|路|号|栋|单元|室))?")),
    # 字段值一般不带句末标点、也不说客套话：出现这些多半是把聊天内容并了进来
    ("看着像聊天内容", re.compile(r"[。！!？?]|谢谢|收到|好的|辛苦|麻烦|请问|你好")),
    # 值末尾挂着一个别的字段的标签名（「……12号 备注」）：多半是把下一行的标签当成续行并了进来
    (
        "末尾还挂着别的字段标签",
        re.compile(
            r"(?:^|\s)(备注|收货人|收件人|手机号码|联系电话|订单编号|订单号|产品名称|规格|数量"
            r"|售价|成交价|商品总价|成本价|运费|手续费|所属供应链|出货闲鱼号|回款支付宝归属人)$"
        ),
    ),
)


# 「标签和值连写」的判断边界：值里出现句中标点就是句子（不是字段值），整行超过这个长度也不拆
SENTENCE_PUNCT_RE = re.compile(r"[，,。.；;！!？?、]")
GLUED_LABEL_MAX_LEN = 24


def _split_glued_label(line: str, labels: set[str]) -> tuple[str, str] | None:
    """拆出「标签和值连写、中间漏了冒号」的行：`备注蓝色` → (`备注`, `蓝色`)。

    运营复制时经常漏打冒号（2026-09-29 朱先生那单的「备注蓝色」）：整行看着像普通
    文本，会被当成上一个字段的续行并进去 —— 地址就变成了「……锦绣东方小区 备注蓝色」，
    备注也因此取不到，只能人工改。这里按「行首是模板里的标签名」把它拆开补成 `标签:值`。

    刻意保守，宁可不拆（退回原来的合并行为）也不要拆错：
      - 整行带冒号（多半是别的字段 / 链接 / 时间）不拆；
      - 标签后紧跟冒号不拆（那是正常的「标签: 值」，前面已经处理过）；
      - 单字标签不拆（太容易误伤商品名）；
      - 值里带句中标点 / 整行太长就不拆 —— 那是句子不是字段，例如截图里的
        「买家已付款，请尽快发货」：行首的「买家」是句子开头，不是「买家昵称」标签；
      - 多个标签命中时取最长的那个（地址信息 > 地址）。
    """
    if not line or HAS_COLON_RE.search(line) or len(line) > GLUED_LABEL_MAX_LEN:
        return None
    best: tuple[str, str] | None = None
    for label in labels:
        if len(label) < 2 or not line.startswith(label):
            continue
        rest = line[len(label) :].strip()
        if not rest or rest[0] in ":：" or SENTENCE_PUNCT_RE.search(rest):
            continue
        if best is None or len(label) > len(best[0]):
            best = (label, rest)
    return best


def merge_continuation_lines(
    text: str, labels: Iterable[str] = ()
) -> tuple[str, dict[str, int]]:
    """把「标签: 值」后面被换行截断的内容并回同一行（空格连接）。

    labels 传进来的是模板里所有字段的标签名（备注 / 收货人 / 售价……）。群里常把
    标签单独写成一行（后面没内容），它不是上一个字段的续行，而是下一个字段的开头：
    不认出来的话就会被并进上一个字段（典型事故：地址后面跟着一行「备注」，
    地址就变成了「……XX号 备注」）。

    返回 (合并后的文本, {归一化标签: 并进来的续行数})。
    合并条件刻意保守，宁可漏合也不要并错：
      - 上一行确实是「标签: 值」；
      - 当前行非空、不含冒号（含冒号多半是另一个字段或时间 / 链接）；
      - 同一个字段最多并 2 行，续行不超过 80 字；空行结束续行窗口。
    """
    lines = (text or "").split("\n")
    out: list[str] = []
    merged: dict[str, int] = {}
    current_idx = -1
    current_key = ""
    current_extra = 0
    bare_labels: set[str] = set(labels)
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        m = LINE_KV_RE.match(line)
        if m:
            out.append(line)
            current_idx = len(out) - 1
            current_key = normalize_label(m.group(1))
            current_extra = 0
            continue
        # 「成本价：」这种上行留空的标签行也算字段行，值在下一行时才能并进来
        pm = PURE_LABEL_RE.match(stripped)
        if pm:
            out.append(line)
            current_idx = len(out) - 1
            current_key = normalize_label(pm.group(0).strip(":："))
            current_extra = 0
            continue
        # 「备注」这种不带冒号的裸标签单独成行：它是下一个字段的开头，不是续行
        bare_key = normalize_label(stripped.rstrip(":：").strip())
        if bare_labels and bare_key in bare_labels and len(stripped) <= 12:
            out.append(line)
            current_idx = len(out) - 1
            current_key = bare_key
            current_extra = 0
            continue
        # 「备注蓝色」：标签和值连写、漏了冒号 —— 同样是下一个字段的开头。
        # 不认出来的话会被并进上一个字段（地址变成「……XX小区 备注蓝色」）。
        glued = _split_glued_label(stripped, bare_labels)
        if glued:
            label, rest = glued
            out.append(f"{label}:{rest}")
            current_idx = len(out) - 1
            current_key = normalize_label(label)
            current_extra = 0
            continue
        if not stripped:
            # 空行：段落结束，不再往上一个字段里并
            out.append(line)
            current_idx = -1
            current_key = ""
            continue
        if (
            current_idx < 0
            or HAS_COLON_RE.search(stripped)
            or len(stripped) > CONTINUATION_MAX_LEN
            or current_extra >= CONTINUATION_MAX_LINES
        ):
            out.append(line)
            continue
        # 续行里的多余空白压成一个空格（复制粘贴常带出双空格 / 制表符）
        out[current_idx] = out[current_idx].rstrip() + " " + re.sub(r"\s+", " ", stripped)
        current_extra += 1
        merged[current_key] = merged.get(current_key, 0) + 1
    return "\n".join(out), merged


def suspicious_hint(label: str, value: str, ftype: str = "text") -> str | None:
    """值里出现「像别的东西」的内容：多半是把别的字段一起并了进来，需要人工确认。"""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > LONG_VALUE_LEN:
        return f"「{label}」内容过长（{len(text)} 字），可能把多余文字并进来了"
    for hint, pattern in SUSPICIOUS_RULES:
        # 地址里本来就有省市区，不算可疑
        if hint == "看着像地址" and ftype == "address":
            continue
        if pattern.search(text):
            return f"「{label}」里{hint}，可能是换行导致并进了别的字段内容"
    return None


def clean_value(raw: str | None) -> str | None:
    """取值时若越界吃到了后一个字段标签，只保留标签之前的部分。"""
    if raw is None:
        return None
    value = raw.strip()
    m = TRAILING_LABEL_RE.search(value)
    if m:
        value = value[: m.start()].strip()
    value = value.strip().strip("，,;；:：").strip()
    # 「成本价：」这类上行时留空的字段，空值不应被当成有效内容
    if not value or PURE_LABEL_RE.match(value):
        return None
    return value


# ---------------------------------------------------------------- 截图 OCR 清洗
# 截图页脚/推荐位/按钮上的文字，不能当成商品名
OCR_NOISE_HINTS = (
    "这些宝贝", "宝贝超好卖", "免费领", "蚂蚁森林", "联系买家", "取消订单",
    "去发货", "速抢", "复制", "待卖家发货", "已优惠", "担保账户", "免息",
)
# 地址续行特征：以「号楼 / 栋 / 单元 / 室 / 层 …」开头
ADDRESS_TAIL_RE = re.compile(r"^(?:[0-9A-Za-z\-]{0,4})?(?:号楼|号院|栋|单元|室|层|楼|弄|幢|座)")
ADDRESS_HEAD_RE = re.compile(r"(?:省|市|自治区|区|县|旗)")
# OCR 会把「2号楼」里的数字认成大写字母块（"2 ARR号楼"），这类噪声在地址里去掉
OCR_UPPER_NOISE_RE = re.compile(r"\s+[A-Z]{2,}(?=\s|$)")


def clean_product_name(value: str | None) -> str | None:
    """截图里取到的商品名：去掉价格残留与页脚噪声，噪声一律判空。"""
    v = (value or "").strip().strip("|｜").strip()
    if not v or any(h in v for h in OCR_NOISE_HINTS):
        return None
    return v


# 截图上的界面元素被 OCR 认成字（「| KRABET, Ata?」这种）：一个中文都没有、还带问号 / 叹号。
# 商品名、规格里不会出现这种写法，判成噪声 —— 否则它会顶掉群消息里的真商品名
# （2026-10-03 那单：规格被这条顶成了「KRABET, Ata?」）。
OCR_UI_NOISE_RE = re.compile(r"[?？!！]")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")


# ---------------------------------------------------------------- 闲鱼买家编号
# 闲鱼给每个买家一个内部编号，模板 / 截图里常连编号一起带出来：
#   `韩晓云[4673]`、`…3号楼1单元403[4673]`、`15781432531-4673`
# 编号不属于姓名 / 地址 / 手机号的一部分，带进去会让人名、地址都对不上
# （2026-09-29 韩晓云那单：收货人、地址全错，只能人工纠错）。
BUYER_CODE_BRACKET_RE = re.compile(r"[\[【(（]\s*\d{1,8}\s*[\]】)）]")
# 无标签的收货信息：姓名行 / 手机号行（运营直接从闲鱼订单页复制时就是这个样子）
NAME_ONLY_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,9}"
    r"(?:\s*[\[【(（]\s*\d{1,8}\s*[\]】)）])?$"
)
PHONE_LINE_RE = re.compile(r"^1[3-9]\d{9}(?:[-_]\d{2,8})?$")
NAME_PHONE_LINE_RE = re.compile(
    r"^(?P<name>[\u4e00-\u9fa5A-Za-z·]{2,10}?)\s*"
    r"(?:[\[【(（]\s*\d{1,8}\s*[\]】)）])?\s*"
    r"(?P<phone>1[3-9]\d{9})(?:[-_]\d{2,8})?$"
)


def strip_buyer_code(value: str | None, key: str = "") -> str | None:
    """去掉闲鱼买家编号后缀：`韩晓云[4673]` → `韩晓云`、`…403[4673]` → `…403`。

    手机号上的 `-4673` 会在 `_to_phone` 取前 11 位时丢掉，这里只管姓名与地址。
    """
    v = str(value or "").strip()
    if not v:
        return None
    v = BUYER_CODE_BRACKET_RE.sub("", v).strip()
    # 取值规则到数字就停，编号会留下一个没闭合的残尾："韩晓云[" / "韩晓云[46"
    v = re.sub(r"[\[【(（]\s*\d*\s*[\]】)）]?\s*$", "", v).strip()
    if key in ("receiver_address", "receiver_name"):
        v = re.sub(r"[\s,，;；\-_]+$", "", v).strip()
    return v or None


def find_unlabeled_contact(text: str) -> tuple[dict[str, str], list[int]]:
    """没有标签的收货信息也能认出来：`姓名 / 手机号 / 地址` 连写。

    多数人按模板写「收货人:… 手机号码:… 地址信息:…」，但也有人直接从闲鱼订单页复制，
    就是几行裸信息（2026-09-29 韩晓云那单）：

        产品名称：N系列网球拍紫色+网球拍底座训练器和训练网球
        韩晓云[4673]
        15781432531-4673
        河北省石家庄市裕华区建通街道仓兴街15号盛邦花园3区3号楼1单元403[4673]

    以前这种写法整段收货信息都取不到，地址 / 收货人全得人工纠错。这里只认「连续行」
    这种强结构（姓名行不含冒号、手机号行是标准 11 位、地址行确实像地址），
    避免把商品名、备注之类的行误认成收货人。

    返回 (取到的字段, 命中的行号)，行号给 `tag_unlabeled_contact` 补标签用。
    """
    raw_lines = (text or "").split("\n")
    idxs = [i for i, line in enumerate(raw_lines) if line.strip()]
    for pos in range(len(idxs) - 2):
        i, j, k = idxs[pos], idxs[pos + 1], idxs[pos + 2]
        name, phone, addr = raw_lines[i].strip(), raw_lines[j], raw_lines[k].strip()
        if HAS_COLON_RE.search(name) or HAS_COLON_RE.search(phone):
            continue
        if not NAME_ONLY_RE.match(name) or not PHONE_LINE_RE.match(phone.strip().replace(" ", "")):
            continue
        if not looks_like_address(addr):
            continue
        values = {
            "receiver_name": strip_buyer_code(name, "receiver_name") or "",
            "receiver_phone": _to_phone(phone) or "",
            "receiver_address": strip_buyer_code(addr, "receiver_address") or "",
        }
        if all(values.values()):
            return values, [i, j, k]
    # 姓名与手机号写在同一行：`韩晓云 15781432531-4673`
    for pos in range(len(idxs) - 1):
        i, k = idxs[pos], idxs[pos + 1]
        m = NAME_PHONE_LINE_RE.match(raw_lines[i].strip())
        if not m:
            continue
        addr = strip_buyer_code(raw_lines[k], "receiver_address")
        if addr and looks_like_address(addr):
            values = {
                "receiver_name": strip_buyer_code(m.group("name"), "receiver_name") or "",
                "receiver_phone": _to_phone(m.group("phone")) or "",
                "receiver_address": addr,
            }
            return values, [i, k]
    return {}, []


def unlabeled_contact(text: str) -> dict[str, str]:
    """只取值（不带行号）：解析取值时的兜底，见 `find_unlabeled_contact`。"""
    return find_unlabeled_contact(text)[0]


# 无标签收货信息补标签时用的标签名，以及「文本里已经写了这个标签」的判断
UNLABELED_CONTACT_LABELS: dict[str, str] = {
    "receiver_name": "收货人",
    "receiver_phone": "手机号码",
    "receiver_address": "地址信息",
}
CONTACT_LABEL_PRESENT: dict[str, re.Pattern[str]] = {
    "receiver_name": re.compile(r"(?:收货人|收件人|姓名|联系人)\s*[:：]"),
    "receiver_phone": re.compile(r"(?:手机号码|手机号|电话号码|联系电话|联系方式)\s*[:：]"),
    "receiver_address": re.compile(r"(?:地址信息|收货地址|详细地址|地址)\s*[:：]"),
}


def tag_unlabeled_contact(text: str) -> tuple[str, int]:
    """给「不带标签的收货信息」补上标签：`韩晓云[4673]` → `收货人:韩晓云[4673]`。

    两件事一起办：
      1. 补完就交给常规的标签解析，不用再单独写一套取值逻辑；
      2. 这几行**不能再被当成上一个字段（商品名）的续行**并进去 ——
         否则商品名会把姓名、手机号一起吃掉，收货信息反而全丢（韩晓云那单就是这样）。

    文本里已经写了该标签的字段不动（避免覆盖运营手写的值）。
    返回 (补好标签的文本, 补了几个字段)。
    """
    src = text or ""
    values, spans = find_unlabeled_contact(src)
    if not values:
        return src, 0
    keep = {
        key: value
        for key, value in values.items()
        if not CONTACT_LABEL_PRESENT[key].search(src)
    }
    if not keep:
        return src, 0
    span_set = set(spans)
    out: list[str] = []
    for idx, line in enumerate(src.split("\n")):
        stripped = line.strip()
        if idx not in span_set or not stripped:
            out.append(line)
            continue
        if looks_like_address(stripped) and "receiver_address" in keep:
            out.append(f"{UNLABELED_CONTACT_LABELS['receiver_address']}:{stripped}")
            continue
        m = NAME_PHONE_LINE_RE.match(stripped)
        if m:
            # 姓名 + 手机号同一行：拆成两行补标签，避免取值越界
            if "receiver_name" in keep:
                out.append(f"{UNLABELED_CONTACT_LABELS['receiver_name']}:{m.group('name')}")
            if "receiver_phone" in keep:
                out.append(f"{UNLABELED_CONTACT_LABELS['receiver_phone']}:{m.group('phone')}")
            continue
        if PHONE_LINE_RE.match(stripped.replace(" ", "")) and "receiver_phone" in keep:
            out.append(f"{UNLABELED_CONTACT_LABELS['receiver_phone']}:{stripped}")
            continue
        if "receiver_name" in keep:
            out.append(f"{UNLABELED_CONTACT_LABELS['receiver_name']}:{stripped}")
            continue
        out.append(line)
    return "\n".join(out), len(keep)


def clean_address(value: str | None) -> str | None:
    v = strip_buyer_code(value, "receiver_address") or ""
    if not v:
        return None
    v = OCR_UPPER_NOISE_RE.sub(" ", v)
    v = re.sub(r"\s+", " ", v).strip()
    # 末尾还挂着一个别的字段的标签名（「……12号 备注」）：那是下一行的标签，不是地址的一部分
    changed = True
    while changed:
        changed = False
        for word in ADDRESS_TAIL_NOISE:
            if v.endswith(word) and len(v) > len(word):
                v = v[: -len(word)].strip()
                changed = True
    return v or None


def clean_ocr_value(fr: FieldRule, value: str | None) -> str | None:
    """截图取值后的清洗：洗不干净（噪声）就返回 None，让群消息里的值保留。"""
    if value is None:
        return None
    stripped = value.strip()
    if not _CJK_CHAR_RE.search(stripped) and OCR_UI_NOISE_RE.search(stripped):
        return None
    if fr.key == "product_name":
        return clean_product_name(value)
    if fr.type == "address":
        return clean_address(value)
    return value


def prefer_ocr_value(text_value: str | None, ocr_value: str | None) -> bool:
    """截图值是否应当覆盖群消息值：只有信息更全（更长/包含）才覆盖。

    OCR 经常把商品名截断（"…30W自带" 丢失 "线充电宝"），这种情况下保留模板里的完整名称。
    """
    t = (text_value or "").strip()
    o = (ocr_value or "").strip()
    if not o:
        return False
    if not t:
        return True
    if o.startswith(t) or t.startswith(o):
        return len(o) >= len(t)
    return len(o) > len(t)


def merge_address_lines(text: str) -> str:
    """OCR 常把一条地址折成两行（"…产业园2" + "号楼1单元438室"），这里合并回一行。"""
    if not text:
        return ""
    out: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        prev = out[-1].strip() if out else ""
        if (
            s
            and prev
            and ADDRESS_HEAD_RE.search(prev)
            and ADDRESS_TAIL_RE.match(s)
            and not re.search(r"[:：¥￥]", s)
        ):
            out[-1] = prev + s
            continue
        out.append(line)
    return "\n".join(out)


def _to_money(raw: str, sum_additive: bool = False) -> float | None:
    text = raw.replace("￥", "").replace("¥", "").replace("元", " ")
    if sum_additive:
        # 加法写法（「130➕80」/「130+80」/「130 加 80」）：整段相加，别只取第一个数
        m = ADDITIVE_MONEY_RE.search(text)
        if m:
            total = 0.0
            for part in ADDITIVE_SEP_RE.split(m.group(0)):
                piece = part.strip().replace(",", "")
                if not piece:
                    return None
                try:
                    total += float(piece)
                except ValueError:
                    return None
            return round(total, 2)
    m = MONEY_RE.search(text)
    if not m:
        return None
    try:
        return round(float(m.group(0).replace(",", "")), 2)
    except ValueError:
        return None


def _to_int(raw: str) -> int | None:
    m = INT_RE.search(raw.replace(",", ""))
    return int(m.group(0)) if m else None


def _to_phone(raw: str) -> str | None:
    m = PHONE_RE.search(raw.replace("-", "").replace(" ", ""))
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", raw)
    return digits[:20] or None


def _to_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    text = re.sub(r"\s+", " ", text)
    for fmt in DATETIME_FORMATS:
        for cand in (text, text.replace("年", "-").replace("月", "-").replace("日", "")):
            try:
                return datetime.strptime(cand.strip(), fmt)
            except ValueError:
                continue
    return None


def convert(value: str, ftype: str, sum_additive: bool = False) -> tuple[object | None, bool]:
    """返回值与是否转换成功。

    ``sum_additive``：金额写成加法时相加（「130➕80」→ 210），只对规则里配了 ``sum: true``
    的字段开（见 FieldRule.sum_additive）。
    """
    value = (value or "").strip()
    if not value:
        return None, False
    if ftype == "money":
        v = _to_money(value, sum_additive=sum_additive)
        return (v, v is not None)
    if ftype == "int":
        v = _to_int(value)
        return (v, v is not None)
    if ftype == "phone":
        v = _to_phone(value)
        return (v, v is not None)
    if ftype == "datetime":
        v = _to_datetime(value)
        return (v, v is not None)
    if ftype == "address":
        return (value.strip().strip(",，;；"), True)
    return (value, True)


def extract_kv_lines(text: str) -> dict[str, str]:
    """把 "标签: 值" 形式的行抽成 {归一化标签: 值}。"""
    result: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = LINE_KV_RE.match(line)
        if not m:
            continue
        key = normalize_label(m.group(1))
        if not key:
            continue
        result.setdefault(key, m.group(2).strip())
    return result


@dataclass
class ParseResult:
    platform: str = ""
    platform_label: str = ""
    fields: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)   # 字段 -> text/ocr/default
    missing_required: list[str] = field(default_factory=list)
    confidence: float = 0.0
    matched: bool = False
    # 需要人工确认的存疑点（比如字段值换行后并进来的内容看着不对）
    warnings: list[str] = field(default_factory=list)
    # 只是说明系统做过处理，不一定要人工确认（例如把换行的字段值合并回一行）
    notes: list[str] = field(default_factory=list)
    # 收货信息里「识别不确定 / 两处对不上」的字段（「地址（只有截图里有）」这种），
    # 会写进订单的待核对说明，并在回群通知里一并提醒 —— 地址错了会发错货。
    review_fields: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return (
            bool(self.missing_required or self.warnings or self.review_fields)
            or self.confidence < 0.5
        )

    def review_reason(self) -> str:
        """落库用的文字说明：存疑的排在前面，方便运营一眼看到要核对什么。"""
        parts = [f"存疑：{w}" for w in self.warnings] + [f"已合并：{n}" for n in self.notes]
        return "；".join(parts)


def detect_platform(text: str, rules: RuleSet) -> PlatformRule | None:
    for p in rules.platforms.values():
        d = p.detect
        if d.any_contains and any(k in text for k in d.any_contains):
            return p
        if d.all_contains and all(k in text for k in d.all_contains):
            return p
        for rx in d.regex:
            try:
                if re.search(rx, text):
                    return p
            except re.error:
                continue
    return rules.platform()


def _match_field(
    fr: FieldRule,
    text: str,
    kv: dict[str, str],
    patterns: list[str] | None = None,
    strict_money: bool = False,
) -> tuple[str | None, str | None]:
    """返回 (原始值, 命中来源)。patterns 不为空时用它替代默认正则。

    strict_money：截图上的金额必须有货币符号或小数点才算数。OCR 常把
    「运费 ¥0.00」认成乱码，旁边孤零零的数字（电量 45、界面坐标）就被当成
    运费 —— 实测真实订单：运费 0 被认成 4，回群通知里就多了「运费 ¥4.00」。
    群消息是手打的，不走这条约束。
    """
    for pattern in (patterns if patterns is not None else fr.patterns):
        try:
            m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        except (re.error, IndexError):
            continue
        if m and m.groups():
            if strict_money and not re.search(r"[¥￥]|\.", m.group(0)):
                continue
            val = clean_value(m.group(1))
            if val:
                return (val, "pattern")
    for label in fr.labels:
        key = normalize_label(label)
        if key and key in kv:
            val = clean_value(kv[key])
            if val:
                return (val, "label")
        # 容忍 "下单时间" / "买家昵称(微信)" 这类标签前后缀差异
        for k, v in kv.items():
            if not key or len(key) < 3 or len(k) < 3:
                continue
            if abs(len(key) - len(k)) > 2:
                continue
            if key == k or key in k or k in key:
                val = clean_value(v)
                if val:
                    return (val, "label")
    return (None, None)


def cross_validate(result: ParseResult, dual: dict[str, tuple[object, object]]) -> None:
    """群消息与订单截图双源交叉验证。

    同一个字段两处都取到了取值没必要二选一，先比对：
      · 手机号：谁完整（11 位）用谁；两边都完整却不同 -> 用群里手打的，标存疑；
      · 收货人 / 地址：说的是一回事（重合度够）就取更完整的那个；完全对不上 -> 用群里的，标存疑；
      · 金额：商品总价应等于「售价 + 运费」，对不上就把缺口摆给运营看（不自动填，宁缺勿错）。
    """
    for key, label in DUAL_SOURCE_FIELDS.items():
        text_val, ocr_val = dual.get(key, (None, None))
        if text_val is None or ocr_val is None:
            continue
        if str(text_val) == str(ocr_val):
            continue  # 两处一致：互为佐证，不必打扰
        if key == "receiver_address" and not looks_like_address(ocr_val):
            continue  # 截图那行压根不是地址（营销文案之类）：不算「两处不一致」，不打扰运营

        if key == "receiver_phone":
            text_ok, ocr_ok = _valid_phone(text_val), _valid_phone(ocr_val)
            if ocr_ok and not text_ok:
                result.fields[key] = ocr_val
                result.sources[key] = "ocr:cross-check"
            else:
                result.fields[key] = text_val
                result.sources[key] = "text:cross-check"
                if text_ok and ocr_ok:
                    result.warnings.append(
                        f"「{label}」群里写 {_norm_digits(text_val)}、截图是 {_norm_digits(ocr_val)}，"
                        f"两处不一致，已按群里的为准，请核对"
                    )
            continue

        # 地址单独判：OCR 认错一两个字（「岳麓区」认成「岳苯区」）不算两处不一致，
        # 按片段重合判断是不是同一个地址；真不一样才提示 —— 地址错了会发错货，
        # 但天天误报也会让人不再看提示。
        if key == "receiver_address":
            same = _address_same(text_val, ocr_val)
        else:
            same = _text_overlap(text_val, ocr_val) >= DUAL_MIN_OVERLAP
        if same:
            if len(str(ocr_val)) > len(str(text_val)):
                result.fields[key] = ocr_val
                result.sources[key] = "ocr:cross-check"
            else:
                result.fields[key] = text_val
                result.sources[key] = "text:cross-check"
        else:
            result.fields[key] = text_val
            result.sources[key] = "text:cross-check"
            if key == "receiver_address":
                result.warnings.append(
                    f"「地址」两处写的不一样：群里「{text_val}」、截图「{ocr_val}」，"
                    f"已按群里的为准，发货前请核对（地址错了会发错货）"
                )
            else:
                result.warnings.append(
                    f"「{label}」群里写「{text_val}」、截图里是「{ocr_val}」，"
                    f"两处说得不像同一个，已按群里的为准，请核对"
                )

    # 商品总价 ≈ 售价 + 运费：对不上通常是运费没写，或者两头写错了
    total = result.fields.get("total_amount")
    sale = result.fields.get("sale_amount") or result.fields.get("deal_amount")
    ship = result.fields.get("shipping_fee")
    if (
        isinstance(total, (int, float))
        and isinstance(sale, (int, float))
        and sale > 0
        and total >= sale
    ):
        gap = round(float(total) - float(sale), 2)
        if gap > 0.009:
            if ship is None:
                result.warnings.append(
                    f"商品总价 ¥{float(total):.2f} 比售价 ¥{float(sale):.2f} 多 ¥{gap:.2f}，"
                    f"运费多半是这个数（群里没写运费），核对后手工补上"
                )
            elif abs(float(sale) + float(ship) - float(total)) > 0.009:
                result.warnings.append(
                    f"商品总价 ¥{float(total):.2f} 不等于 售价 ¥{float(sale):.2f} + 运费 ¥{float(ship):.2f}，请核对"
                )


def parse_text(
    text: str,
    ocr_texts: Iterable[str] | None = None,
    rules: RuleSet | None = None,
    platform_key: str | None = None,
) -> ParseResult:
    rules = rules or get_rules()
    # 模板里所有字段的标签名：拿来判断「单独成行的裸标签」（如「备注」），别把它并入上一个字段
    all_labels = {
        normalize_label(label)
        for platform_rule in rules.platforms.values()
        for field_rule in platform_rule.fields
        for label in field_rule.labels
    }
    # 先纠一遍常见的人为错误（空格 / 全角 / 货币符号），后面的取值逻辑就不用管这些了
    text = apply_typo_fixes(normalize_lines(text or ""), rules)
    # 有人不按模板写「收货人:… 手机号码:… 地址信息:…」，直接贴几行裸信息：
    # 先补上标签，既能照常解析，也避免这几行被当成商品名的续行吃掉。
    text, _tagged = tag_unlabeled_contact(text)
    # 群里是复制粘贴的模板，字段值经常被人换行（商品名一行、规格一行）：
    # 先把紧跟在「标签:」后面的续行并回同一行，后面的取值逻辑就不用再管换行了。
    text, merged_labels = merge_continuation_lines(text, all_labels)
    platform = (
        rules.platforms.get(platform_key)
        or detect_platform(text, rules)
        or rules.platform()
    )
    if platform is None:
        return ParseResult()

    ocr_texts = [
        merge_address_lines(
            merge_continuation_lines(apply_typo_fixes(normalize_lines(t), rules), all_labels)[0]
        )
        for t in (ocr_texts or [])
        if t
    ]
    ocr_combined = "\n".join(ocr_texts)
    kv_main = extract_kv_lines(text)
    kv_ocr = extract_kv_lines(ocr_combined)
    # 有人不按模板写标签，直接贴闲鱼订单页的三行：姓名 / 手机号 / 地址（见 unlabeled_contact）
    unlabeled = unlabeled_contact(text)

    result = ParseResult(platform=platform.key, platform_label=platform.label, matched=True)
    required_total = 0
    required_hit = 0
    optional_total = 0
    optional_hit = 0
    dual_values: dict[str, tuple[object, object]] = {}   # 群消息 / 截图 各自取到的值，供交叉验证比对

    for fr in platform.fields:
        required_total += 1 if fr.required else 0
        optional_total += 0 if fr.required else 1

        # 这个字段的值原来被换行截成了多行：合并后检查一下有没有并错
        extra = 0
        for label in fr.labels:
            extra = max(extra, merged_labels.get(normalize_label(label), 0))

        raw, source = _match_field(fr, text, kv_main)
        where = "text"
        # 模板里没写标签、只有裸的三行收货信息（姓名 / 手机号 / 地址）：按位置兜底认出来
        if raw is None and fr.key in _RECEIVER_CONTACT_KEYS and unlabeled:
            fallback = unlabeled.get(fr.key)
            if fallback:
                raw, source, where = fallback, "unlabeled", "unlabeled"
        # 闲鱼买家编号（韩晓云[4673] / …403[4673]）不属于姓名、地址的一部分，先剥掉
        if raw is not None and fr.key in ("receiver_name", "receiver_address"):
            raw = strip_buyer_code(raw, fr.key)
        # 人名跨行（收货人:张 / 女士）合并后会带空格，而取值规则到空格就停了 —— 这时放宽到行尾
        if extra and raw is not None and rules.typo_on("name_spaces") and fr.key in TYPO_NAME_FIELDS:
            loose = loose_name_value(fr, text)
            if loose:
                raw, source = loose, "loose-name"
        text_raw = raw          # 群消息这一路的值：截图可能会覆盖它，但比对要用，先留一份
        ocr_raw: str | None = None
        if ocr_combined:
            ocr_raw, ocr_source = _match_field(
                fr,
                ocr_combined,
                kv_ocr,
                patterns=list(fr.ocr_patterns) + list(fr.patterns),
                # 金额（售价 / 运费 / 手续费）：截图里必须带 ¥ 或小数，孤立的数字不认
                strict_money=(fr.type == "money"),
            )
            # 截图噪声多，先清洗；洗不干净就当作没取到，保留群消息里的值
            ocr_raw = clean_ocr_value(fr, ocr_raw)
            # 地址专用闸门：截图里「地址」标签下面经常是营销文案（「领券狂补省得Duo! 马上和领」），
            # 空有标签、内容根本不像地址时不能当成地址 —— 改从截图里挑最像地址的那一行。
            if ocr_raw is not None and (fr.type == "address" or fr.key == "receiver_address"):
                if not looks_like_address(ocr_raw):
                    ocr_raw = best_address_line(ocr_texts)
            # 截图里的信息往往比群消息更完整（商品全称、订单编号），这类字段以截图为准
            if ocr_raw is not None and (raw is None or (fr.ocr_override and prefer_ocr_value(raw, ocr_raw))):
                raw, source, where = ocr_raw, ocr_source, "ocr"
        if raw is not None and fr.normalize == "strip_spaces":
            raw = re.sub(r"\s+", "", raw)

        if raw is None and fr.default:
            raw = fr.default
            source = "default"
            where = "default"

        if extra:
            result.notes.append(f"「{fr.label}」在原消息里跨了 {extra + 1} 行，已自动合并成一行")
            hint = suspicious_hint(fr.label or fr.key, str(raw or ""), fr.type)
            if hint:
                result.warnings.append(hint)

        value, ok = convert(raw, fr.type, sum_additive=fr.sum_additive)
        if ok and value is not None:
            result.fields[fr.key] = value
            result.sources[fr.key] = where + (f":{source}" if source else "")
            if text_raw is not None or ocr_raw is not None:
                dual_values[fr.key] = (
                    _convert_or_none(text_raw, fr),
                    _convert_or_none(ocr_raw, fr),
                )
            if fr.required:
                required_hit += 1
            else:
                optional_hit += 1
        else:
            if fr.required:
                result.missing_required.append(fr.label or fr.key)

    # 群消息和截图两处都有的字段，先比对再定夺（不一致的会写进 warnings 让运营核对）
    cross_validate(result, dual_values)

    # 金额交叉校验：运费比售价还高基本不可能 —— 多半是把闲鱼截图底部的广告
    # （「运费减188元」）当成运费了。宁缺勿错：留空并提示人工核对，别让错值进账。
    shipping_fee = result.fields.get("shipping_fee")
    sale_ref = result.fields.get("sale_amount") or result.fields.get("deal_amount")
    if (
        isinstance(shipping_fee, (int, float))
        and isinstance(sale_ref, (int, float))
        and sale_ref > 0
        and shipping_fee > sale_ref
    ):
        result.fields.pop("shipping_fee", None)
        result.sources.pop("shipping_fee", None)
        result.warnings.append(
            f"运费 ¥{float(shipping_fee):.2f} 比售价 ¥{float(sale_ref):.2f} 还高"
            f"（常见原因：把截图里的「运费减X元」广告当成了运费），已留空，请人工核对"
        )

    # 收货信息有没有「不确定」的（地址 / 收货人 / 手机号）：有的话订单要标待核对，
    # 回群通知里也要一并提醒 —— 发货前没核对，发错了就是售后问题。
    result.review_fields = pending_check_fields(result, dual_values)

    if required_total:
        score = required_hit / required_total * 0.7
        score += (optional_hit / optional_total if optional_total else 1.0) * 0.3
    else:
        score = (optional_hit / optional_total) if optional_total else 0.0
    result.confidence = round(min(1.0, score), 3)
    return result
