"""类目解析认到三级（2026-10-07）。

渔枢系统的商品类目是自由文本，渔货栈那套是 1688 的三级树，两边名字还差分隔符
（「个护家清」 vs 「个护/家清」）。以前只认到二级，货源广场按三级筛货靠
「三级名出现在标题里」这个老口径 —— 标题里没写的货就永远筛不出来。

现在：名字比较先归一化（抹掉 / 、 ， 这类分隔符），三级**填了按名认领、没填就按
标题 / 别名联想**，认到就写进 supplier_items.category_l3；货源广场筛三级时先认这一列。
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
from app.services import sourcing_publish as sp  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import reset_hub_caches  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _feed(text: str) -> str:
    """只取 feed 流那一段（顶部运营位用的是全量货源，不算筛选结果）。"""
    start = text.find('class="fish-grid feed-grid"')
    end = text.find('class="harbor-pager"')
    if start < 0:
        return text
    return text[start:end] if end > start else text[start:]

# 测试自建类目：一级名故意带分隔符（与线上 1688 那套一致）
_CAT_NAMES = [
    "数码、电脑", "个护/家清", "服饰配件、饰品",
    "手机配件", "智能设备", "美容美体", "眼镜",
    "移动电源", "充电器", "智能手表", "脱毛仪", "太阳镜",
]


def _reset() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id.like("hub-prd-%")).delete(
            synchronize_session=False
        )
        s.query(Product).filter(Product.code.like("SPD8%")).delete(synchronize_session=False)
        s.query(ProductCategory).filter(ProductCategory.name.in_(_CAT_NAMES)).delete(
            synchronize_session=False
        )
        s.commit()
    reset_hub_caches()


def _categories() -> None:
    """一级（名字带分隔符）+ 二级 + 三级。"""
    with SessionLocal() as s:
        dg = ProductCategory(level=1, name="数码、电脑", sort_order=1, source_id="dg", enabled=True)
        care = ProductCategory(level=1, name="个护/家清", sort_order=2, source_id="care", enabled=True)
        acc = ProductCategory(level=1, name="服饰配件、饰品", sort_order=3, source_id="acc", enabled=True)
        s.add_all([dg, care, acc])
        s.flush()
        dg_phone = ProductCategory(level=2, parent_id=dg.id, name="手机配件", sort_order=1, source_id="dg_phone", enabled=True)
        dg_smart = ProductCategory(level=2, parent_id=dg.id, name="智能设备", sort_order=2, source_id="dg_smart", enabled=True)
        care_beauty = ProductCategory(level=2, parent_id=care.id, name="美容美体", sort_order=1, source_id="care_beauty", enabled=True)
        acc_glasses = ProductCategory(level=2, parent_id=acc.id, name="眼镜", sort_order=1, source_id="acc_glasses", enabled=True)
        s.add_all([dg_phone, dg_smart, care_beauty, acc_glasses])
        s.flush()
        s.add_all(
            [
                ProductCategory(level=3, parent_id=dg_phone.id, name="移动电源", sort_order=1, source_id="dg_phone_l3_0", enabled=True),
                ProductCategory(level=3, parent_id=dg_phone.id, name="充电器", sort_order=2, source_id="dg_phone_l3_1", enabled=True),
                ProductCategory(level=3, parent_id=dg_smart.id, name="智能手表", sort_order=1, source_id="dg_smart_l3_0", enabled=True),
                ProductCategory(level=3, parent_id=care_beauty.id, name="脱毛仪", sort_order=1, source_id="care_beauty_l3_0", enabled=True),
                ProductCategory(level=3, parent_id=acc_glasses.id, name="太阳镜", sort_order=1, source_id="acc_glasses_l3_0", enabled=True),
            ]
        )
        s.commit()
    reset_hub_caches()


def _product(**kw) -> int:
    data = {
        "name": "测试商品",
        "code": "SPD8001",
        "category": "数码、电脑",
        "brand": "测试",
        "fish_price": 88.0,
        "cost_amount": 60.0,
        "price": 199.0,
        "stock": 10,
        "source_type": "self_operated",
        "audit_status": "approved",
    }
    data.update(kw)
    with SessionLocal() as s:
        row = Product(**data)
        s.add(row)
        s.commit()
        return row.id


def _hit(pid: int):
    with SessionLocal() as s:
        return sp.resolve_category(s, s.get(Product, pid))


class CategoryLeafTest(unittest.TestCase):
    """类目解析：归一化 + 三级认领 + 标题联想。"""

    def setUp(self):
        init_db()
        _reset()
        _categories()

    def tearDown(self):
        _reset()

    def test_10_normalized_top_name(self):
        """「个护家清」要认到「个护/家清」—— 只差一个分隔符不该被拒之门外。"""
        pid = _product(name="测试脱毛仪", category="个护家清")
        hit = _hit(pid)
        self.assertTrue(hit.ok, hit.note)
        self.assertEqual(hit.top_name, "个护/家清")
        self.assertEqual(hit.category_id, "care_beauty", "落到该一级下的第一个二级")

    def test_15_normalized_full_path(self):
        """三级路径也走归一化：「服饰配件饰品 / 眼镜 / 太阳镜」。"""
        pid = _product(name="范思哲墨镜", category="服饰配件饰品 / 眼镜 / 太阳镜")
        hit = _hit(pid)
        self.assertTrue(hit.ok, hit.note)
        self.assertEqual(hit.category_id, "acc_glasses")
        self.assertEqual(hit.leaf_name, "太阳镜")
        self.assertEqual(hit.text, "服饰配件、饰品 / 眼镜 / 太阳镜")

    def test_20_leaf_guessed_from_title(self):
        """类目只填到一级：按标题里的「移动电源」认到三级。"""
        pid = _product(name="小米移动电源3 20000毫安", category="数码、电脑")
        hit = _hit(pid)
        self.assertTrue(hit.ok, hit.note)
        self.assertEqual(hit.category_id, "dg_phone")
        self.assertEqual(hit.leaf_name, "移动电源")
        self.assertIn("移动电源", hit.note, "提示要说清是按标题认的")

    def test_25_leaf_not_guessed_from_aliases(self):
        """别名已下线（2026-10-11）：认类目只看商品名 / 品牌，不再吃别名。"""
        pid = _product(name="小米新款 20000毫安", category="数码、电脑")
        hit = _hit(pid)
        self.assertNotEqual(hit.leaf_name, "移动电源")

    def test_30_leaf_may_move_the_sub(self):
        """三级挂在同级的别的二级下：连二级一起换过去，别硬塞进第一个二级。"""
        pid = _product(name="华为智能手表 GT", category="数码、电脑")
        hit = _hit(pid)
        self.assertEqual(hit.leaf_name, "智能手表")
        self.assertEqual(hit.category_id, "dg_smart", "二级跟着换到「智能设备」")
        self.assertIn("智能设备", hit.note)

    def test_32_leaf_may_move_the_whole_branch(self):
        """一级内没有这个三级时全树兜底：标题里的品类词比填粗的类目更可信，连一级一起换。"""
        pid = _product(name="范思哲同款太阳镜", category="数码、电脑")
        hit = _hit(pid)
        self.assertEqual(hit.leaf_name, "太阳镜")
        self.assertEqual(hit.category_id, "acc_glasses", "二级换成「眼镜」")
        self.assertEqual(hit.top_name, "服饰配件、饰品", "一级也跟着换")
        self.assertIn("改挂到", hit.note)

    def test_35_no_leaf_keeps_two_levels(self):
        """认不到三级就照旧挂二级，不因此拒绝上架。"""
        pid = _product(name="测试商品XYZ", category="数码、电脑")
        hit = _hit(pid)
        self.assertTrue(hit.ok, hit.note)
        self.assertEqual(hit.leaf_name, "")
        self.assertEqual(hit.text, "数码、电脑 / 手机配件")


class LeafOnPublishTest(unittest.TestCase):
    """上架到渔货栈：三级写进 supplier_items.category_l3，货源广场按三级筛得到。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _categories()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def _publish(self, **kw) -> int:
        pid = _product(**kw)
        r = self.client.post(f"/products/{pid}/sourcing-publish", follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text[:200])
        return pid

    def test_50_publish_writes_leaf(self):
        pid = self._publish(name="小米移动电源3 20000毫安", code="SPD8002")
        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_id == f"hub-prd-{pid}")).first()
            self.assertIsNotNone(item)
            self.assertEqual(item.category_l3, "移动电源")

    def test_60_harbor_filters_by_leaf_column(self):
        """认到三级之后，货源广场按这个三级筛就能捞到这件货（靠 category_l3）。"""
        pid = self._publish(name="Ulike 蓝宝石冰点脱毛仪 家用款", code="SPD8003", category="个护家清")
        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_id == f"hub-prd-{pid}")).first()
            self.assertEqual(item.category_l3, "脱毛仪", "按标题里的「脱毛仪」认到三级")

        hit = self.client.get("/self/harbor?l3=%E8%84%B1%E6%AF%9B%E4%BB%AA")
        self.assertEqual(hit.status_code, 200)
        self.assertIn("Ulike 蓝宝石冰点脱毛仪 家用款", _feed(hit.text), "按三级筛要命中")

        other = self.client.get("/self/harbor?l3=%E5%A4%AA%E9%98%B3%E9%95%9C")
        self.assertEqual(other.status_code, 200)
        self.assertNotIn("Ulike 蓝宝石冰点脱毛仪 家用款", _feed(other.text), "别的三级不该命中")

    def test_70_title_match_still_works(self):
        """老口径不能丢：没有 category_l3 的货（1688 货源）仍按标题匹配。"""
        pid = self._publish(name="测试商品", code="SPD8004", category="数码、电脑")
        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_id == f"hub-prd-{pid}")).first()
            item.category_l3 = None
            item.title = "杂牌 充电器 快充"
            s.commit()
        page = self.client.get("/self/harbor?l3=%E5%85%85%E7%94%B5%E5%99%A8")
        self.assertEqual(page.status_code, 200)
        self.assertIn("杂牌 充电器 快充", _feed(page.text))


if __name__ == "__main__":
    unittest.main()
