"""商品类目（货源 · 类目管理 + 发布页三级级联）。

业务背景（2026-09-28 需求）：
  · 类目口径与渔货栈货源广场那套三级结构一致（10 一级 / 64 二级 / 375 三级）；
  · 渔枢系统端「货源 · 类目管理」里维护这份原始数据，发布商品 / 编辑商品的类目都读它；
  · 商品上的 Product.category 仍是文本「一级 / 二级 / 三级」，历史两级数据要能继续用。

这个文件守住三件事：
  1. 首次打开管理页会按渔货栈那套类目自动播种，层级 / 数量对得上；
  2. 管理页能增删改（三级封顶、同级重名要拦住）；
  3. 发布页拿到的是三级级联数据，编辑页的类目候选里有三级路径，也有历史两级写法。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401

from sqlalchemy import select  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from src.goods.catalog import reset_hub_caches  # noqa: E402
from app.models import Product, ProductCategory  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.services import shipping as shipping_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _client() -> TestClient:
    init_db()
    ensure_default_admin()
    client = TestClient(app)
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return client


def _reset_categories() -> None:
    """把类目表清空，让每个用例都从「首次播种」这条路径开始。"""
    init_db()  # 第一次跑时 product_categories 表还没建
    with SessionLocal() as s:
        s.query(ProductCategory).delete()
        s.commit()


class CategorySeedTest(unittest.TestCase):
    def setUp(self):
        _reset_categories()

    def tearDown(self):
        _reset_categories()

    def test_10_seed_matches_sourcing_tree(self):
        """首次播种：数量与渔货栈那套三级类目一致。"""
        from src.goods.catalog import SOURCING_CATEGORY_TREE, SOURCING_SUB_LEAVES

        with SessionLocal() as db:
            written = category_service.ensure_seeded(db)
            stat = category_service.stats(db)
            self.assertEqual(stat["level1"], len(SOURCING_CATEGORY_TREE))
            self.assertEqual(
                stat["level2"], sum(len(c["children"]) for c in SOURCING_CATEGORY_TREE)
            )
            self.assertEqual(stat["level3"], sum(len(v) for v in SOURCING_SUB_LEAVES.values()))
            self.assertEqual(written, stat["level1"] + stat["level2"] + stat["level3"])
            # 再调一次不会重复播种
            self.assertEqual(category_service.ensure_seeded(db), 0)

    def test_20_tree_is_three_levels(self):
        """树结构三级、层级标注正确，且发布页选项就是这份数据。"""
        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            tree = category_service.tree(db)
            self.assertEqual(tree[0]["level"], 1)
            self.assertEqual(tree[0]["children"][0]["level"], 2)
            self.assertEqual(tree[0]["children"][0]["children"][0]["level"], 3)
            options = category_service.options(db)
            self.assertEqual(options[0]["n"], tree[0]["name"])
            self.assertEqual(options[0]["c"][0]["c"][0]["c"], [])

    def test_30_path_text_matches_and_joins(self):
        """路径文本："一级 / 二级 / 三级"，与商品字段同一口径。"""
        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            top = category_service.tree(db)[0]
            leaf_id = top["children"][0]["children"][0]["id"]
            leaf_name = top["children"][0]["children"][0]["name"]
            self.assertEqual(
                category_service.path_text(db, leaf_id),
                f"{top['name']} / {top['children'][0]['name']} / {leaf_name}",
            )
        self.assertEqual(category_service.text("数码配件", "耳机音响", "真无线"), "数码配件 / 耳机音响 / 真无线")
        self.assertEqual(category_service.text("数码配件", "", ""), "数码配件")


class CategoryCrudTest(unittest.TestCase):
    def setUp(self):
        _reset_categories()
        self.client = _client()

    def tearDown(self):
        _reset_categories()

    def test_10_page_and_auto_seed(self):
        """管理页首次打开自动播种；页面上能新增一级类目。"""
        page = self.client.get("/products/categories", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("类目管理", page.text)
        self.assertIn("新增一级", page.text)
        with SessionLocal() as db:
            self.assertGreater(category_service.stats(db)["level3"], 100)
        resp = self.client.post(
            "/products/categories/create", data={"name": "测试大类HTTP"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已新增类目", resp.text)
        with SessionLocal() as db:
            node = db.query(ProductCategory).filter(ProductCategory.name == "测试大类HTTP").one()
            self.assertEqual((node.level, node.parent_id), (1, None))

    def test_20_create_rename_delete(self):
        """新增一级 → 二级 → 三级、三级封顶、同级重名，改名与删除（连同子级）。

        id 全部取自创建返回值，不按名字回查 —— 播种数据里可能有同名，回查会串。
        """
        with SessionLocal() as db:
            top = category_service.create_node(db, None, "测试大类A")
            mid = category_service.create_node(db, top.id, "测试中类A")
            leaf = category_service.create_node(db, mid.id, "测试小类A")
            self.assertIsNotNone(top, "一级类目应该建得出来")
            self.assertIsNotNone(mid)
            self.assertIsNotNone(leaf)
            self.assertEqual((top.level, mid.level, leaf.level), (1, 2, 3))
            # 三级下面不能再加
            self.assertIsNone(category_service.create_node(db, leaf.id, "第四级"))
            # 同级重名要拦住
            self.assertIsNone(category_service.create_node(db, top.id, "测试中类A"))
            top_id, mid_id, leaf_id = top.id, mid.id, leaf.id
        # 改名（走过 HTTP 这条链路）
        self.client.post(
            "/products/categories/rename", data={"id": mid_id, "name": "测试中类A2"}, follow_redirects=True
        )
        with SessionLocal() as db:
            self.assertEqual(db.get(ProductCategory, mid_id).name, "测试中类A2")
            self.assertEqual(category_service.path_text(db, leaf_id), "测试大类A / 测试中类A2 / 测试小类A")
        # 删一级会把下面的子级一起删掉
        #（按名字断言：删空后跳转的页面会重新按渔货栈那套播种，sqlite 的 id 会被复用）
        resp = self.client.post("/products/categories/delete", data={"id": top_id}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        with SessionLocal() as db:
            names = {n.name for n in db.query(ProductCategory).all()}
        self.assertNotIn("测试大类A", names)
        self.assertNotIn("测试中类A2", names)
        self.assertNotIn("测试小类A", names)

    def test_30_toggle_and_move(self):
        """停用的类目不进发布页选项，但仍留在管理页；排序能上下移。

        HTTP 操作走的是另一个 session，每次读完都重新开 session，别读老缓存。
        """
        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            tree = category_service.tree(db)
        first, second = tree[0], tree[1]
        # 停用：发布页选项里没有它，管理页的树里还在
        self.client.post(
            "/products/categories/toggle", data={"id": first["id"], "enabled": "0"}, follow_redirects=True
        )
        with SessionLocal() as db:
            option_names = [item["n"] for item in category_service.options(db)]
            tree_names = [item["name"] for item in category_service.tree(db)]
        self.assertNotIn(first["name"], option_names)
        self.assertIn(first["name"], tree_names)
        # 再启用，然后往下挪一位
        self.client.post(
            "/products/categories/toggle", data={"id": first["id"], "enabled": "1"}, follow_redirects=True
        )
        self.client.post(
            "/products/categories/move", data={"id": first["id"], "direction": "down"}, follow_redirects=True
        )
        with SessionLocal() as db:
            names = [item["name"] for item in category_service.tree(db)[:2]]
        self.assertEqual(names, [second["name"], first["name"]])


class CategorySyncSelfSideTest(unittest.TestCase):
    """渔货栈读的就是渔枢系统这张类目表 —— 类目只维护一份。"""

    def setUp(self):
        _reset_categories()
        # 类目树有 60 秒缓存（一万多个节点，一屏页面会调好几次）：改完表先清掉，
        # 不然读到的是上一个用例留下的树
        reset_hub_caches()
        self.client = _client()

    def tearDown(self):
        _reset_categories()
        reset_hub_caches()

    def test_10_self_side_reads_hub_table(self):
        """播种后：渔货栈的货源类目 / 发布页类目 / 类目路径都来自渔枢系统那张表。"""
        from src.goods import catalog

        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            hub_tree = category_service.tree(db)

        self.assertIsNotNone(catalog.hub_category_rows(force=True))
        self.assertIn("类目管理", catalog.hub_category_source())
        self.assertEqual(catalog.sourcing_tree()[0]["name"], hub_tree[0]["name"])
        self.assertEqual(catalog.category_tree()[0]["name"], hub_tree[0]["name"])
        self.assertEqual(
            catalog.category_path()[catalog.category_tree()[0]["children"][0]["id"]],
            f"{hub_tree[0]['name']} / {hub_tree[0]['children'][0]['name']}",
        )

    def test_20_rename_in_hub_shows_up_in_self_side(self):
        """在渔枢系统改了名，渔货栈这边同步过来（同一份数据，不用再改两遍）。"""
        from src.goods import catalog

        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            top_id = category_service.tree(db)[0]["id"]
        self.client.post(
            "/products/categories/rename",
            data={"id": top_id, "name": "渔货栈改名验证"},
            follow_redirects=True,
        )
        # force=True：跳过 60 秒缓存，确认这次读到的就是库里的最新值
        catalog.hub_category_rows(force=True)
        names = [top["name"] for top in catalog.sourcing_tree()]
        self.assertIn("渔货栈改名验证", names)

    def test_30_falls_back_to_constants_without_table(self):
        """类目表读不到（独立部署 / 还没播种）时回落内置常量，页面不至于开天窗。"""
        from src.goods import catalog

        self.assertIsNone(catalog.hub_category_rows(force=True))
        self.assertIs(catalog.sourcing_tree(), catalog.SOURCING_CATEGORY_TREE)
        self.assertIs(catalog.category_tree(), catalog.CATEGORY_TREE)


class CategoryNavTest(unittest.TestCase):
    """类目管理与商品管理同级（都在「货源」板块下的一级菜单）。"""

    def test_10_category_is_top_level_next_to_products(self):
        from app.web import nav

        section = next(s for s in nav.NAV if s.title == "货源")
        labels = [item.label for item in section.items]
        # 渔货栈发布（/sourcing）与它们同级：商品内容在渔枢系统端维护，一键上架到渔货栈
        self.assertEqual(
            labels,
            [
                "商品管理",
                "渔货栈发布",
                "采购节点",
                # 展示基数（2026-10-13）：渔货栈卡片「X 家上架 / 已售 N」垫的展示基数
                "展示基数",
                "类目管理",
                "商品标签",
                "运费模板",
                "地址管理",
            ],
        )
        category = next(item for item in section.items if item.label == "类目管理")
        self.assertEqual(category.url, "/products/categories")
        self.assertEqual(category.children, ())
        # 运费模板 / 闲鱼发货方式：与类目管理同级，上架商品时默认套用的规则
        freight = next(item for item in section.items if item.label == "运费模板")
        self.assertEqual(freight.url, "/shipping-rules")
        # 地址管理：与运费模板同级（同一套管理逻辑，多一层「关联指定商品」），
        # 里面用 tab 分「退货地址 / 发货地址」，老链接 /return-addresses 仍可用
        addresses = next(item for item in section.items if item.label == "地址管理")
        self.assertEqual(addresses.url, "/addresses")
        self.assertIn("/return-addresses", addresses.match)

    def test_20_only_category_highlighted_on_its_page(self):
        """在 /products/categories 上：只有「类目管理」高亮，「商品管理」不上底色。"""
        from app.web import nav

        groups = nav.build("/products/categories", {}, True)
        section = next(g for g in groups if g["section"].title == "货源")
        active = [row["item"].label for row in section["nav_items"] if row["active"]]
        self.assertEqual(active, ["类目管理"])
        crumbs = nav.crumbs("/products/categories", {}, True)
        self.assertEqual(crumbs[-1]["label"], "类目管理")

    def test_30_products_page_still_highlights_products(self):
        from app.web import nav

        groups = nav.build("/products", {}, True)
        section = next(g for g in groups if g["section"].title == "货源")
        active = [row["item"].label for row in section["nav_items"] if row["active"]]
        self.assertEqual(active, ["商品管理"])


class CategoryPublishPageTest(unittest.TestCase):
    def setUp(self):
        _reset_categories()
        self.client = _client()

    def tearDown(self):
        _reset_categories()

    def test_10_new_product_page_has_three_levels(self):
        """发布页有三级下拉，且把三级树喂给了前端。"""
        page = self.client.get("/products/new")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="cat3"', page.text)
        self.assertIn("onCat3Change", page.text)
        self.assertIn("catTree", page.text)
        with SessionLocal() as db:
            options = category_service.options(db)
            self.assertEqual(options[0]["n"], category_service.tree(db)[0]["name"])

    def test_20_suggestions_keep_old_two_level_values(self):
        """编辑页候选：既有三级路径，也保留库里历史的两级写法。"""
        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            product = Product(name="类目候选测试商品", category="数码配件 / 耳机音响", price=10.0)
            db.add(product)
            db.commit()
            try:
                values = category_service.suggestions(db)
                self.assertIn("数码配件 / 耳机音响", values)
                self.assertTrue(any(v.count(" / ") == 2 for v in values), values[:5])
            finally:
                db.delete(product)
                db.commit()

    def test_25_top_level_order_drives_front_end(self):
        """一级类目排序：管理页调完，发布页级联与运费模板 / 退货地址的类目下拉跟着变。"""
        with SessionLocal() as db:
            category_service.ensure_seeded(db)
            tops = [
                n for n in db.scalars(select(ProductCategory))
                if n.parent_id is None
            ]
            tops.sort(key=lambda n: (n.sort_order or 0, n.id))
            self.assertGreaterEqual(len(tops), 3)
            first, second = tops[0], tops[1]
            names_before = [item["n"] for item in category_service.options(db)]
            self.assertEqual(names_before[0], first.name)

            self.assertTrue(category_service.move_node(db, int(second.id), -1), "上移一位")
            names_after = [item["n"] for item in category_service.options(db)]
            self.assertEqual(names_after[:2], [second.name, first.name], "发布页级联顺序跟着变")

            # 运费模板 / 退货地址的类目下拉也按这个顺序（只给一级）
            options = shipping_service.top_category_options(db)
            self.assertEqual([o["label"] for o in options][:2], [second.name, first.name])
            self.assertTrue(all(o["level"] == 1 for o in options))

            category_service.move_node(db, int(second.id), 1)   # 还原

    def test_30_export_json(self):
        """导出 JSON：给渔货栈侧对齐口径用，结构是三级树。"""
        resp = self.client.get("/products/categories/export")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("tree", payload)
        self.assertEqual(payload["tree"][0]["level"], 1)
        self.assertEqual(payload["tree"][0]["children"][0]["level"], 2)


if __name__ == "__main__":
    unittest.main()
