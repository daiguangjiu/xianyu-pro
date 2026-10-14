"""商品详情页：地址用系统流水号（商品编码）、字段与「新增商品」对齐、编辑保存生效。

要点：
  1 详情页地址用商品编码（/products/SPD0001），不用中文商品名；老链接里的数字 id 继续能开；
  2 跳转提示走一次性 cookie，URL 里不再出现中文商品名（msg=…）；
  3 详情页展示的字段 = 新增商品时的字段（属性 / 类目 / 编码 / 信息 / 价格库存 / 规格 / 物流售后 / 图文）；
  4 品牌属于商品属性（可留空）；型号 / 成色 / 重量是**规格维度**，按类目推荐、用户自己选；
    在售闲鱼号是**统计数量**（近 90 天出过单的闲鱼号数）；闲鱼商品 ID 与链接不再维护，老值只读留档；
  5 编辑保存（标题 / 价格 / 类目 / 供应商）即时生效；编码不能和别的商品撞。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Product, ProductSku  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class ProductDetailTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(ProductSku).delete()
            s.query(Product).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(ProductSku).delete()
            s.query(Product).delete()
            s.commit()

    def _seed(self, **kw) -> int:
        form = {
            "name": kw.pop("name", "全新未拆封Apple pencil 苹果三代笔"),
            "code": kw.pop("code", "SPD0013"),
            "source_type": "third_party",
            "supplier_name": "BH001",
            "category": "数码配件 / 手机配件",
            "selling_points": "全新未拆封\n正品保障",
            "delivery_days": "48 小时内发货",
            "cost_amount": "120",
            "fish_price": "135",
            "price": "168",
            "original_price": "199",
            "stock": "50",
            "stock_enabled": "1",
            "freight_template": "全国包邮",
            "shipping_mode": "free",
            "after_sale": "7 天无理由退换",
            "spec_input": "颜色: 白色",
            "image_path": "/static/brand/favicon.png",
            "detail_html": "<p>全新未拆封</p>",
            "brand": "Apple",
            "model": "Pencil 3",
            "shop_name": "珊瑚家的令狐冲",
        }
        form.update(kw)
        with SessionLocal() as s:
            row = product_service.create_product(s, form, created_by="测试")
            s.add(
                ProductSku(
                    product_id=row.id,
                    spec="白色",
                    sku_code="SKU-1",
                    price=168.0,
                    cost_amount=120.0,
                    stock=50,
                )
            )
            s.commit()
            return row.id

    # ------------------------------------------------------------ 详情分组展示
    def test_60_detail_view_groups(self):
        """查看页：文案一组一段展示，详情图一组一张居中大图 + 角标 + 下面缩略图。"""
        self._seed(
            detail_html='<p>第一段说明</p>\n<p><img src="/media/oh/d1.png" alt=""></p>\n<p>第二段说明</p>',
        )
        html = self.client.get("/products/SPD0013").text
        for text in ("文案一", "文案二", "detail-group", "详情图 第 1 组",
                     "detail-main-img", "img-index", "switchGroupImage", "1/1"):
            self.assertIn(text, html, f"查看页详情应有「{text}」")

    def test_62_detail_image_grouped_by_nine(self):
        """详情图每 9 张一组：11 张 → 两组（9 + 2），每组的其余图在大图下面给缩略图。"""
        self._seed(
            detail_html="说明文字\n"
            + "\n".join(f'<p><img src="/media/oh/i{n}.png" alt=""></p>' for n in range(1, 12)),
        )
        html = self.client.get("/products/SPD0013").text
        self.assertEqual(html.count('id="detailMain'), 2, "11 张详情图要切成 9 + 2 两组")
        self.assertEqual(html.count('class="detail-thumbs"'), 2, "每组的其它图放成大图下面的缩略图")
        self.assertIn("详情图 第 1 组", html)
        self.assertIn("详情图 第 2 组", html)
        self.assertIn("共 9 张", html)
        self.assertIn("1/9", html)

    def test_63_main_gallery_centered_with_index_badge(self):
        """商品图片：整块居中，大图右下角带「第几张 / 共几张」角标，缩略图也能改角标。"""
        self._seed()
        html = self.client.get("/products/SPD0013").text
        self.assertIn('id="galleryIndex"', html)
        self.assertIn("1/1", html)
        self.assertIn(".detail-gallery { align-items: center; }", html)
        self.assertIn(".detail-thumbs { justify-content: center; }", html)
        self.assertIn(".img-index", html)

    def test_64_listing_preview_shows_detail_counts(self):
        """渔货栈上架预览：文案几组 / 详情图几组几张都列出来，长文案按录入换行显示。"""
        self._seed(
            detail_html='<p>第一段说明</p>\n<p><img src="/media/oh/i1.png" alt=""></p>\n<p>第二段说明</p>',
            supplier_tip="档口周一只发货\n急单慎拍",
        )
        html = self.client.get("/products/SPD0013").text
        for text in ("详情文案", "详情图", "2 组", "1 张"):
            self.assertIn(text, html, f"上架预览应显示「{text}」")
        # 备注 / 描述这类长文案：录入时的换行要显示出来
        self.assertIn("white-space: pre-wrap", html)

    def test_66_right_column_scrolls_on_its_own(self):
        """右栏（上架到渔货栈 + 上架预览）自己带滚动条，不用等左边滑到底。"""
        self._seed()
        html = self.client.get("/products/SPD0013").text
        self.assertIn(".detail-sticky", html)
        self.assertIn("overflow-y: auto", html, "右栏要能单独滚动")

    # ------------------------------------------------------------ 地址（流水号）
    def test_10_detail_url_uses_product_code(self):
        """详情页地址用商品编码，数字 id 的老链接继续能开。"""
        pid = self._seed(code="SPD0013")
        by_code = self.client.get("/products/SPD0013")
        self.assertEqual(by_code.status_code, 200)
        self.assertIn("全新未拆封Apple pencil 苹果三代笔", by_code.text)
        by_id = self.client.get(f"/products/{pid}")
        self.assertEqual(by_id.status_code, 200, "老链接（数字 id）也要能打开")
        self.assertEqual(self.client.get("/products/NO-SUCH-CODE").status_code, 404)

    def test_20_list_and_create_link_use_code(self):
        """列表页链接用编码；新建商品后跳到编码地址，且 URL 里没有中文提示语。"""
        self._seed(code="SPD0014", name="测试商品甲")
        page = self.client.get("/products")
        self.assertIn("/products/SPD0014", page.text)

        resp = self.client.post(
            "/products",
            data={"name": "新建商品乙", "code": "SPD0015", "source_type": "self_operated"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("/products/SPD0015", location)
        self.assertNotIn("msg=", location, "提示改走 cookie，URL 里不再拼中文")
        self.assertNotIn("%E5", location, "URL 里不该有中文标题参数")

    def test_30_flash_shows_once_from_cookie(self):
        """提示走一次性 cookie：跟随后能看到，刷新一次就没（不会一直挂在 URL 上）。"""
        resp = self.client.post(
            "/products",
            data={"name": "提示商品", "code": "SPD0016", "source_type": "self_operated"},
            follow_redirects=False,
        )
        target = resp.headers["location"]
        first = self.client.get(target)
        self.assertIn("商品已创建", first.text)
        second = self.client.get(target)
        self.assertNotIn("商品已创建", second.text)

    # ------------------------------------------------------------ 字段口径
    def test_40_detail_shows_create_fields(self):
        """详情页按「新增商品」的字段展示：属性 / 类目 / 编码 / 信息 / 规格价格库存 / 物流售后。

        口径（2026-10-04）：价格与库存跟着 SKU 走，「价格与库存」「规格与 SKU」
        两块合成一块「规格 · 价格 · 库存」；成本价 / 渔货价 / 建议售价 / 市场价 / 库存
        都是「填了才显示」，发布页也不再单独维护总库存与库存管理。
        """
        pid = self._seed()
        page = self.client.get("/products/SPD0013")
        html = page.text
        for text in (
            "商品属性", "对应供应链", "商品类目", "商家编码", "上架状态",
            "商品信息", "商品标题", "承诺发货时效", "卖点",
            "规格 · 价格 · 库存", "成本价", "渔货价", "建议售价", "市场价（划线价）", "总库存",
            "SKU 编码", "物流与售后", "运费模板", "发货方式", "售后政策", "商品详情",
        ):
            self.assertIn(text, html, f"详情页应有「{text}」")
        # 合成一块后，旧的两块标题不该再单独出现
        self.assertNotIn(">价格与库存<", html)
        self.assertNotIn("规格与 SKU</div>", html)
        # 库存管理不再单独占一格（库存跟着规格走）
        self.assertNotIn(">库存管理<", html)
        # 具体值也要在（详情页是「查看全部商品信息」）
        for value in ("BH001", "数码配件 / 手机配件", "SPD0013", "48 小时内发货", "全国包邮"):
            self.assertIn(value, html, f"详情页应显示「{value}」")
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(Product, pid))

    def test_50_brand_in_main_info_and_old_fields_archived(self):
        """字段口径：品牌进商品信息；型号 / 重量 / 闲鱼商品 ID 与链接只留历史值，不再可编辑。"""
        pid = self._seed()
        html = self.client.get("/products/SPD0013").text
        # 品牌属于商品属性（可留空），展示在主信息里
        self.assertIn("品牌", html)
        self.assertIn("Apple", html)
        # 老字段：只有值留在「历史字段（已不再维护）」里，页面不再提供编辑入口
        self.assertIn("历史字段（已不再维护）", html)
        for field in ('name="model"', 'name="weight"', 'name="listing_id"',
                      'name="listing_url"', 'name="shop_name"'):
            self.assertNotIn(field, html, f"{field} 不该再是可编辑字段")
        # 查看页只读：编辑表单那套字段一个都不该有（编辑 / 审核 / 规格维护都在编辑页）
        for field in ('name="cost_amount"', 'name="fish_price"', 'name="selling_points"',
                      'name="detail_html"', 'name="aliases"'):
            self.assertNotIn(field, html, f"查看页不该再出现编辑字段 {field}")
        self.assertIn("编辑商品资料", html, "查看页要留给编辑页的入口（吸底）")
        # 默认运费在**编辑页**表单里；成色早就是规格维度、最近上架由系统按上架动作记录，都不手填
        edit_html = self.client.get(f"/products/{pid}/edit").text
        self.assertIn('name="shipping_fee"', edit_html, "默认运费应在编辑页表单里")
        self.assertNotIn('name="listed_at"', edit_html, "最近上架由系统记录，不手填")
        self.assertNotIn('name="condition"', edit_html, "成色是规格维度，不是固定字段")

    def test_55_live_shop_is_counted_from_orders(self):
        """在售闲鱼号是**统计数量**：近 90 天出过这款商品订单的闲鱼号数，不是手填字段。"""
        pid = self._seed()
        html = self.client.get("/products/SPD0013").text
        self.assertIn("在售闲鱼号", html)
        # 没在售（草稿）→ 0 个；标为在售并挂上两个闲鱼号的订单后 → 2 个
        self.assertIn("0 个（未上架）", html)
        with SessionLocal() as s:
            row = s.get(Product, pid)
            row.status = product_service.PRODUCT_STATUS_ON
            s.commit()
            s.add(Order(order_no="T-LIVE-1", product_id=pid, product_name=row.name,
                        xianyu_account="闲鱼号甲", sale_amount=168, cost_amount=120, quantity=1))
            s.add(Order(order_no="T-LIVE-2", product_id=pid, product_name=row.name,
                        xianyu_account="闲鱼号乙", sale_amount=168, cost_amount=120, quantity=1))
            s.commit()
        html2 = self.client.get("/products/SPD0013").text
        self.assertIn("2</b> 个", html2)
        self.assertIn("闲鱼号甲", html2)

    def test_56_spec_template_follows_category(self):
        """规格按类目推荐：型号 / 成色 / 重量都是规格维度，新建页与详情页拿到同一份模板。"""
        page = self.client.get("/products/new")
        self.assertIn('name="brand"', page.text, "品牌是新增商品的属性（可不填）")
        self.assertIn('name="shipping_fee"', page.text, "默认运费进新增商品")
        self.assertIn('name="supplier_tip"', page.text, "供应商友情提示（渔货栈特色）写在图文详情下")
        # 最近上架改成系统按上架动作记录：新建页不手填，详情页只读展示
        self.assertNotIn('name="listed_at"', page.text, "最近上架不再手填")
        # 类目名按「类目管理」里那份：家用电器看「型号 / 成色」，女装男装看「尺码 / 颜色 / 成色」，
        # 家纺家饰 / 食品生鲜这类重货看「重量」
        digi = product_service.spec_template_for("家用电器 / 手机配件")
        self.assertIn("成色", [d["name"] for d in digi])
        self.assertIn("型号", [d["name"] for d in product_service.spec_template_for("家用电器 / 耳机音响")])
        cloth = product_service.spec_template_for("女装男装 / 男装")
        self.assertIn("尺码", [d["name"] for d in cloth])
        self.assertIn("成色", [d["name"] for d in cloth])
        home = product_service.spec_template_for("家纺家饰 / 床上用品")
        self.assertIn("重量", [d["name"] for d in home])
        self.assertIn("重量", [d["name"] for d in product_service.spec_template_for("食品生鲜 / 休闲零食")])

    def test_60_edit_form_uses_same_options_as_create(self):
        """编辑页与「新增商品」是同一套表单：三级类目、发货时效、运费模板、售后政策、SKU 矩阵。"""
        pid = self._seed()
        html = self.client.get(f"/products/{pid}/edit").text
        self.assertIn('id="cat1"', html)
        self.assertIn('id="cat2"', html)
        self.assertIn('id="cat3"', html)
        self.assertIn('name="freight_template"', html)
        self.assertIn('name="after_sale"', html)
        self.assertIn('name="delivery_days"', html)
        self.assertIn('id="specEditor"', html, "规格在编辑页里维护")
        self.assertIn('id="skuTable"', html, "SKU 矩阵在编辑页里维护")
        # 编辑模式：表单提交到 /products/{id}/edit，并且预填了现有资料
        self.assertIn(f'action="/products/{pid}/edit"', html)
        self.assertIn("编辑商品", html)
        self.assertIn("Apple", html, "品牌要预填")
        self.assertIn("SPD0013", html, "编码要预填")

    # ------------------------------------------------------------ 编辑保存
    def test_70_edit_saves_and_keeps_code_url(self):
        """编辑保存生效；保存后地址仍是编码。"""
        pid = self._seed()
        resp = self.client.post(
            f"/products/{pid}/edit",
            data={
                "name": "改过的标题",
                "code": "SPD0013",
                "source_type": "third_party",
                "supplier_name": "BH001",
                "category": "数码配件 / 手机配件",
                "cost_amount": "130",
                "price": "188",
                "stock": "40",
                "stock_enabled": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/products/SPD0013", resp.headers.get("location", ""))
        with SessionLocal() as s:
            row = s.get(Product, pid)
            self.assertEqual(row.name, "改过的标题")
            self.assertEqual(row.price, 188.0)
            self.assertEqual(row.stock, 40)
            self.assertEqual(row.category, "数码配件 / 手机配件")

    def test_80_duplicate_code_is_rejected(self):
        """编码是详情页流水号：不能和别的商品撞（否则链接会指错东西）。"""
        self._seed(code="SPD0020", name="商品甲")
        self._seed(code="SPD0021", name="商品乙")
        with SessionLocal() as s:
            row = s.scalar(select(Product).where(Product.code == "SPD0021"))
            with self.assertRaises(ValueError) as ctx:
                product_service.update_product(s, row, {"code": "SPD0020"})
            self.assertIn("已被商品", str(ctx.exception))
        # 新建也一样拦
        with SessionLocal() as s:
            with self.assertRaises(ValueError):
                product_service.create_product(
                    s, {"name": "商品丙", "code": "spd0020", "source_type": "self_operated"}
                )

    def test_90_search_by_code_is_case_insensitive(self):
        """编码查商品大小写不敏感（运营手打 spd0013 也能开）。"""
        self._seed(code="SPD0030")
        self.assertEqual(self.client.get("/products/spd0030").status_code, 200)


if __name__ == "__main__":
    unittest.main()
