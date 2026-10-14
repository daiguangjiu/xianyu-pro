"""商品列表批量审核 / 上架 / 下架 + 全站统一分页组件（2026-10-07，HUB v2.53）。

两条口径：
① 分页只有一份实现 —— templates/_pager.html 的 `pager` 宏（渔枢 / 渔货栈 / 商家端共用，
   渔枢侧靠 app/web/pages.py 的 loader 回落加载同一份）。不到 10 页页码全列；**超过 10 页**
   折叠成窗口式并多一个「跳到第几页」输入框；需要时传 sizes 还能带出「每页 N 条」下拉。
② 商品列表的批量操作（上架 / 下架 / 审核通过 / 驳回）走同一个 /products/bulk，
   操作完回到**原来的筛选与页码**（页面带 return_to，服务端只认本站 /products 开头）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Product, ProductSku  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from app.web.pages import templates  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
KEY = "分页批量用例"  # 唯一关键词：把用例数据与库里的其它商品隔开


class PagerComponentTest(unittest.TestCase):
    """pager 宏本身：10 页是折叠 / 跳页的分界，页数能按 total / size 自己算。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pager = templates.env.get_template("_pager.html").module.pager

    def test_01_ten_pages_show_all_and_no_jump(self):
        html = self.pager(3, 10, "/products", {"q": "充电器"})
        self.assertIn("第 3 / 10 页", html)
        self.assertNotIn("…", html, "10 页以内页码全部列出，不要省略号")
        self.assertNotIn("跳转", html, "10 页以内不给跳页框")
        for p in range(1, 11):
            if p == 3:  # 当前页是文字高亮，不带链接
                self.assertIn('aria-current="page">3<', html)
                continue
            self.assertIn(f"page={p}", html)
        self.assertIn("q=", html, "翻页要保留筛选条件")

    def test_02_eleven_pages_fold_and_jump(self):
        html = self.pager(3, 11, "/products", {"q": "充电器"})
        self.assertIn("第 3 / 11 页", html)
        self.assertIn("…", html, "超过 10 页折叠成窗口式")
        self.assertIn("跳转", html, "超过 10 页要给跳页框")
        self.assertIn('name="page"', html)
        self.assertIn('max="11"', html)
        # 跳页表单保留筛选（hidden 里带着原来的条件，中文原样写进 value）
        self.assertIn('<input type="hidden" name="q" value="充电器">', html)

    def test_03_pages_from_total_and_size(self):
        """不知道总页数时（商家端订单页就是只有 total 与每页条数）组件自己算。"""
        html = self.pager(1, 0, "/self/merchant/orders", {"status": "pending"}, 95, size=10)
        self.assertIn("第 1 / 10 页", html)
        self.assertIn("共 95 条", html)
        self.assertIn("status=pending", html)

    def test_04_page_size_select(self):
        html = self.pager(1, 3, "/products", {"q": "充电器"}, 120, size=20, sizes=(20, 50, 100))
        self.assertIn('name="per_page"', html, "给了 sizes 就渲染每页条数下拉")
        self.assertIn(">50 条<", html)
        self.assertIn('value="20" selected', html.replace("'", '"'))
        # 每页条数表单不带页码（改每页条数一律回第 1 页）
        size_form = html.split("</form>")[0]
        self.assertNotIn("page=", size_form)


class ProductListBulkTest(unittest.TestCase):
    """/products：批量审核 / 上下架，操作完回到原来的筛选页。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).filter(Order.product_name == KEY).delete()
            s.query(Product).filter(Product.name.like(f"{KEY}%")).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            rows = s.query(Product).filter(Product.name.like(f"{KEY}%")).all()
            for row in rows:
                s.query(ProductSku).filter(ProductSku.product_id == row.id).delete()
                s.delete(row)
            s.commit()

    def _seed(self, name: str, code: str) -> int:
        form = {
            "name": name,
            "code": code,
            "source_type": "self_operated",
            "category": "数码配件 / 手机配件",
            "cost_amount": "100",
            "fish_price": "115",
            "price": "155",
            "stock": "10",
            "stock_enabled": "1",
        }
        with SessionLocal() as s:
            row = product_service.create_product(s, form, created_by="测试")
            s.commit()
            return row.id

    def _bulk(self, ids, action: str, **extra):
        data = {"ids": [str(i) for i in ids], "action": action}
        data.update(extra)
        return self.client.post("/products/bulk", data=data, follow_redirects=False)

    def _status(self, pid: int) -> tuple[str, str]:
        with SessionLocal() as s:
            row = s.get(Product, pid)
            return row.audit_status, row.status

    # ---------------------------------------------------------------- 列表与分页
    def test_10_list_renders_pager(self):
        ids = [self._seed(f"{KEY}商品{i:02d}", f"PG{i:03d}") for i in range(21)]
        self.assertEqual(len(ids), 21)
        html = self.client.get(f"/products?per_page=20&q={quote(KEY)}").text
        self.assertEqual(self.client.get("/products").status_code, 200)
        self.assertIn("第 1 / 2 页", html, "每页 20 条、21 款商品 → 2 页")
        self.assertIn("共 21 条", html)
        self.assertIn(f"q={quote(KEY)}", html, "翻页链接要带关键词")
        self.assertIn('name="per_page"', html, "每页条数下拉由统一组件渲染")

    def test_20_bulk_bar_buttons(self):
        self._seed(f"{KEY}甲", "PGA001")
        html = self.client.get("/products").text
        for word in ("批量上架", "批量下架", "批量通过", "批量驳回", "全选本页"):
            self.assertIn(word, html, f"批量条要有「{word}」")
        self.assertIn('action="/products/bulk"', html)
        self.assertIn('name="return_to"', html)

    # ---------------------------------------------------------------- 批量动作
    def test_30_bulk_approve_reject_note(self):
        one = self._seed(f"{KEY}甲", "PGB001")
        two = self._seed(f"{KEY}乙", "PGB002")
        resp = self._bulk([one, two], "approve")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._status(one)[0], product_service.AUDIT_APPROVED)
        self.assertEqual(self._status(two)[0], product_service.AUDIT_APPROVED)

        self._bulk([one], "reject", note="图片不清晰")
        self.assertEqual(self._status(one)[0], product_service.AUDIT_REJECTED)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, one).audit_note, "图片不清晰")

    def test_40_bulk_list_and_unlist(self):
        one = self._seed(f"{KEY}甲", "PGC001")
        two = self._seed(f"{KEY}乙", "PGC002")
        self._bulk([one, two], "list")
        self.assertEqual(self._status(one)[1], product_service.PRODUCT_STATUS_ON)
        self.assertEqual(self._status(two)[1], product_service.PRODUCT_STATUS_ON)

        self._bulk([one, two], "unlist")
        self.assertEqual(self._status(one)[1], product_service.PRODUCT_STATUS_OFF)
        self.assertEqual(self._status(two)[1], product_service.PRODUCT_STATUS_OFF)

    def test_50_bulk_returns_to_current_filters(self):
        """批量操作完不该被踢回第 1 页：页面把当前地址带过来，服务端原样回跳。"""
        one = self._seed(f"{KEY}甲", "PGD001")
        resp = self._bulk([one], "approve", return_to="/products?audit=pending&per_page=20&page=2")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/products?audit=pending&per_page=20&page=2")

        # 站外地址一律忽略（防开放重定向）
        evil = self._bulk([one], "unlist", return_to="http://evil.com/x")
        self.assertEqual(evil.headers["location"], "/products")

    def test_55_bulk_bar_visible_by_default(self):
        """批量条默认就得看得见：CSS 与页面 JS 的类名必须对齐（has-selection）。

        踩过的坑：CSS 写 .bulk-bar{display:none} + .bulk-bar.show{display:flex}，
        而 JS 加的是 .has-selection —— 两边对不上，批量审核 / 上架 / 下架在页面上根本不出现。
        """
        html = self.client.get("/products").text
        css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
        self.assertIn("has-selection", html, "页面 JS 加的类名")
        self.assertIn(".bulk-bar.has-selection", css, "CSS 要有对应的高亮规则")
        self.assertNotIn(".bulk-bar.show", css, "别再留着对不上的 .show 规则")
        self.assertIn("bulk-btn", html, "批量按钮要能统一置灰")

    def test_58_backfill_button_is_gone(self):
        """「按历史订单补齐」入口下线：商品列表与店铺列表两处的按钮都不再出现。"""
        products_html = self.client.get("/products").text
        self.assertNotIn("按历史订单补齐", products_html)
        self.assertNotIn("/products/backfill", products_html)

        shops = self.client.get("/shops")
        self.assertEqual(shops.status_code, 200)
        self.assertNotIn("按历史订单补齐", shops.text)
        self.assertNotIn("/shops/backfill", shops.text)

    def test_60_audit_one_uses_same_endpoint(self):
        """列表行里的「通过 / 驳回」走的是同一个批量接口（只塞 1 个 id）。"""
        one = self._seed(f"{KEY}甲", "PGE001")
        self._bulk([one], "approve", return_to=f"/products?q={quote(KEY)}")
        self.assertEqual(self._status(one)[0], product_service.AUDIT_APPROVED)


if __name__ == "__main__":
    unittest.main()
