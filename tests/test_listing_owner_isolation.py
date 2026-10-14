"""铺货状态按账号隔离（2026-10-13 用户拍板）。

背景（用户报的现象）：货源广场里只有 Apple pencil 那一款显示「已上架」，而当前账号
**根本没铺过它、名下也没有店铺**。根因是 `channel_listings` 全站只有一条
(item_id, channel) 记录、不带归属 —— 谁把货收进店铺商品池，所有账号（包括学员）
看到的都是同一个「已上架」。

现在记录带 `owner`（`_purchase_owner` 的 key，形如 u1）：

- 同一款货每个账号各一条记录，卡片只认当前账号自己那条；
- owner 为空 = 平台级记录（选品引擎批量跑出来的），不算任何人的铺货状态；
- 入池 / 移出只改这个账号自己那条，别人的记录不受影响。
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.timeutil import utc_now  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from src.channel_xianyu.listing import save_listing, status_text  # noqa: E402
from src.models import ChannelListing, ShopGoods, SupplierItem  # noqa: E402
from src.self_purchase import shop_pool  # noqa: E402

ITEM_ID = "T-LIST-OWNER-1"
B_USERNAME = "t-owner-b"


def _wipe() -> None:
    with SessionLocal() as s:
        for model in (ChannelListing, ShopGoods):
            s.query(model).filter(model.item_id == ITEM_ID).delete(
                synchronize_session=False
            )
        s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete(
            synchronize_session=False
        )
        s.commit()


def _make_item() -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-1",
                title="测试-归属隔离 Apple pencil",
                category_id="dg_audio",
                main_image="https://img.example.com/a.jpg",
                images=["https://img.example.com/a.jpg"],
                cost_price=Decimal("150.00"),
                fish_price=Decimal("150.00"),
                sale_price=Decimal("199.00"),
                stock=100,
                source_type="self_operated",
                audit_status="approved",
                is_listed=True,
                last_synced_at=utc_now(),
            )
        )
        s.commit()


def _statuses() -> dict[str, str]:
    with SessionLocal() as s:
        rows = (
            s.execute(select(ChannelListing).where(ChannelListing.item_id == ITEM_ID))
            .scalars()
            .all()
        )
        return {r.owner: r.status for r in rows}


def _card_state(html: str, item_id: str) -> str:
    """从货源广场的 HTML 里取出这张卡片右下角的状态文案。

    同一款货在页面里可能出现多次（feed / 活动节点），所以从这款货的详情链接往后
    找它**紧跟着的**那块 fish-actions，而不是按整段切（会串到别的卡片上去）。
    """
    # 卡片是 <article class="fish-card">…：页面头部内联 CSS 里也有 fish-actions 这个
    # 类名，活动节点区块里也可能先出现这款货的链接，所以按 <article 切开只取卡片本体
    for seg in html.split("<article")[1:]:
        if item_id not in seg:
            continue
        pos = seg.find("fish-actions")
        if pos < 0:
            return "no-actions"
        tail = seg[pos : pos + 700]
        for marker, label in (
            ("已铺货</span>", "已铺货"),
            ("待发布</a>", "待发布"),
            ("发布失败</span>", "发布失败"),
            ("铺货到店铺", "铺货到店铺"),
            ("已下架</button>", "已下架"),
        ):
            if marker in tail:
                return label
        return "?"
    return "no-card"


class ListingOwnerIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            first = s.query(User).order_by(User.id).first()
            cls.uid_a = first.id
            b = s.query(User).filter(User.username == B_USERNAME).first()
            if b is None:
                b = User(
                    username=B_USERNAME,
                    display_name="B 号学员",
                    portals="self",
                    status="active",
                )
                s.add(b)
                s.commit()
                s.refresh(b)
            cls.uid_b = b.id
        cls.owner_a = f"u{cls.uid_a}"
        cls.owner_b = f"u{cls.uid_b}"

    def setUp(self):
        _wipe()
        _make_item()

    def tearDown(self):
        _wipe()

    def _client(self, uid: int) -> TestClient:
        c = TestClient(app)
        c.cookies.set(COOKIE_NAME, make_session(uid)[0])
        return c

    def _item(self):
        with SessionLocal() as s:
            return (
                s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).first()
            )

    # ---------------------------------------------------------------- 落库
    def test_10_save_listing_is_per_owner(self):
        """同一款货每个账号各一条记录；同一账号重复铺货只更新自己那条。"""
        with SessionLocal() as db:
            item = (
                db.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).first()
            )
            a = save_listing(db, item, owner=self.owner_a, source="draft", commit=True)
            b = save_listing(db, item, owner=self.owner_b, source="draft", commit=True)
            self.assertNotEqual(a.id, b.id, "两个账号各存一条，不能共用")
            a_again = save_listing(
                db, item, owner=self.owner_a, source="handoff", commit=True
            )
            self.assertEqual(a_again.id, a.id, "同一账号幂等：不新增记录")
        statuses = _statuses()
        self.assertEqual(set(statuses), {self.owner_a, self.owner_b})
        self.assertEqual(len(statuses), 2)

    # ------------------------------------------------------------ 入池 / 移出
    def test_20_pool_sync_only_touches_that_owner(self):
        """A 入池只把 A 的记录置 published；B 的记录保持 draft。移出同理只回退 A。"""
        with SessionLocal() as db:
            item = (
                db.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).first()
            )
            save_listing(db, item, owner=self.owner_a, source="draft", commit=True)
            save_listing(db, item, owner=self.owner_b, source="draft", commit=True)
            shop_pool.add_to_pool(db, self.owner_a, item)

        statuses = _statuses()
        self.assertEqual(statuses.get(self.owner_a), "published", "入池 = 我铺过了")
        self.assertEqual(
            statuses.get(self.owner_b), "draft", "别人的记录不能被我入池改掉"
        )

        with SessionLocal() as db:
            rows = shop_pool.list_pool(db, self.owner_a)
            self.assertEqual(len(rows), 1)
            shop_pool.remove_from_pool(db, self.owner_a, rows[0].id)

        statuses = _statuses()
        self.assertEqual(statuses.get(self.owner_a), "draft", "移出池子回到未铺")
        self.assertEqual(statuses.get(self.owner_b), "draft")

    # ------------------------------------------------------------ 卡片呈现
    def test_30_harbor_card_shows_only_my_own_state(self):
        """A 铺过 → A 的卡片是「已铺货」，B 的卡片仍是「铺货到店铺」。"""
        with SessionLocal() as db:
            item = (
                db.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).first()
            )
            save_listing(db, item, owner=self.owner_a, source="draft", commit=True)
            shop_pool.add_to_pool(db, self.owner_a, item)

        html_a = self._client(self.uid_a).get("/self/harbor").text
        html_b = self._client(self.uid_b).get("/self/harbor").text
        self.assertEqual(
            _card_state(html_a, ITEM_ID), "已铺货", "A 自己铺过，卡片显示已铺货"
        )
        self.assertEqual(
            _card_state(html_b, ITEM_ID),
            "铺货到店铺",
            "B 没铺过，不能跟着显示已铺货（这正是用户报的问题）",
        )

    def test_40_platform_record_is_not_my_state(self):
        """平台级老记录（owner 为空，选品跑出来的）不算任何人的铺货状态。"""
        with SessionLocal() as db:
            db.add(
                ChannelListing(
                    item_id=ITEM_ID,
                    channel="xianyu",
                    owner="",
                    status="published",
                    created_at=datetime(2026, 10, 5, 9, 20),
                )
            )
            db.commit()

        for uid in (self.uid_a, self.uid_b):
            html = self._client(uid).get("/self/harbor").text
            self.assertEqual(
                _card_state(html, ITEM_ID),
                "铺货到店铺",
                f"账号 {uid} 没铺过，平台级记录不该算在他头上",
            )

    # ---------------------------------------------------------------- 文案
    def test_50_status_text_and_page_wording(self):
        """「已上架」改叫「已铺货」；发布失败单独一档，不再混进「铺货到店铺」。"""
        self.assertEqual(status_text("published"), "已铺货")
        self.assertEqual(status_text("failed"), "发布失败")
        html = self._client(self.uid_a).get("/self/harbor").text
        self.assertNotIn("已上架", html, "货源广场不再出现「已上架」这个说法")


if __name__ == "__main__":
    unittest.main()
