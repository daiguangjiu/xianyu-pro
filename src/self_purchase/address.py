"""收货地址簿（个人中心 · 地址管理）。

口径（2026-10-03）：
- 归属与采购域一致：`owner` = `u{登录用户 id}`，未登录 anonymous —— 各人各的地址；
- 一个账号只有一条默认地址（设默认的动作会把其它条清掉），新增第一条自动成为默认；
- 结算页一键带入：下单时照旧落到 stock_orders 的 `receiver_*` 三个字段上，
  地址以后改了不影响历史采购单（地址簿只是填表用的快捷方式）；
- 支持「粘贴一整段收货信息 → 自动认人 / 认电话 / 认省市区 / 认详细地址」（2026-10-03），
  解析纯函数见 `parse_address_text`（前端粘完点「智能识别」调 /self/addresses/parse）。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.timeutil import utc_now
from src.models import UserAddress

# 收货人 / 电话 / 地址：缺一样就没法发货，路由那边按这个清单校验
REQUIRED_FIELDS = ("receiver_name", "phone", "address")


def list_addresses(db: Session, owner: str) -> list[UserAddress]:
    """这个账号的地址：默认那条排最前，其余按新建倒序。"""
    return list(
        db.execute(
            select(UserAddress)
            .where(UserAddress.owner == owner)
            .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
        )
        .scalars()
        .all()
    )


def get_address(db: Session, owner: str, address_id: int) -> UserAddress | None:
    """取一条（带归属校验：别人的地址拿不到）。"""
    row = db.get(UserAddress, int(address_id or 0))
    if row is None or row.owner != owner:
        return None
    return row


def add_address(
    db: Session,
    owner: str,
    *,
    receiver_name: str,
    phone: str,
    region: str = "",
    address: str = "",
    tag: str = "",
    is_default: bool = False,
) -> UserAddress:
    """新增一条；第一条（或勾了默认）自动成为默认地址。"""
    first = not list_addresses(db, owner)
    row = UserAddress(
        owner=owner,
        receiver_name=str(receiver_name or "").strip(),
        phone=str(phone or "").strip(),
        region=str(region or "").strip(),
        address=str(address or "").strip(),
        tag=str(tag or "").strip(),
        is_default=bool(is_default) or first,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if row.is_default:
        set_default(db, owner, row.id)
    return row


def update_address(
    db: Session, owner: str, address_id: int, **fields
) -> UserAddress | None:
    """改一条（只改传进来的字段）。"""
    row = get_address(db, owner, address_id)
    if row is None:
        return None
    for key in ("receiver_name", "phone", "region", "address", "tag"):
        if key in fields and fields[key] is not None:
            setattr(row, key, str(fields[key] or "").strip())
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return row


def remove_address(db: Session, owner: str, address_id: int) -> bool:
    """删一条；删掉的是默认地址时，把剩下最早那条顶成默认。"""
    row = get_address(db, owner, address_id)
    if row is None:
        return False
    was_default = bool(row.is_default)
    db.delete(row)
    db.commit()
    if was_default:
        rest = list_addresses(db, owner)
        if rest:
            set_default(db, owner, rest[0].id)
    return True


def set_default(db: Session, owner: str, address_id: int) -> UserAddress | None:
    """设为默认（同一账号下其它条自动取消默认）。"""
    row = get_address(db, owner, address_id)
    if row is None:
        return None
    for other in db.execute(
        select(UserAddress).where(UserAddress.owner == owner, UserAddress.id != row.id)
    ).scalars().all():
        if other.is_default:
            other.is_default = False
    row.is_default = True
    db.commit()
    db.refresh(row)
    return row


def default_address(db: Session, owner: str) -> UserAddress | None:
    """这个账号的默认地址（没有就返回 None，结算页不去猜）。"""
    return (
        db.execute(
            select(UserAddress).where(
                UserAddress.owner == owner, UserAddress.is_default.is_(True)
            )
        )
        .scalars()
        .first()
    )


def full_text(row: UserAddress | None) -> str:
    """地址全文：地区 + 详细地址（下单时落进 receiver_address 的就是它）。"""
    if row is None:
        return ""
    return f"{row.region or ''} {row.address or ''}".strip()


def as_dict(row: UserAddress) -> dict:
    """给页面 / 结算页选用用的简版（不含归属等内部字段）。"""
    return {
        "id": row.id,
        "receiver_name": row.receiver_name or "",
        "phone": row.phone or "",
        "region": row.region or "",
        "address": row.address or "",
        "full": full_text(row),
        "tag": row.tag or "",
        "is_default": bool(row.is_default),
    }


# =====================================================================
# 粘贴一段收货信息 → 拆成 收货人 / 电话 / 省市区 / 详细地址（2026-10-03）
#
# 闲鱼 / 微信 / 快递面单上复制下来的地址千奇百怪，规则按「从强到弱」兜：
#   1. 手机号：1[3-9]xxxxxxxxx（带 - 或空格也能认），也认 0571-88888888 这种座机；
#   2. 收货人：先认「收货人：张三」这种带标签的；没有标签就取手机号左边最近的
#      2~4 个汉字（排除 province / 地址词），再不行取手机号右边紧跟的那段；
#   3. 省市区：按 省/自治区/直辖市 → 市/自治州 → 区/县/旗 三级顺序从文本里啃，
#      啃到几级算几级（只写「上海市 闵行区」也行）；
#   4. 详细地址：原文去掉 姓名 / 电话 / 省市区 / 标签词之后剩下的那一段。
# 一个字段都认不出来时返回空值，前端让用户自己填（不猜、不硬塞）。
# =====================================================================
# 手机号：13800000001 / 138-0000-0001 / 138 0000 0001 都认（认完统一去掉分隔符）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d(?:[-\s]?\d{4}){2})(?!\d)")
_TEL_RE = re.compile(r"(?<!\d)(0\d{2,3}[- ]?\d{7,8})(?!\d)")
_LABEL_NAME_RE = re.compile(
    r"(?:收货人|收件人|联系人|姓名)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z·]{2,10})"
)
_MOBILE_PREFIX = (
    "手机", "电话", "联系方式", "收货人", "收件人", "联系人", "姓名", "地址", "收货地址",
)
_LABEL_WORDS_RE = re.compile(
    r"(?:收货人|收件人|联系人|姓名|手机号?|电话|联系方式|收货地址|详细地址|地址)\s*[:：]?"
)
# 直辖市 / 省份关键字：用来判断「这个汉字串是地址，不是人名」
_PROVINCE_HINTS = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏",
    "宁夏", "新疆", "香港", "澳门",
)
_PROVINCE_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,8}?(?:省|自治区|特别行政区)|(?:北京|天津|上海|重庆)市?)"
)
_CITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:市|自治州|地区|盟))")
_DISTRICT_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:区|县|旗))")
# 详细地址里常见的关键字：判断一段汉字像不像地址（不像才可能是人名）
_ADDRESS_HINT_RE = re.compile(
    r"(?:路|街|道|巷|弄|号|幢|栋|单元|室|楼|层|村|镇|乡|开发区|工业园|小区|大厦|广场|"
    r"大道|花园|公寓|商厦|市场|仓库|库房|门面|商铺|店)"
)
# 常见标签词：只在文本里明确写了才认（不瞎猜「家」——「家居」这种词太常见了）
_TAG_HINTS = (("公司", "公司"), ("单位", "公司"), ("仓库", "仓库"),
              ("学校", "学校"), ("大学", "学校"))


def _normalize_text(text: str) -> str:
    """全角转半角、统一分隔符、去掉多余空白。"""
    raw = str(text or "")
    out = []
    for ch in raw:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        elif ch in "，,、；;：:｜|":
            out.append(" ")
        else:
            out.append(ch)
    text = "".join(out)
    # 全角标点上面已经变成半角，这里统一把各种分隔符（含刚转出来的 , ; :）收成空格
    text = re.sub(r"[,;:、｜|]+", " ", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " ", text)
    return text.strip(" ,，.。;；:：")


def _name_from_mobile_side(text: str, phone: str) -> str:
    """没有「收货人：」标签时，从手机号两侧猜姓名。"""
    idx = text.find(phone)
    if idx < 0:
        return ""
    head = text[:idx].strip(" ,，.。;；:：")
    tail = text[idx + len(phone):].strip(" ,，.。;；:：")
    for chunk, at_end in ((head, True), (tail, False)):
        if not chunk:
            continue
        # 去掉紧邻的标签词（「手机」「电话」之类），只留汉字人名
        chunk = _LABEL_WORDS_RE.sub(" ", chunk).strip()
        words = [w for w in re.split(r"[ \t]+", chunk) if w]
        if not words:
            continue
        cand = words[-1] if at_end else words[0]
        cand = cand.strip(" ,，.。;；:：-")
        if not (2 <= len(cand) <= 4):
            continue
        if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", cand):
            continue
        if cand in _PROVINCE_HINTS or any(cand.startswith(p) for p in _PROVINCE_HINTS):
            continue  # 「上海市」这种不是人名
        if _ADDRESS_HINT_RE.search(cand) or _CITY_RE.fullmatch(cand) or _DISTRICT_RE.fullmatch(cand):
            continue
        return cand
    return ""


def _split_region(text: str) -> tuple[str, str]:
    """从文本里啃省 / 市 / 区三级，返回（地区文本, 啃完剩下的文本）。"""
    rest = text
    parts: list[str] = []
    for pattern in (_PROVINCE_RE, _CITY_RE, _DISTRICT_RE):
        found = None
        for m in pattern.finditer(rest):
            cand = m.group(1)
            if any(cand in p or p in cand for p in parts):
                continue  # 别把上一级又认一遍（「上海市 市辖区」这类）
            found = m
            break
        if found is None:
            continue
        parts.append(found.group(1))
        rest = (rest[: found.start()] + " " + rest[found.end():]).strip()
    return " ".join(parts), re.sub(r"[ \t]+", " ", rest).strip()


def parse_address_text(text: str) -> dict:
    """把一段粘贴来的收货信息拆成地址簿字段。

    返回 `{ok, receiver_name, phone, region, address, tag, missing}`：
    认不出电话时 ok 仍为 True（地址簿也允许座机 / 稍后补），missing 里列出没认出来的字段。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "message": "粘贴内容为空", "receiver_name": "", "phone": "",
                "region": "", "address": "", "tag": "", "missing": []}

    clean = _normalize_text(raw)
    # 1) 电话（138-0000-0001 / 138 0000 0001 都归一成 11 位；原文那一串单独记下来，
    #    后面从详细地址里抠掉 —— 归一化后的号码跟原文对不上，不能用它 replace）
    phone = ""
    phone_raw = ""
    m = _PHONE_RE.search(clean)
    if m:
        phone_raw = m.group(1)
        phone = re.sub(r"[-\s]", "", phone_raw)
    else:
        m = _TEL_RE.search(clean)
        if m:
            phone_raw = phone = m.group(1)
    # 2) 姓名
    name = ""
    label = _LABEL_NAME_RE.search(clean)
    if label:
        name = label.group(1).strip()
    if not name and phone_raw:
        name = _name_from_mobile_side(clean, phone_raw)
    # 3) 省市区 + 详细地址
    region, rest = _split_region(clean)
    addr = rest
    for token in (phone_raw, phone):
        if token:
            addr = addr.replace(token, " ")
    if name:
        addr = re.sub(re.escape(name), " ", addr, count=1)
    addr = _LABEL_WORDS_RE.sub(" ", addr)
    addr = re.sub(r"[ \t]+", " ", addr).strip(" ,，.。;；:：-")
    # 4) 标签（只在明确写了「公司 / 仓库 / 学校」时认，不瞎猜）
    tag = ""
    for key, value in _TAG_HINTS:
        if key in clean:
            tag = value
            break

    missing = []
    if not name:
        missing.append("receiver_name")
    if not phone:
        missing.append("phone")
    if not addr:
        missing.append("address")
    return {
        "ok": True,
        "message": "",
        "receiver_name": name,
        "phone": phone,
        "region": region,
        "address": addr,
        "tag": tag,
        "missing": missing,
    }
