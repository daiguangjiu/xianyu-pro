"""渔货栈商品详情页的价格口径与文案（2026-10-07 用户拍板）。

1. **渔货栈这边没有「成本价」这个概念，只有一个渔货价** —— 学员看到的渔货价就是他的
   进货价（成本）。所以页面上一律写「渔货价」：SKU 表头、结算条那句「成本 ¥」都改过来，
   「0 元成本价」那句提示也改成「会显示『待定』」。
2. **渔货价那行括号里的来源说明删掉**（以前是「（渔枢系统渔货价 · 按商家编码
   XYSK202610070031 匹配到渔枢系统商品）」）—— 学员只关心这个价是多少，不关心它从哪来。
3. **默认运费没填时显示「包邮 / 未填」，不能再套 `mono-sm`** —— 那是等宽字体，中文会
   回落到别的字体，看着比旁边「品牌 / 类目 / 售后政策」几行大一号。有金额时才用等宽。
"""
from __future__ import annotations

import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src import deps as src_deps  # noqa: E402
from src.models import Base as SrcBase, SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ITEM_ID = "zz-wording-check"


def _row(html: str, label: str) -> str:
    """取参数表 / SKU 表里某一行的 HTML 原文。"""
    m = re.search(r"<tr><th[^>]*>%s</th>.*?</tr>" % label, html, re.S)
    return m.group(0).strip() if m else ""


class PriceWordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        engine = src_deps._get_engine()
        SrcBase.metadata.create_all(engine)
        src_deps._ensure_columns(engine, SrcBase)
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete()
            s.add(
                SupplierItem(
                    item_id=ITEM_ID,
                    title="文案口径检查用商品",
                    stock=10,
                    fish_price=Decimal("88"),
                    sale_price=Decimal("199"),
                    shipping_fee=Decimal("0"),
                    sku_list={
                        "skus": [
                            {
                                "sku_id": "sku-1",
                                "spec_text": "默认规格",
                                "price": 88,
                                "sale_price": 199,
                                "stock": 10,
                            }
                        ]
                    },
                )
            )
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete()
            s.commit()

    def _html(self) -> str:
        page = self.client.get(f"/self/goods/detail/{ITEM_ID}")
        self.assertEqual(page.status_code, 200)
        return page.text

    def test_10_price_row_without_source_note(self):
        """渔货价那行只留金额：括号里的「渔枢系统渔货价 · 按商家编码…」不再出现。"""
        html = self._html()
        self.assertNotIn("匹配到渔枢系统商品", html)
        self.assertNotIn("渔枢系统渔货价", html)
        row = _row(html, "渔货价")
        self.assertIn("¥88.00", row)
        self.assertFalse(re.search(r"¥88\.00（", row), "金额后面不该再挂括号说明")

    def test_20_no_cost_price_wording(self):
        """渔货栈不讲「成本价」：SKU 表头与结算条一律叫渔货价。"""
        html = self._html()
        self.assertNotIn("成本价", html, "详情页不该出现「成本价」这个叫法")
        self.assertNotIn("成本 ¥", html, "结算条那句「成本 ¥」改成「渔货价 ¥」")
        self.assertIn("渔货价 ¥", html)
        # SKU 明细表头：渔货价 / 建议售价
        self.assertTrue(
            re.search(r"<th[^>]*>渔货价</th><th[^>]*>建议售价</th>", html),
            "SKU 表头第一列应是「渔货价」",
        )

    def test_30_shipping_fee_text_not_monospace(self):
        """默认运费没填时是中文，不能套等宽字体；填了金额才用等宽。"""
        html = self._html()
        row = _row(html, "默认运费")
        self.assertTrue(row, "参数表里要有默认运费这一行")
        self.assertIn("包邮 / 未填", row)
        self.assertNotIn('class="mono-sm"', row, "「包邮 / 未填」不该套等宽字体（中文会变字号）")

        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_id == ITEM_ID)).first()
            item.shipping_fee = Decimal("12")
            s.commit()
        row2 = _row(self._html(), "默认运费")
        self.assertIn("¥12.00", row2)
        self.assertIn('class="mono-sm"', row2, "有金额时仍用等宽小字")


if __name__ == "__main__":
    unittest.main()
