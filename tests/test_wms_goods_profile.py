"""商品资料（型号 / 规格 / 品牌 / 商品名称）补齐链路（2026-10-15）。

用户四条里的前两条：

  1. 扫码检测页：型号 / 规格等**全空**时提醒检测人员去维护；这些信息要带进
     渔枢系统与渔货栈的商品详情页；
  2. 货品台账详情页支持编辑商品信息（整款一起改，条码终身不变）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class GoodsProfileTest(unittest.TestCase):
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

    def _batch(self, qty: int = 5, supplier: str = "商品资料测试") -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"category": "HA", "source_type": "purchase", "qty_expected": qty,
                 "supplier_name": supplier},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def _register(self, batch: WmsBatch, rows: list[dict]) -> list[int]:
        with SessionLocal() as s:
            return [it.id for it in intake.register(s, batch.id, rows, operator="tester")]

    def _fish_of(self, item_id: int) -> str:
        with SessionLocal() as s:
            return str(s.get(WmsItem, item_id).fish_code or "")

    # ---------------------------------------------- ① 检测台：资料缺失提醒
    def test_10_profile_missing_helpers(self):
        """型号 / 规格缺一个就提醒；两个都填了就不提醒。

        品牌不参与判定 —— 无品牌的货太多了，不能因为它空着就天天弹提醒。
        """
        batch = self._batch()
        ids = self._register(batch, [{"sku_code": "PF-1", "product_name": ""}])
        with SessionLocal() as s:
            item = s.get(WmsItem, ids[0])
            self.assertTrue(intake.need_profile(item))
            self.assertEqual(
                intake.profile_missing(item),
                ["商品名称", "品牌", "型号", "规格"])
            # 只填品牌：仍然提醒（型号规格才是关键）
            item.brand = "美的"
            s.commit()
            self.assertTrue(intake.need_profile(item))
            self.assertEqual(
                intake.profile_missing(item), ["商品名称", "型号", "规格"])
            # 型号规格补齐 → 不再提醒
            item.model = "MJ-PB40E254D"
            item.spec = "1.75L / 黑色"
            s.commit()
            self.assertFalse(intake.need_profile(item))
            self.assertEqual(intake.profile_missing(item), ["商品名称"])
        self.assertFalse(intake.need_profile(None))
        self.assertEqual(intake.profile_missing(None), [])

    def test_15_item_json_carries_profile_flags(self):
        """检测台的 item.json 要把「缺哪几项 / 是不是全空」带过去。"""
        batch = self._batch()
        ids = self._register(batch, [{"sku_code": "PFJ-1", "product_name": ""}])
        barcode = None
        with SessionLocal() as s:
            barcode = s.get(WmsItem, ids[0]).barcode
        data = self.client.get(
            f"/wms/api/item.json?code={barcode}").json()
        self.assertTrue(data["ok"], data)
        item = data["item"]
        self.assertTrue(item["need_profile"])
        self.assertEqual(
            item["profile_missing"], ["商品名称", "品牌", "型号", "规格"])
        # 补了型号规格以后：need_profile 关掉，缺的只剩名称与品牌
        with SessionLocal() as s:
            it = s.get(WmsItem, ids[0])
            it.model = "MJ-PB40E254D"
            it.spec = "1.75L / 黑色"
            s.commit()
        data = self.client.get(
            f"/wms/api/item.json?code={barcode}").json()
        self.assertFalse(data["item"]["need_profile"])
        self.assertEqual(
            data["item"]["profile_missing"], ["商品名称", "品牌"])

    def test_20_inspect_page_has_profile_tip(self):
        """检测台页面要有那条提醒的容器与渲染函数。"""
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="profileTip"', page.text)
        self.assertIn("renderProfileTip", page.text)
        # 2026-10-16 文案精简：按钮改「补全商品信息」（标准主色尺寸，可点击性强）
        self.assertIn("补全商品信息", page.text)

    # ---------------------------------------------- ② 货品台账：整款编辑
    def test_30_goods_edit_page_prefills(self):
        """台账详情页要有「编辑商品信息」入口，编辑页按这一款第一台的值回填。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "GE-1", "product_name": "整款编辑的款",
             "brand": "美的", "model": "M1", "spec": "黑色", "qty": 2},
            {"sku_code": "GE-2", "product_name": "整款编辑的款",
             "brand": "美的", "model": "M1", "spec": "黑色", "qty": 3},
        ])
        fish = self._fish_of(ids[0])
        detail = self.client.get(f"/wms/goods/{fish}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(f"/wms/goods/{fish}/edit", detail.text, "详情页要有入口")

        page = self.client.get(f"/wms/goods/{fish}/edit")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn('id="g-product"', page.text)
        self.assertIn('id="g-brand"', page.text)
        self.assertIn('id="g-model"', page.text)
        self.assertIn('id="g-spec"', page.text)
        self.assertIn(fish, page.text)
        # 回填：第一台的产品名与型号规格
        self.assertIn('value="整款编辑的款"', page.text)
        self.assertIn('value="M1"', page.text)

    def test_35_goods_edit_post_syncs_all_items(self):
        """整款保存：这一款下每一台都跟着改，**条码一个都不动**。"""
        batch = self._batch()
        # 只认到商品名，型号 / 规格空着（清单里没写）→ 检测台要提醒人来补
        ids = self._register(batch, [
            {"sku_code": "GS-1", "product_name": "整款同步的款", "qty": 2},
            {"sku_code": "GS-2", "product_name": "整款同步的款", "qty": 3},
        ])
        fish = self._fish_of(ids[0])
        barcodes = []
        with SessionLocal() as s:
            barcodes = [str(s.get(WmsItem, i).barcode or "") for i in ids]

        # 先确认：改之前两台的型号规格是空的（检测台才会提醒）
        with SessionLocal() as s:
            self.assertTrue(intake.need_profile(s.get(WmsItem, ids[0])))

        resp = self.client.post(
            f"/wms/goods/{fish}/edit",
            data={"product_name": "整款同步的款（已补全）",
                  "brand": "九阳", "model": "Y88", "spec": "1.2L / 白色"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:400])

        with SessionLocal() as s:
            rows = intake.find_items_by_fish(s, fish)
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(str(row.product_name or ""), "整款同步的款（已补全）")
                self.assertEqual(str(row.brand or ""), "九阳")
                self.assertEqual(str(row.model or ""), "Y88")
                self.assertEqual(str(row.spec or ""), "1.2L / 白色")
            # 条码终身不变
            after = [str(r.barcode or "") for r in rows]
            self.assertEqual(sorted(after), sorted(barcodes))
            # 改完以后检测台不再提醒
            self.assertFalse(intake.need_profile(rows[0]))
            # 数量没被动
            self.assertEqual(sorted(int(r.qty or 0) for r in rows), [2, 3])

    def test_36_goods_edit_post_does_not_wipe_other_fields(self):
        """整款保存只动「商品名称 / 品牌 / 型号 / 规格」四项，其余字段一个都不许碰。

        **回归**：``update_goods_profile`` 只把表单里那四项塞进 ``fields`` 交给
        ``update_item``，而 ``update_item`` 原来是「按模型字段清单逐个扫，
        没给的当空值写回」—— 于是整款一改，每台货的**货号被清空、品类被清空、
        市场价被改成 0**（2026-10-15 部署后实测到的真实事故）。
        部分更新就该只改传进来的键，没传的一律不动。
        """
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "KP-1", "product_name": "别清空我", "category": "HA.JR.CCJQR",
             "base_price": 500},
            {"sku_code": "KP-2", "product_name": "别清空我", "category": "HA.JR.CCJQR",
             "base_price": 500},
        ])
        fish = self._fish_of(ids[0])

        resp = self.client.post(
            f"/wms/goods/{fish}/edit",
            data={"product_name": "别清空我", "brand": "九阳",
                  "model": "Y1", "spec": "1.2L / 白"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:400])

        with SessionLocal() as s:
            rows = sorted(intake.find_items_by_fish(s, fish),
                          key=lambda r: int(r.id))
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                [str(r.sku_code or "") for r in rows], ["KP-1", "KP-2"],
                "进货货号是一台一个的供应商编号，整款保存不能把它清掉")
            for row in rows:
                self.assertEqual(str(row.category or ""), "HA.JR.CCJQR",
                                 "品类不能被整款保存清掉")
                self.assertEqual(round(float(row.base_price or 0.0), 2), 500.0,
                                 "市场价不能被整款保存改成 0")
                # 该改的照旧改到位
                self.assertEqual(str(row.brand or ""), "九阳")
                self.assertEqual(str(row.model or ""), "Y1")
                self.assertEqual(str(row.spec or ""), "1.2L / 白")

    def test_40_goods_edit_unknown_code_404(self):
        """没有这个渔货号 → 404。"""
        self.assertEqual(
            self.client.get("/wms/goods/NOPE9999/edit").status_code, 404)

    def test_45_goods_edit_bad_category_hint(self):
        """分类认不出来给提示条，不能 500。"""
        batch = self._batch()
        ids = self._register(batch, [{"sku_code": "GC-1", "product_name": "分类兜底款"}])
        fish = self._fish_of(ids[0])
        resp = self.client.post(
            f"/wms/goods/{fish}/edit",
            data={"product_name": "x", "brand": "", "model": "", "spec": "",
                  "category": "不存在的分类ABC"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            rows = intake.find_items_by_fish(s, fish)
            self.assertNotEqual(str(rows[0].product_name or ""), "x",
                                "分类认不出来整条不保存")


if __name__ == "__main__":
    unittest.main()
