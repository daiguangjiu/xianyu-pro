"""详情页结算条两个按钮的服务端落库：POST /self/goods/{item_id}/listing。

口径（2026-10-02 用户拍板）：

- 「上架到店铺」以前只生成预填包 + 打开闲鱼发布页，**不写库** ——「我的上架」永远是空的；
  现在必须落一条 `channel_listings`（source=handoff + handoff_at），并在返回里给页面
  显示「铺货记录 #12（待发布）」；
- 「仅生成草稿」落同一条记录（source=draft），不打开任何页面，也不因选品规则被拒而失败；
- 幂等：同一款货只有一条记录；已上架（published）的记录只补时间戳，状态不许被拉回 draft；
- 已由渔枢系统下架的货源：409，且不落库（与 POST /api/v1/xianyu/handoff 同一道闸）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import ChannelListing, ShopGoods, SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ITEM_ID = "T-LISTING-1"
UNLISTED_ID = "T-LISTING-UNLISTED"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(ChannelListing).filter(
            ChannelListing.item_id.in_([ITEM_ID, UNLISTED_ID])
        ).delete(synchronize_session=False)
        # 「上架到店铺」会顺带入店铺商品池（两套铺货记录互通），这里也要清干净，
        # 否则会污染别的用例（采购域的池子断言）
        s.query(ShopGoods).filter(
            ShopGoods.item_id.in_([ITEM_ID, UNLISTED_ID])
        ).delete(synchronize_session=False)
        s.query(SupplierItem).filter(
            SupplierItem.item_id.in_([ITEM_ID, UNLISTED_ID])
        ).delete(synchronize_session=False)
        s.commit()


def _make_item(item_id: str = ITEM_ID, *, unlisted: bool = False) -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=item_id,
                supplier_id="sup-1",
                title="测试-北欧四件套 1.8m",
                category_id="ht_bedding",
                main_image="https://img.example.com/a.jpg",
                images=["https://img.example.com/a.jpg"],
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                stock=100,
                source_type="third_party",
                audit_status="approved",
                is_listed=not unlisted,
                shipping_mode="free",
                after_sale="7天无理由退换",
                unlisted_at=None if not unlisted else datetime(2026, 10, 2, 10, 0),
                unlisted_reason="渔枢系统下架（商品档案已下架）" if unlisted else None,
            )
        )
        s.commit()


def _listing(item_id: str = ITEM_ID) -> ChannelListing | None:
    with SessionLocal() as s:
        return s.query(ChannelListing).filter(ChannelListing.item_id == item_id).first()


def _listing_count(item_id: str = ITEM_ID) -> int:
    with SessionLocal() as s:
        return int(
            s.scalar(
                select(func.count(ChannelListing.id)).where(
                    ChannelListing.item_id == item_id
                )
            )
            or 0
        )


class ListingActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        _wipe()
        _make_item()

    def tearDown(self):
        _wipe()

    # ---------- 上架到店铺 ----------
    def test_10_handoff_writes_listing(self):
        """「上架到店铺」：落一条铺货记录 + 给预填包 / 发布页 URL。"""
        resp = self.client.post(f"/self/goods/{ITEM_ID}/listing", json={"mode": "handoff"})
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "handoff")
        self.assertTrue(data["publish_url"], "要给出闲鱼发布页 URL")
        self.assertTrue(data["package"]["title"])
        brief = data["listing"]
        self.assertEqual(brief["status"], "draft")
        self.assertEqual(brief["status_text"], "待发布")
        self.assertEqual(brief["source"], "handoff")
        self.assertTrue(brief["handoff_at"], "要记带入发布的时间")
        self.assertEqual(brief["id"], _listing().id)

        row = _listing()
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "draft")
        self.assertEqual(row.source, "handoff")
        self.assertIsNotNone(row.handoff_at)

        # 与「店铺商品池」互通：铺到自己店里的货要同时在池子里（两套记录同一件事）
        self.assertTrue(data.get("pool"), "上架到店铺要同步进店铺商品池")
        with SessionLocal() as s:
            pooled = (
                s.query(ShopGoods)
                .filter(ShopGoods.item_id == ITEM_ID, ShopGoods.status == "pooled")
                .first()
            )
        self.assertIsNotNone(pooled, "店铺商品池里要能看到这款货")
        self.assertEqual(float(row.suggested_price), 99.0)

    def test_15_handoff_is_idempotent(self):
        """同一款货点两次：还是同一条记录（不会越点越多）。"""
        first = self.client.post(
            f"/self/goods/{ITEM_ID}/listing", json={"mode": "handoff"}
        ).json()["listing"]["id"]
        second = self.client.post(
            f"/self/goods/{ITEM_ID}/listing", json={"mode": "handoff"}
        ).json()["listing"]["id"]
        self.assertEqual(first, second)
        self.assertEqual(_listing_count(), 1)

    def test_20_published_status_is_kept(self):
        """已铺货的商品再点「上架到店铺」：状态仍是已铺货，只补带入发布时间。

        记录带 owner（当前登录的 admin = u1），否则这条历史记录不属于当前账号，
        会被 save_listing 另起一条（2026-10-13 起铺货状态按账号隔离）。
        """
        with SessionLocal() as s:
            s.add(
                ChannelListing(
                    item_id=ITEM_ID,
                    channel="xianyu",
                    owner="u1",
                    status="published",
                    channel_listing_id="xy-999",
                )
            )
            s.commit()
        resp = self.client.post(f"/self/goods/{ITEM_ID}/listing", json={"mode": "handoff"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["listing"]["status"], "published")
        row = _listing()
        self.assertEqual(row.status, "published")
        self.assertEqual(row.channel_listing_id, "xy-999")
        self.assertIsNotNone(row.handoff_at)

    def test_25_unlisted_item_is_rejected(self):
        """渔枢系统下架的货源：409，且一条记录都不落。"""
        _make_item(UNLISTED_ID, unlisted=True)
        resp = self.client.post(
            f"/self/goods/{UNLISTED_ID}/listing", json={"mode": "handoff"}
        )
        self.assertEqual(resp.status_code, 409, resp.text[:300])
        self.assertIn("下架", resp.json()["detail"])
        self.assertEqual(_listing_count(UNLISTED_ID), 0)

    def test_30_missing_item_404_and_bad_mode_400(self):
        self.assertEqual(
            self.client.post("/self/goods/not-exist-xyz/listing", json={"mode": "draft"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/self/goods/{ITEM_ID}/listing", json={"mode": "whatever"}).status_code,
            400,
        )

    # ---------- 仅生成草稿 ----------
    def test_35_draft_writes_listing_without_page(self):
        """「仅生成草稿」：落记录（source=draft）、不带发布页 URL、不去跑选品规则。"""
        resp = self.client.post(f"/self/goods/{ITEM_ID}/listing", json={"mode": "draft"})
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        data = resp.json()
        self.assertEqual(data["mode"], "draft")
        self.assertEqual(data["publish_url"], "")
        self.assertEqual(data["listing"]["source"], "draft")
        self.assertIsNone(data["listing"]["handoff_at"])
        self.assertIn("已保存", data["message"])
        row = _listing()
        self.assertEqual(row.status, "draft")
        self.assertEqual(row.source, "draft")
        self.assertIsNone(row.handoff_at)

    def test_40_listing_status_endpoint(self):
        """GET 查询铺货记录：没有返回 {}，有了返回同一条。"""
        empty = self.client.get(f"/self/goods/{ITEM_ID}/listing").json()
        self.assertEqual(empty["listing"], {})
        created = self.client.post(
            f"/self/goods/{ITEM_ID}/listing", json={"mode": "draft"}
        ).json()["listing"]
        got = self.client.get(f"/self/goods/{ITEM_ID}/listing").json()["listing"]
        self.assertEqual(got["id"], created["id"])
        self.assertEqual(got["source_text"], "仅存草稿")

    # ---------- 页面 ----------
    def test_45_detail_page_buttons_and_js(self):
        """详情页结算条：1688 那种等宽样式（主按钮实心 + 一个白底描边），无图标。

        2026-10-03 起按钮语义换成采购域（照 1688）：立即下单 / 加采购单 / 店铺铺货；
        **2026-10-11 去掉「加采购单」，只剩「立即下单 + 店铺铺货」两个**；
        **2026-10-12 用户又拍板补回「加采购车」**（攒到车里，能给不同的人各带一件）。
        """
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        for marker in (
            "grid-template-columns: repeat(3, minmax(0, 1fr))",
            "pdp-btn primary\" id=\"btnBuyNow",
            "pdp-btn sec",
            "id=\"btnPool",
            "id=\"btnCart",
            "立即下单",
            "加采购车",
            "店铺铺货",
        ):
            self.assertIn(marker, html, f"结算条缺少 {marker}")
        # 叫法统一成「加采购车」（旧的「加采购单」已经不用了）
        self.assertNotIn("加采购单", html)
        self.assertIn("pdpCartAdd", html)
        # 老的三个动作（上架到店铺 / 仅生成草稿 / 复制预填字段）也已下线
        self.assertNotIn("上架到店铺", html)
        self.assertNotIn("btnHandoff", html)
        self.assertNotIn("LISTING_ACTION_URL", html)
        # 两个动作分别打：结算页 / 店铺商品池
        self.assertIn(f"/self/goods/{ITEM_ID}/pool", html)
        self.assertIn("/self/checkout", html)

    def test_50_selection_page_removed(self):
        """「我的上架」页 2026-10-11 下线：入口与页面都没了，铺货记录仍照落库。

        页面删了不影响「上架到店铺 / 仅生成草稿」的落库与幂等（那几条用例在上面）。
        """
        self.client.post(f"/self/goods/{ITEM_ID}/listing", json={"mode": "handoff"})
        self.assertEqual(self.client.get("/self/selection").status_code, 404, "页面已下线")
        for page in ("/self/harbor", "/self/shop-goods"):
            html = self.client.get(page).text
            self.assertNotIn("/self/selection", html, f"{page} 不该再有「我的上架」入口")
        self.assertEqual(_listing_count(ITEM_ID), 1, "铺货记录照旧落库")


if __name__ == "__main__":
    unittest.main()
