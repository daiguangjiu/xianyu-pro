"""渔枢系统 · 商品模块（2026-10-03 改版）：列表操作、只读查看页、独立编辑页、图片上限、详情排版。

覆盖：
  1 列表（表格 / 卡片）都有 编辑 / 删除 / 上架 / 下架，批量条有 批量下架 / 批量删除；
  2 查看页只读：没有编辑表单，没有「添加规格 / 生成 SKU 组合 / 单独加一个规格」，
    也没有「原始数据」「关联订单」板块；
  3 编辑走独立页（与「发布商品」同一套表单），保存后 SKU 矩阵按全量同步（页面上删掉的规格真的没了）；
  4 图文详情图上限 100 张，页面直接显示 已用 x/100 与还能再传几张；
  5 详情描述按录入格式展示：纯文本的换行 / 空行保留，写了 HTML 标签才按 HTML 渲染；
  6 演示商品彻底下线：模型没有 is_demo、服务里没有播种函数、页面没有「演示数据」字样。
"""
from __future__ import annotations

import json
import re
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


class HubProductOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).delete()
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

    def _seed(self, **kw) -> Product:
        form = {
            "name": kw.pop("name", "商品甲"),
            "code": kw.pop("code", "OPS0001"),
            "source_type": "self_operated",
            "category": "数码配件 / 手机配件",
            "cost_amount": "100",
            "fish_price": "120",
            "price": "150",
            "stock": "10",
            "stock_enabled": "1",
            "detail_html": kw.pop("detail_html", "<p>详情</p>"),
        }
        form.update(kw)
        with SessionLocal() as s:
            row = product_service.create_product(s, form, created_by="测试")
            s.commit()
            return row

    # ------------------------------------------------------------ 1. 列表操作
    def test_10_list_row_actions(self):
        """列表行里要能直接 编辑 / 删除 / 上架 / 下架。"""
        row = self._seed()
        html = self.client.get("/products").text
        self.assertIn(f'href="/products/{row.id}/edit"', html, "列表要有编辑入口")
        self.assertIn(f'action="/products/{row.id}/delete"', html, "列表要有删除按钮")
        self.assertIn(f'action="/products/{row.id}/listing"', html)
        self.assertIn("批量下架", html)
        self.assertIn("批量删除", html)
        self.assertIn("编辑商品资料", html)
        # 下架：标为在然后列表里应给出「下架」按钮，点一下状态回到已下架
        listing = self.client.post(f"/products/{row.id}/listing", data={"listed": "1"}, follow_redirects=False)
        self.assertEqual(listing.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, row.id).status, product_service.PRODUCT_STATUS_ON)
        self.assertIn("下架", self.client.get("/products").text)
        self.client.post(f"/products/{row.id}/listing", data={"listed": "0"}, follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, row.id).status, product_service.PRODUCT_STATUS_OFF)

    def test_20_bulk_unlist_and_delete(self):
        """批量下架 / 批量删除走同一个批量接口。"""
        first = self._seed(name="商品甲", code="OPS0010")
        second = self._seed(name="商品乙", code="OPS0011")
        self.client.post(
            "/products/bulk",
            data={"ids": [str(first.id), str(second.id)], "action": "list"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, first.id).status, product_service.PRODUCT_STATUS_ON)
        self.client.post(
            "/products/bulk",
            data={"ids": [str(first.id), str(second.id)], "action": "unlist"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, first.id).status, product_service.PRODUCT_STATUS_OFF)
        self.client.post(
            "/products/bulk",
            data={"ids": [str(first.id), str(second.id)], "action": "delete"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertIsNone(s.get(Product, first.id))
            self.assertIsNone(s.get(Product, second.id))

    def test_30_single_delete_keeps_orders(self):
        """删商品不删订单：订单本身保留，只是解除关联。"""
        row = self._seed()
        with SessionLocal() as s:
            s.add(Order(order_no="OPS-DEL-1", product_id=row.id, product_name=row.name,
                        sale_amount=150, cost_amount=100, quantity=1))
            s.commit()
        resp = self.client.post(f"/products/{row.id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.get(Product, row.id))
            order = s.scalar(select(Order).where(Order.order_no == "OPS-DEL-1"))
            self.assertIsNotNone(order, "订单不能被一起删掉")
            self.assertIsNone(order.product_id, "只是解除关联")

    # ------------------------------------------------------------ 2. 查看页只读
    def test_40_detail_page_is_read_only(self):
        """查看页：没有编辑表单 / 规格编辑 / 原始数据 / 关联订单，但有底部编辑入口。"""
        row = self._seed()
        html = self.client.get(f"/products/{row.code}").text
        for gone in ("生成 SKU 组合", "单独加一个规格", "添加规格", "原始数据",
                     "关联订单", "补挂历史订单", 'name="spec"'):
            self.assertNotIn(gone, html, f"查看页不该再有「{gone}」")
        for field in ('name="cost_amount"', 'name="detail_html"', 'name="selling_points"'):
            self.assertNotIn(field, html, f"查看页不该再出现编辑字段 {field}")
        self.assertIn("编辑商品资料", html)
        self.assertIn(f'href="/products/{row.id}/edit"', html, "吸底按钮要进编辑页")
        # 商品图片板块留了「添加主图 / 添加详情图」那种没作用的按钮
        self.assertNotIn("添加主图", html)
        self.assertNotIn("添加详情图", html)

    def test_45_no_demo_leftovers(self):
        """演示商品彻底下线：模型没有 is_demo，服务里没有播种 / 判定函数，页面没有「演示」。"""
        self.assertFalse(hasattr(Product, "is_demo"))
        self.assertFalse(hasattr(product_service, "ensure_demo_products"))
        self.assertFalse(hasattr(product_service, "is_demo"))
        self.assertFalse(hasattr(product_service, "demo_products_count"))
        row = self._seed()
        for page in ("/products", f"/products/{row.code}", "/products/new", f"/products/{row.id}/edit"):
            self.assertNotIn("演示", self.client.get(page).text, f"{page} 不该再有「演示」字样")

    def test_48_list_note_removed(self):
        """列表页底部那段备注说明已删掉。"""
        html = self.client.get("/products").text
        self.assertNotIn("商品名与「别名」是订单自动挂商品的依据", html)

    # ------------------------------------------------------------ 3. 编辑页
    def test_50_edit_page_prefills_and_syncs_skus(self):
        """编辑页与新建页同一套表单：预填现有资料；保存后 SKU 矩阵全量同步。"""
        row = self._seed()
        with SessionLocal() as s:
            s.add_all([
                ProductSku(product_id=row.id, spec="白色", price=150, cost_amount=100, stock=5),
                ProductSku(product_id=row.id, spec="黑色", price=160, cost_amount=100, stock=6),
            ])
            s.commit()
        html = self.client.get(f"/products/{row.id}/edit").text
        self.assertIn(f'action="/products/{row.id}/edit"', html)
        self.assertIn("OPS0001", html)
        # SKU 矩阵与主图是前端状态预填的（JSON 里的中文会被转义成 \uXXXX，所以只查结构）
        self.assertIn('"skus"', html, "现有 SKU 要预填进矩阵")
        self.assertIn('"specDefs"', html, "规格维度要预填进编辑区")
        self.assertIn("DETAIL_IMAGE_MAX = 100", html)
        # 保存时只留「白色」：黑色那行被删掉，价格也跟着改
        self.client.post(
            f"/products/{row.id}/edit",
            data={
                "name": row.name,
                "code": row.code,
                "source_type": "self_operated",
                "category": row.category,
                "cost_amount": "100",
                "price": "150",
                "stock": "10",
                "stock_enabled": "1",
                "detail_html": "<p>详情</p>",
                "skus_json": '[{"spec": "白色", "price": "133", "stock": "3", "code": "W1"}]',
            },
            follow_redirects=False,
        )
        with SessionLocal() as s:
            specs = {sku.spec: sku for sku in s.scalars(select(ProductSku).where(ProductSku.product_id == row.id))}
            self.assertEqual(set(specs), {"白色"}, "页面上删掉的规格要真的删掉")
            self.assertEqual(float(specs["白色"].price), 133.0)
            self.assertEqual(int(specs["白色"].stock), 3)

    def test_52_edit_cleans_duplicate_skus(self):
        """历史数据里同一规格重复建过多条：编辑保存后收敛成一条。

        口径（2026-10-03）：SKU 编码由商家编码派生（商家编码-01），页面上手填的不算，
        所以收敛后这条的编码是「商品编码-01」而不是手填的 W1。
        """
        row = self._seed()
        with SessionLocal() as s:
            s.add_all([
                ProductSku(product_id=row.id, spec="白色", price=150, cost_amount=100, stock=5, sku_code="W1"),
                ProductSku(product_id=row.id, spec="白色", price=140, cost_amount=100, stock=4, sku_code="W0"),
            ])
            s.commit()
        self.client.post(
            f"/products/{row.id}/edit",
            data={"name": row.name, "code": row.code, "source_type": "self_operated",
                  "category": row.category, "cost_amount": "100", "price": "150", "stock": "10",
                  "skus_json": '[{"spec": "白色", "price": "150", "stock": "5", "code": "W1"}]'},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            skus = list(s.scalars(select(ProductSku).where(ProductSku.product_id == row.id)))
            self.assertEqual(len(skus), 1, "同一规格只该留一条")
            self.assertEqual(skus[0].sku_code, f"{row.code}-01", "SKU 编码由商家编码派生")

    def test_55_edit_saves_and_republishes(self):
        """编辑保存会回传渔货栈（已上架的商品），历史订单不受影响。"""
        row = self._seed()
        with SessionLocal() as s:
            s.add(Order(order_no="OPS-EDIT-1", product_id=row.id, product_name=row.name,
                        sale_amount=150, cost_amount=100, quantity=1))
            s.commit()
        resp = self.client.post(
            f"/products/{row.id}/edit",
            data={"name": "改过的名字", "code": row.code, "source_type": "self_operated",
                  "category": row.category, "cost_amount": "100", "price": "150", "stock": "10"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, row.id).name, "改过的名字")
            order = s.scalar(select(Order).where(Order.order_no == "OPS-EDIT-1"))
            self.assertEqual(order.product_id, row.id, "历史订单的关联不受编辑影响")

    def test_58_edit_keeps_category_when_not_in_tree(self):
        """老商品的类目不在现在的类目树里时，编辑保存不能把类目洗掉。"""
        row = self._seed(category="数码配件 / 手机配件")
        edit_html = self.client.get(f"/products/{row.id}/edit").text
        self.assertIn("ensureCatOption", edit_html, "选不出来的类目要有兜底选项")
        self.assertIn('"category"', edit_html, "编辑页要把原类目一起下发，选不出来时兜底")
        self.client.post(
            f"/products/{row.id}/edit",
            data={"name": row.name, "code": row.code, "source_type": "self_operated",
                  "category": "数码配件 / 手机配件", "cost_amount": "100", "price": "150",
                  "stock": "10"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, row.id).category, "数码配件 / 手机配件")

    # ------------------------------------------------------------ 4/5. 图片与详情排版
    def test_60_detail_image_limit_is_100(self):
        """图文详情图上限 100：标题写明 100 张，并显示 x/100 与还能再传几张。"""
        html = self.client.get("/products/new").text
        self.assertIn("DETAIL_IMAGE_MAX = 100", html)
        self.assertIn("最多 100 张", html)
        self.assertIn('id="detailImageCount"', html)
        self.assertIn('id="detailImageLeft"', html)
        self.assertIn("还能再传", html)

    def test_70_detail_rich_keeps_format(self):
        """详情排版：纯文本的换行 / 空行 / 图标保留，写了 HTML 才按 HTML 渲染。"""
        self.assertEqual(
            product_service.detail_rich("第一行\n第二行\n\n第三段"),
            "<p>第一行<br>第二行</p>\n<p>第三段</p>",
        )
        self.assertIn("<b>", product_service.detail_rich("<b>已是 HTML</b>"))
        self.assertNotIn("<b>", product_service.detail_rich("<script>alert(1)</script>"))
        row = self._seed(detail_html="第一行\n第二行\n\n第三段")
        html = self.client.get(f"/products/{row.code}").text
        self.assertIn("<p>第一行<br>第二行</p>", html, "换行要按录入的样子显示")
        self.assertIn("<p>第三段</p>", html, "空行要分段")

    def test_72_detail_image_click_to_edit(self):
        """详情图上传后点图片能编辑：改地址 / 换一张 / 调顺序 / 删除，别只剩一个叉。"""
        html = self.client.get("/products/new").text
        for marker in ('id="detailImageEditor"', "openDetailImageEditor", "slot-edit-tip",
                       "moveDetailImage", "replaceDetailImageFile", "deleteDetailImage"):
            self.assertIn(marker, html, f"详情图编辑入口缺了 {marker}")
        # 渲染函数里要点整张图打开编辑条（不是只给删除按钮）
        self.assertIn("slot.onclick", html)

    # ------------------------------------------------------------ 6. 多组详情文案
    def test_90_detail_blocks_multi(self):
        """详情描述支持多组文案：默认「文案一」，可新建，最多 20 组。"""
        html = self.client.get("/products/new").text
        self.assertIn("DETAIL_BLOCK_MAX = 20", html)
        self.assertIn("新建商品文案", html)
        self.assertIn('id="detailBlockCount"', html)
        self.assertIn('name="detail_blocks_json"', html)
        self.assertIn("文案一", html, "默认第一组叫「文案一」")

    def test_95_detail_blocks_saved_and_joined(self):
        """提交多组文案：结构化落 detail_blocks，并按顺序拼回 detail_html。"""
        row = self._seed()
        blocks = [
            {"title": "文案一", "content": "第一段说明"},
            {"title": "文案二", "content": "第二段说明"},
        ]
        form = {
            "name": row.name, "code": row.code, "category": row.category,
            "cost_amount": "100", "stock": "10", "stock_enabled": "1",
            "detail_blocks_json": json.dumps(blocks, ensure_ascii=False),
        }
        resp = self.client.post(f"/products/{row.id}/edit", data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            saved = s.get(Product, row.id)
            saved_blocks = product_service.detail_blocks_of(saved)
            self.assertEqual(len(saved_blocks), 2)
            self.assertEqual([b["title"] for b in saved_blocks], ["文案一", "文案二"])
            self.assertEqual(saved.detail_html, "第一段说明\n\n第二段说明")
        # 编辑页要把多组回填出来，不能只剩一份
        edit_html = self.client.get(f"/products/{row.id}/edit").text
        found = re.search(r'"detailBlocks":\s*(\[.*?\])', edit_html)
        self.assertIsNotNone(found, "编辑页要把多组文案回填进前端状态")
        self.assertEqual(
            [b["content"] for b in json.loads(found.group(1))],
            ["第一段说明", "第二段说明"],
        )

    def test_97_detail_blocks_capped_at_20(self):
        """多组文案最多 20 组：超出的丢掉，标题按顺序补。"""
        many = [{"title": "", "content": f"第{i}组"} for i in range(1, 26)]
        blocks = product_service.parse_detail_blocks(json.dumps(many, ensure_ascii=False))
        self.assertEqual(len(blocks), product_service.DETAIL_BLOCK_MAX)
        self.assertEqual(blocks[0]["title"], "文案一")
        self.assertEqual(blocks[19]["title"], "文案二十")
        # 空组不入库；老数据（只有 detail_html）兜成一组
        self.assertEqual(product_service.parse_detail_blocks("[]", fallback_html="老详情"), [
            {"title": "文案一", "content": "老详情"},
        ])

    def test_99_section_order_info_before_detail(self):
        """「商品信息」排在「商品详情」前面，且不再叫「商品详情（图文）」。"""
        html = self.client.get("/products/new").text
        self.assertNotIn("商品详情（图文）", html, "闲鱼没有图文结构，去掉「图文」字样")
        self.assertGreater(html.find("商品详情"), html.find("商品信息"), "商品信息（第 3 步）要排在商品详情（第 4 步）前面")
        self.assertLess(html.find('step">3'), html.find('step">4'), "步骤号 3 在 4 之前")

    # ------------------------------------------------------------ 7. 详情图回填与拖拽
    def test_74_edit_refills_detail_images(self):
        """详情图不能丢：编辑进来要回填，且挂在「商品详情」板块里（不再放第 1 步）。"""
        row = self._seed(
            detail_html='<p>说明文字</p>\n<p><img src="/media/oh/a.png" alt=""></p>\n'
                        '<p><img src="/media/oh/b.png" alt=""></p>'
        )
        edit_html = self.client.get(f"/products/{row.id}/edit").text
        found = re.search(r'"detailImages":\s*(\[[^\]]*\])', edit_html)
        self.assertIsNotNone(found, "编辑页要回填已有的详情图")
        self.assertEqual(json.loads(found.group(1)), ["/media/oh/a.png", "/media/oh/b.png"])
        blocks = re.search(r'"detailBlocks":\s*(\[.*?\])', edit_html)
        self.assertIsNotNone(blocks)
        state_blocks = json.loads(blocks.group(1))
        self.assertNotIn("<img", str(state_blocks), "图已经单独管理，文案里不该再带一遍")
        self.assertIn("说明文字", str(state_blocks))
        # 位置：在「商品详情」（第 4 步）板块里，不在第 1 步图片区
        grid_at = edit_html.find('id="detailImageGrid"')
        self.assertGreater(grid_at, edit_html.find('step">4'), "详情图要放在商品详情板块里")
        self.assertLess(edit_html.find('step">1'), grid_at, "第 1 步只留主图")

    def test_75_detail_images_saved_after_blocks(self):
        """提交：文案落 detail_blocks，图拼在文案后面写进 detail_html，且不重复。"""
        row = self._seed()
        form = {
            "name": row.name, "code": row.code, "category": row.category,
            "cost_amount": "100", "stock": "10", "stock_enabled": "1",
            "detail_blocks_json": json.dumps([{"title": "文案一", "content": "正文"}], ensure_ascii=False),
            "detail_images": "/media/oh/a.png\n/media/oh/b.png",
        }
        resp = self.client.post(f"/products/{row.id}/edit", data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            saved = s.get(Product, row.id)
            self.assertEqual(product_service.detail_blocks_of(saved), [{"title": "文案一", "content": "正文"}])
            self.assertEqual(product_service.detail_images_of(saved), ["/media/oh/a.png", "/media/oh/b.png"])
            self.assertEqual(
                saved.detail_html,
                '正文\n<p><img src="/media/oh/a.png" alt=""></p>\n<p><img src="/media/oh/b.png" alt=""></p>',
            )
            # 再进编辑页：图还在、文案里也没有多出来的图（保存两次不会越攒越多）
            edit_html = self.client.get(f"/products/{row.id}/edit").text
        self.assertEqual(
            json.loads(re.search(r'"detailImages":\s*(\[[^\]]*\])', edit_html).group(1)),
            ["/media/oh/a.png", "/media/oh/b.png"],
        )
        self.assertNotIn("<img", re.search(r'"detailBlocks":\s*(\[.*?\])', edit_html).group(1))

    def test_76_detail_image_drag_and_block_keep_text(self):
        """主图 / 详情图支持拖拽换顺序；「新建商品文案」不会把已填的文案清掉。"""
        html = self.client.get("/products/new").text
        for marker in ("attachDrag", "draggable", "drop-target", "dragstart"):
            self.assertIn(marker, html, f"拖拽换顺序缺了 {marker}")
        # 输入内容实时写回 state，重建 DOM（新建 / 删组 / 换顺序）时不会丢
        self.assertIn("syncBlocksFromDom", html)
        self.assertIn("state.detailBlocks[i].content = e.target.value", html)
        self.assertIn("addDetailBlock", html)

    def test_80_sourcing_preview_block(self):
        """渔货栈上架预览：关键字段卡片 + 折叠明细（SKU 价目表可折叠）。"""
        row = self._seed()
        with SessionLocal() as s:
            s.add(ProductSku(product_id=row.id, spec="白色", price=150, cost_amount=100, stock=5))
            s.commit()
        html = self.client.get(f"/products/{row.code}").text
        self.assertIn("渔货栈上架预览", html)
        self.assertIn("prefill-grid", html, "关键字段用卡片网格排")
        self.assertIn("prefill-item", html)
        self.assertIn("规格价目", html, "SKU 价目表要能折叠看到")


if __name__ == "__main__":
    unittest.main()
