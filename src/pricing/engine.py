"""定价引擎。

规则优先级：channel > category > supplier > global
数字 priority 越小越优先；is_active=False 视为不存在。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from src.models import PriceRule, SupplierItem


def pick_rule(
    db: Session,
    *,
    channel: str | None = None,
    category_id: str | None = None,
    supplier_id: str | None = None,
) -> PriceRule | None:
    """从 PriceRule 表挑出最匹配的规则。"""
    candidates: Iterable[PriceRule] = db.query(PriceRule).filter(PriceRule.is_active.is_(True))

    # 按 scope 精准匹配 + 优先级排序，取第一条
    candidates_sorted = sorted(candidates, key=lambda r: r.priority)

    for scope, key in [
        ("channel", channel),
        ("category", category_id),
        ("supplier", supplier_id),
        ("global", None),
    ]:
        for r in candidates_sorted:
            if r.scope == scope and (r.scope_key == key or scope == "global"):
                return r
    return None


def calculate_price(
    item: SupplierItem,
    rule: PriceRule | None = None,
    *,
    margin_rate: float | None = None,
    fixed_shipping: Decimal | None = None,
) -> Decimal:
    """根据 PriceRule 计算下游售价。

    入参未传 rule 时使用 margin_rate + fixed_shipping（用于影子模式）。
    """
    margin = (
        Decimal(str(rule.margin_rate))
        if rule and margin_rate is None
        else Decimal(str(margin_rate if margin_rate is not None else 0.20))
    )
    shipping = (
        rule.fixed_shipping
        if rule and fixed_shipping is None
        else (fixed_shipping if fixed_shipping is not None else Decimal("0"))
    )

    price = (item.cost_price * (Decimal("1") + margin) + shipping).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # 价格区间保护
    if rule and rule.price_min is not None:
        price = max(price, rule.price_min)
    if rule and rule.price_max is not None:
        price = min(price, rule.price_max)

    return price


__all__ = ["calculate_price", "pick_rule"]