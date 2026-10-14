"""渔货栈商品与渔枢系统「同一套字段口径」（2026-09-29 版本）。

渔枢系统商品详情页是这套口径的基础，渔货栈 /self/goods/detail/{item_id} 同步：

  1 品牌属于商品属性，可留空；型号 / 成色 / 重量是**规格维度**，按类目推荐、用户自己挑；
  2 在售闲鱼号是**统计数量**（渔货栈按已发布的铺货记录数），不是手填字段；
  3 商家编码自动生成：YHZ + 一二三级类目简拼 + 日期 + 4 位流水号；
  4 成本口径：渔货栈对供应链的采购价不对外展示、渔货栈不显示；渔货价对自营的人就是成本价，
    毛利 = 建议零售价 − 渔货价；
  5 供应商友情提示（渔货栈特色信息）写在商品详情（图文）下面；
  6 运费模板 / 闲鱼发货方式与渔枢系统共用「货源 · 运费模板」的规则，上架时按类目自动套用；
  7 别名按商品详情给 5 个左右推荐，可删可改；
  8 最近上架不再手填（新建页已去掉该字段），详情页只读展示。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductCategory, ShippingRule  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.services import shipping as shipping_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src import deps as src_deps  # noqa: E402
from src.goods import catalog as goods_catalog  # noqa: E402
from src.goods import service as goods_service  # noqa: E402
from src.models import (  # noqa: E402
    Base as SrcBase,
    ChannelListing,
    SupplierItem,
)
from src.schemas import GoodsItemCreateIn  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class SelfGoodsDetailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        # 渔货栈自己的表（supplier_items 等）：建表 + 补列兜底
        engine = src_deps._get_engine()
        SrcBase.metadata.create_all(engine)
        src_deps._ensure_columns(engine, SrcBase)
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ChannelListing).delete()
            s.query(SupplierItem).delete()
            s.query(ShippingRule).delete()
            s.query(Product).delete()
            s.query(ProductCategory).delete()
            s.commit()
            # 重新播「类目管理」里那套真实类目：渔货栈类目 id（dg_audio）要靠它翻成中文路径
            category_service.ensure_seeded(s)
            top = s.scalar(
                select(ProductCategory).where(
                    ProductCategory.name == "家用电器", ProductCategory.level == 1
                )
            )
            sub = s.scalar(select(ProductCategory).where(ProductCategory.source_id == "dg_audio"))
            self.assertIsNotNone(top)
            self.assertIsNotNone(sub, "播种的类目里应有 dg_audio（耳机音响）")
            # 类目特性规则 + 运费 / 发货方式规则（与渔枢系统共用同一张表）
            category_service.set_rules(
                s, int(top.id),
                "产品词：无线蓝牙耳机\n修饰词：全新未拆封\n热搜词：降噪、长续航\n经营提示：客单价 100-300 元",
            )
            shipping_service.create_rule(s, kind="freight", name="全国包邮", fee=0, is_default=True)
            shipping_service.create_rule(s, kind="freight", name="家用电器运费 ¥6", fee=6, category_id=int(top.id))
            shipping_service.create_rule(s, kind="method", name="家用电器固定收费", mode="fixed", category_id=int(top.id))
            s.commit()
            self.top_id, self.sub_id = int(top.id), int(sub.id)
            self.category_id = "dg_audio"
        # 类目刚重播过：清掉渔货栈那份 30 秒缓存，让它按新类目算编码 / 运费
        goods_catalog._hub_category_cache = None  # noqa: SLF001

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(ChannelListing).delete()
            s.query(SupplierItem).delete()
            s.commit()

    def _make_item(self, **kw) -> SupplierItem:
        payload = GoodsItemCreateIn(
            title=kw.pop("title", "罗技 MX Master 3S 无线鼠标"),
            category_id=kw.pop("category_id", self.category_id),
            brand=kw.pop("brand", "罗技"),
            cost_price=Decimal(kw.pop("cost_price", "140")),
            sale_price=Decimal(kw.pop("sale_price", "189")),
            stock=kw.pop("stock", 30),
            fish_price=Decimal(kw.pop("fish_price", "140")),
            shipping_fee=Decimal(kw.pop("shipping_fee", "0")),
            submit_action=kw.pop("submit_action", "publish"),
            **kw,
        )
        with SessionLocal() as s:
            item = goods_service.create_item(s, payload)
            s.commit()
            return item

    # ------------------------------------------------------------ 详情分组展示
    def test_80_detail_groups_and_image_tab(self):
        """渔货栈商品详情：文案一组一段（可复制 / AI 重写）；详情图单独一个「商品详情图」页签，
        每 9 张一组，每组一张居中大图（带 x/x 角标）+ 下面缩略图。"""
        item = self._make_item(
            detail_html="<p>第一段说明</p>\n<p><img src=\"/media/x.png\"></p>\n<p>第二段说明</p>",
            detail_images=[f"/media/oh/d{i}.png" for i in range(1, 12)],
            selling_points="全新未拆封,包邮",
        )
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        for text in ("文案一", "文案二", "复制", "AI 重写", "pdp-detail-group",
                     'data-tab="pdpTabImages"', "商品详情图",
                     "pdp-img-group", "pdp-imgpick-stage", "pdp-img-index", "1/9"):
            self.assertIn(text, html, f"渔货栈详情应有「{text}」")
        # 11 张图 → 9 + 2 两组，各组都带缩略图条
        self.assertEqual(html.count('class="pdp-img-group"'), 2, "11 张图要切成两组")
        self.assertEqual(html.count('class="pdp-imgpick-thumbs"'), 2, "每组的其它图放成缩略图")
        # 详情图不再堆在「商品详情」页签里，那边只留入口按钮
        self.assertIn("去「商品详情图」看看", html)
        # AI 重写要带上商品信息（品牌 / 类目 / 卖点）
        self.assertIn("REWRITE_CTX", html)
        self.assertIn("罗技", html)

    def test_86_selling_points_tab_and_real_image_count(self):
        """卖点独立「商品卖点」页签（可复制），右列标题下不再塞卖点；
        铺货预填的「图片」数 = 商品实际关联图数（/media 站内图也算数）。"""
        item = self._make_item(
            selling_points="全新未拆封,官方正品\n顺丰包邮",
            main_image="/media/oh/main.png",
            images=["/media/oh/g1.png", "/media/oh/g2.png"],
            detail_images=["/media/oh/d1.png"],
        )
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        # 卖点页签与面板：三条卖点逐条展示 + 一键复制
        self.assertIn('data-tab="pdpTabPoints"', html)
        self.assertIn('id="pdpTabPoints"', html)
        for point in ("全新未拆封", "官方正品", "顺丰包邮"):
            self.assertIn(point, html)
        self.assertIn("复制全部卖点", html)
        self.assertIn("function pdpCopyPoints", html)
        # 右列标题下的卖点小字没了（.pdp-sub 连同样式一起删掉）
        self.assertNotIn('class="pdp-sub"', html)
        # 主图 1 + 图组 2 + 详情图 1 = 4 张（站内 /media 路径不再被丢掉）
        self.assertIn('class="n">4</span><span class="l">图片', html, "预填卡「图片」显示实际张数")
        self.assertIn("4 张", html, "铺货页签「图片 N 张」同口径")
        # 没有卖点时不出现卖点页签
        plain = self._make_item(title="没有卖点的货")
        html2 = self.client.get(f"/self/goods/detail/{plain.item_id}").text
        self.assertNotIn('data-tab="pdpTabPoints"', html2)

    def test_82_detail_images_inside_html_get_own_tab(self):
        """1688 抓来的货没有 detail_images 字段：detail_html 里混着的图要捞进「商品详情图」页签，
        文案组里只剩文字（不然分组展示里文案和图又混在一起）。"""
        import re

        item = self._make_item(
            detail_html='<p>第一段说明</p>\n<p><img src="/media/oh/a1.png"></p>\n'
                        '<p><img src="/media/oh/a2.png"></p>',
        )
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn('data-tab="pdpTabImages"', html, "有详情图就该出现「商品详情图」页签")
        self.assertIn("/media/oh/a1.png", html)
        self.assertIn("/media/oh/a2.png", html)
        first = re.search(r'data-group="1">(.*?)</div>', html, re.S)
        self.assertIsNotNone(first, "应有一组文案")
        self.assertNotIn("<img", first.group(1), "文案组里只放文字，图归「商品详情图」页签")

    def test_85_ai_rewrite_endpoint(self):
        """AI 重写接口：按类目词库把一组文案重写一版（前端拿到后自动进剪贴板）。"""
        resp = self.client.post(
            "/products/ai-rewrite-block",
            json={"text": "1. 全新未拆封\n2. 支持验货", "name": "罗技 MX Master 3S",
                  "brand": "罗技", "category": "家用电器 / 耳机音响",
                  "selling_points": "全新未拆封,包邮"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("全新未拆封", data["text"])
        self.assertNotIn("<img", data["text"], "重写结果里不该带图片标签")

    # ------------------------------------------------------------ 字段同步
    def test_10_detail_shows_same_fields_as_hub(self):
        """渔货栈商品详情（1688 版式）：品牌 / 在售闲鱼号（数量）/ 默认运费 /
        最近上架 / 发货方式 / 供应商提示，与渔枢系统端同一口径。"""
        item = self._make_item()
        page = self.client.get(f"/self/goods/detail/{item.item_id}")
        self.assertEqual(page.status_code, 200, "渔货栈商品详情页应能打开")
        html = page.text
        for text in ("品牌", "在售闲鱼号", "默认运费", "最近上架", "发货方式", "供应商提示"):
            self.assertIn(text, html, f"渔货栈详情页应有「{text}」")
        self.assertIn("罗技", html)

    def test_20_live_shop_count_comes_from_listings(self):
        """在售闲鱼号是统计值：有已发布的铺货记录才计数（右列「在售闲鱼号（个）」）。"""
        import re

        def _live_count(text: str) -> int:
            m = re.search(r'class="n">(\d+)</span>\s*<span class="l">在售闲鱼号', text)
            self.assertIsNotNone(m, "详情页应展示在售闲鱼号")
            return int(m.group(1))

        item = self._make_item()
        self.assertEqual(_live_count(self.client.get(f"/self/goods/detail/{item.item_id}").text), 0)
        # 铺货记录按账号隔离（2026-10-13）：一条记录 = 一个账号的一款货，
        # 所以要带 owner；平台级记录（owner 为空）不算任何人的在售
        with SessionLocal() as s:
            s.add(
                ChannelListing(
                    item_id=item.item_id, channel="xianyu", owner="u1", status="published"
                )
            )
            s.add(
                ChannelListing(
                    item_id=item.item_id, channel="xianyu", owner="u2", status="published"
                )
            )
            s.add(
                ChannelListing(
                    item_id=item.item_id, channel="xianyu", owner="", status="published"
                )
            )
            s.commit()
        self.assertEqual(
            _live_count(self.client.get(f"/self/goods/detail/{item.item_id}").text),
            2,
            "在售闲鱼号按账号计数（两个账号铺过 = 2，平台级记录不算）",
        )

    def test_30_brand_optional_and_spec_dims_follow_category(self):
        """品牌可留空；型号 / 成色 / 重量按类目推荐（与渔枢系统端同名维度）。"""
        item = self._make_item(brand=None, category_id="dg_audio")
        with SessionLocal() as s:
            row = s.get(SupplierItem, item.id)
            self.assertIsNone(row.brand, "品牌可以不填")
        digi = [d["name"] for d in goods_catalog.spec_template_for("dg_audio")]
        self.assertIn("型号", digi)
        self.assertIn("成色", digi)
        self.assertIn("成色", [d["name"] for d in goods_catalog.spec_template_for("ap_men")])
        self.assertIn("重量", [d["name"] for d in goods_catalog.spec_template_for("ht_bed")])

    # ------------------------------------------------------------ 编码 / 规则 / 成本口径
    def test_40_publish_form_uses_same_headline_fields(self):
        """渔货栈上架表单：品牌 / 供应商友情提示 / 渔货价 / 发货方式 / 编码预览，
        不再手填「成本价」「最近上架」；别名随渔枢系统一起下线（2026-10-11）。"""
        page = self.client.get("/self/goods/new")
        self.assertEqual(page.status_code, 200)
        for field in ('name="brand"', 'name="supplier_tip"',
                      'id="fishInput"', 'name="shipping_mode"', 'id="codePreview"', 'AI 生成渔货商品标题'):
            self.assertIn(field, page.text, f"{field} 应在渔货栈上架表单里")
        for gone in ('name="cost_price"', 'name="listed_at"', 'name="aliases"'):
            self.assertNotIn(gone, page.text, f"{gone} 不该再出现在渔货栈上架表单里")

    def test_45_item_code_auto_generated(self):
        """商家编码自动生成：YHZ + 一二三级类目简拼 + 日期 + 4 位流水号。"""
        item = self._make_item()
        self.assertEqual(item.item_code, f"YHZ-JYDQ-EJYX-{datetime.now():%Y%m%d}-0001")
        second = self._make_item(title="第二件")
        self.assertTrue(second.item_code.endswith("-0002"), second.item_code)

    def test_50_shipping_defaults_applied_from_hub_rules(self):
        """上架时留「自动」，就按类目套用渔枢系统「货源 · 运费模板」里的模板与发货方式。"""
        item = self._make_item()
        with SessionLocal() as s:
            row = s.get(SupplierItem, item.id)
            self.assertEqual(row.freight_template, "家用电器运费 ¥6")
            self.assertEqual(float(row.shipping_fee), 6.0)
            self.assertEqual(row.shipping_mode, "fixed")

    def test_55_cost_basis_is_fish_price(self):
        """成本口径：渔货价对自营的人就是成本价；毛利 = 建议零售价 − 渔货价。"""
        item = self._make_item(cost_price="999", fish_price="150", sale_price="199")
        with SessionLocal() as s:
            row = s.get(SupplierItem, item.id)
            self.assertEqual(float(row.cost_price), 999.0, "库里存的采购成本不动")
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        # 页面上统一按「渔货价（向平台采购价格）」展示，不再暴露成本来源那套措辞
        self.assertIn("渔货价", html)
        self.assertNotIn("拿货价（1688 成本）", html, "渔货栈不展示渔货栈对供应链的采购价")
        self.assertIn("49.00", html, "单件毛利 = 199 − 150")

    def _add_hub_product(self, *, name: str, code: str = "", fish_price: float = 0) -> int:
        """往渔枢系统（写入库）放一条商品；测试结束自动清掉，不污染别的用例。"""
        with SessionLocal() as s:
            row = Product(
                name=name,
                code=code,
                fish_price=fish_price,
                cost_amount=50,
                price=189,
                source_type="self_operated",
            )
            s.add(row)
            s.commit()
            pid = int(row.id)

        def _cleanup() -> None:
            with SessionLocal() as s:
                obj = s.get(Product, pid)
                if obj:
                    s.delete(obj)
                    s.commit()

        self.addCleanup(_cleanup)
        return pid

    def test_56_cost_comes_from_hub_by_item_code(self):
        """成本价取渔枢系统的渔货价：商家编码一致时按编码对应（渔枢系统是写入库）。"""
        code = "YHZ-渔枢系统-0001"
        self._add_hub_product(name="罗技 MX Master 3S 无线鼠标", code=code, fish_price=88)
        item = self._make_item(fish_price="140", sale_price="189")
        with SessionLocal() as s:
            s.get(SupplierItem, item.id).item_code = code
            s.commit()
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("渔货价", html, "成本应当以渔枢系统那边的渔货价为准")
        self.assertIn("88.00", html)
        self.assertIn("101.00", html, "单件毛利 = 189 − 88（按渔枢系统渔货价算）")

    def test_57_cost_matches_hub_by_name(self):
        """编码还没同步上时，按商品名对应渔枢系统商品。"""
        self._add_hub_product(name="罗技 MX Master 3S 无线鼠标", fish_price=99)
        item = self._make_item(fish_price="140", sale_price="189")
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("渔货价", html)
        self.assertIn("90.00", html, "单件毛利 = 189 − 99")

    def test_58_hub_without_fish_price_falls_back(self):
        """渔枢系统那边还没定渔货价（0）：不拿 0 当成本，回落渔货栈口径。"""
        self._add_hub_product(name="罗技 MX Master 3S 无线鼠标", fish_price=0)
        item = self._make_item(fish_price="140", sale_price="189")
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("渔货价", html)
        self.assertIn("49.00", html, "单件毛利 = 189 − 140")

    def test_60_supplier_tip_sits_under_detail_html(self):
        """供应商提示：渔货栈特色信息，1688 版独立成页签（商品详情 → … → 供应商提示）。"""
        item = self._make_item(supplier_tip="档口周一到周五发货，急单慎拍")
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("供应商提示", html)
        tip_at = html.find("档口周一到周五发货")
        self.assertGreater(tip_at, 0)
        self.assertGreater(tip_at, html.find('id="pdpTabTip"'), "提示内容在「供应商提示」页签里")

    def test_65_suggest_helpers_use_category_rules(self):
        """标题推荐在渔货栈也能用（走同一套类目特性规则）；别名接口已随字段下线。"""
        with SessionLocal() as s:
            titles = s and self.client.post(
                "/self/goods/suggest-title",
                json={"name": "苹果 AirPods Pro 3 降噪耳机", "brand": "苹果", "category_id": self.category_id},
            )
        self.assertEqual(titles.status_code, 200)
        data = titles.json()
        self.assertTrue(data["titles"])
        self.assertLessEqual(max(len(t["title"]) for t in data["titles"]), 30)
        # 别名（含推荐接口）已整体下线（2026-10-11）
        self.assertEqual(
            self.client.post("/self/goods/suggest-aliases", json={"name": "x"}).status_code, 404
        )

    def test_70_next_code_preview(self):
        """发布页的编码预览接口：选完类目就能看到将要用的编号。"""
        resp = self.client.get(f"/self/goods/next-code?category_id={self.category_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["code"].startswith("YHZ-JYDQ-EJYX-"))

    def test_75_category_rules_exposed_to_publish_page(self):
        """发布页能拿到类目特性规则（词库 + 经营提示）。"""
        resp = self.client.get(f"/self/goods/category-rules?category_id={self.category_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["category"], "家用电器")
        self.assertIn("降噪", data["words"]["热搜词"])
        self.assertTrue(any("客单价" in tip for tip in data["tips"]))


if __name__ == "__main__":
    unittest.main()
