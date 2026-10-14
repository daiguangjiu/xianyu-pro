"""商品相关的「规则」能力（2026-09-29 版本）：

  1 商家编码自动生成：YHZ + 一二三级类目简拼 + 日期 + 4 位流水号（不用手填）；
  2 类目特性规则：一级类目上维护经验规则，不影响类目显示，
    用于 ① AI 生成渔货商品标题（品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词，填满 30 字）
        ② 推荐别名（5 个左右，可删可改）③ 上架时把经营提示展示给运营；
  3 运费模板 / 闲鱼发货方式：一条全局默认 + 关联供应商 + 关联类目，越具体越优先，
    上架商品时留「自动」就按供应商 + 类目套用；
  4 供应商友情提示：渔货栈特色信息，写在商品详情（图文）下面。
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
from app.models import Product, ProductCategory, ShippingRule, Supplier  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.services import shipping as shipping_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
RULES_SAMPLE = """产品词：无线蓝牙耳机
修饰词：全新未拆封、正品保障
热搜词：降噪、长续航
长尾词：学生党、通勤
经营提示：客单价 100-300 元；主图用白底"""


class GoodsRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ShippingRule).delete()
            s.query(Product).delete()
            s.query(Supplier).delete()
            s.query(ProductCategory).delete()
            s.commit()
            # 重新播一遍「类目管理」里那套真实类目（渔货栈编码 / 运费规则都按类目名对）
            category_service.ensure_seeded(s)
            top = s.scalar(
                select(ProductCategory).where(
                    ProductCategory.name == "家用电器", ProductCategory.level == 1
                )
            )
            self.top_id = int(top.id)

    def tearDown(self) -> None:
        self.setUp()

    # -------------------------------------------------- 1 商家编码
    def test_10_code_is_global_serial(self):
        """商品流水 ID = XYSK + 日期 + 4 位序号，序号全局累计（不按天 / 不按类目重置）。"""
        with SessionLocal() as s:
            first = product_service.next_code(s, "家用电器 / 耳机音响 / 蓝牙耳机")
            self.assertEqual(first, f"XYSK{datetime.now():%Y%m%d}0001")
            # 占掉 0001 再取下一个：序号递增，不会撞
            s.add(Product(name="占位", code=first))
            s.commit()
            second = product_service.next_code(s, "家用电器 / 耳机音响 / 蓝牙耳机")
            self.assertTrue(second.endswith("0002"))
            s.add(Product(name="占位二", code=second))
            s.commit()
            # 换个类目不重新起算
            self.assertTrue(product_service.next_code(s, "女装男装 / 男装").endswith("0003"))
            # 没选类目也能出号（前缀与序号规则不变）
            self.assertTrue(product_service.next_code(s, "").endswith("0003"))
            # 老编码（YHZ-…）不算数，不参与序号计算
            self.assertEqual(product_service.serial_of("YHZ-JYDQ-EJYX-20260929-0007"), 0)
            self.assertEqual(product_service.serial_of("XYSK202610030015"), 15)

    # -------------------------------------------------- 2 类目特性规则
    def test_20_category_rules_do_not_touch_display(self):
        """类目特性规则挂在类目上，但类目本身的名字 / 层级 / 显示不受影响。"""
        before = [c["name"] for c in category_service.tree(SessionLocal())]
        self.assertTrue(category_service.set_rules(SessionLocal(), self.top_id, RULES_SAMPLE))
        with SessionLocal() as s:
            node = s.get(ProductCategory, self.top_id)
            self.assertEqual(node.rules.strip(), RULES_SAMPLE.strip())
            self.assertEqual([c["name"] for c in category_service.tree(s)], before, "类目显示不变")
            # 二级 / 三级类目上取规则时回落到一级
            rule = category_service.rules_for(s, "家用电器 / 耳机音响 / 蓝牙耳机")
            self.assertEqual(rule["category"], "家用电器")
            self.assertIn("降噪", rule["rules"]["hot"])
            self.assertTrue(any("客单价" in tip for tip in rule["rules"]["tips"]))

    def test_25_rules_parsing_is_forgiving(self):
        """规则解析要宽容：认不出的行当「经营提示」，值用顿号 / 逗号 / 空格都行。"""
        parsed = category_service.parse_rules("热搜词：降噪 长续航,低延迟\n随手写的一段经验\n产品词:耳机")
        self.assertEqual(parsed["hot"], ["降噪", "长续航", "低延迟"])
        self.assertEqual(parsed["product"], ["耳机"])
        self.assertIn("随手写的一段经验", parsed["tips"])

    def test_30_ai_title_uses_category_rules(self):
        """AI 生成标题：品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词，每条 ≤ 30 字。"""
        with SessionLocal() as s:
            category_service.set_rules(s, self.top_id, RULES_SAMPLE)
            data = product_service.suggest_titles(
                s,
                name="苹果 AirPods Pro 3 主动降噪蓝牙耳机",
                brand="苹果",
                category="家用电器 / 耳机音响",
                selling_points="主动降噪\n长续航",
            )
        self.assertTrue(data["has_rule"], "维护过类目规则就该走规则那套")
        self.assertGreaterEqual(len(data["titles"]), 3, "至少要给几条候选")
        for item in data["titles"]:
            self.assertLessEqual(item["length"], 30)
            self.assertLessEqual(len(item["title"]), 30)
        joined = " ".join(item["title"] for item in data["titles"])
        self.assertIn("苹果", joined, "品牌词要进标题")
        self.assertTrue("降噪" in joined or "长续航" in joined, "热搜词要进标题")
        self.assertIn(data["rule_hint"], "品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词")

    def test_35_ai_title_works_without_rules(self):
        """类目没维护规则也要能用（退回商品自身信息），不能空白。"""
        with SessionLocal() as s:
            data = product_service.suggest_titles(s, name="罗技 MX 机械键盘", brand="罗技", category="")
            self.assertFalse(data["has_rule"])
            self.assertGreaterEqual(len(data["titles"]), 1)
            self.assertIn("罗技", data["titles"][0]["title"])

    # 别名推荐（test_40）已随「别名」字段整体下线删除，2026-10-11

    # -------------------------------------------------- 3 运费模板 / 发货方式
    def test_50_shipping_rule_priority(self):
        """命中顺序：供应商 + 类目 > 类目 > 供应商 > 全局默认。"""
        with SessionLocal() as s:
            supplier = Supplier(name="深圳数码档口", code="SZ01")
            s.add(supplier)
            s.commit()
            sid = int(supplier.id)
            shipping_service.create_rule(s, kind="freight", name="全国包邮", fee=0, is_default=True)
            shipping_service.create_rule(s, kind="freight", name="该档口运费 ¥8", fee=8, supplier_id=sid)
            shipping_service.create_rule(s, kind="freight", name="家用电器运费 ¥6", fee=6, category_id=self.top_id)
            shipping_service.create_rule(s, kind="method", name="默认包邮", mode="free", is_default=True)

            # 只有全局默认时：谁都用默认
            r = shipping_service.resolve_for_product(s, category_text="女装男装", supplier_id=None)
            self.assertEqual(r["freight_template"], "全国包邮")
            # 类目命中（比供应商更具体时优先）
            r = shipping_service.resolve_for_product(s, category_text="家用电器 / 耳机音响", supplier_id=sid)
            self.assertEqual(r["freight_template"], "家用电器运费 ¥6")
            # 类目对不上 → 落到供应商规则
            r = shipping_service.resolve_for_product(s, category_text="女装男装", supplier_id=sid)
            self.assertEqual(r["freight_template"], "该档口运费 ¥8")
            # 发货方式走全局默认
            r = shipping_service.resolve_for_product(s, category_text="家用电器", supplier_id=None)
            self.assertEqual(r["shipping_mode"], "free")

    def test_55_shipping_method_has_four_options(self):
        """闲鱼发货方式四种：包邮 / 按距离付费 / 固定收费 / 买家自提。"""
        values = [value for value, _ in product_service.SHIPPING_CHOICES]
        self.assertEqual(values, ["free", "distance", "fixed", "self_pickup"])
        self.assertEqual(shipping_service.method_label("fee"), "邮寄：不包邮 - 固定收费", "历史值要有说法")

    def test_57_new_product_takes_shipping_defaults(self):
        """新建商品时留「自动」，就按供应商 + 类目套用规则里的模板与发货方式。"""
        with SessionLocal() as s:
            shipping_service.create_rule(s, kind="freight", name="全国包邮", fee=0, is_default=True)
            shipping_service.create_rule(s, kind="freight", name="家用电器运费 ¥6", fee=6, category_id=self.top_id)
            shipping_service.create_rule(s, kind="method", name="家用电器固定收费", mode="fixed", category_id=self.top_id)
            product = product_service.create_product(
                s,
                {"name": "测试耳机", "category": "家用电器 / 耳机音响", "price": "199", "status": "draft"},
            )
            s.commit()
            self.assertEqual(product.freight_template, "家用电器运费 ¥6")
            self.assertEqual(float(product.shipping_fee), 6.0)
            self.assertEqual(product.shipping_mode, "fixed")
            self.assertTrue(product.code.startswith(f"XYSK{datetime.now():%Y%m%d}"))

    def test_59_global_default_is_unique_per_kind(self):
        """每种规则只能有一条全局默认（设了新的，旧的自动取消）。"""
        with SessionLocal() as s:
            first = shipping_service.create_rule(s, kind="freight", name="包邮 A", is_default=True)
            shipping_service.create_rule(s, kind="freight", name="包邮 B", is_default=True)
            s.refresh(first)
            self.assertFalse(first.is_default)
            self.assertEqual(shipping_service.default_rule(s, "freight").name, "包邮 B")

    def test_60_renaming_template_updates_products(self):
        """运费模板改名后，商品上存的旧名字要跟着改（不然就对不上了）。"""
        with SessionLocal() as s:
            rule = shipping_service.create_rule(s, kind="freight", name="固定运费 ¥6", fee=6, is_default=True)
            product = product_service.create_product(
                s, {"name": "测试商品", "price": "10", "freight_template": "固定运费 ¥6"}
            )
            s.commit()
            shipping_service.update_rule(s, rule, {"name": "固定运费 ¥8"})
            s.refresh(product)
            self.assertEqual(product.freight_template, "固定运费 ¥8")

    # -------------------------------------------------- 4 页面
    def test_70_pages_render(self):
        """/shipping-rules 与类目管理页都要能打开，并带上新东西。"""
        page = self.client.get("/shipping-rules")
        self.assertEqual(page.status_code, 200)
        for text in ("运费模板", "闲鱼发货方式", "全局默认", "关联类目", "关联供应商"):
            self.assertIn(text, page.text)

        cats = self.client.get("/products/categories")
        self.assertEqual(cats.status_code, 200)
        self.assertIn("类目特性规则", cats.text)
        self.assertIn("/products/categories/rules", cats.text)

    def test_75_new_product_page_has_ai_title_and_code_preview(self):
        """新建商品页：AI 生成渔货商品标题按钮、编码预览、供应商友情提示、没有手填的最近上架。"""
        page = self.client.get("/products/new")
        self.assertEqual(page.status_code, 200)
        for text in ("AI 生成渔货商品标题", "品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词", "codePreview",
                     'name="supplier_tip"'):
            self.assertIn(text, page.text)
        # 别名（含「推荐别名」按钮）已随字段整体下线（2026-10-11）
        self.assertNotIn("推荐别名", page.text)
        self.assertNotIn('name="aliases"', page.text)
        # 标题框按 60-100 字设计：两行文本框、上限 100、计数跟着走（2026-10-11）
        self.assertIn('id="titleInput"', page.text)
        self.assertIn('maxlength="100"', page.text)
        self.assertIn("0 / 100", page.text)
        self.assertNotIn('name="listed_at"', page.text, "最近上架不再手填，详情页只读展示")

    def test_77_detail_page_shows_supplier_tip(self):
        """详情页：供应商友情提示在「商品详情」之后；最近上架仍可查。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "测试耳机", "code": "YHZ-TEST-0001", "price": "199",
                    "detail_html": "<p>详情</p>", "supplier_tip": "档口周一到周五发货，急单慎拍",
                },
            )
            s.commit()
            code = product.code
        html = self.client.get(f"/products/{code}").text
        self.assertIn("供应商友情提示", html)
        self.assertIn("档口周一到周五发货", html)
        self.assertGreater(html.find("档口周一到周五发货"), html.find("商品详情"), "要排在商品详情后面")
        self.assertIn("最近上架", html)

    def test_80_category_rules_form_saves(self):
        """类目管理页的规则表单能存进库（不影响类目显示）。"""
        resp = self.client.post(
            "/products/categories/rules",
            data={"id": str(self.top_id), "rules": RULES_SAMPLE},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(ProductCategory, self.top_id).rules.strip(), RULES_SAMPLE.strip())

    def test_85_system_default_shipping_rules(self):
        """系统默认：运费模板 + 发货方式各一条全局默认，幂等，且不覆盖运营自建的。"""
        with SessionLocal() as s:
            s.query(ShippingRule).delete()
            s.commit()
            self.assertEqual(shipping_service.ensure_defaults(s), 2)
            self.assertEqual(shipping_service.ensure_defaults(s), 0, "再调一次不重复建")
            data = shipping_service.resolve_for_product(
                s, category_text="家用电器 / 大家电", supplier_id=None
            )
            self.assertTrue(data["freight_template"], "商品上架要有默认运费模板")
            self.assertEqual(data["shipping_mode"], "free")

            # 运营自己建过运费模板 → 只补发货方式
            s.query(ShippingRule).delete()
            s.commit()
            shipping_service.create_rule(
                s, kind=shipping_service.KIND_FREIGHT, name="运营自建模板", fee=6, is_default=True
            )
            self.assertEqual(shipping_service.ensure_defaults(s), 1)

    def test_90_system_default_category_rules(self):
        """系统默认：每个一级类目一套类目特性规则（幂等，不覆盖已维护的）。"""
        with SessionLocal() as s:
            for node in s.scalars(select(ProductCategory)):
                node.rules = ""
            s.commit()
            self.assertGreater(category_service.ensure_default_rules(s), 0)
            self.assertEqual(category_service.ensure_default_rules(s), 0, "再调一次不重复写")

            rule = category_service.rules_for(s, "家用电器 / 大家电")
            self.assertTrue(rule["rules"]["product"], "默认规则要有产品词")
            self.assertTrue(rule["rules"]["tips"], "默认规则要有经营提示")

            top = next(n for n in s.scalars(select(ProductCategory)) if n.parent_id is None)
            top.rules = RULES_SAMPLE
            s.commit()
            self.assertEqual(category_service.ensure_default_rules(s), 0)
            self.assertIn("无线蓝牙耳机", top.rules, "运营维护过的规则不被系统默认覆盖")


if __name__ == "__main__":
    unittest.main()
