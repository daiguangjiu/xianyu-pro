"""自营端「我要录单」（原「我要下单」，2026-10-14 改名）：闲鱼出单之后，在这儿录一笔在线订单。

两种单：
- **选品商品订单** —— 在「我的店铺商品」里直接下单，商品信息自动带出来；
- **其他订单** —— 按飞书群模板填，或传一张闲鱼订单详情图让系统认（认不全就手填）。

落库就是 `downstream_orders`（与飞书群收进来的单同一张表），后面走同一套订单流程：
确认接单 → 推给渔枢系统采购 → 发货 → 完成 / 退款（见 `src/order/flow.py` 与
`src/order/state.py`），逆向（退款）逻辑也一样。
"""
from __future__ import annotations

import logging
import random
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from ..models import DownstreamOrder, SupplierItem
from .address import parse_address_text

logger = logging.getLogger(__name__)

# 单号前缀：手工补的单用 XY + 时间，与飞书群同步进来的单区分得开
ORDER_NO_PREFIX = "XY"

SOURCE_SHOP_GOODS = "shop_goods"   # 从「我的店铺商品」下的单
SOURCE_OTHER = "other"             # 其他订单（飞书群模板 / 闲鱼订单详情图）

# 闲鱼订单详情图里像金额的那一行（实付款 / 订单金额 / 合计…）
_AMOUNT_RE = re.compile(
    r"(?:实付款|实付|订单金额|付款金额|合计|总价|金额)[^\d]{0,8}(\d+(?:\.\d{1,2})?)"
)
_IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _now():
    """北京时间（库里存 naive，别用 utcnow，页面会差 8 小时）。"""
    from app.timeutil import now  # noqa: PLC0415

    return now()


def new_order_no() -> str:
    """XY + 年月日时分秒 + 2 位随机：一天里手补几十单也撞不上。"""
    return f"{ORDER_NO_PREFIX}{_now():%Y%m%d%H%M%S}{random.randint(10, 99)}"


def parse_buyer(text: str) -> dict:
    """从一段「闲鱼下单用户信息」里认出收货人 / 电话 / 地址。

    口径与地址簿的粘贴识别一致（`address.parse_address_text`），
    认不出电话也照样返回（收货人 / 地址能认多少算多少，剩下的手填）。
    """
    try:
        return parse_address_text(text)
    except Exception as exc:  # noqa: BLE001  解析出错不该挡着下单
        logger.debug("识别买家信息失败：%s", exc)
        return {"ok": False, "message": str(exc), "receiver_name": "", "phone": "",
                "region": "", "address": "", "tag": "", "missing": []}


def pick_amount(text: str) -> str:
    """从闲鱼订单详情里抠金额（实付款 / 订单金额 …），认不出给空串。"""
    m = _AMOUNT_RE.search(str(text or ""))
    return m.group(1) if m else ""


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def item_snapshot(db, item_id: str) -> dict:
    """下单页的商品预填（标题 / 主图 / 渔货价 / 建议售价）。

    这款货不在货源表里也照样能下单（其他订单经常是店里别的货），所以查不到只给空。
    """
    out = {
        "item_id": str(item_id or ""),
        "title": "",
        "image": "",
        "fish_price": 0.0,
        "suggested_price": 0.0,
    }
    if not item_id:
        return out
    try:
        item = db.execute(
            select(SupplierItem).where(SupplierItem.item_id == str(item_id))
        ).scalars().first()
    except Exception as exc:  # noqa: BLE001
        logger.debug("取商品 %s 失败：%s", item_id, exc)
        return out
    if item is None:
        return out

    out["title"] = str(getattr(item, "title", "") or "")
    imgs = getattr(item, "main_image", None) or getattr(item, "images", None)
    if isinstance(imgs, list) and imgs:
        out["image"] = str(imgs[0] or "")
    elif isinstance(imgs, str):
        out["image"] = imgs
    # 渔货价（我们向平台拿货的成本）：没有就退到采购价 —— 没有成本的单没法算毛利
    out["fish_price"] = float(
        getattr(item, "fish_price", 0) or getattr(item, "cost_price", 0) or 0
    )
    out["suggested_price"] = float(getattr(item, "sale_price", 0) or 0)
    return out


def create_manual_order(
    db,
    *,
    owner: str,
    item_id: str = "",
    title: str = "",
    sku_id: str = "",
    qty: int = 1,
    sale_price=0,
    fish_price=0,
    buyer_name: str = "",
    buyer_phone: str = "",
    address: str = "",
    shop_id: int = 0,
    shop_name: str = "",
    note: str = "",
    images=None,
    operator: str = "",
    source: str = SOURCE_OTHER,
) -> DownstreamOrder:
    """落一笔手工补的在线订单（状态 pending 待确认，后面走正常流转）。

    商品名 / 店铺 / 图片都塞在 raw_json 里 —— downstream_orders 只有单号与金额那几列，
    履约与逆向看的是这些事实，剩下的字段由渔枢系统建档后补。
    """
    urls = [str(u) for u in (images or []) if str(u or "").strip()]
    row = DownstreamOrder(
        downstream_order_id=new_order_no(),
        channel="xianyu",
        channel_sku_id=str(item_id or sku_id or "")[:128],
        buyer_name=str(buyer_name or "")[:64],
        buyer_phone=str(buyer_phone or "")[:32],
        address=str(address or ""),
        qty=max(int(qty or 1), 1),
        sale_price=_money(sale_price),
        fish_price=_money(fish_price),
        status="pending",
        raw_json={
            "source": source,
            "owner": str(owner or ""),
            # 运营姓名（2026-10-14）：与飞书群报单人对齐，谁录的单 / 谁在群里报的都看得到
            "operator": str(operator or "")[:64],
            "title": str(title or "")[:255],
            "item_id": str(item_id or ""),
            "shop_id": int(shop_id or 0),
            "shop_name": str(shop_name or ""),
            "note": str(note or ""),
            "images": urls,
        },
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def save_upload(filename: str, data: bytes) -> str:
    """存一张上传的闲鱼订单详情图，返回可访问的 URL（/media/orders/xxx）。"""
    try:
        from app.config import settings  # noqa: PLC0415

        root = Path(settings.media_path) / "orders"
    except Exception as exc:  # noqa: BLE001
        logger.debug("取不到媒体目录：%s", exc)
        return ""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("建目录失败 %s：%s", root, exc)
        return ""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in _IMAGE_SUFFIX:
        suffix = ".jpg"
    name = f"{_now():%Y%m%d%H%M%S}{random.randint(100, 999)}{suffix}"
    try:
        (root / name).write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("存图失败：%s", exc)
        return ""
    return f"/media/orders/{name}"


def ocr_of(url: str) -> dict:
    """认一张闲鱼订单详情图：能认就返回整篇文字 + 认出来的收货信息。

    没装 tesseract / 图太糊 → `ok=False`，页面提示手填或粘贴文本
    （图还是留在订单上，供人核对）。
    """
    out = {"ok": False, "text": "", "buyer": None, "amount": "", "message": "没认出来"}
    rel = str(url or "").split("/media/", 1)[-1]
    if not rel or rel == str(url or ""):
        out["message"] = "图片地址不对"
        return out
    try:
        from app.config import settings  # noqa: PLC0415

        file = Path(settings.media_path) / rel
    except Exception as exc:  # noqa: BLE001
        out["message"] = f"读不到图片：{exc}"
        return out
    if not file.exists():
        out["message"] = "图片已不在服务器上了"
        return out
    try:
        from app.parser.ocr import ocr_image_ex  # noqa: PLC0415

        res = ocr_image_ex(file)
        text = str(getattr(res, "text", "") or "")
        status = str(getattr(res, "status", "") or "")
    except Exception as exc:  # noqa: BLE001  OCR 没装 / 超时，降级成手填
        logger.info("订单详情图 OCR 失败：%s", exc)
        out["message"] = "这张图没认出文字（服务器没装 OCR 或图太糊），请手动填或粘贴文本"
        return out

    out["text"] = text
    if status == "failed" or not text.strip():
        out["message"] = "这张图没认出文字，请手动填或粘贴文本"
        return out
    out["ok"] = True
    out["buyer"] = parse_buyer(text)
    out["amount"] = pick_amount(text)
    out["message"] = "已识别，核对一下再提交"
    return out
