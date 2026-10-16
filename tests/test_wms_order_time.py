"""「下单时间」在商品维度显示，并同步到渔枢 / 渔货栈（2026-10-16 用户要求）。

三条口径：

  1. **渔集商品维度要能看到下单时间** —— 它是导入 Excel 时认不出的列沉淀下来的
     （存在 ``WmsItem.extra`` 里，不是货品的固定列），款级页面（货品台账 / 商品详情）
     按渔货号把多台货的值聚成一句；
  2. 多台时间不一样 → 给「最早 ~ 最晚（N 个时间）」，不拿其中一台糊弄；
  3. 同步出去：**渔货栈**写 ``supplier_items.order_time``（规格参数页签多一行）；
     **渔枢**商品档案不存这一列，详情页按「商家编码 == 渔货号」实时从渔集读。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.wms_bridge import lookup_item, sync_profile_from_wms  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from scripts.wms_order_time_sync import apply_rows, plan  # noqa: E402
from src.models import SupplierItem  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
FISH = "YS202610169011"
OTHER = "YS202610169012"
SUPPLIER = "下单时间测试"
TIME_A = "2026-05-15 11:38"
TIME_B = "2026-06-02 09:10"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(
            SupplierItem.item_id.like("hub-prd-%"),
            SupplierItem.title.like("下单时间测试%"),
        ).delete(synchronize_session=False)
        s.query(Product).filter(Product.code.in_((FISH, OTHER))).delete(
            synchronize_session=False)
        s.query(WmsItem).filter(WmsItem.fish_code.in_((FISH, OTHER))).delete(
            synchronize_session=False)
        s.query(WmsBatch).filter(WmsBatch.supplier_name == SUPPLIER).delete(
            synchronize_session=False)
        s.commit()


def _fake(extra) -> SimpleNamespace:
    return SimpleNamespace(extra=extra)


class OrderTimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def _mk(self, fish: str, *, extra=None, name: str = "下单时间测试款") -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 3,
                    "supplier_name": SUPPLIER}, operator="tester")
            batch = s.get(WmsBatch, batch.id)
            rows = intake.register(s, batch.id, [{
                "sku_code": f"OT-{fish[-4:]}", "product_name": name,
                "fish_code": fish, "qty": 1, "extra": dict(extra or {}),
            }], operator="tester")
            return s.get(WmsItem, rows[0].id)

    # ------------------------------------------------------------ 聚合口径
    def test_10_order_time_of(self):
        """商品维度的下单时间：单值直接给；多台不一样给区间；全都没有给空串。"""
        self.assertEqual(intake.order_time_of([_fake({"下单时间": TIME_A})]), TIME_A)
        self.assertEqual(
            intake.order_time_of([_fake({"下单时间": TIME_A}), _fake({"下单时间": TIME_A})]),
            TIME_A, "同值不重复，也不给区间")
        self.assertEqual(
            intake.order_time_of([_fake({"下单时间": TIME_B}), _fake({"下单时间": TIME_A})]),
            f"{TIME_A} ~ {TIME_B}（2 个时间）")
        self.assertEqual(intake.order_time_of([_fake({}), _fake(None)]), "")
        # 别的清单表头写法也认（订单时间 / 下单日期）
        self.assertEqual(intake.order_time_of([_fake({"订单时间": TIME_A})]), TIME_A)

    # ------------------------------------------------------------ 渔集商品维度
    def test_20_goods_list_shows_order_time(self):
        """货品台账多一列「下单时间」。"""
        self._mk(FISH, extra={"下单时间": TIME_A})
        page = self.client.get("/wms/goods")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<th>下单时间</th>", page.text)
        self.assertIn(FISH, page.text)
        self.assertIn(TIME_A, page.text)

    def test_30_goods_detail_shows_order_time(self):
        """商品详情页「商品信息」卡里有一行下单时间。"""
        self._mk(FISH, extra={"下单时间": TIME_A})
        page = self.client.get(f"/wms/goods/{FISH}")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("下单时间", page.text)
        self.assertIn(TIME_A, page.text)

    def test_35_detail_aggregates_two_items(self):
        """一款挂两台、时间不一样 → 页面给区间。"""
        self._mk(FISH, extra={"下单时间": TIME_A})
        self._mk(FISH, extra={"下单时间": TIME_B})
        page = self.client.get(f"/wms/goods/{FISH}").text
        self.assertIn(f"{TIME_A} ~ {TIME_B}（2 个时间）", page)

    # ------------------------------------------------------------ 同步出去
    def _product(self, code: str = FISH) -> tuple[int, str]:
        with SessionLocal() as s:
            product = Product(name="下单时间测试款", code=code, status="on_sale")
            s.add(product)
            s.commit()
            pid = int(product.id)
            s.add(SupplierItem(item_id=f"hub-prd-{pid}", title="下单时间测试款"))
            s.commit()
            return pid, f"hub-prd-{pid}"

    def test_40_sync_to_supplier_item(self):
        """下单时间写进渔货栈货源；传空的不动（别把已有的清掉）。"""
        _pid, item_id = self._product()
        report = sync_profile_from_wms(FISH, order_time=TIME_A)
        self.assertTrue(report["updated"], report["notes"])
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertEqual(item.order_time, TIME_A)

        again = sync_profile_from_wms(FISH, order_time="")
        self.assertFalse(again["updated"], "传空不动")
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertEqual(item.order_time, TIME_A)
        # 同值再推一次不算改动
        self.assertFalse(sync_profile_from_wms(FISH, order_time=TIME_A)["updated"])

    def test_50_lookup_item_returns_order_time(self):
        """渔枢详情页的 wms_profile 里能拿到下单时间（按商家编码 == 渔货号）。"""
        item = self._mk(FISH, extra={"下单时间": TIME_A})
        snapshot = lookup_item(str(item.barcode or ""))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["order_time"], TIME_A)

    def test_55_hub_detail_page_shows_it(self):
        """渔枢商品详情页「商品信息」里显示这一行（商品档案不存，实时从渔集读）。"""
        self._mk(FISH, extra={"下单时间": TIME_A})
        self._product()
        page = self.client.get(f"/products/{FISH}")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("下单时间", page.text)
        self.assertIn(TIME_A, page.text)

    # ------------------------------------------------------------ 存量脚本
    def test_60_script_plan_and_apply(self):
        """脚本挑出有下单时间的款，推给渔货栈；跑第二遍没有要改的。"""
        self._mk(FISH, extra={"下单时间": TIME_A})
        self._mk(OTHER, extra={"下单时间": TIME_B})
        _pid, item_id = self._product()

        with SessionLocal() as s:
            rows = plan(s)
            codes = {r["fish_code"] for r in rows}
            self.assertIn(FISH, codes)
            self.assertIn(OTHER, codes)
            stat = apply_rows(rows)

        self.assertEqual(stat["synced"], 1, "只有 FISH 在渔货栈里有档案")
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertEqual(item.order_time, TIME_A)
            # 幂等：值已经一样 → 再推一次不算改动
            self.assertFalse(sync_profile_from_wms(FISH, order_time=TIME_A)["updated"])


if __name__ == "__main__":
    unittest.main()
