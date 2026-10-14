"""菜单徽标与商品类目（2026-09-28 版本）。

1. 供应链推单菜单的数字 = 「待填回单」：已推给供应商、供应商还没回物流单号的订单数。
   它与首页「待供应商回单」、推单页副标题走同一个判定（models.awaiting_tracking），
   待推单发货 / 已回单 / 退单 / 撤回都不算。
2. 新增商品的一级 → 二级类目联动：类目树只有一份（services/product.CATEGORY_TREE），
   拼类目文本、候选列表、素材图（主图 + 图集去重）都读它。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_CANCELED,
    ORDER_STATE_REFUND_REQUEST,
    Order,
    Product,
)
from app.services import product as product_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web import nav  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402


def _order(order_no: str, **kwargs) -> Order:
    data = {"order_no": order_no, "product_name": "测试商品", "sale_amount": 10, "cost_amount": 5}
    data.update(kwargs)
    return Order(**data)


class AwaitingTrackingBadgeTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        nav._badge_cache.clear()          # 徽标有 30 秒缓存，用例之间必须清掉
        with SessionLocal() as db:
            db.query(Order).delete()
            db.query(Product).delete()
            db.commit()
            db.add_all(
                [
                    # 已推单、还没回单 -> 唯一计入的
                    _order("B1", supplier_status="pushed", pushed_at=now(), tracking_no=""),
                    # 已推单、已回单 -> 不算
                    _order("B2", supplier_status="pushed", pushed_at=now(), tracking_no="SF1001"),
                    # 还没推给供应商 -> 那是「待推单发货」，不是待填回单
                    _order("B3", supplier_status="pending", tracking_no=""),
                    # 退单的不发货，不用回单
                    _order("B4", supplier_status="pushed", pushed_at=now(), tracking_no="",
                           refund_status="pending"),
                    # 群里撤回的留档单不算
                    _order("B5", supplier_status="pushed", pushed_at=now(), tracking_no="",
                           is_recalled=True),
                ]
            )
            db.commit()

    def test_badge_counts_awaiting_tracking(self) -> None:
        counts = nav.badge_counts()
        self.assertEqual(counts.get("batches_tracking"), 1)
        # 老的「待推单发货」徽标已经不用了，别又冒出来
        self.assertNotIn("batches_pending", counts)

    def test_badge_matches_dashboard_metric(self) -> None:
        """菜单上的数字要和首页「待供应商回单」数出来的一样。"""
        with SessionLocal() as db:
            self.assertEqual(stats_service.summary(db)["pending_tracking"], 1)

    def test_badge_hint_explains_the_number(self) -> None:
        self.assertIn("待填回单", nav.BADGE_HINTS["batches_tracking"])


class PendingPushExcludesReverseTest(unittest.TestCase):
    """「待推单发货」不该包含撤单 / 退单的订单。

    这些订单的推单状态已经按事实派生为「用户撤单，无需推单」「用户退单，无需推单」，
    页面上写着无需推单、却还挂在待推单发货列表里，运营会以为漏了一单没推。
    """
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post(
            "/login", data={"username": "admin", "password": "xianyu2026"}, follow_redirects=False
        )
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        init_db()
        nav._badge_cache.clear()
        with SessionLocal() as db:
            db.query(Order).delete()
            db.commit()
            db.add_all(
                [
                    # 正常订单：还没推给供应商 -> 唯一计入的
                    _order("P1", supplier_status="pending"),
                    # 用户退单（未推单）：推单状态 = 用户退单，无需推单
                    _order("P2", supplier_status="pending", refund_status="pending"),
                    # 用户撤单：推单状态 = 用户撤单，无需推单
                    _order("P3", supplier_status="pending", order_state=ORDER_STATE_CANCELED),
                    # 已退单（订单状态已落退单）
                    _order(
                        "P4", supplier_status="pending",
                        order_state=ORDER_STATE_REFUND_REQUEST,
                    ),
                    # 群里撤回的留档单不参与任何流程
                    _order("P5", supplier_status="pending", is_recalled=True),
                ]
            )
            db.commit()

    def test_badge_counts_only_real_pending(self) -> None:
        counts = nav.badge_counts()
        self.assertEqual(counts.get("orders_pending_push"), 1)

    def test_dashboard_and_push_list_agree(self) -> None:
        with SessionLocal() as db:
            self.assertEqual(stats_service.summary(db)["pending_push"], 1)
            self.assertEqual([o.order_no for o in push_service.pending_orders(db)], ["P1"])

    def test_order_page_pending_filter_hides_reverse_orders(self) -> None:
        html = self.client.get("/orders?supplier_status=pending").text
        self.assertIn("P1", html)
        for no in ("P2", "P3", "P4", "P5"):
            self.assertNotIn(no, html, f"{no} 是撤单 / 退单，不该出现在待推单发货里")
        # 它们仍然能在全部订单里查到（只是不归到待推单发货）
        all_html = self.client.get("/orders").text
        self.assertIn("P2", all_html)
        self.assertIn("用户退单，无需推单", all_html)


class CategoryCascadeTest(unittest.TestCase):
    def test_category_text(self) -> None:
        self.assertEqual(
            product_service.category_text("数码配件", "耳机音响"), "数码配件 / 耳机音响"
        )
        # 「其他（手工填写）」是前端专用值，不写进商品数据
        self.assertEqual(
            product_service.category_text("其他", product_service.CATEGORY_CUSTOM), "其他"
        )
        self.assertEqual(product_service.category_text("家居家纺", ""), "家居家纺")
        # 只给了二级（前端会拦住，这里兜底）：不要拼出「 / 耳机音响」
        self.assertEqual(product_service.category_text("  ", "耳机音响"), "耳机音响")
        self.assertEqual(product_service.category_text("", ""), "")

    def test_every_top_category_has_a_tree_entry(self) -> None:
        self.assertEqual(product_service.TOP_CATEGORIES, list(product_service.CATEGORY_TREE))
        for cat1, subs in product_service.CATEGORY_TREE.items():
            self.assertEqual(len(subs), len(set(subs)), f"{cat1} 的二级类目有重复")

    def test_suggestions_keep_existing_values(self) -> None:
        init_db()
        with SessionLocal() as db:
            db.query(Product).delete()
            db.add(Product(name="老商品", category="数码配件 / 老写法"))
            db.commit()
            values = product_service.category_suggestions(db)
            self.assertIn("数码配件", values)
            self.assertIn("数码配件 / 耳机音响", values)
            # 库里已经在用的写法（老数据）也要留在候选里，不能被类目树挤掉
            self.assertIn("数码配件 / 老写法", values)

    def test_gallery_images_puts_main_first(self) -> None:
        row = Product(
            name="素材测试",
            image_path="/static/brand/logo.png",
            images='["/static/brand/favicon.png", "/static/brand/logo.png"]',
        )
        # 主图排最前，图集里的重复项去掉（详情页素材区与预填包共用这份）
        self.assertEqual(
            product_service.gallery_images(row),
            ["/static/brand/logo.png", "/static/brand/favicon.png"],
        )

    def test_gallery_falls_back_to_images(self) -> None:
        """主图没维护时用图集第一张，别让详情页素材区空着。"""
        row = Product(name="只有图集", images='["/static/brand/favicon.png"]')
        self.assertEqual(product_service.gallery_images(row), ["/static/brand/favicon.png"])
        self.assertEqual(product_service.gallery_images(Product(name="没图")), [])


class OrderSubmenuCountTest(unittest.TestCase):
    """订单归档的二级菜单：每个状态右侧显示自己的单数（与点进去的列表同一口径）。"""

    def setUp(self) -> None:
        init_db()
        nav._badge_cache.clear()
        with SessionLocal() as db:
            db.query(Order).delete()
            db.query(Product).delete()
            db.commit()
            db.add_all(
                [
                    # 待推单发货 2 单（默认 supplier_status=pending 就是还没推给供应商）
                    _order("S1", supplier_status="pending"),
                    _order("S2", supplier_status="pending"),
                    # 待人工核对 1 单
                    _order("S3", supplier_status="pushed", needs_review=True),
                    # 退单：待拦截（还没处理完）
                    _order("S4", supplier_status="pushed", refund_status="pending"),
                    # 退单：已退款（处理完了）—— 算「已退单」不算「待拦截」
                    _order("S5", supplier_status="pushed", refund_status="refunded"),
                    # 老数据退单：只落了 order_state，没记处理状态 -> 也算待拦截
                    _order("S6", supplier_status="done", order_state=ORDER_STATE_REFUND_REQUEST),
                    # 群里撤回、只留档
                    _order("S7", supplier_status="pushed", is_recalled=True),
                ]
            )
            db.commit()

    def _orders_entry(self, path: str = "/orders", query: dict | None = None) -> dict:
        for section in nav.build(path, query or {}, True):
            for entry in section["nav_items"]:
                if entry["item"].key == "orders":
                    return entry
        raise AssertionError("侧边栏里没找到「订单归档」")

    def _counts(self, **kwargs) -> dict[str, int]:
        return {c["item"].label: c["count"] for c in self._orders_entry(**kwargs)["children"]}

    def test_each_status_shows_its_count(self) -> None:
        counts = self._counts()
        self.assertEqual(counts["待人工核对"], 1)
        self.assertEqual(counts["待推单发货"], 2)
        self.assertEqual(counts["已退单"], 3)          # S4 + S5 + S6
        self.assertEqual(counts["待拦截退单"], 2)      # S4 + S6（老数据兜底）
        self.assertEqual(counts["撤单回收站"], 1)

    def test_top_level_badge_is_pending_total(self) -> None:
        """一级徽标 = 这个菜单共有几单要处理：三项的并集，同一订单只算一次。"""
        entry = self._orders_entry()
        # S1 / S2 待推单发货 + S3 待人工核对 + S4 / S6 待拦截退单（S5 已退款、S7 已撤回都不算）
        self.assertEqual(entry["count"], 5)

    def test_top_level_badge_breaks_down_in_hint(self) -> None:
        """合计数字看不出组成，鼠标提示里把拆开的三项写出来。"""
        hint = self._orders_entry()["count_hint"]
        self.assertIn("待人工核对 1", hint)
        self.assertIn("待推单发货 2", hint)
        self.assertIn("待拦截退单 2", hint)
        self.assertIn("同一订单只算一次", hint)

    def test_top_level_badge_not_a_child_count(self) -> None:
        """一级不该再和某个子项重复：它比「待人工核对」多算了待推单发货、待拦截退单。"""
        counts = self._counts()
        self.assertGreater(self._orders_entry()["count"], counts["待人工核对"])

    def test_all_orders_has_no_count(self) -> None:
        """「全部订单」是总数不是待办，不给数字（菜单数字都表示「有几件事要处理」）。"""
        self.assertEqual(self._counts()["全部订单"], 0)

    def test_pending_push_item(self) -> None:
        children = {c["item"].label: c for c in self._orders_entry()["children"]}
        push = children["待推单发货"]
        self.assertEqual(push["item"].url, "/orders?supplier_status=pending")
        self.assertIn("待推单发货", push["count_hint"])

    def test_pending_push_matches_home_metric(self) -> None:
        """菜单上的待推单发货数 = 首页「待推单发货」那个数字。"""
        with SessionLocal() as db:
            self.assertEqual(stats_service.summary(db)["pending_push"], 2)

    def test_pending_push_item_highlights(self) -> None:
        entry = self._orders_entry(query={"supplier_status": "pending"})
        self.assertTrue(entry["open"])                                  # 一级展开
        self.assertFalse(entry["active"])                               # 一级不上底色
        marked = {c["item"].label: c["active"] for c in entry["children"]}
        self.assertTrue(marked["待推单发货"])
        self.assertFalse(marked["全部订单"])                             # 同一路径下不重复高亮

    def test_recalled_orders_not_counted_as_pending_push(self) -> None:
        """撤单的留档单不参与业务，不算待推单发货。"""
        with SessionLocal() as db:
            db.add(_order("S8", supplier_status="pending", is_recalled=True))
            db.commit()
        nav._badge_cache.clear()
        self.assertEqual(self._counts()["待推单发货"], 2)


    def test_refund_items_order(self) -> None:
        """待拦截退单排在已退单前面：前者要动手处理，后者是处理完的结果。"""
        labels = [c["item"].label for c in self._orders_entry()["children"]]
        self.assertLess(labels.index("待拦截退单"), labels.index("已退单"))


class ProductsMenuWipTest(unittest.TestCase):
    """商品管理菜单挂「Beta 开发中」标签（NavItem.wip）。"""

    def test_products_menu_marked_beta(self) -> None:
        nav._badge_cache.clear()
        for section in nav.build("/products", {}, True):
            for entry in section["nav_items"]:
                if entry["item"].key == "products":
                    self.assertTrue(entry["item"].wip, "商品管理应该带「Beta 开发中」标签")
                    return
        self.fail("侧边栏里没找到「商品管理」")


if __name__ == "__main__":
    unittest.main()
