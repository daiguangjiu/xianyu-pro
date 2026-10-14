"""商家商品进货源广场 feed（2026-10-04 用户反馈「渔枢商家的商品为什么在商品 feed 里没有展示」）。

排查结论：货源广场 feed（/self/harbor）扫 supplier_items 时按 `last_synced_at DESC NULLS LAST`
排序、只取 HARBOR_SCAN_LIMIT=5000 条，顶部「最新上架」也是按这个时间取前 8 款 ——
而商家发布商品（src/merchant/service.py::create_merchant_item）**从来不写 last_synced_at**，
该列为 NULL → NULLS LAST 把它排到扫描窗口最末尾（货源一超 5000 条直接被切掉），
默认排序下也被埋到很后面的分页，商家就会觉得「我发布的商品没展示出来」。

修法：发布 / 审核通过 / 上架时都写 `last_synced_at = utc_now()`（发布=刚上新）。

顺带回归本轮另外两条：货源广场不再出现「暂无货源，先通过 Swagger POST /api/v1/goods/sync」
与底部那条「上架到店铺：点击后走选品规则引擎…」开发者说明（用户要求删除）。
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
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Supplier, User  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import reset_hub_caches  # noqa: E402
from src.merchant.security import AUTH_COOKIE, create_token  # noqa: E402
from src.merchant.service import create_merchant_item, set_item_listed  # noqa: E402
from src.models import Merchant, SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).delete()
        s.query(Merchant).delete()
        s.query(Supplier).delete()
        s.commit()


class MerchantItemFeed(unittest.TestCase):
    def setUp(self):
        init_db()
        _wipe()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        with SessionLocal() as s:
            m = Merchant(name="测试商家", status="approved")
            s.add(m)
            s.commit()
            self.merchant_id = m.id

    def tearDown(self):
        _wipe()
        reset_hub_caches()

    def _publish(self, title: str = "商家小风扇 USB 便携") -> str:
        with SessionLocal() as s:
            m = s.get(Merchant, self.merchant_id)
            item = create_merchant_item(
                s,
                m,
                {
                    "title": title,
                    "cost_price": "12.5",
                    "sale_price": "29.9",
                    "stock": "88",
                    "category_id": "",
                },
            )
            s.commit()
            return item.item_id

    def test_10_publish_stamps_synced_at(self):
        """发布即写 last_synced_at：没有它，商品会被 NULLS LAST 排到扫描窗口最末尾。"""
        item_id = self._publish()
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertIsNotNone(item.last_synced_at)
            self.assertEqual(item.audit_status, "pending")
            self.assertFalse(item.is_listed)

    def _sign_in_admin(self) -> None:
        """审核台写操作要求 role=admin：直接签一张商家端会话票据（与商家端登录同一条路）。"""
        with SessionLocal() as s:
            user = s.query(User).filter(User.role == "admin").first()
            self.client.cookies.set(
                AUTH_COOKIE, create_token(user.id, user.phone or "", user.role)
            )

    def _approve(self, item_id: str) -> None:
        self._sign_in_admin()
        self.client.post(
            f"/self/platform/items/{item_id}/audit",
            data={"action": "approve"},
            follow_redirects=False,
        )

    def test_20_approve_lists_and_refreshes(self):
        """平台审核通过 → 自动上架 + 刷新时间（刚上架就该排在「最新上架」里）。"""
        item_id = self._publish()
        with SessionLocal() as s:
            before = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id)).last_synced_at
        self._approve(item_id)
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertEqual(item.audit_status, "approved")
            self.assertTrue(item.is_listed)
            self.assertGreaterEqual(item.last_synced_at, before)

    def test_30_merchant_item_shows_in_feed(self):
        """核心回归：审核通过后，商品真的出现在货源广场 feed 里（「最新上架」最前面）。"""
        item_id = self._publish("商家小风扇 USB 便携")
        self._approve(item_id)
        html = self.client.get("/self/harbor", params={"tab": "new"}).text
        self.assertIn("商家小风扇 USB 便携", html)
        self.assertIn(f'data-item="{item_id}"', html)

    def test_40_relist_refreshes_synced_at(self):
        """重新上架也刷时间（否则商品还排在老位置，商家找不到自己刚上架的货）。"""
        item_id = self._publish()
        with SessionLocal() as s:
            m = s.get(Merchant, self.merchant_id)
            svc_item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            svc_item.audit_status = "approved"
            s.commit()
        with SessionLocal() as s:
            svc_item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            svc_item.last_synced_at = None
            s.commit()
        with SessionLocal() as s:
            m = s.get(Merchant, self.merchant_id)
            self.assertIsNotNone(set_item_listed(s, m, item_id, True))
            s.commit()
        with SessionLocal() as s:
            item = s.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
            self.assertTrue(item.is_listed)
            self.assertIsNotNone(item.last_synced_at)


class HarborNoDevHints(unittest.TestCase):
    """用户删掉的两块开发者提示不该再出现在货源广场上。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def test_10_no_sync_hint_in_empty_state(self):
        html = self.client.get("/self/harbor").text
        self.assertNotIn("goods/sync", html)
        self.assertNotIn("先通过 Swagger", html)

    def test_20_no_selection_alert(self):
        html = self.client.get("/self/harbor").text
        self.assertNotIn("点击后走选品规则引擎", html)
        self.assertNotIn("闲参谋（SaaS）凭一次性票据取包", html)

    def test_30_more_categories_style_matches_rail(self):
        """「更多类目」与其它类目条同款：不是虚线框居中，左边距 / 图标位 / 字号 一致。"""
        html = self.client.get("/self/harbor").text
        i = html.find(".mega-rail-more {")
        self.assertGreater(i, 0)
        block = html[i : i + 420]
        self.assertNotIn("dashed", block)
        self.assertIn("border-left: 3px solid transparent", block)
        self.assertIn("font-size: 13px", block)


if __name__ == "__main__":
    unittest.main()
