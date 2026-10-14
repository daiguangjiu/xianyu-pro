"""商品详情页（2026-10-03 起只有 1688 版式这一版，经典版已废弃）。

口径：
1. 路由 /self/goods/detail/{item_id} 渲染 1688 版式（左图右价 + 规格选择 + Tab + 同类货源）；
2. 老地址 /self/goods/detail/{item_id}/1688 做 301 跳到无后缀那个（老书签 / 外链不失效），
   页面上不再有「经典版详情」入口；
3. 商品不存在时同样返回 404，不报错。
"""
from __future__ import annotations

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
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ITEM_ID = "T1688-DETAIL-1"
ITEM_TITLE = "测试-全棉四件套 1.8m"


def _reset() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete()
        s.commit()


def _item() -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-1",
                title=ITEM_TITLE,
                category_id="ht_bedding",
                main_image="https://img.example.com/a.jpg",
                images=["https://img.example.com/a.jpg", "https://img.example.com/b.jpg"],
                detail_images=["https://img.example.com/d1.jpg"],
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                market_price=Decimal("139.00"),
                stock=320,
                item_code="YHZ-JFJS-0001",
                brand="优眠",
                spec_defs=[{"name": "颜色", "values": ["灰", "米白"]}],
                sku_list={
                    "skus": [
                        {"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": 45.5, "stock": 120},
                        {"sku_id": "s2", "spec_text": "灰 / 1.8m", "price": 48.0, "stock": 200},
                    ]
                },
                source_type="third_party",
                audit_status="approved",
                is_listed=True,
                shipping_mode="free",
                after_sale="7天无理由退换",
                supplier_tip="走量款，别和低价款比",
            )
        )
        s.commit()


class GoodsDetail1688Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        _reset()
        _item()

    def tearDown(self):
        _reset()

    def test_10_1688_page_renders(self):
        """1688 版式能打开：价格块 / 规格 / Tab / 同类货源都在。"""
        resp = self.client.get(f"/self/goods/detail/{ITEM_ID}")
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        html = resp.text
        # 2026-10-11：「建议售价」改叫「建议零售价」（用户拍板，要强调显示）
        for marker in ("pdp-pricebox", "渔货价", "建议零售价", "pdp-sku-btn", "pdp-tab", "同类货源"):
            self.assertIn(marker, html, f"新版详情页缺少 {marker}")

    def test_14_prices_side_by_side_and_aligned(self):
        """渔货价与建议零售价并排、同一骨架对齐（2026-10-12 用户拍板）。

        以前渔货价是一行内联、建议零售价是右下角的卡片：一个在左上一个在右下，
        高度也不一样。现在两块同一个结构（标签 + 大字价 / 下面一行说明），
        CSS 用两列 grid，两块上沿与高度都齐。
        """
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text

        def cell(cls: str) -> str:
            i = html.find(f'class="pdp-price-cell {cls}"')
            self.assertGreater(i, -1, f"缺 {cls} 价格块")
            return html[i : i + 700]

        for cls, label in (("pdp-price-fish", "渔货价"), ("pdp-price-sug", "建议零售价")):
            block = cell(cls)
            self.assertIn('<div class="row1">', block, f"{label} 要「标签 + 价」一行")
            self.assertIn(f'<span class="tag">{label}</span>', block)
            self.assertIn('<span class="val">', block)
            self.assertIn('<div class="hint">', block, f"{label} 要有一行说明")
        self.assertIn("建议闲鱼售价不低于此价", cell("pdp-price-sug"))
        self.assertIn("下单按这个价结算", cell("pdp-price-fish"))
        # 两块并排（两列 grid），不是一左一右错开
        main = html[html.find(".pdp-price-main {") : html.find("}", html.find(".pdp-price-main {"))]
        self.assertIn("display: grid", main)
        self.assertIn("repeat(2, minmax(0, 1fr))", main)
        self.assertIn("align-items: stretch", main)

    def test_15_title_has_no_layout_suffix(self):
        """页面标题不再挂「（1688 版式）」（2026-10-11 用户拍板）。

        详情页只有这一版，写个版式后缀会让人以为还有别的版本可以切。
        2026-10-14 起标题统一带系统前缀【渔货栈】（见 templates/base.html）。
        """
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        # 只断言 <title>：base.html 的 CSS 注释里还写着「1688 版式」（注释会原样输出，
        # 拿整页 assertNotIn 会误命中）
        end = html.find("</title>") + len("</title>")
        title = html[html.find("<title>") : end]
        self.assertEqual(title, f"<title>【渔货栈】{ITEM_TITLE} · 商品详情</title>", title)
        self.assertNotIn("版式", title)

    def test_20_price_uses_same_numbers(self):
        """与经典版同一口径：渔货价 45.50、建议零售价 99.00。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("¥45.50", html)
        self.assertIn("¥99.00", html)

    def test_25_suggested_price_emphasised_with_note(self):
        """建议零售价要突出显示，下面还写着「建议闲鱼售价不低于此价」（2026-10-11 用户拍板）。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("pdp-price-sug", html, "建议零售价要有独立的强调卡片")
        self.assertIn("建议闲鱼售价不低于此价", html)
        self.assertIn("下单按这个价结算", html, "渔货价旁边写清它是下单结算价")

    def test_27_price_range_only_when_multi_spec(self):
        """价格区间：多规格才是 min ~ max，单规格只显示一个价（2026-10-11 用户拍板）。

        用户报障：单规格也写成「¥40.38 ~ 40.38」，而且那两个数还是按加价规则现算的，
        跟上面的建议售价（¥99.00）两个数打架。现在区间按**渔货价**算、且与规格行同源。
        """
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        # 两条规格 45.50 / 48.00 → 区间是 45.50 ~ 48.00
        self.assertIn("渔货价区间", html)
        self.assertIn("45.50 ~ 48.00", html)

    def test_28_single_spec_range_is_one_number(self):
        """单规格：区间不写成「x ~ x」。"""
        with SessionLocal() as s:
            row = s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).one()
            row.sku_list = {"skus": [
                {"sku_id": "only", "spec_text": "默认", "price": 45.5, "stock": 10},
            ]}
            s.commit()
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("渔货价区间", html)
        self.assertNotIn("45.50 ~ 45.50", html, "单规格不该写成 x ~ x")

    def test_29_order_price_is_fish_price(self):
        """结算条金额按渔货价算（2026-10-11 用户报障：这里的价格不对）。

        规格行显示渔货价、金额也按渔货价累计；「建议 ¥」只做小字提示。
        """
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("qty * (Number(data.cost_price) || 0)", html, "金额按渔货价累计")
        self.assertIn("建议 ¥", html, "规格行右侧有小字建议零售价")

    def test_26_market_price_naming(self):
        """页面上的「原价」一律叫「市场价」（2026-10-11 用户拍板）。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertNotIn("原价", html, "详情页不该再有「原价」字样")
        self.assertIn("市场价", html)

    def test_30_legacy_url_redirects(self):
        """老地址（带 /1688 后缀）301 到无后缀那个，经典版不再单独存在。"""
        resp = self.client.get(f"/self/goods/detail/{ITEM_ID}/1688", follow_redirects=False)
        self.assertEqual(resp.status_code, 301, resp.status_code)
        self.assertEqual(resp.headers.get("location"), f"/self/goods/detail/{ITEM_ID}")
        # 跟过去就是这一版
        page = self.client.get(f"/self/goods/detail/{ITEM_ID}/1688")
        self.assertEqual(page.status_code, 200)
        self.assertIn("pdp-pricebox", page.text)

    def test_40_no_classic_entry_left(self):
        """页面上不该再有「经典版详情」入口（只有一版了）。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertNotIn("经典版详情", html)
        self.assertNotIn(f"/goods/detail/{ITEM_ID}/1688", html)

    def test_45_left_scroll_right_sticky(self):
        """1688 那种两栏：左列（图 + 详情）整页滚动，右列（价格/规格/按钮）sticky 固定。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        # grid 两栏区域：gallery / detail 在左列，buy 独占右列
        self.assertIn('"gallery buy"', html)
        self.assertIn('"detail  buy"', html)
        # 右列 sticky + 左列分类 Tab 吸顶，都挂在顶栏高度（--topbar-h）下面
        self.assertIn("position: sticky; top: var(--pdp-sticky-top)", html)
        self.assertIn("top: var(--pdp-sticky-top); z-index: 30", html)
        self.assertIn("--pdp-sticky-top: var(--topbar-h)", html)
        # 左列结构：供应商条 + 竖排缩略图（含「参数」格）+ 吸顶 Tab
        for marker in ("pdp-supbar", "pdp-thumbs-v", "pdp-thumb-tool", "pdp-tabbar", "pdp-skurow"):
            self.assertIn(marker, html, f"新版详情页缺少 {marker}")

    def test_46_mobile_single_column_and_dock(self):
        """手机端退回单列 + 吸底操作条。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("@media (max-width: 900px)", html)
        self.assertIn("pdp-dockbar", html)
        self.assertIn("pdpDockBuyNow", html)

    def test_47_tabs_and_supplier_tip(self):
        """Tab：商品详情 / 规格参数 / 铺货 / 供应商提示（⚠️）/ 同类货源，供应商提示独立成页签。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        # 每个 Tab 都带图标；「铺货与预填包」已改名「铺货」
        for tab_id in ("pdpTabDetail", "pdpTabSpec", "pdpTabHandoff", "pdpTabTip", "pdpTabSimilar"):
            self.assertIn(f'data-tab="{tab_id}"', html, f"缺少 {tab_id} 页签")
        self.assertIn("bi-exclamation-triangle-fill", html)
        self.assertIn(">供应商提示</button>", html)
        self.assertNotIn("铺货与预填包", html)
        self.assertNotIn("闲鱼发布预填包（共", html)
        self.assertIn("闲鱼发布铺货信息", html)
        # 供应商提示独立面板，不再塞在商品详情面板里
        self.assertIn('class="pdp-panel" id="pdpTabTip" hidden', html)
        self.assertIn("pdp-tipbox", html)
        # 标题不再带「（29/30）」这类字数提示
        import re
        self.assertIsNone(re.search(r"（\d+/30）", html), "标题字数提示应已去掉")

    def test_48_thumbs_match_stage_height(self):
        """竖排缩略图列与大图等高：外层定位框 + 内层绝对铺满，超出列内滚动。"""
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn(".pdp-thumbs-col { position: relative; }", html)
        self.assertIn("position: absolute; inset: 0;", html)
        self.assertIn("pdp-thumbs-col", html)

    def test_50_missing_item_404(self):
        """商品不存在：新版返回 404（不是 500）。"""
        resp = self.client.get("/self/goods/detail/not-exist-xyz/1688")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
