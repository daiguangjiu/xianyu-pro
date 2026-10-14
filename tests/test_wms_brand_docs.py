"""渔集改版三项回归：系统名 / 系统设计方案入口 / 渔货价口径。

 1. 左上角主名「渔集」，副标题「检测入库出库管理系统」在它下面；
 2. 「规则 · 报表 · 系统」下有「系统设计方案」菜单，文档连接得上（且白名单之外的文件不给下）；
 3. 对外供货的销售价一律叫**渔货价**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.config import config as wms_config  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem, WmsPriceRule  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class WmsBrandDocsTest(unittest.TestCase):
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
        with SessionLocal() as s:
            s.query(WmsItem).delete(synchronize_session=False)
            s.query(WmsBatch).delete(synchronize_session=False)
            # W 档价格规则是「渔货价」那条用例临时加的（播种只给 N/S/A/B/C/D），跑完就清掉，
            # 免得别的文件读到一条凭空多出来的规则
            s.query(WmsPriceRule).filter(WmsPriceRule.grade == "W").delete(
                synchronize_session=False)
            s.commit()

    # ------------------------------------------------------------ 系统名
    def test_10_brand_is_yuji(self):
        """左上角主名是「渔集」，副标题是「检测入库出库管理系统」。"""
        self.assertEqual(wms_config.brand, "渔集")
        self.assertEqual(wms_config.system_name, "检测入库出库管理系统")
        html = self.client.get("/wms").text
        self.assertIn('class="logo-tx"', html)
        self.assertIn("渔集", html)
        self.assertIn("检测入库出库管理系统", html)

    # ------------------------------------------------------------ 设计方案
    def test_20_design_page_and_doc(self):
        """菜单里有「系统设计方案」，页面 200 且文档接得上。"""
        self.assertIn("系统设计方案", self.client.get("/wms").text)
        page = self.client.get("/wms/design").text
        self.assertIn('class="doc-frame"', page)
        self.assertIn("/wms/static/design_plan.html", page)
        doc = self.client.get("/wms/static/design_plan.html")
        self.assertEqual(doc.status_code, 200)
        self.assertIn("text/html", doc.headers.get("content-type", ""))
        self.assertIn("货品检测入库出库管理系统", doc.text)

    def test_30_static_is_whitelisted(self):
        """静态目录只对白名单开放：目录穿越与陌生文件名一律 404。"""
        self.assertEqual(self.client.get("/wms/static/../models.py").status_code, 404)
        self.assertEqual(self.client.get("/wms/static/models.py").status_code, 404)

    # ------------------------------------------------------------ 渔货价
    def test_40_fish_price_wording(self):
        """对外供货的销售价叫渔货价：录入框、价格系数表、改价提示都改口径。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "PH.DJ.MN", "source_type": "recycle", "qty_expected": 1},
                operator="tester",
            )
            intake.register(s, batch.id, [
                {"brand": "Hytera", "model": "TC-320", "spec": "U段", "serial_no": "SN9001",
                 "purchase_cost": 60.0, "base_price": 120.0}
            ], operator="tester")
            item = s.scalars(__import__("sqlalchemy").select(WmsItem).where(WmsItem.batch_id == batch.id)).first()
            item_id = item.id

        detail = self.client.get(f"/wms/items/{item_id}").text
        # 2026-10-10：渔货价不再是「留空按系数算」，改成按「市场价 × 该档系数」自动带出来、可改
        self.assertIn("渔货价", detail)
        self.assertIn("自动带出", detail)
        self.assertNotIn("销售价（留空按系数算）", detail)
        rules = self.client.get("/wms/rules").text
        self.assertIn("渔货价 = 基准价 × 系数", rules)
        self.assertNotIn("销售价 = 基准价 × 系数", rules)

        # 渔货价不能高于建议售价（2026-10-10）：这台货没定级（W 档没配系数 → 建议售价 = 基准价 120），
        # 填 188 会被压到 120，并在提示里说清楚（要调高就把该档的建议售价系数抬上去）
        resp = self.client.post(
            f"/wms/items/{item_id}/price",
            data={"selling_price": "188", "reason": "对外供货调价"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(WmsItem, item_id)
            self.assertEqual(float(row.selling_price or 0), 120.0, "高于建议售价 → 压到建议售价")
            s.add(WmsPriceRule(grade="W", factor=1.0, suggest_factor=2.0))
            s.commit()
        # 抬上建议售价系数之后，188 就存得进去了
        self.client.post(
            f"/wms/items/{item_id}/price",
            data={"selling_price": "188", "reason": "对外供货调价"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            row = s.get(WmsItem, item_id)
            self.assertEqual(float(row.selling_price or 0), 188.0)
