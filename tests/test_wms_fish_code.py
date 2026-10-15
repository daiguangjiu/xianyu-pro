"""渔货号（2026-10-14）回归。

三条口径（用户拍板）：
  1. 渔货号 = ``YS + 建档日期(8 位) + 当天 4 位流水``，例 ``YS202610140001``；
  2. 历史数据按**商品名**归并（指纹一致 = 同一款货，共用一个渔货号）；
  3. 新货建档 / 导入时**全库按商品名自动认亲**，认不到才发新号。

跟着一起改的还有：渔枢 / 渔货栈的关联键从「条码」换成「渔货号」，
库存按这个渔货号下所有条码商品**合计**。
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import intake  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import STATUS_SELLABLE, WmsBatch, WmsItem  # noqa: E402
from wms import seed as wms_seed  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
FISH_RE = re.compile(r"^YS\d{8}\d{4}$")


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class FishCodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()

    def _batch(self, qty: int = 2, supplier: str = "渔货号测试") -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"category": "HA", "source_type": "purchase", "qty_expected": qty,
                 "supplier_name": supplier},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def _register(self, batch: WmsBatch, rows: list[dict]):
        with SessionLocal() as s:
            return [it.id for it in intake.register(s, batch.id, rows, operator="tester")]

    # ------------------------------------------------------------ 编号
    def test_10_next_fish_code_format(self):
        """渔货号格式：YS + 8 位日期 + 当天 4 位流水（当天第一个是 0001）。"""
        with SessionLocal() as s:
            at = datetime(2026, 10, 14, 9, 30)
            first = intake.next_fish_code(s, at=at)
            self.assertEqual(first, "YS202610140001", first)
            self.assertRegex(first, FISH_RE)

    def test_20_same_name_shares_one_fish_code(self):
        """同款同名 → 认亲共用一个渔货号（不同条码，渔货号一样）。"""
        batch = self._batch(3)
        ids = self._register(batch, [
            {"sku_code": "SKU-A1", "product_name": "飞利浦电动牙刷 HX2431"},
            {"sku_code": "SKU-A2", "product_name": "飞利浦电动牙刷 HX2431 "},  # 尾部空格
            {"sku_code": "SKU-B1", "product_name": "完全不同的另外一款货"},
        ])
        with SessionLocal() as s:
            items = [s.get(WmsItem, i) for i in ids]
            codes = [str(it.fish_code or "") for it in items]
            for code in codes:
                self.assertRegex(code, FISH_RE, code)
            self.assertEqual(codes[0], codes[1], "同名（差空格）要认成同一款")
            self.assertNotEqual(codes[0], codes[2], "不同商品名各自发号")

    def test_22_name_fingerprint(self):
        """认亲指纹：大小写 / 空格 / 标点都压掉才算同一款。"""
        key = intake.fish_name_key
        self.assertEqual(key(" 摩飞电器（Morphy） 绞肉机 "),
                         key("摩飞电器(Morphy)绞肉机"))
        self.assertNotEqual(key("摩飞绞肉机"), key("摩飞破壁机"))

    def test_30_find_item_and_search_by_fish_code(self):
        """扫渔货号能认出货（返回组里第一台），台账也能按渔货号搜到全部。"""
        batch = self._batch(2)
        ids = self._register(batch, [
            {"sku_code": "SKU-C1", "product_name": "同款燃气灶 JZT-1"},
            {"sku_code": "SKU-C2", "product_name": "同款燃气灶 JZT-1"},
        ])
        with SessionLocal() as s:
            head = s.get(WmsItem, ids[0])
            fish = str(head.fish_code or "")
            self.assertTrue(intake.find_item(s, fish) is not None, "扫渔货号要认得出来")
            twins = intake.find_items_by_fish(s, fish)
            self.assertEqual(len(twins), 2, "这一款下面挂着 2 台")
            # 台账搜索：搜渔货号把这一款的两台都捞出来
            self.assertEqual(intake.count_items(s, q=fish), 2)

    # ------------------------------------------------------------ 页面
    def test_40_goods_ledger_page(self):
        """货品台账：以渔货号为维度，列表显示关联几个条码商品。"""
        batch = self._batch(3)
        self._register(batch, [
            {"sku_code": "SKU-D1", "product_name": "库存台账测试款 X1"},
            {"sku_code": "SKU-D2", "product_name": "库存台账测试款 X1"},
            {"sku_code": "SKU-D3", "product_name": "另一款完全不同"},
        ])
        page = self.client.get("/wms/goods")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("货品台账", page.text)
        self.assertIn("条码商品", page.text, "表头要有「条码商品」这一列")
        self.assertIn("库存台账测试款 X1", page.text)
        # 两款
        with SessionLocal() as s:
            groups = {str(r[0]) for r in s.execute(
                select(WmsItem.fish_code).where(WmsItem.fish_code != "")
            ).all()}
        self.assertEqual(len(groups), 2, f"3 台归成 2 款：{groups}")
        for code in groups:
            detail = self.client.get(f"/wms/goods/{code}")
            self.assertEqual(detail.status_code, 200, detail.text[:300])
            self.assertIn(code, detail.text)
        self.assertEqual(self.client.get("/wms/goods/YS209901010001").status_code, 404)

    def test_45_goods_detail_lists_barcodes(self):
        """台账详情：一个渔货号下面挂着的每个条码商品都列出来。"""
        batch = self._batch(2)
        self._register(batch, [
            {"sku_code": "SKU-E1", "product_name": "明细页测试款 Y2"},
            {"sku_code": "SKU-E2", "product_name": "明细页测试款 Y2"},
        ])
        with SessionLocal() as s:
            fish = str(s.get(WmsItem, self._last_id()).fish_code or "")
        page = self.client.get(f"/wms/goods/{fish}")
        self.assertEqual(page.status_code, 200)
        with SessionLocal() as s:
            for it in intake.find_items_by_fish(s, fish):
                self.assertIn(it.barcode, page.text, "每个条码商品都要列出来")
                self.assertIn(it.sku_code or "", page.text)

    def test_50_item_json_exposes_fish_code(self):
        """检测台 /wms/api/item.json 要给渔货号（扫同款挑货靠它）。"""
        batch = self._batch(2)
        self._register(batch, [
            {"sku_code": "SKU-F1", "product_name": "检测台同款 Z3"},
            {"sku_code": "SKU-F2", "product_name": "检测台同款 Z3"},
        ])
        with SessionLocal() as s:
            first = s.scalar(
                select(WmsItem).order_by(WmsItem.id).limit(1)
            )
            fish = str(first.fish_code or "")
            barcode = first.barcode
        data = self.client.get(f"/wms/api/item.json?code={barcode}").json()
        item = data["item"]
        self.assertEqual(item["fish_code"], fish)
        self.assertEqual(len(item["twins"]), 1, "同款的另一台要列出来给检测员挑")

    # ------------------------------------------------------------ 换绑
    def test_60_set_fish_code_merge_and_split(self):
        """换绑：填目标号 = 并进去；留空 = 拆出来单独发一个新号。"""
        batch = self._batch(2)
        ids = self._register(batch, [
            {"sku_code": "SKU-G1", "product_name": "换绑测试款 A"},
            {"sku_code": "SKU-G2", "product_name": "换绑测试款 B"},
        ])
        with SessionLocal() as s:
            a = s.get(WmsItem, ids[0])
            b = s.get(WmsItem, ids[1])
            fish_a, fish_b = str(a.fish_code or ""), str(b.fish_code or "")
            self.assertNotEqual(fish_a, fish_b)
            # 合并：b 归入 a 的那一款
            intake.set_fish_code(s, b, fish_a, operator="tester")
            self.assertEqual(str(b.fish_code or ""), fish_a)
            self.assertEqual(len(intake.find_items_by_fish(s, fish_a)), 2)
            # 拆分：b 又单独发一个号
            intake.set_fish_code(s, b, "", operator="tester")
            self.assertNotEqual(str(b.fish_code or ""), fish_a)
            self.assertRegex(str(b.fish_code or ""), FISH_RE)
            self.assertEqual(len(intake.find_items_by_fish(s, fish_a)), 1)

    def test_62_fish_code_post_route(self):
        """页面上的换绑按钮（POST /wms/items/{id}/fish-code）走得通。"""
        batch = self._batch(1)
        ids = self._register(batch, [{"sku_code": "SKU-H1", "product_name": "路由换绑款"}])
        item_id = ids[0]
        with SessionLocal() as s:
            before = str(s.get(WmsItem, item_id).fish_code or "")
        resp = self.client.post(f"/wms/items/{item_id}/fish-code",
                                data={"fish_code": ""}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        with SessionLocal() as s:
            after = str(s.get(WmsItem, item_id).fish_code or "")
        self.assertNotEqual(before, after, "留空提交 = 拆出来发新号")
        self.assertRegex(after, FISH_RE)

    def test_70_merge_whole_group(self):
        """整款合并：把一个渔货号下的所有条码商品一起挪到另一个号上。"""
        batch = self._batch(3)
        self._register(batch, [
            {"sku_code": "SKU-I1", "product_name": "被合并的那一款"},
            {"sku_code": "SKU-I2", "product_name": "被合并的那一款"},
            {"sku_code": "SKU-I3", "product_name": "目标款"},
        ])
        with SessionLocal() as s:
            rows = s.scalars(select(WmsItem).order_by(WmsItem.id)).all()
            src = str(rows[0].fish_code or "")
            dst = str(rows[2].fish_code or "")
        resp = self.client.post(f"/wms/goods/{src}/merge",
                                data={"fish_code": dst}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        with SessionLocal() as s:
            self.assertEqual(len(intake.find_items_by_fish(s, dst)), 3)
            self.assertEqual(len(intake.find_items_by_fish(s, src)), 0)

    # ------------------------------------------------------------ 跨平台关联
    def test_80_push_stock_merges_by_fish_code(self):
        """出库回写：条码进、渔货号出，同款多台**合并成一条**推给渔枢。"""
        from wms import bridge

        batch = self._batch(2)
        self._register(batch, [
            {"sku_code": "SKU-J1", "product_name": "库存同步款 Q1"},
            {"sku_code": "SKU-J2", "product_name": "库存同步款 Q1"},
        ])
        with SessionLocal() as s:
            rows = s.scalars(select(WmsItem).order_by(WmsItem.id)).all()
            fish = str(rows[0].fish_code or "")
            codes = [str(r.barcode or "") for r in rows]
        merged = bridge.to_fish_rows([(codes[0], -1), (codes[1], -2)])
        self.assertEqual(merged, [(fish, -3)], "同款两台合并成一条 −3")

    def test_82_inspect_state_sellable_counts_sum(self):
        """可售台数 = 这一款下面所有可售状态的台数之和（不是行数）。"""
        batch = self._batch(2)
        ids = self._register(batch, [
            {"sku_code": "SKU-K1", "product_name": "可售合计款 R1", "qty": 3},
            {"sku_code": "SKU-K2", "product_name": "可售合计款 R1", "qty": 4},
        ])
        with SessionLocal() as s:
            for i in ids:
                it = s.get(WmsItem, i)
                it.status = STATUS_SELLABLE
            s.commit()
            fish = str(s.get(WmsItem, ids[0]).fish_code or "")
        page = self.client.get(f"/wms/goods/{fish}")
        self.assertIn("合计 7 台", page.text, page.text[:400])
        self.assertIn("可售 7 台", page.text)

    # ------------------------------------------------------------ 辅助
    def _last_id(self) -> int:
        with SessionLocal() as s:
            return int(s.scalar(select(WmsItem.id).order_by(WmsItem.id.desc())) or 0)


if __name__ == "__main__":
    unittest.main()
