"""渔货栈采购域：商品详情页三个按钮的完整闭环（2026-10-03 用户拍板）。

口径：
- 「立即下单」→ 结算页（收货地址 / 商品明细 / 留言 / 交易与配送 / 价格明细）→ 提交订单
  → 收银台付款 → 采购单（待支付 → 待发货 → 已发货 → 已完成，可取消）；
- 「加采购单」→ 采购车（同款同规格累加、改数量、删行、勾选结算），走同一条结算链路；
- 「店铺铺货」→ 店铺商品池（幂等入池 + 个人闲置店铺场景下的**素材打包下载**）。

价格口径：单价 = 该规格的成本价（sku 级优先，回落商品渔货价）；整单运费按「同一款货只算一次」。
"""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import (  # noqa: E402
    PurchaseCartItem,
    ShopGoods,
    StockOrder,
    StockOrderItem,
    SupplierItem,
)

ITEM_ID = "T-PURCHASE-1"
ITEM2_ID = "T-PURCHASE-2"
NO_STOCK_ID = "T-PURCHASE-NOSTOCK"
UNLISTED_ID = "T-PURCHASE-UNLISTED"
ALL_IDS = [ITEM_ID, ITEM2_ID, NO_STOCK_ID, UNLISTED_ID]


def _wipe() -> None:
    """清干净：采购域三张表是本轮新建的，只有这个测试文件在用，整表清即可。"""
    with SessionLocal() as s:
        for model in (PurchaseCartItem, ShopGoods):
            s.query(model).filter(model.item_id.in_(ALL_IDS)).delete(synchronize_session=False)
        s.query(StockOrderItem).delete(synchronize_session=False)
        s.query(StockOrder).delete(synchronize_session=False)
        s.query(SupplierItem).filter(
            SupplierItem.item_id.in_(ALL_IDS)
        ).delete(synchronize_session=False)
        s.commit()


def _make_item(
    item_id: str = ITEM_ID,
    *,
    title: str = "测试-全棉四件套 1.8m",
    stock: int = 100,
    sale_price: Decimal | None = Decimal("99.00"),
    shipping_mode: str = "free",
    shipping_fee: Decimal = Decimal("0.00"),
    unlisted: bool = False,
) -> None:
    skus = [
        {"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": 45.5, "stock": 60},
        {"sku_id": "s2", "spec_text": "灰 / 1.8m", "price": 48.0, "stock": 40},
    ]
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=item_id,
                supplier_id="sup-1",
                title=title,
                category_id="ht_bedding",
                # 用本机不可达端口当图床：素材打包会立刻失败并优雅跳过（不拖慢测试）
                main_image="http://127.0.0.1:9/a.jpg",
                images=["http://127.0.0.1:9/a.jpg"],
                detail_images=["http://127.0.0.1:9/d.jpg"],
                detail_html="<p>第一行说明</p><p>第二行说明</p>",
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=sale_price,
                market_price=Decimal("169.00"),
                stock=stock,
                sku_list={"skus": skus},
                source_type="third_party",
                audit_status="approved",
                is_listed=not unlisted,
                item_code="YHZ-TEST-0001",
                brand="优眠",
                shipping_mode=shipping_mode,
                shipping_fee=shipping_fee,
                delivery_days=2,
                after_sale="7天无理由退换",
                supplier_tip="档口下午 5 点前下单当天发",
                unlisted_at=None if not unlisted else datetime(2026, 10, 3, 9, 0),
                unlisted_reason=None if not unlisted else "渔枢系统下架（商品档案已下架）",
            )
        )
        s.commit()


def _cart_rows() -> list[PurchaseCartItem]:
    with SessionLocal() as s:
        return list(s.query(PurchaseCartItem).all())


def _order(order_no: str) -> StockOrder | None:
    with SessionLocal() as s:
        return s.query(StockOrder).filter(StockOrder.order_no == order_no).first()


def _order_items(order_no: str) -> list[StockOrderItem]:
    with SessionLocal() as s:
        order = s.query(StockOrder).filter(StockOrder.order_no == order_no).first()
        if order is None:
            return []
        return list(
            s.query(StockOrderItem).filter(StockOrderItem.order_id == order.id).all()
        )


def _pool_rows() -> list[ShopGoods]:
    with SessionLocal() as s:
        return list(s.query(ShopGoods).filter(ShopGoods.status == "pooled").all())


# 铺货到店铺的前置条件（2026-10-11 用户拍板）：名下得有一家**审核通过**的店铺。
# 测试里给 admin 现造一家，用完就删（别影响别的用例数店铺）。
SHOP_NAME = "测试-铺货用店铺"


def _set_shop(approved: bool = True) -> None:
    from app.models import SHOP_AUDIT_PENDING, Shop, User  # noqa: PLC0415

    with SessionLocal() as s:
        uid = int(s.query(User).filter(User.username == "admin").one().id)
        s.query(Shop).filter(Shop.name == SHOP_NAME).delete(synchronize_session=False)
        s.add(
            Shop(
                name=SHOP_NAME,
                owner_user_id=uid,
                audit_status="approved" if approved else SHOP_AUDIT_PENDING,
            )
        )
        s.commit()


def _drop_shop() -> None:
    from app.models import Shop  # noqa: PLC0415

    with SessionLocal() as s:
        s.query(Shop).filter(Shop.name == SHOP_NAME).delete(synchronize_session=False)
        s.commit()


class SelfPurchaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        # /self 全站要登录（合并部署时与渔枢系统同一套账号）
        resp = cls.client.post(
            "/login",
            data={"username": "admin", "password": "xianyu2026"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()
        _make_item()
        _make_item(ITEM2_ID, title="测试-乳胶枕", stock=50, sale_price=Decimal("59.00"))
        _make_item(NO_STOCK_ID, title="测试-无库存", stock=0)
        _make_item(UNLISTED_ID, title="测试-已下架", unlisted=True)

    def tearDown(self):
        _wipe()
        _drop_shop()

    # ---------- 加采购单 / 采购车 ----------
    def test_10_cart_add_and_accumulate(self):
        resp = self.client.post(
            "/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 2}
        )
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        # 单价取该规格成本价 45.50，合计 91.00
        self.assertEqual(data["summary"]["goods_amount"], "91.00")
        self.assertIn("灰 / 1.5m", data["message"])

        # 同款同规格再加 3 件 → 还是一行，数量累加
        again = self.client.post(
            "/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 3}
        ).json()
        self.assertEqual(again["count"], 1)
        rows = _cart_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].qty, 5)

    def test_15_cart_add_guards(self):
        """没库存 / 已下架 / 不存在：都要挡住并给话说。"""
        no_stock = self.client.post(
            "/self/cart/add", json={"item_id": NO_STOCK_ID, "sku_id": "", "qty": 1}
        )
        self.assertEqual(no_stock.status_code, 400)
        self.assertIn("库存", no_stock.json()["detail"])

        unlisted = self.client.post(
            "/self/cart/add", json={"item_id": UNLISTED_ID, "sku_id": "", "qty": 1}
        )
        self.assertEqual(unlisted.status_code, 404)
        self.assertIn("下架", unlisted.json()["detail"])

        missing = self.client.post(
            "/self/cart/add", json={"item_id": "no-such-item", "sku_id": "", "qty": 1}
        )
        self.assertEqual(missing.status_code, 404)

    def test_20_cart_page_update_remove_clear(self):
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 1})
        self.client.post("/self/cart/add", json={"item_id": ITEM2_ID, "sku_id": "", "qty": 1})

        page = self.client.get("/self/cart")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("采购车", html)
        self.assertIn("测试-全棉四件套 1.8m", html)
        self.assertIn("测试-乳胶枕", html)
        self.assertIn("去结算", html)

        line_id = _cart_rows()[0].id
        updated = self.client.post(
            "/self/cart/update", json={"line_id": line_id, "qty": 4}
        ).json()
        self.assertEqual(updated["line"]["qty"], 4)

        removed = self.client.post("/self/cart/remove", json={"line_ids": [line_id]}).json()
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(removed["count"], 1)

        cleared = self.client.post("/self/cart/clear", json={}).json()
        self.assertEqual(cleared["removed"], 1)
        self.assertEqual(len(_cart_rows()), 0)

    # ---------- 立即下单：结算 → 支付 ----------
    def test_25_checkout_page_buy_now(self):
        page = self.client.get(
            f"/self/checkout?item_id={ITEM_ID}&sku_id=s2&qty=2"
        )
        self.assertEqual(page.status_code, 200)
        html = page.text
        for marker in ("确认订单", "收货地址", "商品信息", "留言", "交易方式", "配送方式",
                       "店铺明细", "价格明细", "立即下单"):
            self.assertIn(marker, html, f"结算页缺少 {marker}")
        # 交易方式（2026-10-12 用户拍板）：担保交易撤掉，改「带教代收」；
        # 「在线支付（微信 / 支付宝）」先置灰，后续版本再放出来。
        self.assertIn("带教代收", html)
        self.assertIn("货款可以线下转付给带教，带教负责带采", html)
        self.assertIn("在线支付", html)
        self.assertIn("后续版本再更新", html)
        self.assertIn("ck-pay-off", html, "在线支付这一行要压暗（还不能选）")
        self.assertNotIn("担保交易", html, "担保交易已从结算页撤掉")
        self.assertIn('name="trade_type" value="agent"', html, "下单落库的交易方式是带教代收")
        # 48.00 × 2 = 96.00，包邮 → 应付 96.00
        self.assertIn("¥96.00", html)

    def test_27_order_records_agent_trade_type(self):
        """下单后订单上的交易方式是「带教代收」，收银台 / 采购单里也这么显示。"""
        order_no = self._new_order()
        self.assertEqual(_order(order_no).trade_type, "agent")

        pay_page = self.client.get(f"/self/pay/{order_no}")
        self.assertEqual(pay_page.status_code, 200)
        self.assertIn("带教代收", pay_page.text)

        detail = self.client.get(f"/self/purchases/{order_no}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("带教代收", detail.text)

    def test_30_submit_order_creates_pending_pay(self):
        resp = self.client.post(
            "/self/checkout/submit",
            data={
                "source": "buy_now",
                "item_id": ITEM_ID,
                "sku_id": "s2",
                "qty": 2,
                "receiver_name": "戴广久",
                "receiver_phone": "13671670430",
                "receiver_address": "上海市 闵行区 虹桥镇 万源路 2161 弄 150 号 1 幢 306 室",
                "remark": "工作日送",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        location = resp.headers["location"]
        self.assertIn("/self/pay/CG", location)
        order_no = location.rsplit("/", 1)[-1]

        order = _order(order_no)
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "pending_pay")
        self.assertEqual(order.source, "buy_now")
        self.assertEqual(order.total_qty, 2)
        self.assertEqual(float(order.goods_amount), 96.0)
        self.assertEqual(float(order.freight), 0.0)
        self.assertEqual(float(order.pay_amount), 96.0)
        self.assertEqual(order.receiver_name, "戴广久")
        items = _order_items(order_no)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].sku_id, "s2")
        self.assertEqual(float(items[0].amount), 96.0)

    def test_35_pay_then_flow_to_completed(self):
        order_no = self._new_order()
        # 收银台
        pay_page = self.client.get(f"/self/pay/{order_no}")
        self.assertEqual(pay_page.status_code, 200)
        self.assertIn("确认支付", pay_page.text)
        self.assertIn("账户余额", pay_page.text)

        # 付款
        paid = self.client.post(
            f"/self/pay/{order_no}", data={"method": "alipay"}, follow_redirects=False
        )
        self.assertEqual(paid.status_code, 303)
        order = _order(order_no)
        # 2026-10-14：支付不走线上，付完先落「待确认」，运营经理确认到账才进「待发货」
        self.assertEqual(order.status, "confirming")
        self.assertEqual(order.pay_method, "alipay")
        self.assertTrue(order.pay_no and order.pay_no.startswith("PAY"))
        self.assertIsNotNone(order.paid_at)

        # 待确认的不能再付
        again = self.client.post(
            f"/self/pay/{order_no}", data={"method": "balance"}, follow_redirects=False
        )
        self.assertEqual(again.status_code, 303)
        self.assertEqual(_order(order_no).status, "confirming")

        # 运营经理确认收款 → 待发货
        confirmed = self.client.post(
            f"/self/purchases/{order_no}/actions",
            data={"action": "confirm_pay"}, follow_redirects=False,
        )
        self.assertEqual(confirmed.status_code, 303)
        order = _order(order_no)
        self.assertEqual(order.status, "paid")

        # 平台发货 → 确认收货
        self.client.post(
            f"/self/purchases/{order_no}/actions",
            data={"action": "ship", "company": "中通", "shipping_no": "ZT123456"},
            follow_redirects=False,
        )
        self.assertEqual(_order(order_no).status, "shipped")
        self.assertEqual(_order(order_no).shipping_no, "ZT123456")

        self.client.post(
            f"/self/purchases/{order_no}/actions", data={"action": "complete"}, follow_redirects=False
        )
        done = _order(order_no)
        self.assertEqual(done.status, "completed")
        self.assertIsNotNone(done.completed_at)

    def test_40_cancel_order(self):
        order_no = self._new_order()
        self.client.post(
            f"/self/purchases/{order_no}/actions",
            data={"action": "cancel", "reason": "不想要了"},
            follow_redirects=False,
        )
        order = _order(order_no)
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.cancel_reason, "不想要了")
        # 取消后不能支付 / 不能确认收货
        self.client.post(f"/self/pay/{order_no}", data={"method": "balance"}, follow_redirects=False)
        self.assertEqual(_order(order_no).status, "cancelled")

    def test_45_purchases_pages(self):
        order_no = self._new_order()
        listing = self.client.get("/self/purchases")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("我的采购单", listing.text)
        self.assertIn(order_no, listing.text)
        self.assertIn("待支付", listing.text)

        detail = self.client.get(f"/self/purchases/{order_no}")
        self.assertEqual(detail.status_code, 200)
        for marker in ("商品明细", "金额明细", "收货信息", "提交订单", "去支付", "取消订单"):
            self.assertIn(marker, detail.text, f"详情页缺少 {marker}")

    # ---------- 采购车结算闭环 ----------
    def test_50_cart_checkout_flow(self):
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 1})
        self.client.post("/self/cart/add", json={"item_id": ITEM2_ID, "sku_id": "", "qty": 2})
        ids = ",".join(str(row.id) for row in _cart_rows())

        page = self.client.get(f"/self/checkout?ids={ids}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("从采购车结算", page.text)

        resp = self.client.post(
            "/self/checkout/submit",
            data={"source": "cart", "line_ids": ids, "receiver_name": "李白"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        order_no = resp.headers["location"].rsplit("/", 1)[-1]
        order = _order(order_no)
        self.assertEqual(order.source, "cart")
        self.assertEqual(order.item_kinds, 2)
        self.assertEqual(order.total_qty, 3)
        self.assertEqual(len(_order_items(order_no)), 2)
        # 下单后采购车清空（已下单的行走采购单）
        self.assertEqual(len(_cart_rows()), 0)

    # ---------- 店铺铺货 / 店铺商品池 / 素材下载 ----------
    def test_55_add_to_pool_idempotent(self):
        _set_shop()
        first = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        self.assertEqual(first.status_code, 200, first.text[:300])
        data = first.json()
        self.assertTrue(data["ok"])
        self.assertIn("店铺商品池", data["message"])
        self.assertTrue(data["materials_url"].endswith("/materials.zip"))

        second = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={}).json()
        self.assertEqual(second["pool"]["id"], data["pool"]["id"])
        self.assertEqual(len(_pool_rows()), 1)

        row = _pool_rows()[0]
        self.assertEqual(float(row.price), 45.5)          # 渔货价
        self.assertEqual(float(row.suggested_price), 99.0)  # 建议售价（与详情页同口径）

    def test_60_pool_page_and_remove(self):
        _set_shop()
        self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        page = self.client.get("/self/shop-goods")
        self.assertEqual(page.status_code, 200)
        # 2026-10-11 改名「我的店铺商品」：列表 / 卡片两种看法 + 选品时间标签
        self.assertIn("我的店铺商品", page.text)
        self.assertIn("测试-全棉四件套 1.8m", page.text)
        self.assertIn("下载素材", page.text)
        self.assertIn("选品", page.text, "要有「选品时间」标签")
        self.assertIn("view=list", page.text, "要有列表视图的切换入口")

        list_page = self.client.get("/self/shop-goods?view=list")
        self.assertEqual(list_page.status_code, 200)
        self.assertIn("选品时间", list_page.text, "列表视图要有「选品时间」这一列")

        pool_id = _pool_rows()[0].id
        removed = self.client.post(f"/self/shop-goods/{pool_id}/remove")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(len(_pool_rows()), 0)

    def test_62_pool_card_layout(self):
        """卡片排版（2026-10-12 用户提「页面布局乱了」）：
        图片以前按 4:3 跟着卡片宽度撑（400px 卡片 = 300px 高的图），一屏看不到价与按钮；
        现在图片定高、卡片排得更密（宽屏一行 4 个）、主操作两个按钮 + 次操作单独一行。
        """
        _set_shop()
        self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        page = self.client.get("/self/shop-goods")
        html = page.text
        self.assertIn(".pool-img { width: 100%; height: 176px", html, "图片要定高")
        self.assertIn("col-12 col-sm-6 col-lg-4 col-xl-3", html, "宽屏一行 4 个卡片")
        self.assertIn('class="pool-foot2"', html, "详情 / 移出单独一行")
        self.assertIn("查看货源详情", html)
        self.assertIn("移出", html)
        # 价格行不许折行挤成两行
        self.assertIn(".pool-prices .p1", html)
        self.assertIn("white-space: nowrap", html)

    # ---------- 合并支付 · 拆单履约（2026-10-12） ----------
    def _two_lines_two_addresses(self) -> tuple[list, str]:
        """车里放两行（同一款货两个规格），各填一个收货地址 → 两组。"""
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 1})
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s2", "qty": 1})
        rows = _cart_rows()
        self.assertEqual(len(rows), 2)
        for row, (name, phone, addr) in zip(
            rows,
            [
                ("张三", "13800000001", "广东省 深圳市 南山区 科技园路 1 号"),
                ("李四", "13800000002", "上海市 闵行区 虹桥镇 万源路 2161 弄"),
            ],
        ):
            resp = self.client.post(
                "/self/cart/address",
                json={
                    "line_id": row.id,
                    "receiver_name": name,
                    "receiver_phone": phone,
                    "receiver_address": addr,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text[:300])
            self.assertEqual(resp.json()["groups"], 2, "两个不同地址 → 两组")
        return rows, ",".join(str(row.id) for row in rows)

    def test_63_cart_line_address_splits_checkout(self):
        """采购车里给每一款货各填一个收货地址 → 结算页按子单分块。"""
        _rows, line_ids = self._two_lines_two_addresses()

        page = self.client.get(f"/self/checkout?ids={line_ids}")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("拆成 2 张子单", html)
        self.assertIn("第 1 张子单", html)
        self.assertIn("第 2 张子单", html)
        for field in ("receiver_name__g1", "receiver_address__g1",
                      "receiver_name__g2", "receiver_address__g2"):
            self.assertIn(field, html, f"结算页缺少 {field}")

    def test_64_split_order_merge_pay_and_ship(self):
        """合并支付 · 拆单履约：母单收一次钱，子单各发各的、分别确认收货。"""
        rows, line_ids = self._two_lines_two_addresses()
        resp = self.client.post(
            "/self/checkout/submit",
            data={
                "source": "cart",
                "line_ids": line_ids,
                "receiver_name__g1": "张三",
                "receiver_phone__g1": "13800000001",
                "receiver_address__g1": "广东省 深圳市 南山区 科技园路 1 号",
                "receiver_name__g2": "李四",
                "receiver_phone__g2": "13800000002",
                "receiver_address__g2": "上海市 闵行区 虹桥镇 万源路 2161 弄",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        order_no = resp.headers["location"].rsplit("/", 1)[-1]

        parent = _order(order_no)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.order_kind, "main")
        self.assertEqual(parent.sub_count, 2)
        self.assertEqual(parent.status, "pending_pay")
        # 45.50 + 48.00 = 93.50（包邮）
        self.assertEqual(float(parent.pay_amount), 93.5)

        with SessionLocal() as s:
            subs = (
                s.query(StockOrder)
                .filter(StockOrder.parent_order_no == order_no)
                .order_by(StockOrder.sub_index)
                .all()
            )
            self.assertEqual(len(subs), 2)
            self.assertEqual(subs[0].receiver_name, "张三")
            self.assertEqual(subs[1].receiver_name, "李四")
            self.assertEqual([float(x.pay_amount) for x in subs], [45.5, 48.0])
            # 明细挂在子单上：各一行
            self.assertEqual(len(_order_items(subs[0].order_no)), 1)
            self.assertEqual(len(_order_items(subs[1].order_no)), 1)
            sub_nos = [x.order_no for x in subs]

        # 车里那两行已经下单，该清掉
        self.assertEqual(len(_cart_rows()), 0)

        # 母单付一次钱 → 两张子单都进「待确认」；运营确认收款后一起转「待发货」
        paid = self.client.post(f"/self/pay/{order_no}", data={"method": "balance"},
                                follow_redirects=False)
        self.assertEqual(paid.status_code, 303, paid.text[:200])
        with SessionLocal() as s:
            self.assertEqual(_order(order_no).status, "confirming")
            for no in sub_nos:
                self.assertEqual(_order(no).status, "confirming")
        self.client.post(f"/self/purchases/{order_no}/actions",
                         data={"action": "confirm_pay"}, follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(_order(order_no).status, "paid")
            for no in sub_nos:
                self.assertEqual(_order(no).status, "paid")

        # 母单不能直接发货（要在子单上发）
        blocked = self.client.post(
            f"/self/purchases/{order_no}/actions",
            data={"action": "ship", "company": "顺丰", "shipping_no": "SF1"},
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 303)
        self.assertEqual(_order(order_no).status, "paid", "母单发货要被拦下")

        # 子单 1：发货 → 确认收货；子单 2：只发货 → 母单还是「已发货」
        self.client.post(
            f"/self/purchases/{sub_nos[0]}/actions",
            data={"action": "ship", "company": "顺丰", "shipping_no": "SF1"},
            follow_redirects=False,
        )
        self.client.post(f"/self/purchases/{sub_nos[0]}/actions",
                         data={"action": "complete"}, follow_redirects=False)
        self.assertEqual(_order(sub_nos[0]).status, "completed")
        self.client.post(
            f"/self/purchases/{sub_nos[1]}/actions",
            data={"action": "ship", "company": "中通", "shipping_no": "ZT2"},
            follow_redirects=False,
        )
        self.assertEqual(_order(order_no).status, "shipped", "还有一张在路上 → 母单已发货")

        # 最后一张收完 → 母单完成
        self.client.post(f"/self/purchases/{sub_nos[1]}/actions",
                         data={"action": "complete"}, follow_redirects=False)
        self.assertEqual(_order(order_no).status, "completed")

        # 列表只显示母单（子单不单独占行），详情里能看到两张子单
        page = self.client.get("/self/purchases")
        self.assertIn("合并单 · 2 张子单", page.text)
        for no in sub_nos:
            self.assertNotIn(no, page.text)
        detail = self.client.get(f"/self/purchases/{order_no}")
        self.assertIn("子单（2 张", detail.text)
        self.assertIn(sub_nos[0], detail.text)
        self.assertIn("分批发货", detail.text)

    def test_66_same_address_no_split(self):
        """地址（与供应商）都一样 → 还是一张普通单，不拆。"""
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 1})
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s2", "qty": 1})
        rows = _cart_rows()
        for row in rows:
            self.client.post(
                "/self/cart/address",
                json={"line_id": row.id, "receiver_name": "王五",
                      "receiver_phone": "13800000003",
                      "receiver_address": "北京市 朝阳区 望京街 1 号"},
            )
        line_ids = ",".join(str(row.id) for row in rows)
        page = self.client.get(f"/self/checkout?ids={line_ids}")
        self.assertNotIn("第 1 张子单", page.text, "只有一个地址时不拆单")
        self.assertNotIn('name="receiver_name__g1"', page.text)

        resp = self.client.post(
            "/self/checkout/submit",
            data={"source": "cart", "line_ids": line_ids,
                  "receiver_name": "王五", "receiver_phone": "13800000003",
                  "receiver_address": "北京市 朝阳区 望京街 1 号"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        order_no = resp.headers["location"].rsplit("/", 1)[-1]
        order = _order(order_no)
        self.assertEqual(order.order_kind, "normal")
        self.assertEqual(order.sub_count, 0)
        self.assertEqual(order.receiver_name, "王五")
        self.assertEqual(len(_order_items(order_no)), 2, "两行货都在这一张单上")

    def test_65_materials_zip(self):
        _set_shop()
        self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        pool_id = _pool_rows()[0].id
        resp = self.client.get(f"/self/shop-goods/{pool_id}/materials.zip")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn("attachment", resp.headers["content-disposition"])

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            self.assertIn("文案.txt", names)
            self.assertIn("规格与价格.csv", names)
            self.assertIn("README.txt", names)
            copy_text = zf.read("文案.txt").decode("utf-8")
            self.assertIn("测试-全棉四件套 1.8m", copy_text)
            self.assertIn("【建议售价】¥99.00", copy_text)
            self.assertIn("档口下午 5 点前下单当天发", copy_text)
            specs = zf.read("规格与价格.csv").decode("utf-8")
            self.assertIn("灰 / 1.5m", specs)
            # 图床不可达：包里仍要有 README 说明缺哪几张（不能让整包失败）
            self.assertIn("没抓到", zf.read("README.txt").decode("utf-8"))

        # 下载会记次数与时间
        row = _pool_rows()[0]
        self.assertEqual(row.download_count, 1)
        self.assertIsNotNone(row.materials_at)

    # ---------- 详情页 ----------
    def test_70_detail_page_markers(self):
        _set_shop()
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        # 2026-10-12：补回「加采购车」—— 攒到车里，能给不同的人各带一件
        for marker in ("立即下单", "加采购车", "店铺铺货", "未入店铺池"):
            self.assertIn(marker, html, f"详情页缺少 {marker}")
        self.assertIn('id="btnCart"', html, "要有「加采购车」按钮")
        self.assertIn("pdpCartAdd", html, "按钮得真的调加购")
        self.assertNotIn("加采购单", html, "叫法统一成「加采购车」")
        self.assertNotIn("上架到店铺", html)

        pooled_resp = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        # 入池要真的成功（以前只断言页面标记，接口报错会被悄悄放过）
        self.assertEqual(pooled_resp.status_code, 200, pooled_resp.text[:400])
        after = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("已入店铺池", after)

    def test_72_pool_needs_approved_shop(self):
        """没店铺 / 店铺没过审：铺货接口拦下、按钮置灰、提示那句原话（2026-10-11 用户拍板）。"""
        msg = "未添加店铺或店铺未通过审核，请联系你的带教老师！"

        # ① 一家店铺都没有 —— 接口拦住
        resp = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertEqual(resp.json()["detail"], msg)
        self.assertEqual(len(_pool_rows()), 0, "拦住了就不该落池")
        html = self.client.get(f"/self/goods/detail/{ITEM_ID}").text
        self.assertIn("is-locked", html, "按钮要置灰")
        self.assertIn(msg, html, "提示文案要在页面上（title / JS 里）")

        # ② 店铺还在待审 —— 一样不行
        _set_shop(approved=False)
        resp = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertEqual(resp.json()["detail"], msg)

        # ③ 过审了就能铺
        _set_shop()
        ok = self.client.post(f"/self/goods/{ITEM_ID}/pool", json={})
        self.assertEqual(ok.status_code, 200, ok.text[:300])
        self.assertEqual(len(_pool_rows()), 1)

    def test_74_no_stock_blocks_order_and_pool(self):
        """库存为 0：结算条两个按钮都置灰、点了提示「该商品已经无库存，即将下架」（2026-10-11）。"""
        _set_shop()
        html = self.client.get(f"/self/goods/detail/{NO_STOCK_ID}").text
        self.assertIn("该商品已经无库存，即将下架", html)
        self.assertIn("btnNoStock", html)
        self.assertNotIn('id="btnBuyNow"', html, "没库存不给下单按钮")
        self.assertNotIn('id="btnPool"', html, "没库存也不给铺货按钮")

    # ---------- 结算页：件数跟着数量走 ----------
    def test_75_checkout_counters_follow_qty(self):
        """确认订单页改数量：顶部「共 N 款 M 件」与两处「商品总计 N 种 M 件」都跟着变。

        以前只有行小计和应付用 JS 重算，这几个件数是首次渲染的写死值 —— 数量改了纹丝不动
        （用户 2026-10-03 反馈）。
        """
        import re

        # 直接采购：进结算页就带数量
        html = self.client.get(f"/self/checkout?item_id={ITEM_ID}&sku_id=s1&qty=3").text
        for marker in ('id="ckQty">3<', 'id="ckShopQty">3<', 'id="pdQty">3<'):
            self.assertIn(marker, html, f"结算页件数初值不对：{marker}")
        # JS 重算时要一并改写这三个节点（否则改完数量还是老数字）
        self.assertIn("ckSet(['ckKinds', 'ckShopKinds', 'pdKinds']", html)
        self.assertIn("ckSet(['ckQty', 'ckShopQty', 'pdQty']", html, "件数要跟着改数量重算")
        for node in ("ckKinds", "ckShopKinds", "pdKinds", "ckQty", "ckShopQty", "pdQty",
                     "pdGoods", "ckShopGoods", "pdPay"):
            self.assertIn(node, html, f"ckRefresh 应更新 {node}")

        # 采购车结算：改数量写库后重开结算页，服务端算出来的件数也跟着变
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 2})
        cart_html = self.client.get("/self/cart").text
        m = re.search(r'id="qty-(\d+)"', cart_html)
        self.assertIsNotNone(m, "采购车里应有这一行")
        line_id = int(m.group(1))
        self.assertIn('id="ckQty">2<', self.client.get("/self/checkout").text)

        resp = self.client.post(
            "/self/cart/update", json={"line_id": line_id, "qty": 5}
        )
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        after = self.client.get("/self/checkout").text
        self.assertIn('id="ckQty">5<', after, "改数量后重开结算页，件数应变成 5")
        self.assertIn('id="pdQty">5<', after)

    # ---------- 手机端：商品明细不再挤标题 ----------
    def test_55_mobile_goods_rows(self):
        """手机端（≤768px）结算页与采购单明细的商品行改成「一货一卡」。

        口径（2026-10-04）：列宽一律写成 class（不能行内 style，否则手机端盖不住），
        单价 / 数量 / 小计三格带 data-label，手机端隐藏表头后用 ::before 补标签。
        """
        checkout = self.client.get(f"/self/checkout?item_id={ITEM_ID}&sku_id=s1&qty=1").text
        self.assertIn("@media (max-width: 768px)", checkout, "结算页要有手机端断点")
        for marker in ('class="ck-cell-main"', 'data-label="数量"', 'data-label="单价"',
                       'data-label="小计"', "class=\"ck-title\"", "ck-w-qty"):
            self.assertIn(marker, checkout, f"结算页缺少 {marker}")
        # 列宽若还是行内 style，手机端的 width:100% 就压不住，标题会被挤成一条缝
        self.assertNotIn('style="width: 160px;"', checkout)
        self.assertNotIn('style="width: 110px;"', checkout)

        order_no = self._new_order()
        detail = self.client.get(f"/self/purchases/{order_no}").text
        self.assertIn("@media (max-width: 768px)", detail, "采购单明细要有手机端断点")
        for marker in ('class="po-cell-main"', 'data-label="单价"', 'data-label="数量"',
                       'data-label="小计"', "class=\"po-title\"", "po-table"):
            self.assertIn(marker, detail, f"采购单明细缺少 {marker}")
        self.assertNotIn('style="width: 90px;"', detail)

        pay = self.client.get(f"/self/pay/{order_no}").text
        self.assertIn("@media (max-width: 768px)", pay, "收银台要有手机端断点")
        self.assertIn('class="d-flex gap-2 align-items-center pay-actions"', pay)

    # ---------- 工具 ----------
    def _new_order(self) -> str:
        resp = self.client.post(
            "/self/checkout/submit",
            data={"source": "buy_now", "item_id": ITEM_ID, "sku_id": "s1", "qty": 1,
                  "receiver_name": "戴广久"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        return resp.headers["location"].rsplit("/", 1)[-1]


if __name__ == "__main__":
    unittest.main()
