"""渔货栈采购域：采购车（加采购车）→ 结算 → 支付 → 采购单 → 店铺商品池。

商品详情页（1688 版式）底部三个按钮的落点：

- 「立即下单」：直接按当前规格数量进结算页（`checkout`），照 1688 的支付流程走完
  地址 → 明细 → 留言 → 交易/配送方式 → 价格明细 → 支付；
- 「加采购车」：进采购车（`cart`），可改数量 / 删行 / 勾选，再合并结算；
- 「店铺铺货」：进店铺商品池（`shop_pool`），池子里的商品可打包下载素材
  （个人闲置店铺没有 PC 后台，只能拿素材去手机端发布）。

单价口径见 `lines.BuyLine`：该规格的成本价（sku 级优先），与详情页 SKU 表一致。
"""
from __future__ import annotations

from src.self_purchase.address import (
    add_address,
    as_dict as address_as_dict,
    default_address,
    get_address,
    list_addresses,
    parse_address_text,
    remove_address,
    set_default,
    update_address,
)
from src.self_purchase.cart import (
    add_to_cart,
    cart_count,
    cart_summary,
    clear_cart,
    group_lines,
    list_cart,
    remove_lines,
    set_line_address,
    to_buy_line,
    update_qty,
)
from src.self_purchase.lines import BuyLine, freight_of, line_of, supplier_name_of
from src.self_purchase.order import (
    PAY_METHODS,
    STATUS_ORDER,
    STATUS_TEXT,
    TRADE_TYPES,
    cancel_order,
    complete_order,
    confirm_pay,
    create_order,
    create_split_order,
    get_items,
    get_order,
    list_orders,
    mark_shipped,
    next_order_no,
    pay_method_text,
    pay_order,
    progress_of,
    status_counts,
    status_text,
    sub_orders,
    sync_parent_status,
    trade_type_text,
)
from src.self_purchase.shop_pool import (
    add_to_pool,
    list_pool,
    materials_zip,
    pool_summary,
    remove_from_pool,
)

__all__ = [
    "BuyLine",
    "PAY_METHODS",
    "STATUS_ORDER",
    "TRADE_TYPES",
    "STATUS_TEXT",
    "add_address",
    "address_as_dict",
    "add_to_cart",
    "add_to_pool",
    "cancel_order",
    "cart_count",
    "cart_summary",
    "clear_cart",
    "complete_order",
    "confirm_pay",
    "create_order",
    "create_split_order",
    "default_address",
    "freight_of",
    "get_address",
    "get_items",
    "get_order",
    "group_lines",
    "line_of",
    "list_addresses",
    "list_cart",
    "list_orders",
    "list_pool",
    "mark_shipped",
    "materials_zip",
    "next_order_no",
    "pay_method_text",
    "parse_address_text",
    "pay_order",
    "pool_summary",
    "trade_type_text",
    "progress_of",
    "remove_address",
    "remove_from_pool",
    "remove_lines",
    "set_default",
    "set_line_address",
    "status_counts",
    "status_text",
    "sub_orders",
    "supplier_name_of",
    "sync_parent_status",
    "to_buy_line",
    "update_qty",
]
