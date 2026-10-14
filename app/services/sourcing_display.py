"""渔货栈货源展示基数（2026-10-13 用户拍板）。

渔货栈货源广场卡片上两个数字以前是**裸的真实值**：

- 「X 家上架」＝铺过这款货的店铺数（铺货记录按账号去重）。刚起步时一款货常常只有
  1 家铺过，卡片就写「1 家上架」，看着冷清、不像有人在做；
- 「已售 N」＝这款货已成交的采购件数，新品一律是 0。

运营要的是「放大一下基数给用户造成热闹感」，但用户同时拍板
**「实际的上架数量仍然要准确的」** —— 所以落成一个规则：

> 展示值 = 后台配的基数 + 真实值；真实值照旧单独算，排序、榜单计数、
> 商品详情页「在售闲鱼号」一律用真实值，基数一个都不加。

配置全站一份（表里只存第 1 行），渔货栈每 60 秒读一次，改完最多 60 秒生效。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import SourcingDisplayConfig
from app.timeutil import utc_now  # noqa: F401  业务代码取时间一律走它（UTC）

# 这张表只存一行（全站一份配置）
CONFIG_ID = 1
# 上限：防手抖填成 99999999 把页面数字撑爆
BASE_MAX = 999999


def _int_of(value: Any, *, low: int = 0, high: int = BASE_MAX) -> int:
    """把表单值变成整数：空 / 乱值一律按 0，负数按 0，超过上限按上限。"""
    try:
        num = int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return max(low, min(high, num))


def get_row(session: Session) -> SourcingDisplayConfig:
    """取配置行（没有就建一份全 0 的）。"""
    row = session.get(SourcingDisplayConfig, CONFIG_ID)
    if row is None:
        row = SourcingDisplayConfig(
            id=CONFIG_ID, listed_shops_base=0, sold_base=0, note=""
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def get_config(session: Session) -> dict[str, Any]:
    """给页面用的配置快照。"""
    row = get_row(session)
    return {
        "listed_shops_base": int(row.listed_shops_base or 0),
        "sold_base": int(row.sold_base or 0),
        "note": str(row.note or ""),
        "updated_by": str(row.updated_by or ""),
        "updated_at": row.updated_at,
    }


def save_config(
    session: Session,
    payload: dict[str, Any],
    *,
    operator: str = "",
) -> SourcingDisplayConfig:
    """保存基数（乱值兜成 0，超过上限封顶）。"""
    row = get_row(session)
    row.listed_shops_base = _int_of(payload.get("listed_shops_base"))
    row.sold_base = _int_of(payload.get("sold_base"))
    row.note = str(payload.get("note") or "").strip()[:255]
    row.updated_by = str(operator or "").strip()[:64]
    row.updated_at = utc_now()
    session.commit()
    session.refresh(row)
    return row
