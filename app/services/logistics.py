"""菜鸟物流查询（LINK 网关）：按运单号查物流轨迹。

接口就是菜鸟 LINK 网关的 `link.do`，四个参数：
    msg_type            调用的接口名（如 CNTECH_LV_LOGISTICS_DETAIL_GET）
    logistic_provider_id 调用方 CPCODE
    logistics_interface 请求报文（JSON 字符串）
    data_digest         签名

签名算法（2026-10-01 用真实凭据实测确认，别再猜）：
    data_digest = base64(md5(logistics_interface + appSecret).digest())
拼错一律回 S02「验签或加签失败」；试过的其它拼法（秘钥在前、hex、hex 大写、两头夹秘钥）
无一例外都被打回。注意是 md5 的**二进制**再 base64，不是 md5 的十六进制字符串。

报文（logistics_interface）里的业务字段：
    mailNo         运单号
    cpCode         快递公司编码（SF / YTO / ZTO …），不传的话菜鸟自己猜
    phone          收件人手机号后 4 位（顺丰等隐私号必填，否则查不到）
    appCode        应用编码（**必填**，2026-10-13 实测：少了它一律 S13 / 500）

报文结构两种说法都出现过，谁对由线上回执说了算，所以**两种都发、自动兜底**：
    arg0 包裹：{"arg0": {"mailNo": ..., "cpCode": ..., "phone": ...}}
    平铺：    {"mailNo": ..., "cpCode": ..., "phone": ...}
2026-10-01 实测：包 arg0 那次网关回 500 SystemError，平铺回 S13；
2026-10-02 拿到的菜鸟服务端堆栈（LinkServiceSupport.getRequestParam →
BeanUtils.copyProperties → Assert.notNull("Source must not be null")）说明：S13 恰恰是
**报文里没有 arg0**，服务端 `requestJSON.get("arg0")` 取到 null 才炸 —— 换结构重算签名再发即可。
两边结论打架，就不再二选一：按设置的「报文结构」先发一次，没查到轨迹就自动换另一种结构
重发一次（签名按新报文原文重算），谁通算谁。

返回可能是 JSON 也可能是 XML（不同接口不一样），两种都解析。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from xml.etree import ElementTree

import requests

from ..config import settings

logger = logging.getLogger(__name__)

# 报文结构（logistics_interface 里业务字段怎么包）：菜鸟 LINK 网关两边说法打架 ——
# 2026-10-01 实测「包 arg0 回 500」，2026-10-02 拿到的服务端堆栈又显示「S13 就是没包 arg0」。
# 不二选一：按设置先发一种，没查到轨迹自动换另一种（签名按新报文重算）。
INTERFACE_STYLE_ARG0 = "arg0"  # {"arg0": {"mailNo": ...}}
INTERFACE_STYLE_FLAT = "flat"  # {"mailNo": ...}
INTERFACE_STYLES = (INTERFACE_STYLE_ARG0, INTERFACE_STYLE_FLAT)
INTERFACE_STYLE_LABELS = {
    INTERFACE_STYLE_ARG0: '包一层 arg0（{"arg0": {...}}）',
    INTERFACE_STYLE_FLAT: '平铺（{"mailNo": ...}）',
}

# 网关错误码 → 人话（运营看得懂，才知道该改哪个配置）
ERROR_CODES = {
    "S01": "菜鸟服务不可用（S01）",
    "S02": "验签失败：CAINIAO_APP_SECRET 与 CPCODE 不匹配（S02）",
    "S12": "请求参数非法：网关地址或报文不对（S12）",
    # 下面三条 2026-10-01 用真实凭据实测过，按实测现象写，别照抄文档：
    # S13 = 报文过了网关、进到菜鸟业务层，但服务端拿不到请求对象（Source must not be null）：
    #       按 2026-10-02 拿到的服务端堆栈，这是它取 arg0 取到 null 后 BeanUtils.copyProperties 炸的
    #       —— 系统已经自动换另一种报文结构重试过；两种都 S13 才是菜鸟侧没配好，找菜鸟对接人
    # S18 / S23 = 网关在鉴权阶段就拦下了（订阅 / 应用授权），是登录开放平台就能自己处理的
    "S13": "菜鸟侧拿不到请求数据（Source must not be null）：两种报文结构都试过了 —— "
    "要么是报文结构不对，要么是菜鸟那边没给这个应用配物流数据源，把「测试查询」的原文发给菜鸟对接人（S13）",
    "S18": "找不到接口订阅信息：这个应用（logistic_provider_id）没订阅该 msg_type 的接口，去开放平台订阅（S18）",
    "S23": "查询不到应用授权信息（can't find app through fromCode）：logistic_provider_id 不对，或这个应用没开通该服务（S23）",
    "500": "菜鸟网关系统异常（SystemError），稍后重试",
}

# =====================================================================
# 物流状态（2026-10-13 定稿）：页面显示与通知都认这一套
#
# 正向六个：待揽收 → 已揽收 → 运输中 → 派送中 → 待取件 → 已签收
# 异常三个：派送失败 / 退回中 / 已退回
#
# 状态是从菜鸟回的状态码（result.data.lastState）或最新一条轨迹描述里认出来的，
# 认不出就留空（页面显示「未识别」，不瞎猜）。
# =====================================================================
ST_PENDING_PICKUP = "pending_pickup"    # 待揽收
ST_ACCEPTED = "accepted"                # 已揽收
ST_TRANSIT = "transit"                  # 运输中
ST_DELIVERING = "delivering"            # 派送中
ST_WAIT_PICKUP = "waiting_pickup"       # 待取件（驿站 / 自提柜，等人来拿）
ST_SIGNED = "signed"                    # 已签收
ST_DELIVER_FAILED = "delivery_failed"   # 派送失败（异常分支）
ST_RETURNING = "returning"              # 退回中（异常分支）
ST_RETURNED = "returned"                # 已退回（异常分支）

STATUS_LABELS = {
    "": "未识别",
    ST_PENDING_PICKUP: "待揽收",
    ST_ACCEPTED: "已揽收",
    ST_TRANSIT: "运输中",
    ST_DELIVERING: "派送中",
    ST_WAIT_PICKUP: "待取件",
    ST_SIGNED: "已签收",
    ST_DELIVER_FAILED: "派送失败",
    ST_RETURNING: "退回中",
    ST_RETURNED: "已退回",
    # 老口径保留（订单上已经存在的状态，别让页面显示成 key）
    "intercepted": "拦截成功",
    "failed": "异常",
}

# 异常分支：这三个要通知到人（正向状态只是进度，异常才需要人去处理）
ABNORMAL_STATUSES = frozenset({ST_DELIVER_FAILED, ST_RETURNING, ST_RETURNED})


def status_label(status: str) -> str:
    """状态 → 中文（页面与通知统一走这里，别在各处自己写字典）。"""
    return STATUS_LABELS.get(str(status or "").strip(), str(status or "") or "未识别")


def is_abnormal(status: str) -> bool:
    """是不是异常分支（派送失败 / 退回中 / 已退回）。"""
    return str(status or "").strip() in ABNORMAL_STATUSES


# 各家快递的官网查询页：菜鸟这边没查到时，至少给人一个能查的地方
# （2026-10-13：顺丰 4 个真实单号实测都能查到，所以这只是**查不到时的兜底**，
#  不是「顺丰不支持」—— 真遇到某家菜鸟查不了，按下面这句给人指条路）
OFFICIAL_SITES = {
    "SF": ("顺丰速运", "https://www.sf-express.com/chn/sc/waybill/waybill-detail"),
    "ZTO": ("中通快递", "https://www.zto.com/GuestService/waybill"),
    "YTO": ("圆通速递", "https://www.yto.net.cn/"),
    "YD": ("韵达快递", "https://www.yundaex.com/cn/query/"),
    "STO": ("申通快递", "https://www.sto.cn/"),
    "JD": ("京东物流", "https://www.jdl.com/"),
    "DBL": ("德邦快递", "https://www.deppon.com/"),
    "EMS": ("邮政快递", "https://www.ems.com.cn/"),
}


def official_site(cp_code: str = "", cp_name: str = "") -> tuple[str, str]:
    """（快递公司名, 官网查询地址）认不出就返回 ("", "")。"""
    code = (cp_code or "").strip().upper()
    hit = OFFICIAL_SITES.get(code)
    if hit:
        return hit
    name = (cp_name or "").strip()
    for key, (label, url) in OFFICIAL_SITES.items():
        if label == name or label in name or name in label:
            return (label, url)
    return ("", "")


# 菜鸟轨迹状态 → 本项目的物流状态（见 refund.TRACKING_STATUS_LABELS）
STATUS_MAP = {
    "TAKEING": "transit",
    "TAKING": "transit",
    "TRANSIT": "transit",
    "TRANSPORT": "transit",
    "ARRIVAL": "transit",
    "DEPARTURE": "transit",
    # 待揽收：单子建了、货还没交给快递
    "CREATED": "pending_pickup",
    "NOT_SEND": "pending_pickup",
    "NOTSEND": "pending_pickup",
    "READY": "pending_pickup",
    "WAIT_SEND": "pending_pickup",
    # 已揽收：快递把货拿走了
    "GOT": "accepted",
    "ACCEPTED": "accepted",
    "ACCEPT": "accepted",
    "COLLECTED": "accepted",
    "PICKED": "accepted",
    "PICKUP": "accepted",
    "DELIVERING": "delivering",
    "SENDING": "delivering",
    # 待取件：到了驿站 / 自提柜，等人来拿
    "WAIT_PICKUP": "waiting_pickup",
    "WAITPICKUP": "waiting_pickup",
    "SELF_PICKUP": "waiting_pickup",
    "STATION": "waiting_pickup",
    "SIGNED": "signed",
    "SIGN": "signed",
    "RECEIVED": "signed",
    "RETURNING": "returning",
    "RETURNED": "returned",
    "RETURN_SIGNED": "returned",
    "INTERCEPT": "intercepted",
    "INTERCEPTED": "intercepted",
    "STOPPED": "intercepted",
    # 派送失败（异常分支）
    "DELIVERY_FAILED": "delivery_failed",
    "DELIVER_FAILED": "delivery_failed",
    "REJECT": "delivery_failed",
    "REJECTED": "delivery_failed",
    "FAILED": "failed",
    "EXCEPTION": "failed",
    "PROBLEM": "failed",
}

# 轨迹列表可能藏在这些字段里（菜鸟各接口叫法不一样，都试一遍）
_TRACE_KEYS = (
    "traceList",
    "traces",
    "traceDetailList",
    "logisticsTraceDetailList",
    "mailDetailList",
    "detailList",
    "details",
    "nodes",
    "list",
    "detail",  # 菜鸟 LINK 网关：result.data.detail（2026-10-13 实测）
    "data",
    "items",
)
_TIME_KEYS = ("time", "acceptTime", "timeStr", "dateTime", "opTime", "statusTime")
# opMessage / cityName 是菜鸟 detail 节点的叫法（2026-10-13 实测确认），少了这两条
# 轨迹节点会被整条丢掉 —— 系统只认 desc / station 那套，等于永远解析不出轨迹
_DESC_KEYS = (
    "desc", "statusDesc", "standerdDesc", "remark", "content", "action",
    "statusName", "opMessage",
)
_PLACE_KEYS = ("station", "nodeName", "orgName", "site", "city", "areaName", "cityName")


# 按运单号认快递公司：(页面显示名, 菜鸟 cpCode, 单号规则)。
# 带字母前缀的是各家自己的面单号（SF / YT / JT …），命中就很准；
# 纯数字的只能按业内常见号段猜（中通 68/75/78/79、韵达 31/43 …），
# 猜不出来就返回空串，页面显示「未识别」—— 宁可不显示，也不瞎编一家。
CP_RULES: tuple[tuple[str, str, str], ...] = (
    ("顺丰速运", "SF", r"^SF\d{10,}$"),
    ("圆通速递", "YTO", r"^YT\d{10,}$"),
    ("中通快递", "ZTO", r"^(ZTO\d{10,}|(68|75|78|79)\d{10,13})$"),
    ("韵达快递", "YUNDA", r"^(31|43|46|19)\d{11,13}$"),
    ("申通快递", "STO", r"^(STO\d{10,}|(66|77|88)\d{10,13})$"),
    ("京东物流", "JD", r"^JD[VX]?\d{8,}$"),
    ("极兔速递", "JT", r"^JT\d{10,}$"),
    ("德邦快递", "DBL", r"^DPK\d{8,}$"),
    ("邮政EMS", "EMS", r"^(E[MZS]\d{9,}|[A-Z]{2}\d{9}[A-Z]{0,2})$"),
    ("邮政快递包裹", "EMS", r"^[19]\d{12}$"),
)


def _match_cp(mail_no: str) -> tuple[str, str] | None:
    no = (mail_no or "").strip().upper().replace(" ", "").replace("-", "")
    if not no:
        return None
    for name, cp_code, pattern in CP_RULES:
        if re.match(pattern, no):
            return name, cp_code
    return None


def guess_company(mail_no: str) -> str:
    """按运单号规则认快递公司，返回显示名（顺丰速运 / 圆通速递 …）；认不出返回 ''。"""
    hit = _match_cp(mail_no)
    return hit[0] if hit else ""


def guess_cp_code(mail_no: str) -> str:
    """按运单号猜菜鸟 cpCode（SF / YTO / ZTO …），查轨迹时带上更准；猜不出返回 ''。"""
    hit = _match_cp(mail_no)
    return hit[1] if hit else ""


def is_enabled() -> bool:
    """没配齐就当没开：网关地址、接口名、秘钥、CPCODE 缺一不可。"""
    return bool(
        settings.cainiao_enabled
        and settings.cainiao_gateway_url
        and settings.cainiao_msg_type
        and settings.cainiao_app_secret
        and settings.cainiao_cp_code
    )


def sign(content: str, secret: str) -> str:
    """data_digest：base64(md5(报文 + 秘钥)) —— 注意是 md5 的二进制再 base64。"""
    return base64.b64encode(hashlib.md5((content + secret).encode("utf-8")).digest()).decode("ascii")


def _style(style: str | None = None) -> str:
    """取报文结构：没传就用设置里的（CAINIAO_INTERFACE_STYLE），认不出的按 arg0。"""
    val = (style if style is not None else (settings.cainiao_interface_style or "")).strip().lower()
    return val if val in INTERFACE_STYLES else INTERFACE_STYLE_ARG0


def _other_style(style: str) -> str:
    return INTERFACE_STYLE_FLAT if style == INTERFACE_STYLE_ARG0 else INTERFACE_STYLE_ARG0


def build_interface(
    mail_no: str, cp_code: str = "", phone: str = "", style: str | None = None
) -> str:
    """请求报文：mailNo / cpCode / phone 三个业务字段 + 固定带上应用编码。

    appCode **必须带**（2026-10-13 用真实单号实测确认）：少了它，菜鸟服务端
    取 arg0 拿到 null —— arg0 报文直接回 500 SystemError，平铺报文回
    S13「Source must not be null」；带上之后同一单号当场就查到了轨迹。

    收件人手机号后 4 位两个名字都发（phone / receiverPhone）：菜鸟各接口叫法不一，
    顺丰这类隐私号必须带，否则查不到轨迹。

    结构按 style：arg0 -> {"arg0": {...}}；flat -> {...}。详见模块头部说明。
    """
    body: dict[str, str] = {"mailNo": (mail_no or "").strip()}
    app_code = (settings.cainiao_app_code or "").strip()
    if app_code:
        body["appCode"] = app_code
    if (cp_code or "").strip():
        body["cpCode"] = cp_code.strip()
    tail = (phone or "").strip()
    if tail:
        body["phone"] = tail
        body["receiverPhone"] = tail
    payload: dict = {"arg0": body} if _style(style) == INTERFACE_STYLE_ARG0 else body
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def phone_tail(phone: str) -> str:
    """取手机号后 4 位：菜鸟查隐私号轨迹要的是后 4 位，不是完整号码。"""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if len(digits) >= 4 else ""


def _find_traces(node) -> list[dict]:
    """在响应里找轨迹列表：菜鸟各接口层级不一样，按常见字段名递归找。"""
    if isinstance(node, dict):
        # 当前层就有已知字段，且里面是列表 → 就是它
        for key in _TRACE_KEYS:
            val = node.get(key)
            if isinstance(val, list) and val and isinstance(val[0], (dict, str)):
                return val
        for val in node.values():
            found = _find_traces(val)
            if found:
                return found
    elif isinstance(node, list):
        # 列表里每个元素都像轨迹节点 → 直接用
        if node and all(isinstance(x, dict) for x in node) and _looks_like_trace(node[0]):
            return node
        for item in node:
            found = _find_traces(item)
            if found:
                return found
    return []


def _looks_like_trace(item: dict) -> bool:
    keys = {k.lower() for k in item}
    return bool(keys & {k.lower() for k in (*_TIME_KEYS, *_DESC_KEYS)})


def _pick(item: dict, keys: tuple[str, ...]) -> str:
    low = {str(k).lower(): v for k, v in item.items()}
    for key in keys:
        val = low.get(key.lower())
        if val not in (None, ""):
            return str(val)
    return ""


def parse_traces(payload) -> list[dict[str, str]]:
    """把响应里的轨迹节点整理成 [{time, desc, station, status}]，按时间倒序（最新在最上）。"""
    raw = _find_traces(payload)
    nodes: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            nodes.append({"time": "", "desc": item, "station": "", "status": ""})
            continue
        if not isinstance(item, dict):
            continue
        desc = _pick(item, _DESC_KEYS)
        if not desc:
            continue
        nodes.append(
            {
                "time": _pick(item, _TIME_KEYS),
                "desc": desc,
                "station": _pick(item, _PLACE_KEYS),
                "status": _pick(item, ("status", "statusCode", "actionCode")),
            }
        )
    # 有时间的按时间倒序排，没时间的保持原样
    timed = [n for n in nodes if n["time"]]
    untimed = [n for n in nodes if not n["time"]]
    timed.sort(key=lambda n: n["time"], reverse=True)
    return timed + untimed


def _expand_data(body: dict) -> None:
    """拆 result.data 那层：菜鸟把轨迹塞在 data 里，而 data 又是一层 JSON 字符串。

    不拆这层，``parse_traces`` 找不到任何列表 —— 明明查到了也显示「没轨迹」
    （2026-10-13 实测：同一单号拆开后立刻拿到全部节点）。
    """
    for key in ("data", "result"):
        val = body.get(key)
        if isinstance(val, str) and val.strip().startswith(("{", "[")):
            try:
                body[key] = json.loads(val)
            except json.JSONDecodeError:
                pass


def _parse_response(text: str) -> tuple[bool, str, dict]:
    """解析网关响应：成功返回 (True, "", 报文 dict)；失败返回 (False, 人话错误, {})。"""
    text = (text or "").strip()
    if not text:
        return False, "菜鸟网关返回空响应", {}
    payload: dict = {}
    if text.startswith("<"):  # XML（部分接口走 XML）
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return False, f"菜鸟返回了看不懂的内容：{text[:120]}", {}
        payload = {child.tag: (child.text or "") for child in root}
        success = str(payload.get("success", "")).lower() == "true"
        if success:
            return True, "", payload
        code = str(payload.get("errorCode", "")).strip()
        msg = str(payload.get("errorMsg", "")).strip()
        return False, _explain(code, msg), {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False, f"菜鸟返回了看不懂的内容：{text[:120]}", {}
    if not isinstance(payload, dict):
        return False, f"菜鸟返回了看不懂的内容：{text[:120]}", {}
    # 业务体在 result 里：成功是 {"result": {"code": "0", "data": "..."}}，
    # 失败是 {"result": {"success": false, "errorCode": "S13", "errorMsg": "..."}}
    # —— 只盯着响应顶层看，失败会被当成「成功但没轨迹」（2026-10-13 实测）。
    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    success = str(body.get("success", payload.get("success", ""))).lower() == "true"
    code = str(body.get("errorCode") or payload.get("errorCode") or "").strip()
    msg = str(body.get("errorMsg") or payload.get("errorMsg") or "").strip()
    if not code:
        # 菜鸟的失败也可能只给业务码：result.code = S01 / S13 / S18 …
        biz_code = str(body.get("code") or "").strip()
        if biz_code and biz_code.upper() not in ("0", "200"):
            code = biz_code.upper()
            msg = msg or str(body.get("msg") or body.get("message") or "")
    if success or (not code and not msg):
        _expand_data(body)
        return True, "", payload
    return False, _explain(code, msg), {}


def _explain(code: str, msg: str) -> str:
    """错误码翻成人话，附带上菜鸟原文，方便对线客服。"""
    known = ERROR_CODES.get(code, "")
    raw = msg.split("traceId:")[0].strip()
    if known and raw:
        return f"{known}（{raw}）"
    return known or raw or "菜鸟查询失败"


def query(
    mail_no: str,
    *,
    cp_code: str = "",
    phone: str = "",
    timeout: int | None = None,
) -> dict:
    """查一条运单的轨迹。

    返回 {"ok": bool, "msg": str, "status": str, "nodes": [{time, desc, station, status}]}
    —— status 是本项目的物流状态（signed / delivering …），查不到就是空串。
    """
    mail_no = (mail_no or "").strip()
    if not mail_no:
        return {"ok": False, "msg": "没有运单号，查不了", "status": "", "nodes": []}
    if not is_enabled():
        return {
            "ok": False,
            "msg": "没启用菜鸟物流查询：去「系统设置 → 菜鸟物流查询」配置（网关地址、接口名、秘钥、CPCODE）",
            "status": "",
            "nodes": [],
        }

    res = _call(mail_no, cp_code=cp_code, phone=phone, timeout=timeout)
    return {key: res[key] for key in ("ok", "msg", "status", "nodes")}


def _try_digest(style: str, out: dict) -> dict:
    """一次尝试的摘要（给页面展示：哪种报文结构、回了什么）。"""
    return {
        "style": style,
        "label": INTERFACE_STYLE_LABELS.get(style, style),
        "http_status": out.get("http_status", 0),
        "msg": out.get("msg", ""),
        "raw": (out.get("raw", "") or "")[:400],
        "elapsed_ms": out.get("elapsed_ms", 0),
    }


def _call(
    mail_no: str,
    *,
    cp_code: str = "",
    phone: str = "",
    timeout: int | None = None,
) -> dict:
    """往菜鸟发请求（查物流 / 设置页测试查询共用）。

    先按设置里的「报文结构」发一次；**没查到轨迹就自动换另一种结构重发一次**
    （签名按新报文原文重算）—— arg0 包裹和平铺哪种对，让线上回执说了算。
    网络层就失败的（超时 / 连不上）不重试：换结构也还是连不上。
    """
    preferred = _style()
    out = _try_call(mail_no, cp_code=cp_code, phone=phone, timeout=timeout, style=preferred)
    out["tried"] = [_try_digest(preferred, out)]
    # 三种情况：查到轨迹 / 网络层就失败（连不上，换结构也没用）/ **网关没报错**
    # —— 网关没报错但没轨迹 = 真没轨迹（刚下单还没揽收），换结构也不会凭空多出节点，
    # 那就别再浪费一次调用（2026-10-13：菜鸟按次算，空结果翻倍刷没意义）
    if out["ok"] or not out["http_status"]:
        return out

    alt = _other_style(preferred)
    alt_out = _try_call(mail_no, cp_code=cp_code, phone=phone, timeout=timeout, style=alt)
    out["tried"].append(_try_digest(alt, alt_out))
    if alt_out["ok"] and alt_out["nodes"]:
        alt_out["tried"] = out["tried"]
        alt_out["elapsed_ms"] = alt_out["elapsed_ms"] + out["elapsed_ms"]
        return alt_out
    # 两种都不行：留首选那次的现场（报文和原文最有价值），把另一种的结果并到提示里
    out["msg"] = _merge_fail_msg(out, alt_out, alt)
    return out


def _merge_fail_msg(first: dict, second: dict, alt_style: str) -> str:
    label = INTERFACE_STYLE_LABELS.get(alt_style, alt_style)
    msg2 = (second.get("msg") or "").strip()
    msg1 = (first.get("msg") or "").strip() or "菜鸟查询失败"
    if not msg2 or msg2 == msg1:
        return f"{msg1}（已换「{label}」报文重试，同样失败）"
    return f"{msg1}；换「{label}」报文重试：{msg2}"


def _try_call(
    mail_no: str,
    *,
    cp_code: str = "",
    phone: str = "",
    timeout: int | None = None,
    style: str = "",
) -> dict:
    """真正往菜鸟发一次请求（单次，报文结构由 style 定）。

    返回的字典除了 {ok, msg, status, nodes} 还带现场信息：request（发出的报文）、
    http_status、raw（菜鸟回的原文）、elapsed_ms —— 排查 S02 / S18 这类回执时，
    光有一句人话不够，得看发出去的报文和菜鸟回的原文。
    """
    out: dict = {
        "ok": False,
        "msg": "",
        "status": "",
        "nodes": [],
        "mail_no": mail_no,
        "cp_code": cp_code,
        "phone": phone,
        "request": {},
        "request_url": settings.cainiao_gateway_url,
        "http_status": 0,
        "raw": "",
        "elapsed_ms": 0,
        "interface_style": style,
    }

    content = build_interface(mail_no, cp_code=cp_code, phone=phone, style=style)
    form = {
        "msg_type": settings.cainiao_msg_type,
        "logistic_provider_id": settings.cainiao_cp_code,
        "logistics_interface": content,
        "data_digest": sign(content, settings.cainiao_app_secret),
    }
    if (settings.cainiao_app_code or "").strip():  # 一般不发，配了才发
        form["app_code"] = settings.cainiao_app_code.strip()
    out["request"] = form

    started = time.monotonic()
    try:
        resp = requests.post(
            settings.cainiao_gateway_url,
            data=form,
            timeout=timeout or settings.cainiao_timeout_sec or 15,
        )
    except requests.Timeout:
        out["msg"] = "菜鸟查询超时（网络慢或网关没响应）"
        return out
    except Exception as exc:  # noqa: BLE001  网络类异常不该把页面打挂
        logger.warning("菜鸟物流查询请求失败：%s", exc)
        out["msg"] = f"菜鸟查询失败：{exc}"
        return out

    out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    out["http_status"] = getattr(resp, "status_code", 0) or 0
    out["raw"] = (resp.text or "")[:4000]

    ok, msg, payload = _parse_response(resp.text)
    if not ok:
        out["msg"] = msg or f"菜鸟返回 HTTP {out['http_status']}"
        return out

    nodes = parse_traces(payload)
    out["ok"] = True
    out["nodes"] = nodes
    out["status"] = map_status(payload, nodes)
    out["msg"] = "" if nodes else "菜鸟没返回轨迹（单号可能有误，或该接口还没订阅）"
    return out


def query_debug(
    mail_no: str,
    *,
    cp_code: str = "",
    phone: str = "",
    timeout: int | None = None,
) -> dict:
    """设置页「测试查询」：跟 query 同一条链路，但把请求报文与菜鸟原文一起带回来。

    跟 query 的区别只有两点：**配置不全也照发**（就是来验配置的），
    以及返回里多带现场信息（供页面展示）。
    """
    mail_no = (mail_no or "").strip()
    if not mail_no:
        return {
            "ok": False,
            "msg": "没有运单号，查不了",
            "status": "",
            "nodes": [],
            "request": {},
            "raw": "",
            "http_status": 0,
            "elapsed_ms": 0,
            "mail_no": "",
            "cp_code": cp_code,
            "phone": phone,
        }
    if not (settings.cainiao_gateway_url or "").strip():
        return {
            "ok": False,
            "msg": "还没填网关地址（正式环境 https://link.cainiao.com/gateway/link.do）",
            "status": "",
            "nodes": [],
            "request": {},
            "raw": "",
            "http_status": 0,
            "elapsed_ms": 0,
            "mail_no": mail_no,
            "cp_code": cp_code,
            "phone": phone,
        }
    return _call(mail_no, cp_code=cp_code, phone=phone, timeout=timeout)


def _deep_status(payload) -> str:
    """递归找状态码：菜鸟把它放在 result.data.lastState，不在响应顶层。"""
    if not isinstance(payload, dict):
        return ""
    for key in (
        "lastState", "status", "statusCode", "state", "bizStatus", "logisticsStatus"
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().upper()
    for val in payload.values():
        found = _deep_status(val) if isinstance(val, dict) else ""
        if found:
            return found
    return ""


def status_from_desc(desc: str) -> str:
    """从最新一条轨迹的描述里认状态（菜鸟不给状态码时走这条）。

    **顺序要紧**：先认具体的（派送失败 / 已退回 / 待取件），再认笼统的
    （派送 / 退回），否则「派送失败」会被前面的「派送」吃掉。
    """
    text = str(desc or "")
    for word, mapped in (
        ("派送失败", ST_DELIVER_FAILED),
        ("投递失败", ST_DELIVER_FAILED),
        ("无法派送", ST_DELIVER_FAILED),
        ("拒收", ST_DELIVER_FAILED),
        ("已退回", ST_RETURNED),
        ("退回签收", ST_RETURNED),
        ("退回", ST_RETURNING),
        ("拦截", "intercepted"),
        ("待取件", ST_WAIT_PICKUP),
        ("请取件", ST_WAIT_PICKUP),
        ("自提", ST_WAIT_PICKUP),
        ("驿站", ST_WAIT_PICKUP),
        ("待揽收", ST_PENDING_PICKUP),
        ("等待揽收", ST_PENDING_PICKUP),
        ("已下单", ST_PENDING_PICKUP),
        ("揽收", ST_ACCEPTED),
        ("已收件", ST_ACCEPTED),
        ("已收取", ST_ACCEPTED),
        ("签收", ST_SIGNED),
        ("派送", ST_DELIVERING),
        ("投递", ST_DELIVERING),
    ):
        if word in text:
            return mapped
    return ""


def map_status(payload: dict, nodes: list[dict] | None = None) -> str:
    """把菜鸟返回的状态映射成本项目的物流状态（signed / delivering / transit …）。"""
    raw = _deep_status(payload)
    if raw:
        return STATUS_MAP.get(raw, "")
    if nodes:
        raw = (nodes[0].get("status") or "").strip().upper()
        if raw:
            return STATUS_MAP.get(raw, "")
        # 没有状态码就从第一条（最新）轨迹的描述里认关键字
        return status_from_desc(nodes[0].get("desc")) or ST_TRANSIT
    return ""
