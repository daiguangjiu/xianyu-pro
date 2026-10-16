"""价格向下取整（2026-10-16 用户拍板）：¥184.06 → ¥184.00。

两条口径：
· 新的：分级定价（市场价 × 该档系数）与人工填价，一律向下取整到元；
· 历史的：渔货价 / 建议售价批量取整，并按渔货号同步给渔枢商品档案与渔货栈货源。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import grading, intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

FISH = "YS202610169001"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).filter(WmsItem.fish_code == FISH).delete(synchronize_session=False)
        s.query(WmsBatch).filter(WmsBatch.supplier_name == "取整测试").delete(
            synchronize_session=False)
        s.commit()


class PriceFloorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()

    def setUp(self):
        _wipe()

    def tearDown(self):
        _wipe()

    def _mk_item(self, **kw) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 2,
                    "supplier_name": "取整测试"}, operator="tester")
            batch = s.get(WmsBatch, batch.id)
            rows = intake.register(s, batch.id, [{
                "sku_code": "PF-9001", "product_name": "取整测试款",
                "fish_code": FISH, "base_price": 613.53, "qty": 1, **kw,
            }], operator="tester")
            return s.get(WmsItem, rows[0].id)

    # ------------------------------------------------------------ 取整函数
    def test_10_floor_price_basics(self):
        self.assertEqual(grading.floor_price(184.06), 184.0, "184.06 → 184")
        self.assertEqual(grading.floor_price(184.99), 184.0)
        self.assertEqual(grading.floor_price(1080.36), 1080.0)
        self.assertEqual(grading.floor_price(0), 0.0)
        self.assertEqual(grading.floor_price(None), 0.0)
        self.assertEqual(grading.floor_price(""), 0.0)
        self.assertEqual(grading.floor_price(184.0), 184.0, "本来就是整数不动")
        # 浮点误差：0.1 * 3 这类不能把 184 判成 185
        self.assertEqual(grading.floor_price(round(61.3333333 * 3, 6)), 184.0)

    # ------------------------------------------------------------ 分级定价取整
    def test_20_price_of_floors_computed_price(self):
        with SessionLocal() as s:
            out = grading.price_of(s, "S", 613.53, 200.0)
        self.assertEqual(out["price"], float(int(out["price"])), "算出来的渔货价必须是整数")
        self.assertEqual(out["suggest_price"], float(int(out["suggest_price"])),
                         "建议售价也必须取整")
        # 人工填的同样取整（¥184.06 → ¥184）
        with SessionLocal() as s:
            out = grading.price_of(s, "S", 613.53, 0.0, manual_price=184.06)
        self.assertEqual(out["price"], 184.0)

    # ------------------------------------------------------------ 建档 / 编辑取整
    def test_30_register_and_update_floor_prices(self):
        item = self._mk_item(selling_price=184.06, suggest_price=199.9)
        self.assertEqual(float(item.selling_price or 0.0), 184.0, "建档填的渔货价取整")
        self.assertEqual(float(item.suggest_price or 0.0), 199.0, "建档填的建议售价取整")
        self.assertEqual(float(item.base_price or 0.0), 613.53, "市场价保留原值")
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            intake.update_item(s, row, fields={"selling_price": 321.7, "suggest_price": 400.4},
                               operator="tester")
            self.assertEqual(float(row.selling_price or 0.0), 321.0)
            self.assertEqual(float(row.suggest_price or 0.0), 400.0)
            # 只传渔货价：建议售价不许被清掉（2026-10-15 的事故口径）
            self.assertEqual(float(row.base_price or 0.0), 613.53)

    # ------------------------------------------------------------ 历史数据处理
    def test_40_script_plan_and_apply(self):
        from scripts import wms_price_floor

        item = self._mk_item()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            row.selling_price = 1080.36        # 老数据：带小数
            row.suggest_price = 116.5
            s.commit()

            rows = wms_price_floor.plan(s)
            self.assertEqual(len(rows), 1, "只挑出带小数那台")
            self.assertEqual(rows[0]["old_sell"], 1080.36)
            self.assertEqual(rows[0]["new_sell"], 1080.0)
            self.assertEqual(rows[0]["new_sug"], 116.0)

            # 同步给渔枢 / 渔货栈：脚本里走 bridge.push_price_change
            with mock.patch("wms.bridge.push_price_change",
                            return_value={"updated": True, "notice": False, "notes": []}) as p:
                stat = wms_price_floor.apply_rows(s, rows)
            self.assertEqual(stat["items"], 1)
            self.assertEqual(stat["synced"], 1, "改完要推给渔枢 / 渔货栈")
            self.assertEqual(p.call_count, 1)

            row = s.get(WmsItem, item.id)
            self.assertEqual(float(row.selling_price or 0.0), 1080.0)
            self.assertEqual(float(row.suggest_price or 0.0), 116.0)

        # 幂等：再跑一次没有待改的行
        with SessionLocal() as s:
            self.assertEqual(len(wms_price_floor.plan(s)), 0, "取整后不该再被挑出来")

    def test_42_resync_pushes_current_prices(self):
        """--resync：把渔集当前价格再推一次（修老数据里带小数的兜底售价）。"""
        from scripts import wms_price_floor

        self._mk_item(selling_price=200.0, suggest_price=0.0)
        with SessionLocal() as s:
            with mock.patch("wms.bridge.push_price_change",
                            return_value={"updated": True, "notice": False, "notes": []}) as p:
                stat = wms_price_floor.resync(s)
            self.assertGreaterEqual(stat["pushed"], 1)
            self.assertGreaterEqual(stat["updated"], 1)
            self.assertGreaterEqual(p.call_count, 1)

    def test_41_script_leaves_integer_prices_alone(self):
        from scripts import wms_price_floor

        self._mk_item(selling_price=200.0, suggest_price=250.0)
        with SessionLocal() as s:
            self.assertEqual(len(wms_price_floor.plan(s)), 0, "整数的不动")


if __name__ == "__main__":
    unittest.main()
