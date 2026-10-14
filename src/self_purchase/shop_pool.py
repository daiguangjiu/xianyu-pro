"""店铺商品池（详情页「店铺铺货」）+ 素材打包下载。

个人闲置店铺没有 PC 端后台，所以「店铺铺货」= 把货源加进这里的商品管理池；
池子里的商品可以一键打包下载素材（主图 / 详情图 / 文案 / 规格价格表），
拿到手机上照着发布即可。

素材包结构：
    <商品标题>/
      文案.txt          标题、两个价、库存、发货售后承诺、描述（去标签）
      规格与价格.csv    规格 / 渔货价 / 建议售价 / 可售库存
      主图/01.jpg …
      详情图/01.jpg …
      README.txt        抓取失败的图片清单（外链挂了也能知道哪张缺）
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# 时间口径（2026-10-03）：库里存 UTC（utc_now），素材包里的日期文案按北京（beijing_now）
from app.timeutil import beijing_now, utc_now
from src.models import ShopGoods, SupplierItem

logger = logging.getLogger(__name__)
from src.self_purchase.lines import TWO, fish_price_of

MAX_IMAGES = 24  # 每类最多打包多少张，避免一下拉几百张
FETCH_TIMEOUT = 6.0


def add_to_pool(
    db: Session,
    owner: str,
    item,
    *,
    shop_id: str = "",
    shop_name: str | None = None,
    suggested_price: Decimal | None = None,
    commit: bool = True,
) -> ShopGoods:
    """店铺铺货：加入店铺商品池（同店同款幂等，重复点只刷新价格与库存）。"""
    shop_key = str(shop_id or "").strip()
    row = (
        db.execute(
            select(ShopGoods).where(
                ShopGoods.owner == owner,
                ShopGoods.shop_id == shop_key,
                ShopGoods.item_id == item.item_id,
            )
        )
        .scalars()
        .first()
    )
    price = fish_price_of(item)
    suggested = (
        Decimal(str(suggested_price)).quantize(TWO)
        if suggested_price not in (None, "")
        else (Decimal(str(item.sale_price)).quantize(TWO) if item.sale_price else None)
    )
    if row is None:
        # 入池时间显式写 UTC：Base.created_at 走 DB now()，页面按用户时区渲染（同渔枢系统）
        row = ShopGoods(
            created_at=utc_now(), owner=owner, shop_id=shop_key, item_id=item.item_id
        )
        db.add(row)
    row.shop_name = shop_name or row.shop_name
    row.title = str(getattr(item, "title", "") or "")
    images = list(getattr(item, "images", None) or [])
    row.image = item.main_image or (images[0] if images else None)
    row.price = price
    row.suggested_price = suggested
    row.stock = int(getattr(item, "stock", 0) or 0)
    row.status = "pooled"
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    # 与铺货记录（ChannelListing）互通：入池 = 这件货已经铺到我店里。
    # 归属跟着账号走（owner），别人的铺货记录不受影响。
    # 放在本职提交之后 —— 旁路同步失败也绝不能把入池一起拖黄。
    _sync_listing(db, item.item_id, owner=owner, item=item, in_shop=True)
    return row


def _sync_listing(
    db: Session,
    item_id: str,
    *,
    owner: str = "",
    item=None,
    in_shop: bool,
    commit: bool = True,
) -> None:
    """入池 / 移出时把「我的上架」（channel_listings）同步成同一个状态。

    两套记录以前各说各话：池子里移出了，「我的上架」还写着已发布；反过来详情页
    点了「上架到店铺」，池子里也看不到。现在以「这件货在不在我店里」为唯一真值：
      入池 → 已有的铺货记录跟着置 published（没有就不新建：入池只是把货收进池子，
             发布动作才建记录，免得详情页被一条「已发布」盖掉「已入店铺池」的标记）；
      移出 → 铺货记录回 draft（表示没铺着，历史事实保留不删）。
    桥接失败只记日志，不影响入池 / 移出本身。
    """
    try:
        from src.models import ChannelListing

        # 只动这个账号自己的铺货记录（2026-10-13 起按账号隔离）
        owner_key = str(owner or "").strip()

        if in_shop:
            rows = list(
                db.execute(
                    select(ChannelListing).where(
                        ChannelListing.item_id == item_id,
                        ChannelListing.channel == "xianyu",
                        ChannelListing.owner == owner_key,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                # 只有「从池子直接铺上」的才算已铺：带着发布页出去过的（handoff_at 有值）
                # 保持「待发布」，等真在闲鱼发布了再算，别替运营把状态提前改掉。
                if row.status == "draft" and row.handoff_at is None:
                    row.status = "published"
        else:
            rows = list(
                db.execute(
                    select(ChannelListing).where(
                        ChannelListing.item_id == item_id,
                        ChannelListing.channel == "xianyu",
                        ChannelListing.owner == owner_key,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                # 只把「已铺着」改回未铺；failed / needs_review 是发布结果，不动
                if row.status == "published":
                    row.status = "draft"
        if commit:
            db.commit()
    except Exception as exc:  # noqa: BLE001  桥接失败不能让入池 / 移出失败
        logger.warning("同步「我的上架」状态失败（item=%s）：%s", item_id, exc)


def list_pool(db: Session, owner: str, *, shop_id: str | None = None, limit: int = 500):
    query = select(ShopGoods).where(
        ShopGoods.owner == owner, ShopGoods.status == "pooled"
    )
    if shop_id is not None and str(shop_id) != "":
        query = query.where(ShopGoods.shop_id == str(shop_id))
    query = query.order_by(ShopGoods.id.desc()).limit(limit)
    return list(db.execute(query).scalars().all())


def get_pool_row(db: Session, owner: str, pool_id: int) -> ShopGoods | None:
    return (
        db.execute(
            select(ShopGoods).where(ShopGoods.id == int(pool_id), ShopGoods.owner == owner)
        )
        .scalars()
        .first()
    )


def remove_from_pool(db: Session, owner: str, pool_id: int, *, commit: bool = True) -> bool:
    """移出商品池（软删：留痕，池子列表不再显示）。"""
    row = get_pool_row(db, owner, pool_id)
    if row is None:
        return False
    row.status = "removed"
    if commit:
        db.commit()
    else:
        db.flush()
    # 与铺货记录互通：移出池子 = 这件货不在店里了，铺货记录跟着回到未铺
    _sync_listing(db, row.item_id, owner=owner, in_shop=False)
    return True


def pool_summary(db: Session, owner: str) -> dict:
    rows = (
        db.execute(
            select(ShopGoods.status, func.count(ShopGoods.id))
            .where(ShopGoods.owner == owner)
            .group_by(ShopGoods.status)
        ).all()
    )
    counts = {str(status): int(count) for status, count in rows}
    downloads = db.scalar(
        select(func.coalesce(func.sum(ShopGoods.download_count), 0)).where(
            ShopGoods.owner == owner
        )
    )
    return {
        "pooled": counts.get("pooled", 0),
        "removed": counts.get("removed", 0),
        "total": sum(counts.values()),
        "downloads": int(downloads or 0),
    }


# --------------------------------------------------------------------------
# 素材打包
# --------------------------------------------------------------------------
def _ext_of(url: str, content_type: str = "") -> str:
    path = str(url).split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if "png" in str(content_type).lower():
        return ".png"
    if "webp" in str(content_type).lower():
        return ".webp"
    return ".jpg"


def _fetch(url: str) -> bytes | None:
    """抓一张外链图（1688 图床）。失败返回 None，不让整包失败。"""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx 是既有依赖
        return None
    try:
        resp = httpx.get(
            url,
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (xianyu-order-hub materials)"},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        return None
    return None


def _dedup(urls: list) -> list[str]:
    out: list[str] = []
    for url in urls:
        text = str(url or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _plain(html: str | None) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(html or ""), flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _copy_text(item, pool: ShopGoods) -> str:
    """文案.txt：手机上要填的字段一条条列好，复制就能用。"""
    images = list(getattr(item, "images", None) or [])
    specs = [dict(s) for s in (getattr(item, "spec_defs", None) or []) if isinstance(s, dict)]
    spec_line = "；".join(
        f"{s.get('name')}：{'/'.join([str(v) for v in (s.get('values') or [])])}" for s in specs
    )
    lines = [
        "【标题】" + str(getattr(item, "title", "") or ""),
        "【渔货价】¥" + str(fish_price_of(item)),
        "【建议售价】¥" + str(pool.suggested_price if pool.suggested_price is not None else "—"),
        "【原价】¥" + str(getattr(item, "market_price", None) or "—"),
        "【库存】" + str(int(getattr(item, "stock", 0) or 0)) + " 件",
        "【成色】全新",
        "【商家编码】" + str(getattr(item, "item_code", None) or "—"),
        "【品牌】" + str(getattr(item, "brand", None) or "—"),
        "【规格】" + (spec_line or "默认规格"),
        "【发货】" + str(int(getattr(item, "delivery_days", 0) or 0) or "—") + " 天内发货",
        "【售后】" + str(getattr(item, "after_sale", None) or "7天无理由退换"),
    ]
    tip = getattr(item, "supplier_tip", None)
    if tip:
        lines += ["", "【供应商提示】", str(tip)]
    detail = _plain(getattr(item, "detail_html", None))
    if detail:
        lines += ["", "【商品描述】", detail[:2000]]
    lines += ["", "【图片】", *(images[:24] or ([item.main_image] if item.main_image else []))]
    return "\n".join(lines) + "\n"


def _specs_csv(item, pool: ShopGoods) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["规格", "渔货价", "建议售价", "可售库存", "规格ID"])
    rows = (getattr(item, "sku_list", None) or {}).get("skus") or []
    suggested = pool.suggested_price
    if not rows:
        writer.writerow(["默认规格", fish_price_of(item), suggested or "", pool.stock or 0, ""])
    else:
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            writer.writerow([
                raw.get("spec_text") or raw.get("spec") or "默认规格",
                raw.get("price") or fish_price_of(item),
                suggested or "",
                raw.get("stock") or 0,
                raw.get("sku_id") or "",
            ])
    return buf.getvalue()


def _safe_name(text: str, fallback: str = "素材") -> str:
    clean = re.sub(r"[\\/:*?\"<>|\s]+", "", str(text or ""))[:40]
    return clean or fallback


def materials_zip(db: Session, pool: ShopGoods) -> tuple[bytes, str]:
    """打包素材，返回 (zip 字节, 建议文件名)。图片抓不到也会出包（README 里列明缺哪些）。"""
    item = (
        db.execute(select(SupplierItem).where(SupplierItem.item_id == pool.item_id))
        .scalars()
        .first()
    )
    if item is None:
        raise ValueError(f"货源不存在：{pool.item_id}")

    mains = _dedup([item.main_image, *(getattr(item, "images", None) or [])])[:MAX_IMAGES]
    details = _dedup(list(getattr(item, "detail_images", None) or []))[:MAX_IMAGES]
    failed: list[str] = []

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("文案.txt", _copy_text(item, pool))
        zf.writestr("规格与价格.csv", _specs_csv(item, pool))
        for folder, urls in (("主图", mains), ("详情图", details)):
            for index, url in enumerate(urls, 1):
                data = _fetch(url)
                if data is None:
                    failed.append(f"{folder} {index:02d}  {url}")
                    continue
                zf.writestr(f"{folder}/{index:02d}{_ext_of(url)}", data)
        readme = [
            "渔货栈 · 店铺商品素材包",
            f"商品：{getattr(item, 'title', '')}",
            f"货源：{pool.item_id}",
            f"生成时间：{beijing_now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "包内文件：",
            "  文案.txt        标题 / 两个价 / 库存 / 发货售后 / 描述",
            "  规格与价格.csv  各规格的渔货价、建议售价、可售库存",
            "  主图/ 、详情图/ 用手机相册导入即可发布",
            "",
            f"主图 {len(mains)} 张、详情图 {len(details)} 张，"
            f"成功 {len(mains) + len(details) - len(failed)} 张。",
        ]
        if failed:
            readme += ["", "以下图片这次没抓到（图床临时挂了，稍后重下即可）：", *failed]
        zf.writestr("README.txt", "\n".join(readme) + "\n")

    pool.materials_at = utc_now()
    pool.download_count = int(pool.download_count or 0) + 1
    db.commit()

    stamp = beijing_now().strftime("%Y%m%d%H%M")
    return buf.getvalue(), f"{_safe_name(pool.title or item.title)}-素材-{stamp}.zip"
