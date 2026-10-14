"""渔货栈「我要下单」：选品商品订单 + 其他订单（2026-10-11 用户口径）。

- 选品商品订单：在「我的店铺商品」点「下单」直接进来，商品自动带出来；
- 其他订单：按飞书群模板填，或传一张闲鱼订单详情图让系统认（认不全手填）。
落库都是 downstream_orders（状态 pending 待确认），后面走同一套订单流程与逆向流程。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import DownstreamOrder, ShopGoods, SupplierItem  # noqa: E402

ITEM_ID = "T-ORDER-NEW-1"
ALL_IDS = [ITEM_ID]

BUYER_TEXT = "张三 13800000001 广东省深圳市南山区科技园路 1 号 A 座 1203"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(DownstreamOrder).filter(
            DownstreamOrder.channel_sku_id.in_(ALL_IDS + ["", "T-ORDER-NEW-MANUAL"])
        ).delete(synchronize_session=False)
        s.query(ShopGoods).filter(ShopGoods.item_id.in_(ALL_IDS)).delete(
            synchronize_session=False
        )
        s.query(SupplierItem).filter(SupplierItem.item_id.in_(ALL_IDS)).delete(
            synchronize_session=False
        )
        s.commit()


def _make_item() -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-1",
                title="测试-下单用四件套",
                cost_price=Decimal("45.00"),
                fish_price=Decimal("45.00"),
                sale_price=Decimal("99.00"),
                stock=100,
                audit_status="approved",
                is_listed=True,
            )
        )
        s.commit()


def _dso(item_id: str) -> DownstreamOrder | None:
    with SessionLocal() as s:
        return (
            s.query(DownstreamOrder)
            .filter(DownstreamOrder.channel_sku_id == item_id)
            .order_by(DownstreamOrder.id.desc())
            .first()
        )


def _pool() -> None:
    """把这款货放进「我的店铺商品」（下单页要能从池子里挑）。"""
    owner = "u1"
    with SessionLocal() as s:
        row = (
            s.query(ShopGoods)
            .filter(ShopGoods.owner == owner, ShopGoods.item_id == ITEM_ID)
            .first()
        )
        if row is None:
            s.add(ShopGoods(owner=owner, item_id=ITEM_ID, shop_id=0, status="pooled"))
            s.commit()


class SelfOrderNewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post(
            "/login",
            data={"username": "admin", "password": "xianyu2026"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()
        _make_item()
        _pool()

    def tearDown(self):
        _wipe()

    # ------------------------------------------------- 下单页
    def test_10_page_has_two_kinds(self):
        """下单页两种单都在：选品商品订单 / 其他订单（含飞书群模板与传图）。"""
        page = self.client.get("/self/orders/new")
        self.assertEqual(page.status_code, 200)
        self.assertIn("我要下单", page.text)
        self.assertIn("选品商品订单", page.text)
        self.assertIn("其他订单", page.text)
        self.assertIn("闲鱼订单详情图", page.text)
        self.assertIn("【闲鱼订单】", page.text, "其他订单要给出飞书群报单模板")

    def test_12_sample_shot_entry(self):
        """「点击查看样例」：点开弹出闲鱼订单详情样例图；样例图放到 media 就能看。"""
        import base64

        page = self.client.get("/self/orders/new")
        self.assertEqual(page.status_code, 200)
        self.assertIn("点击查看样例", page.text)
        self.assertIn('id="btnShotSample"', page.text)
        self.assertIn("/media/orders/sample-order.jpg", page.text, "页面要指向样例图地址")
        # 样例图放上就能看（media 目录挂载出来的）
        from app.config import settings

        sample = settings.media_path / "orders" / "sample-order.jpg"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        img = self.client.get("/media/orders/sample-order.jpg")
        self.assertEqual(img.status_code, 200, "样例图本身要能打开")

    def test_15_page_prefills_selected_item(self):
        """从「我的店铺商品」点进来：商品自动带出来（标题 + 渔货价）。"""
        page = self.client.get(f"/self/orders/new?item_id={ITEM_ID}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("测试-下单用四件套", page.text)
        self.assertIn(ITEM_ID, page.text)

    def test_18_pool_has_order_entry(self):
        """「我的店铺商品」每件货都有「下单」入口。"""
        page = self.client.get("/self/shop-goods")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/orders/new?item_id=", page.text)

    # ------------------------------------------------- 提交：选品商品订单
    def test_20_create_shop_goods_order(self):
        """选品商品订单：落一笔 pending 的在线订单，买家信息与金额都在。"""
        resp = self.client.post(
            "/self/orders/new",
            data={
                "item_id": ITEM_ID,
                "title": "测试-下单用四件套",
                "qty": "2",
                "sale_price": "99.00",
                "fish_price": "45.00",
                "buyer_name": "张三",
                "buyer_phone": "13800000001",
                "address": "广东省深圳市南山区科技园路 1 号",
                "note": "买家要发票",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:200])

        row = _dso(ITEM_ID)
        self.assertIsNotNone(row, "订单要落库")
        assert row is not None
        self.assertEqual(row.status, "pending", "新单是待确认")
        self.assertEqual(row.channel, "xianyu")
        self.assertEqual(row.qty, 2)
        self.assertEqual(float(row.sale_price), 99.0)
        self.assertEqual(float(row.fish_price or 0), 45.0)
        self.assertEqual(row.buyer_name, "张三")
        self.assertEqual(row.buyer_phone, "13800000001")
        self.assertIn("科技园路", row.address or "")
        self.assertEqual(row.raw_json.get("source"), "shop_goods")
        self.assertEqual(row.raw_json.get("title"), "测试-下单用四件套")
        self.assertEqual(row.raw_json.get("note"), "买家要发票")

    def test_25_create_other_order(self):
        """其他订单：没有 item_id，按模板手填商品名，照样落成在线订单。"""
        resp = self.client.post(
            "/self/orders/new",
            data={
                "item_id": "",
                "title": "自家店里的二手相机",
                "qty": "1",
                "sale_price": "1280.00",
                "fish_price": "900.00",
                "buyer_name": "李四",
                "buyer_phone": "13900000002",
                "address": "浙江省杭州市西湖区文三路 100 号",
                "images": "/media/orders/test.jpg",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:200])

        row = _dso("")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.raw_json.get("source"), "other")
        self.assertEqual(row.raw_json.get("title"), "自家店里的二手相机")
        self.assertIn("/media/orders/test.jpg", row.raw_json.get("images") or [])

    def test_30_empty_goods_rejected(self):
        """商品没填：拦回去提示，不落脏单。"""
        before = len([r for r in [_dso("")] if r])
        resp = self.client.post(
            "/self/orders/new",
            data={"item_id": "", "title": "", "qty": "1", "sale_price": "0"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/orders/new", resp.headers.get("location", ""))
        self.assertEqual(len([r for r in [_dso("")] if r]), before)

    # ------------------------------------------------- 粘贴识别
    def test_40_parse_buyer_text(self):
        """粘贴一段闲鱼收货信息 → 认出收货人 / 电话 / 地址。"""
        resp = self.client.post(
            "/self/orders/parse-buyer", json={"text": BUYER_TEXT}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"), data)
        self.assertEqual(data.get("buyer_name"), "张三")
        self.assertEqual(data.get("phone"), "13800000001")
        self.assertIn("科技园路", data.get("address") or "")

    def test_45_parse_empty_text(self):
        """空文本不给假结果。"""
        resp = self.client.post("/self/orders/parse-buyer", json={"text": "  "})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json().get("ok"))
