"""2026-10-16 三条（渔集）：① 编辑商品信息页的保存按钮 ② 建议售价补算 ③ 报修筛选一行。

  1. **「编辑商品信息」页没有保存按钮** —— 按钮一直在 HTML 里，但它的容器写的是
     ``class="bar"``，而 wms_design.css 里 ``.bar`` 是报表那根**占比条**
     （``height:8px`` + ``overflow:hidden``），把 38px 高的按钮裁成一条 8px 灰条。
     改成专用的 ``.btn-row``。
  2. **有渔货价、没建议售价的货**：检测定级只落渔货价，``suggest_price`` 永远空着，
     渔枢只能拿「市场价 × 0.5」兜底（兜出来有时比渔货价还低）。现在定级时按该档
     建议售价系数一起算出来，历史数据走 ``scripts/wms_suggest_price_backfill.py``。
  3. **报修工单筛选堆叠**：``.filters`` 挂在外层 div 上（form 是块级，每个 .field
     独占一行）→ 改成直接挂 form。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from wms import grading, intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsAuditLog, WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class SuggestAndUiTest(unittest.TestCase):
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

    # ------------------------------------------------- helpers
    def _batch(self, qty: int = 5, supplier: str = "建议售价测试") -> WmsBatch:
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

    # ------------------------------------------------- ① 保存按钮
    def test_10_goods_edit_save_button_not_clipped(self):
        """编辑商品信息页：保存按钮要看得见 —— 容器不能再是报表那根占比条 .bar。"""
        css = (ROOT / "app" / "static" / "wms_design.css").read_text(encoding="utf-8")
        self.assertIn("height:8px", css.split(".bar{", 1)[1][:120],
                      "wms_design.css 里 .bar 是占比条（8px + overflow:hidden）")

        batch = self._batch()
        ids = self._register(batch, [{"sku_code": "SAVE-1", "product_name": "保存按钮款"}])
        fish = self._fish_of(ids[0])
        page = self.client.get(f"/wms/goods/{fish}/edit").text
        self.assertIn("保存（同步", page, "按钮在页面里")
        self.assertIn('class="btn-row"', page, "换成专用的按钮行容器")
        self.assertNotIn('<div class="bar"', page, "不能再用占比条的 .bar 包按钮")

    # ------------------------------------------------- ② 检测定级落建议售价
    def test_20_submit_persists_suggest_price(self):
        """定级出渔货价的同时，把按该档建议售价系数算出的建议售价一起写回。"""
        from wms import inspection

        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "SUG-1", "product_name": "补建议售价的款", "base_price": 1000},
        ])
        with SessionLocal() as s:
            row = s.get(WmsItem, ids[0])
            inspection.submit(
                s, row, tester="t", results={}, final_grade="S",
                override_reason="测试定档", recheck_reason="测试：复检覆盖")
            selling = float(row.selling_price or 0.0)
            suggest = float(row.suggest_price or 0.0)
        self.assertGreater(selling, 0, "渔货价照旧要出")
        # 建议售价 = 该档规则算出来的那个数（向下取整到元）
        with SessionLocal() as s:
            row = s.get(WmsItem, ids[0])
            expected = grading.price_of(
                s, "S", float(row.base_price or 0.0), float(row.purchase_cost or 0.0)
            )["suggest_price"]
        self.assertEqual(round(suggest, 2), expected)
        self.assertGreater(suggest, 0, "建议售价不能还是空的")
        self.assertGreaterEqual(suggest, selling, "建议售价不能低于渔货价")
        self.assertEqual(suggest, float(int(suggest)), "向下取整到元")

    def test_22_auto_suggest_recomputed_on_regrade(self):
        """系统算的建议售价（suggest_auto）下次定级要跟着重算，不能被老档的数锁住。

        **回归**：直接把算出来的值写进 ``suggest_price`` 而不打标记，下一次复检时
        它会被当成「人填的人工值」—— 老档的建议售价会把新档的渔货价压住
        （渔货价不得高于建议售价）。
        """
        from wms import inspection

        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "RG-1", "product_name": "复检重算的款", "base_price": 1000},
        ])

        def _submit():
            with SessionLocal() as s:
                row = s.get(WmsItem, ids[0])
                inspection.submit(
                    s, row, tester="t", results={}, final_grade="S",
                    override_reason="测试定档", recheck_reason="测试：复检覆盖")
                return (float(row.selling_price or 0.0),
                        float(row.suggest_price or 0.0),
                        bool(row.suggest_auto))

        _, sug1, auto1 = _submit()
        self.assertTrue(auto1, "系统算出来的要打上自动标记")
        self.assertGreater(sug1, 0)

        with SessionLocal() as s:
            rule = grading.price_rule_of(s, "S")
            old_sf = float(rule.suggest_factor or 0.0)
            rule.suggest_factor = old_sf + 1.0
            s.commit()
        try:
            _, sug2, _ = _submit()
        finally:
            with SessionLocal() as s:
                grading.price_rule_of(s, "S").suggest_factor = old_sf
                s.commit()
        self.assertGreater(sug2, sug1, "系数调高后，自动算的建议售价要跟着涨")

    def test_24_manual_suggest_not_overwritten(self):
        """人填的建议售价（建档 / 编辑页那一格）不被动 —— 定级不会把它盖掉。"""
        from wms import inspection

        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "MN-1", "product_name": "人填建议售价的款", "base_price": 1000},
        ])
        with SessionLocal() as s:
            row = s.get(WmsItem, ids[0])
            row.suggest_price = 777.0
            row.suggest_auto = False
            s.commit()
        with SessionLocal() as s:
            row = s.get(WmsItem, ids[0])
            inspection.submit(
                s, row, tester="t", results={}, final_grade="S",
                override_reason="测试定档", recheck_reason="测试：复检覆盖")
            self.assertEqual(round(float(row.suggest_price or 0.0), 2), 777.0,
                             "人填的数不能被系数盖掉")
            self.assertFalse(bool(row.suggest_auto))

    # ------------------------------------------------- ② 历史数据补算脚本
    def test_30_backfill_script(self):
        """scripts/wms_suggest_price_backfill.py：预演不写库，--apply 补齐 + 留痕 + 幂等。"""
        import wms_suggest_price_backfill as backfill

        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "BF-1", "product_name": "有市场价的款", "base_price": 333.33},
            {"sku_code": "BF-2", "product_name": "没市场价的款"},
        ])
        with SessionLocal() as s:
            s.get(WmsItem, ids[0]).grade = "S"
            s.get(WmsItem, ids[0]).selling_price = 300.0
            s.get(WmsItem, ids[1]).grade = "S"
            s.get(WmsItem, ids[1]).selling_price = 500.0
            s.commit()

        with SessionLocal() as s:
            rows = backfill.plan(s)
            self.assertEqual(len(rows), 2, "两台都是「有渔货价、没建议售价」")
            before = {r["barcode"]: r["suggest"] for r in rows}
            for value in before.values():
                self.assertGreater(value, 0)
                self.assertEqual(value, float(int(value)), "向下取整到元")
            # 预演不写库
            self.assertEqual(
                round(float(s.get(WmsItem, ids[0]).suggest_price or 0.0), 2), 0.0)

            stat = backfill.apply_rows(s, rows)

        self.assertEqual(stat["items"], 2)
        with SessionLocal() as s:
            for i in ids:
                row = s.get(WmsItem, i)
                self.assertGreater(float(row.suggest_price or 0.0), 0.0)
                self.assertGreaterEqual(float(row.suggest_price or 0.0),
                                        float(row.selling_price or 0.0))
            logs = s.query(WmsAuditLog).filter(
                WmsAuditLog.action == "suggest_price").all()
            self.assertTrue(logs, "补算要留审计痕")
            # 幂等：再跑一次没有要补的了
            self.assertEqual(len(backfill.plan(s)), 0)

    # ------------------------------------------------- ③ 报修筛选一行
    def test_40_repair_filters_one_row(self):
        """报修工单：.filters 直接挂 form（挂外层 div 会让每个条件各占一行）。"""
        page = self.client.get("/wms/repairs").text
        self.assertIn('action="/wms/repairs" class="filters padded"', page,
                      "筛选类要挂在 form 上")
        self.assertNotIn('<div class="filters padded">', page)
        self.assertIn('id="f-q"', page)
        self.assertIn('id="f-s"', page)
        self.assertIn('id="f-to"', page)


if __name__ == "__main__":
    unittest.main()
