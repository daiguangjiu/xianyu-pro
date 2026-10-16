"""渔货栈「闲鱼发布铺货信息」页签（2026-10-16 用户两条）。

  1. **所有字段一个字号一个字体** —— 以前 销售价 / 库存 / 图片 三行套了 ``mono``
     （JetBrains Mono 的 x-height 比正文高，同一列看着大一号），描述行又单独给
     12px 灰色，一页里三个字号。
  2. **描述要规整，不能出现「类目：1688-653」** —— 描述里以前挂着
     「【工厂一手货源】类目：1688-653。进价 ¥45.50。建议描述点：…」：
     类目 id 买家看不懂，**进货价是底价更不能往外说**，「建议描述点」是给运营看的
     内部提示。现在按 标题 / 卖点 / 规格 / 发货 / 售后 分块。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src import deps as src_deps  # noqa: E402
from src.goods import service as goods_service  # noqa: E402
from src.models import Base as SrcBase  # noqa: E402
from src.models import SupplierItem  # noqa: E402
from src.schemas import GoodsItemCreateIn  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class PrefillDescriptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        engine = src_deps._get_engine()
        SrcBase.metadata.create_all(engine)
        src_deps._ensure_columns(engine, SrcBase)
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(SupplierItem).delete()
            s.commit()
            category_service.ensure_seeded(s)

    def _make_item(self, **kw):
        payload = GoodsItemCreateIn(
            title=kw.pop("title", "罗技 MX Master 3S 无线鼠标"),
            category_id=kw.pop("category_id", "dg_audio"),
            brand=kw.pop("brand", "罗技"),
            cost_price=Decimal(kw.pop("cost_price", "140")),
            sale_price=Decimal(kw.pop("sale_price", "189")),
            stock=kw.pop("stock", 30),
            fish_price=Decimal(kw.pop("fish_price", "140")),
            shipping_fee=Decimal(kw.pop("shipping_fee", "0")),
            submit_action=kw.pop("submit_action", "publish"),
            **kw,
        )
        with SessionLocal() as s:
            item = goods_service.create_item(s, payload)
            s.commit()
            return item

    # ------------------------------------------------ 描述文案：不能有机读码与底价
    def test_10_hint_has_no_category_id_nor_cost(self):
        """推荐描述文案：不写类目 id、不写进货价、不写内部提示。"""
        from src.channel_xianyu.prepare import _build_description_hint

        item = self._make_item()
        text = _build_description_hint(item)
        self.assertNotIn("1688-", text, "类目 id 不能出现在描述里")
        self.assertNotIn("类目：", text)
        self.assertNotIn("进价", text, "进货价是底价，不能写进给买家看的描述")
        self.assertNotIn("建议描述点", text, "那是给运营看的内部提示")
        self.assertIn("一手货源", text)

    def test_20_package_description_is_structured(self):
        """预填包的描述按 标题 / 卖点 / 规格 / 发货 / 售后 分块。"""
        from src.channel_xianyu.publish import build_publish_package

        item = self._make_item(selling_points="全新未拆封,官方正品")
        with SessionLocal() as s:
            row = s.scalar(
                select(SupplierItem).where(SupplierItem.item_id == item.item_id))
            package = build_publish_package(row, s)
        desc = package.description
        self.assertIn("【卖点】", desc)
        self.assertIn("【发货】", desc)
        self.assertIn("【售后】", desc)
        for bad in ("进价", "类目：", "1688-", "建议描述点"):
            self.assertNotIn(bad, desc, f"描述里不该有「{bad}」")

    def test_25_category_text_falls_back_to_readable_word(self):
        """类目认不出中文路径时写「其他」—— 不能把 1688-653 这种机器码贴出去。"""
        from src.channel_xianyu.publish import _category_text

        with SessionLocal() as s:
            item = self._make_item()
            row = s.scalar(
                select(SupplierItem).where(SupplierItem.item_id == item.item_id))
            row.category_id = "1688-653"
            s.commit()
            self.assertEqual(_category_text(row), "其他")
            row.category_id = "dg_audio"
            s.commit()
            self.assertTrue(any("一" <= ch <= "鿿" for ch in _category_text(row)),
                            "认得到的类目要给中文路径")

    # ------------------------------------------------ 页签：字号一致
    def test_30_handoff_tab_font_uniform(self):
        """铺货页签所有字段一个字号一个字体（描述不再单独小一号灰字）。"""
        item = self._make_item()
        html = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn('id="pdpTabHandoff"', html)
        self.assertIn('class="v desc"', html, "描述用统一的值样式")
        self.assertNotIn('class="v mono"', html, "铺货信息里不再有等宽字体那三行")
        self.assertNotIn("font-size: 12px; color: var(--c-text-muted);\">{{ package.description }}",
                         html, "描述不再单独给 12px 灰字")
        # 字号统一的 CSS（认 #pdpTabHandoff 作用域下的 .prefill-row）
        self.assertIn("#pdpTabHandoff .prefill-row .k, #pdpTabHandoff .prefill-row .v", html)
        self.assertIn("#pdpTabHandoff .prefill-row .v, #pdpTabHandoff .prefill-row .v.mono", html)


if __name__ == "__main__":
    unittest.main()
