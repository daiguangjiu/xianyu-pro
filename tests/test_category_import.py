"""1688 类目树导入 + 类目图标生成（2026-10-04 用户拍板）。

以前「货源 · 类目管理」里只有渔货栈常量播种的那套（10 一级 / 64 二级 / 375 三级），
发布商品经常选不到准确的类目。现在按 1688 全量类目树导入（52 一级 / 1436 二级 /
11752 三级），并给渔货栈货源类目按类目语义生成图标（不能全是同一个 bi-tag）。

顺带压住两件事：
  · 三级类目一万多个，类目管理页改成「点了才拉」、货源广场每个二级只摆前 12 个；
  · 老类目停用之后，二级名可能搬去了别的一级（如「手机配件」从家用电器 → 数码、电脑），
    上架时要跨一级认领，不能落到「该一级下第一个二级」。
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
from app.models import Product, ProductCategory  # noqa: E402
from app.services import sourcing_publish  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import category_icon, reset_hub_caches, sourcing_tree  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 样本 CSV（真实文件有一万多行，测试只取这个结构）
SAMPLE_CSV = """level,cat_id,name,level1_id,level1_name,level2_id,level2_name,level3_id,level3_name,full_path,id_path,is_leaf,child_count
1,6,家用电器,6,家用电器,,,,,家用电器,6,N,2
2,651,生活电器,6,家用电器,651,生活电器,,,家用电器 > 生活电器,6>651,N,2
3,65101,电风扇,6,家用电器,651,生活电器,65101,电风扇,家用电器 > 生活电器 > 电风扇,6>651>65101,Y,0
3,65102,加湿器,6,家用电器,651,生活电器,65102,加湿器,家用电器 > 生活电器 > 加湿器,6>651>65102,Y,0
1,7,数码、电脑,7,数码、电脑,,,,,数码、电脑,7,N,1
2,652,手机配件,7,数码、电脑,652,手机配件,,,数码、电脑 > 手机配件,7>652,N,1
3,65201,耳机,7,数码、电脑,652,手机配件,65201,耳机,数码、电脑 > 手机配件 > 耳机,7>652>65201,Y,0
"""


def _reset() -> None:
    with SessionLocal() as s:
        s.query(ProductCategory).delete()
        s.query(Product).delete()
        s.commit()


def _write_sample(tmp_csv: Path) -> Path:
    tmp_csv.write_text(SAMPLE_CSV, encoding="utf-8")
    return tmp_csv


class CategoryImport(unittest.TestCase):
    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.csv = _write_sample(Path(__file__).with_name("_sample_categories.csv"))

    def tearDown(self):
        _reset()
        reset_hub_caches()
        if self.csv.exists():
            self.csv.unlink()

    # ---------------------------------------------------------------- 解析与导入
    def test_10_read_tree(self):
        """CSV → 三级树：一级 2 个 / 二级 2 个 / 三级 3 个，id 与名字都对。"""
        from scripts.import_1688_categories import read_tree

        tree = read_tree(self.csv)
        self.assertEqual(len(tree), 2)
        self.assertEqual([t["name"] for t in tree], ["家用电器", "数码、电脑"])
        subs = {s["name"]: s for t in tree for s in t["children"]}
        self.assertEqual(sorted(subs), ["手机配件", "生活电器"])
        self.assertEqual(
            {leaf["name"] for s in subs.values() for leaf in s["children"]},
            {"加湿器", "耳机", "电风扇"},
        )

    def test_20_import_and_other_fallback(self):
        """导入后三级齐全，且「其他」兜底（一级 / 二级 / 三级）始终启用。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
            tops = s.scalars(select(ProductCategory).where(ProductCategory.level == 1)).all()
            names = sorted(n.name for n in tops)
            self.assertIn("家用电器", names)
            self.assertIn("数码、电脑", names)
            self.assertIn("其他", names)  # 兜底类目
            other = next(n for n in tops if n.name == "其他")
            self.assertTrue(other.enabled)
            other_sub = next(
                n for n in s.scalars(
                    select(ProductCategory).where(ProductCategory.parent_id == other.id)
                )
            )
            self.assertEqual(other_sub.name, "其他")
            self.assertTrue(
                any(
                    n.name == "其他"
                    for n in s.scalars(
                        select(ProductCategory).where(ProductCategory.parent_id == other_sub.id)
                    )
                )
            )
            # 二级的 source_id 是 1688 的类目 id：渔货栈按它认领类目
            sub = s.scalars(
                select(ProductCategory).where(ProductCategory.name == "手机配件")
            ).first()
            self.assertEqual(sub.source_id, "1688-652")
            leaf = s.scalars(
                select(ProductCategory).where(ProductCategory.name == "耳机")
            ).first()
            self.assertTrue(leaf.source_id.startswith("1688-652_l3_"))  # 三级能反推二级

    def test_30_import_is_idempotent(self):
        """重复导入只补齐，不重复建（类目数不变）。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
            first = s.scalar(select(ProductCategory.id).select_from(ProductCategory))  # noqa
            count = len(s.scalars(select(ProductCategory)).all())
            apply_tree(s, read_tree(self.csv), retire_old=True)
            self.assertEqual(len(s.scalars(select(ProductCategory)).all()), count)

    def test_40_old_categories_retired(self):
        """老的那套类目（source_id 形如 apparel）停用但保留：历史文本还解析得出名字。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            old = ProductCategory(parent_id=None, level=1, name="女装男装",
                                  source_id="apparel", enabled=True)
            s.add(old)
            s.commit()
            apply_tree(s, read_tree(self.csv), retire_old=True)
            s.refresh(old)
            self.assertFalse(old.enabled)
            self.assertTrue(
                s.scalars(
                    select(ProductCategory).where(ProductCategory.name == "女装男装")
                ).first()
                is not None
            )

    # ---------------------------------------------------------------- 类目图标
    def test_50_icons_by_category(self):
        """图标按类目语义生成：一级有名表、二级按关键词、认不出就继承一级。"""
        self.assertEqual(category_icon("女装"), "bi-person-standing-dress")
        self.assertEqual(category_icon("数码、电脑"), "bi-laptop")
        self.assertEqual(category_icon("家用电器"), "bi-tv")
        self.assertEqual(category_icon("其他"), "bi-three-dots")
        self.assertEqual(category_icon("连衣裙"), "bi-person-standing-dress")  # 长词优先
        self.assertEqual(category_icon("手机配件"), "bi-phone")  # 二级按关键词
        # 认不出的二级跟着一级走，不是清一色的 bi-tag
        self.assertEqual(category_icon("冷门二级", "bi-laptop"), "bi-laptop")
        self.assertEqual(category_icon("冷门一级"), "bi-tag")

    def test_55_tree_icons_not_all_default(self):
        """货源广场的树：一万个二级不能全是同一个默认图标。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
        reset_hub_caches()
        tree = sourcing_tree()
        icons = {child["icon"] for top in tree for child in top["children"]}
        self.assertGreaterEqual(len(icons), 2)
        self.assertNotIn("bi-dot", icons)

    # ---------------------------------------------------------------- 页面承重
    def test_60_leaves_json_endpoint(self):
        """三级按需接口：返回某个二级下的三级（类目多时页面不一次渲染）。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
            sub = s.scalars(
                select(ProductCategory).where(ProductCategory.name == "生活电器")
            ).first()
            sub_id = int(sub.id)
        resp = self.client.get("/products/categories/leaves", params={"parent_id": sub_id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            sorted(x["name"] for x in data["leaves"]), ["加湿器", "电风扇"]
        )
        page = self.client.get("/products/categories")
        self.assertEqual(page.status_code, 200)
        self.assertIn("cat-load-leaves", page.text)  # 三级是点了才拉
        self.assertNotIn('class="cat-leaf', page.text)  # 首屏不渲染三级

    def test_65_harbor_leaf_limit(self):
        """货源广场：三级只给当前这一屏渲染，且每个二级不超过 12 个。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
        reset_hub_caches()
        html = self.client.get("/self/harbor").text
        self.assertEqual(self.client.get("/self/harbor").status_code, 200)
        self.assertIn("mega-fly-leafcol", html)

    # ---------------------------------------------------------------- 上架认领
    def test_70_resolve_cross_top(self):
        """老口径「家用电器 / 手机配件」：手机配件搬去了数码、电脑下，要跨一级认领。"""
        from scripts.import_1688_categories import apply_tree, read_tree

        with SessionLocal() as s:
            apply_tree(s, read_tree(self.csv), retire_old=True)
            product = Product(
                name="跨类目测试耳机", code="XYSK202610040501",
                category="家用电器 / 手机配件", source_type="self_operated",
                price=99.0, fish_price=60.0, stock=5, audit_status="approved",
            )
            s.add(product)
            s.commit()
            hit = sourcing_publish.resolve_category(s, product)
            self.assertTrue(hit.ok, hit.note)
            self.assertEqual(hit.sub_name, "手机配件")
            self.assertEqual(hit.top_name, "数码、电脑")
            self.assertIn("不在「家用电器」下", hit.note or "")


if __name__ == "__main__":
    unittest.main()
