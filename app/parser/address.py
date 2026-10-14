"""地址判定：一个值像不像收货地址（群消息解析与截图 OCR 共用同一套口径）。

只做**形状**判断，不管来源：什么内容算地址、什么内容一眼不是地址。
两条链路（文本解析 parser.engine、截图 OCR parser.ocr）必须用同一套规则，
否则会出现「OCR 层当成地址、解析层又否掉」这种自相矛盾的行为。

真实事故（2026-09-28）：
  群里地址写「湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科技园」，
  截图 OCR 出来的「地址」是闲鱼截图上的营销文案「领券狂补省得Duo! 马上和领」。
  营销文案里带个「省」字（省得），只看关键字就会被当成地址，所以这里同时排噪声。
"""
from __future__ import annotations

import re
from typing import Iterable

# 行政区划：真的地址一定有（省 / 市 / 区 / 县 / 镇 / 街道…）
ADDRESS_REGION_RE = re.compile(
    r"(?:省|自治区|市|区|县|旗|镇|乡|街道|开发区|工业园|园区|新区|大厦|小区|公寓)"
)
# 道路门牌：没有行政区划时的第二条判据（路 / 街 / 号 / 栋 / 单元 / 室…）
ADDRESS_DETAIL_RE = re.compile(
    r"(?:路|街|巷|弄|号|栋|幢|单元|室|楼|层|村|组|苑|广场|花园|号楼|号院)"
)
# 带「号」但不是地址的写法：订单编号 / 手机号码 / 快递单号…
ADDRESS_ID_WORDS = ("编号", "号码", "单号", "账号", "邮编", "批号", "券号")
# 截图上的营销 / 按钮 / 状态文案：这类文字不会是地址，出现就否掉。
# 「领券狂补省得Duo! 马上领」就是这么被挡下的。
# 注意：**括号不算噪声**。真地址里带括号的太多（「…15号(满天下网咖这栋)704」），
# 一律否掉会把整条地址丢掉，运营只能手打（2026-10-03 那单就是这样）。
ADDRESS_NOISE_RE = re.compile(
    r"(?:领券|优惠券|狂补|补贴|省得|马上|立即|点击|抢购|优惠|满减|包邮|待发货|已发货"
    r"|确认收货|评价|客服|咨询|退款|退货|下单|付款|秒杀|限时|返现|正品保障|包退|换货"
    r"|价|金额|担保|账户|账号|余额|红包|账单"
    r"|[!！?？]|[￥¥$])"
)
# 括号里的内容：楼栋 / 驿站名（「(满天下网咖这栋)」）是地址的一部分，放行；
# 写的是界面文案（「(在支付宝担保账户中)」）才判成噪声 —— 这才是当初要挡的东西。
ADDRESS_BRACKET_RE = re.compile(r"[（(]([^）)]{1,30})[）)]")
ADDRESS_BRACKET_NOISE_RE = re.compile(
    r"担保|账户|支付宝|余额|红包|优惠|券|活动|补贴|价格|价"
)
# 金额写法（168.00 / 12.5）：地址里的门牌号是整数，带小数点的一律不像地址。
# 实测线上截图：OCR 把「成交价 (在支付宝担保账户中) x168.00」整条认了出来，
# 里面的「区」（成交价认成「成区价」）让它混过了地址判定。
ADDRESS_MONEY_RE = re.compile(r"\d+\.\d{1,2}")
# 地址长度区间：太短不像地址，太长多半并进了别的内容
ADDRESS_MIN_LEN = 6
ADDRESS_MAX_LEN = 80


def looks_like_address(value: str | None) -> bool:
    """这个值像不像一条收货地址。

    宁可判「不像」也不要把错值写进订单 —— 漏了地址运营能一眼看到空并补上，
    写错地址则要等发货才发现问题。判定：
      · 长度 6~80 字；
      · 中文占一半以上（OCR 认出的英文 / 符号串不是地址）；
      · 不含营销、按钮类文案与感叹号；
      · 括号里是界面文案（担保账户 / 优惠券）才否，楼栋名（「(满天下网咖这栋)」）放行
        （2026-10-03：括号一律否掉，把「…15号(满天下网咖这栋)704」整条丢了，地址变空）；
      · 带行政区划，或带道路门牌且不是「编号 / 号码」这类写法。
    """
    v = re.sub(r"\s+", "", str(value or ""))
    if not ADDRESS_MIN_LEN <= len(v) <= ADDRESS_MAX_LEN:
        return False
    if ADDRESS_NOISE_RE.search(v):
        return False
    if ADDRESS_MONEY_RE.search(v):
        return False
    # 括号里是界面文案（担保账户 / 优惠券…）才否；楼栋名照常放行
    if any(ADDRESS_BRACKET_NOISE_RE.search(inner) for inner in ADDRESS_BRACKET_RE.findall(v)):
        return False
    han = sum(1 for ch in v if "\u4e00" <= ch <= "\u9fa5")
    if not v or han / len(v) < 0.5:
        return False
    # 英文（含拼音被 OCR 认出来的）占了一大半：多半是截图上的其它文案。
    # 但楼宇名带英文是正常的（「…SOHO现代城A座」），所以只在占比很高时才否
    latin = sum(1 for ch in v if ch.isascii() and ch.isalpha())
    if latin / len(v) > 0.4:
        return False
    if ADDRESS_REGION_RE.search(v):
        return True
    if not ADDRESS_DETAIL_RE.search(v):
        return False
    return not any(word in v for word in ADDRESS_ID_WORDS)


# 直辖市：地址里没有「省」也正常（上海市浦东新区…）
_MUNICIPALITIES = ("北京市", "上海市", "天津市", "重庆市")


def address_complete(value: str | None) -> bool:
    """这条地址写得完不完整（省 / 市 / 区县 三级都在）。

    「像地址」说明它确实是地址，「完整」说明它认全了 —— 只有截图这一个来源时，
    认全的（浙江省绍兴市柯桥区…）可以直接用，缺一级的多半是 OCR 只认出半截，
    那种要标「待核对」让运营看一眼，不能直接照着发货。
    """
    v = re.sub(r"\s+", "", str(value or ""))
    if len(v) < 12:
        return False
    province = any(v.startswith(city) for city in _MUNICIPALITIES) or bool(
        re.search(r"(?:省|自治区)", v)
    )
    city = bool(re.search(r"(?:市|地区|自治州|盟)", v))
    district = bool(re.search(r"(?:区|县|旗|镇|乡|街道|路|街)", v))
    return province and city and district


def best_address_line(texts: Iterable[str]) -> str | None:
    """从截图文本里挑一条最像地址的行。

    「地址信息」标签后面那一行的 OCR 结果常常不是地址（营销文案 / 按钮），
    真地址在别的行上 —— 这时按「最像地址」挑最长的一条，挑不到就返回 None。
    """
    best: str | None = None
    for text in texts or []:
        for line in str(text or "").split("\n"):
            line = re.sub(
                r"^(?:地址信息|收货地址|收件地址|详细地址|地址|所在地区)\s*[:：]?\s*", "", line.strip()
            ).strip()
            if not line:
                continue
            # 续行合并后真地址可能跟营销文案挤在同一行：按空白 / 逗号切开来逐个看
            for piece in [line, *re.split(r"[\s，,]+", line)]:
                if not looks_like_address(piece):
                    continue
                if best is None or len(piece) > len(best):
                    best = piece
    return best
