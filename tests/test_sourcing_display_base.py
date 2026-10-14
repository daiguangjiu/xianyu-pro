"""渔货栈货源展示基数（2026-10-13 用户拍板）。

渔货栈货源广场卡片上的「X 家上架」与「已售 N」以前是**裸的真实值**：

- 「X 家上架」以前取的是「同款商品有多少供货商在架」，跟"谁铺过这款货"没关系，
  所以永远显示 1 —— 现在改成**真实铺过这款货的店铺数**（铺货记录按账号去重）；
- 新品刚上架真实销量是 0，卡片就写「已售 0」，看着没人买。

现在渔枢系统「货源 · 展示基数」页可以配两个基数，卡片显示「基数 + 真实值」。
规矩是**真实值照旧单独算**：排序、热销榜计数、商品详情页「在售闲鱼号」一律不加
基数（用户原话「实际的上架数量仍然要准确的」）。
"""
from __future__ import annotations

import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SourcingDisplayConfig  # noqa: E402
from app.services import sourcing_display as display_service  # noqa: E402
from app.timeutil import utc_now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import hub_display_base, reset_hub_caches  # noqa: E402
from src.models import ChannelListing, PurchaseOrder, SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ITEM_ID = "T-SHOW-BASE-1"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(PurchaseOrder).filter(
            PurchaseOrder.ali_item_id == ITEM_ID
        ).delete(synchronize_session=False)
        s.query(ChannelListing).filter(
            ChannelListing.item_id == ITEM_ID
        ).delete(synchronize_session=False)
        s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete(
            synchronize_session=False
        )
        s.query(SourcingDisplayConfig).delete()
        s.commit()


def _make_item() -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-1",
                title="测试-展示基数 北欧四件套",
                category_id="ht_bedding",
                main_image="https://img.example.com/a.jpg",
                images=["https://img.example.com/a.jpg"],
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                stock=100,
                source_type="self_operated",
                audit_status="approved",
                is_listed=True,
                last_synced_at=utc_now(),
            )
        )
        s.commit()


def _card_numbers(html: str, item_id: str) -> tuple[int | None, int | None]:
    """从货源广场取这张卡片的「X 家上架」与「已售 N」。

    页面头部内联 CSS 与活动节点区块也会出现商品链接/数字，所以按 <article 切卡片。
    """
    for seg in html.split("<article")[1:]:
        if item_id not in seg:
            continue
        shops = re.search(r"(\d+)\s*家上架", seg)
        sold = re.search(r"已售\s*(\d+)", seg)
        return (
            int(shops.group(1)) if shops else None,
            int(sold.group(1)) if sold else None,
        )
    return (None, None)


class DisplayBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        _wipe()
        _make_item()
        reset_hub_caches()

    def tearDown(self):
        _wipe()

    # ---------------------------------------------------------------- 默认值
    def test_10_default_is_zero(self):
        """没配过基数：读到 0，卡片显示的就是真实值。"""
        with SessionLocal() as s:
            cfg = display_service.get_config(s)
        self.assertEqual(cfg["listed_shops_base"], 0)
        self.assertEqual(cfg["sold_base"], 0)
        base = hub_display_base(force=True)
        self.assertEqual(base, {"listed_shops_base": 0, "sold_base": 0})

    def test_20_config_page_reachable(self):
        """渔枢后台「货源 · 展示基数」页能打开且有那两个输入框。"""
        page = self.client.get("/sourcing-display")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn('name="listed_shops_base"', page.text)
        self.assertIn('name="sold_base"', page.text)

    # ---------------------------------------------------------------- 保存
    def test_30_save_and_read_back(self):
        """保存 10 / 5：库里落值，渔货栈清缓存后读到 10 / 5。"""
        resp = self.client.post(
            "/sourcing-display",
            data={"listed_shops_base": "10", "sold_base": "5", "note": "冷启动"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            cfg = display_service.get_config(s)
        self.assertEqual(cfg["listed_shops_base"], 10)
        self.assertEqual(cfg["sold_base"], 5)
        self.assertEqual(cfg["note"], "冷启动")
        reset_hub_caches()
        self.assertEqual(
            hub_display_base(force=True),
            {"listed_shops_base": 10, "sold_base": 5},
        )

    def test_40_bad_values_fall_back(self):
        """乱值按 0，负数按 0，超大封顶 —— 页面上的数字不能被填坏。"""
        with SessionLocal() as s:
            row = display_service.save_config(
                s,
                {"listed_shops_base": "abc", "sold_base": "-8"},
            )
            self.assertEqual(row.listed_shops_base, 0)
            self.assertEqual(row.sold_base, 0)
            row = display_service.save_config(
                s, {"listed_shops_base": "99999999"}
            )
            self.assertEqual(row.listed_shops_base, display_service.BASE_MAX)

    # ---------------------------------------------------------------- 卡片
    def _seed_real(self) -> None:
        """真实值：2 个账号铺过这款货 + 已成交 3 件。"""
        with SessionLocal() as s:
            s.add(
                ChannelListing(
                    item_id=ITEM_ID,
                    channel="xianyu",
                    owner="u1",
                    status="published",
                )
            )
            s.add(
                ChannelListing(
                    item_id=ITEM_ID,
                    channel="xianyu",
                    owner="u2",
                    status="published",
                )
            )
            # 平台级记录（owner 为空）不算任何人的店铺
            s.add(
                ChannelListing(
                    item_id=ITEM_ID, channel="xianyu", owner="", status="published"
                )
            )
            s.add(
                PurchaseOrder(
                    downstream_order_id="dso-test-1",
                    ali_item_id=ITEM_ID,
                    ali_sku_id="sku-1",
                    qty=3,
                    cost_price=Decimal("45.50"),
                    status="paid",
                )
            )
            s.commit()

    def test_50_card_shows_base_plus_real(self):
        """没配基数 → 2 家 / 已售 3；配了 10 与 5 → 12 家 / 已售 8。"""
        self._seed_real()
        html = self.client.get("/self/harbor").text
        self.assertEqual(_card_numbers(html, ITEM_ID), (2, 3), "没配基数时就是真实值")

        self.client.post(
            "/sourcing-display",
            data={"listed_shops_base": "10", "sold_base": "5"},
            follow_redirects=False,
        )
        reset_hub_caches()
        html = self.client.get("/self/harbor").text
        self.assertEqual(
            _card_numbers(html, ITEM_ID),
            (12, 8),
            "卡片显示的是「后台基数 + 真实数量」",
        )

    def test_60_real_value_stays_accurate(self):
        """真实值不能被基数污染：详情页「在售闲鱼号」仍然是 2。"""
        self._seed_real()
        self.client.post(
            "/sourcing-display",
            data={"listed_shops_base": "10", "sold_base": "5"},
            follow_redirects=False,
        )
        reset_hub_caches()
        page = self.client.get(f"/self/goods/detail/{ITEM_ID}")
        self.assertEqual(page.status_code, 200)
        m = re.search(r'class="n">(\d+)</span>\s*<span class="l">在售闲鱼号', page.text)
        self.assertIsNotNone(m, "详情页应展示在售闲鱼号")
        self.assertEqual(int(m.group(1)), 2, "运营看到的真实数量不能被基数虚高")

    def test_70_menu_has_entry(self):
        """渔枢后台「货源」组有「展示基数」入口。"""
        page = self.client.get("/sourcing")
        if page.status_code == 200:
            self.assertIn("/sourcing-display", page.text)


if __name__ == "__main__":
    unittest.main()
