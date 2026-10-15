"""渔货栈「通知」：渔集价格变动（2026-10-15 用户拍板）。

渔集那边改了渔货价 / 建议售价并同步过来时，在这里落一条人话通知，
「异常提醒」（/self/logs）页面顶部的「商品价格变动」区块直接读它。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class PriceChangeNotice(Base):
    __tablename__ = "price_change_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fish_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    product_code: Mapped[str] = mapped_column(String(64), default="")
    product_name: Mapped[str] = mapped_column(String(255), default="")
    # 渔货价（卖给分销商的成本口径）变动
    old_fish_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    new_fish_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # 销售价（建议售价口径）变动
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # up=涨价 / down=降价（按渔货价方向定，两个价反着走时以渔货价为准）
    direction: Mapped[str] = mapped_column(String(8), default="")
    source: Mapped[str] = mapped_column(String(32), default="渔集价格同步")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
