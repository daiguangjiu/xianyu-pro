"""资金板块：公司自营应收 / 公司自营应付。

业务口径（2026-09）：
  公司自营应收 = 销售金额（买家实付）− 平台技术服务费 0.6%（平台结算给公司时扣除）；
  公司自营应付 = 采购成本 + 运费，需结算给上游供应商；
  撤单 / 已受理的退单不再结算（流水作废），「已发货无法拦截」的退单未受理、照常结算；
  默认按明细（逐笔）展示，也可按天 / 按月聚合，并可单笔或整期标记已结算。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_REFUNDED,
    ORDER_STATE_SHIPPED,
    SETTLEMENT_INITIATOR,
    SETTLEMENT_PENDING,
    SETTLEMENT_REFUND,
    SETTLEMENT_SETTLED,
    SETTLEMENT_SUPPLIER,
    SETTLEMENT_VOID,
    Order,
    RawMessage,
    Settlement,
)
from app.services import finance as finance_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class FinanceRulesTest(unittest.TestCase):
    """流水生成 / 金额口径 / 作废与恢复（服务层）。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(
        self,
        order_no: str = "FIN-1",
        *,
        day: str = "2026-09-20",
        sale: float = 200.0,
        deal: float = 0.0,
        cost: float = 180.0,
        shipping: float = 0.0,
        platform_fee: float = 0.0,
        sender: str = "小A",
        supplier: str = "BH001 华强北",
        **kw,
    ) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="徕芬吹风机LF03",
                receiver_name="蓝曼波",
                sale_amount=sale,
                deal_amount=deal,
                cost_amount=cost,
                shipping_fee=shipping,
                platform_fee=platform_fee,
                sender_name=sender,
                supplier_name=supplier,
                ordered_at=datetime.strptime(day, "%Y-%m-%d"),
                msg_time=datetime.strptime(day, "%Y-%m-%d"),
                **kw,
            )
            s.add(order)
            s.flush()
            finance_service.sync_order(s, order)
            s.commit()
            return order.id

    def _rows(self, order_id: int) -> dict[str, Settlement]:
        with SessionLocal() as s:
            rows = s.query(Settlement).filter(Settlement.order_id == order_id).all()
            return {r.kind: r for r in rows}

    # ---------------------------------------------------------------- 生成
    def test_01_new_order_creates_two_flows(self):
        order_id = self._order("FIN-NEW", sale=200, cost=180, shipping=10)
        rows = self._rows(order_id)
        self.assertEqual(set(rows), {SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER})
        # 应收 = 销售金额 200 − 平台技术服务费 0.6%（¥1.20）= ¥198.80
        self.assertEqual(
            rows[SETTLEMENT_INITIATOR].amount, 198.8, "公司自营应收 = 销售金额 - 0.6% 技术服务费"
        )
        self.assertEqual(rows[SETTLEMENT_INITIATOR].direction, "in")
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].amount, 190.0, "供应商应付 = 成本 + 运费")
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].direction, "out")
        for row in rows.values():
            self.assertEqual(row.status, SETTLEMENT_PENDING)
            self.assertEqual(row.biz_day, "2026-09-20")
            self.assertEqual(row.biz_month, "2026-09")
        self.assertEqual(rows[SETTLEMENT_INITIATOR].counterparty, "小A")
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].counterparty, "BH001 华强北")

    def test_02_deal_amount_wins(self):
        """有成交价取成交价（销售金额），没成交价才退回售价；两种都按 0.6% 扣技术服务费。"""
        order_id = self._order("FIN-DEAL", sale=200, deal=188.5)
        # 188.5 − 188.5×0.6%（¥1.13）= ¥187.37
        self.assertEqual(self._rows(order_id)[SETTLEMENT_INITIATOR].amount, 187.37)

    def test_03_profit_math(self):
        with SessionLocal() as s:
            order = Order(sale_amount=200, deal_amount=0, cost_amount=180,
                          shipping_fee=10, platform_fee=6)
            amounts = finance_service.order_amounts(order)
        self.assertEqual(amounts["paid"], 200.0, "销售金额（买家实付）")
        # 平台技术服务费就是订单上的 platform_fee（一个东西，不再另算一份）
        self.assertEqual(amounts["tech_fee"], 6.0, "订单上记了金额就以它为准")
        self.assertEqual(amounts["platform_fee"], 6.0, "tech_fee 与 platform_fee 是同一个数")
        self.assertEqual(amounts["receivable"], 194.0, "应收 = 销售金额 - 技术服务费")
        self.assertEqual(amounts["supplier_payable"], 190.0)
        self.assertEqual(amounts["profit"], 4.0, "预计毛利 = 应收 - 应付（技术服务费不扣第二遍）")

    def test_03b_fee_falls_back_to_rate(self):
        """订单上没记金额（老数据）：按店铺费率算，默认 0.6%。"""
        with SessionLocal() as s:
            order = Order(sale_amount=200, deal_amount=0, cost_amount=180, shipping_fee=10)
            amounts = finance_service.order_amounts(order)
        self.assertEqual(amounts["tech_fee"], 1.2, "没记金额就按 0.6% 现算")
        self.assertEqual(amounts["profit"], 8.8, "200 - 1.2 - 190")
        # 费率可传：店铺设成 1% 就按 1% 算
        with SessionLocal() as s:
            order = Order(sale_amount=200, deal_amount=0, cost_amount=180, shipping_fee=10)
            amounts = finance_service.order_amounts(order, rate_percent=1.0)
        self.assertEqual(amounts["tech_fee"], 2.0, "店铺费率 1% 时按 1% 算")

    def test_03c_sync_service_fee(self):
        """落金额：成交价 > 0 的单按店铺费率补上；标了人工的不动。"""
        with SessionLocal() as s:
            order = Order(order_no="FEE-1", sale_amount=1000, deal_amount=0, xianyu_account="")
            s.add(order)
            s.flush()
            filled = finance_service.sync_service_fee(s, order)
            self.assertEqual(filled, 6.0, "1000 × 0.6% = 6")
            self.assertEqual(float(order.platform_fee or 0), 6.0)
            # 人工填过（platform_fee_manual）的不动
            order.platform_fee = 12.0
            order.platform_fee_manual = True
            self.assertEqual(finance_service.sync_service_fee(s, order), 0.0)
            self.assertEqual(float(order.platform_fee or 0), 12.0)

    def test_03d_service_fee_follows_price_change(self):
        """改售价后技术服务费跟着重算（2026-10-03）：自动算的值不再赖在老数上。"""
        with SessionLocal() as s:
            order = Order(order_no="FEE-2", sale_amount=1000, deal_amount=0, xianyu_account="")
            s.add(order)
            s.flush()
            finance_service.sync_service_fee(s, order)
            self.assertEqual(float(order.platform_fee or 0), 6.0)
            # 售价 1000 → 2000，手续费应跟着变 12（系统自动算的，不是人工值）
            order.sale_amount = 2000
            filled = finance_service.sync_service_fee(s, order)
            self.assertEqual(filled, 12.0, "2000 × 0.6% = 12")
            self.assertEqual(float(order.platform_fee or 0), 12.0)
            # 人工值不受影响（并把人工值还原出来）
            order.platform_fee = 30.0
            order.platform_fee_manual = True
            self.assertEqual(finance_service.sync_service_fee(s, order), 0.0)
            self.assertEqual(float(order.platform_fee or 0), 30.0)
            # force=True 连人工值也重算（页面清空那一栏时走这条路）
            self.assertEqual(finance_service.sync_service_fee(s, order, force=True), 12.0)

    def test_04_amount_edit_syncs_flow(self):
        order_id = self._order("FIN-EDIT", cost=100)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.cost_amount = 130
            finance_service.sync_order(s, order, commit=True)
        self.assertEqual(self._rows(order_id)[SETTLEMENT_SUPPLIER].amount, 130.0)

    # ---------------------------------------------------------------- 作废
    def test_05_recalled_order_voids_flows(self):
        order_id = self._order("FIN-RECALL")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = True
            finance_service.sync_order(s, order, commit=True)
        for row in self._rows(order_id).values():
            self.assertEqual(row.status, SETTLEMENT_VOID, "撤单不再结算")

    def test_06_refund_voids_flows(self):
        order_id = self._order("FIN-REFUND")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
        rows = self._rows(order_id)
        # 应收 / 应付：已受理的退单不再结算
        for kind in (SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER):
            self.assertEqual(rows[kind].status, SETTLEMENT_VOID, "已受理的退单不再结算")
        # 退款：退单才出现的那条（要退给买家的钱），挂在待结算等线下退完
        self.assertEqual(rows[SETTLEMENT_REFUND].status, SETTLEMENT_PENDING)
        self.assertEqual(rows[SETTLEMENT_REFUND].amount, 200.0, "按成交价退（没成交价退回售价）")

    def test_07_shipped_refund_voids_flows(self):
        """已发货的退单同样受理（同步物流拦截 / 收货退单）：订单不再结算。"""
        order_id = self._order(
            "FIN-SHIPPED", order_state=ORDER_STATE_SHIPPED, tracking_no="SF001"
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted, "已发货的退单也要受理")
        rows = self._rows(order_id)
        for kind in (SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER):
            self.assertEqual(rows[kind].status, SETTLEMENT_VOID)
        self.assertEqual(rows[SETTLEMENT_REFUND].status, SETTLEMENT_PENDING)

    def test_08_undo_recall_restores_flows(self):
        order_id = self._order("FIN-UNDO")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = True
            finance_service.sync_order(s, order, commit=True)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = False
            finance_service.sync_order(s, order, commit=True)
        for row in self._rows(order_id).values():
            self.assertEqual(row.status, SETTLEMENT_PENDING, "撤销撤回后重新参与结算")

    # ---------------------------------------------------------------- 汇总
    def test_09_summary_and_totals(self):
        a = self._order("FIN-A", day="2026-09-01", sale=100, cost=60)
        b = self._order("FIN-B", day="2026-09-02", sale=300, cost=200, shipping=20)
        with SessionLocal() as s:
            stat = finance_service.summary(s)
            # 应收：100×0.994=99.4 + 300×0.994=298.2 = 397.6
            self.assertEqual(stat["pending_in"], 397.6)
            self.assertEqual(stat["pending_out"], 280.0)
            self.assertEqual(stat["pending_net"], 117.6)
            self.assertEqual(stat["pending_count_in"], 2)
            # 按天聚合：两天各一笔应收一笔应付
            days = finance_service.aggregate_rows(
                s, finance_service.filters(kind=SETTLEMENT_SUPPLIER), "day"
            )
            self.assertEqual([d["period"] for d in days], ["2026-09-02", "2026-09-01"])
            self.assertEqual(days[0]["count"], 1)
            self.assertEqual(days[0]["amount"], 220.0)
            # 按月聚合
            months = finance_service.aggregate_rows(s, finance_service.filters(), "month")
            self.assertEqual(len(months), 1)
            self.assertEqual(months[0]["period"], "2026-09")
            self.assertEqual(months[0]["count"], 4)
            self.assertEqual(months[0]["amount"], 677.6, "397.6 应收 + 280 应付")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    # ---------------------------------------------------------------- 结算
    def test_10_settle_by_period_and_undo(self):
        # 日期取「当前月」：month_settled_* 是按本月统计的，写死月份会在跨月后失败
        day = f"{now().strftime('%Y-%m')}-03"
        order_id = self._order("FIN-SETTLE", day=day, sale=100, cost=60)
        with SessionLocal() as s:
            count = finance_service.settle(s, day=day, actor="admin")
            self.assertEqual(count, 2, "整期结算：应收 + 应付一笔")
            stat = finance_service.summary(s)
            self.assertEqual(stat["pending_in"], 0.0)
            # 应收 100 − 0.6% 技术服务费 = 99.4
            self.assertEqual(stat["settled_in"], 99.4)
            self.assertEqual(stat["month_settled_in"], 99.4)
            # 「应收已收 / 应付已付」的笔数跟着结算走
            self.assertEqual(stat["settled_count_in"], 1)
            self.assertEqual(stat["settled_count_out"], 1)
            self.assertEqual(stat["settled_out"], 60.0)
            rows = s.query(Settlement).filter(Settlement.order_id == order_id).all()
            self.assertTrue(all(r.status == SETTLEMENT_SETTLED for r in rows))
            self.assertTrue(all(r.settled_by == "admin" for r in rows))
            # 撤销结算
            undone = finance_service.unsettle(s, [r.id for r in rows])
            self.assertEqual(undone, 2)
            self.assertIn(
                SETTLEMENT_PENDING,
                [r.status for r in s.query(Settlement).filter(Settlement.order_id == order_id)],
            )

    def test_11_settle_requires_scope(self):
        self._order("FIN-NO-SCOPE")
        with SessionLocal() as s:
            with self.assertRaises(ValueError):
                finance_service.settle(s)

    def test_12_void_rows_not_counted(self):
        order_id = self._order("FIN-VOID", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = True
            finance_service.sync_order(s, order, commit=True)
        with SessionLocal() as s:
            self.assertEqual(finance_service.totals(s, finance_service.filters())["count"], 0)
            stat = finance_service.summary(s)
            self.assertEqual(stat["pending_in"], 0.0)
            self.assertEqual(stat["void_count"], 2)
            self.assertEqual(finance_service.count_rows(s, finance_service.filters()), 0)

    # ---------------------------------------------------------------- 已转账
    def test_13_transfer_settles_supplier_flow(self):
        """「已转账」= 钱已经付给供应商：这笔的应付流水自动核销为已结算。"""
        order_id = self._order("FIN-PAID", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "paid"
            changed = finance_service.sync_transfer(s, order, commit=True)
        self.assertEqual(changed, 1)
        rows = self._rows(order_id)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].status, SETTLEMENT_SETTLED)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].settled_by, "转账自动结算")
        self.assertIsNotNone(rows[SETTLEMENT_SUPPLIER].settled_at)
        # 应收不受影响：钱是付出去的，跟收进来的是两回事
        self.assertEqual(rows[SETTLEMENT_INITIATOR].status, SETTLEMENT_PENDING)
        with SessionLocal() as s:
            stat = finance_service.summary(s)
            self.assertEqual(stat["pending_out"], 0.0, "付掉的不再算待结算")
            self.assertEqual(stat["settled_out"], 60.0)

    def test_14_untransfer_reverts_auto_settled(self):
        """改回「未转账」：当初自动核销的那条退回待结算。"""
        order_id = self._order("FIN-UNPAID", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "paid"
            finance_service.sync_transfer(s, order, commit=True)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "unpaid"
            finance_service.sync_transfer(s, order, commit=True)
        rows = self._rows(order_id)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].status, SETTLEMENT_PENDING)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].settled_by, "")

    def test_15_manual_settled_not_reverted_by_untransfer(self):
        """人工在资金页核销的流水，不会因为改回未转账被撤销。"""
        order_id = self._order("FIN-MANUAL", sale=100, cost=60)
        with SessionLocal() as s:
            supplier_id = s.scalar(
                select(Settlement.id).where(
                    Settlement.order_id == order_id, Settlement.kind == SETTLEMENT_SUPPLIER
                )
            )
            finance_service.settle(s, ids=[supplier_id], actor="admin")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "unpaid"
            changed = finance_service.sync_transfer(s, order, commit=True)
        self.assertEqual(changed, 0, "人工核销的不动")
        rows = self._rows(order_id)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].status, SETTLEMENT_SETTLED)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].settled_by, "admin")

    def test_16_transfer_ignores_void_flows(self):
        """撤单 / 退单作废的流水：即便标了已转账也不参与结算。"""
        order_id = self._order("FIN-PAID-VOID", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = True
            finance_service.sync_order(s, order, commit=True)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "paid"
            changed = finance_service.sync_transfer(s, order, commit=True)
        self.assertEqual(changed, 0)
        self.assertEqual(self._rows(order_id)[SETTLEMENT_SUPPLIER].status, SETTLEMENT_VOID)

    def test_17_sync_order_applies_transfer_rule(self):
        """订单编辑保存走 sync_order：转账状态一并同步到应付流水。"""
        order_id = self._order("FIN-SYNC", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.transfer_status = "paid"
            finance_service.sync_order(s, order, commit=True)
        self.assertEqual(self._rows(order_id)[SETTLEMENT_SUPPLIER].status, SETTLEMENT_SETTLED)

    def test_18b_transfer_settled_at_is_transfer_time(self):
        """结算时间 = 订单标记「已转账」的时刻（不是跑同步的时间）。"""
        order_id = self._order("FIN-TS", sale=100, cost=60)
        marked_at = datetime(2026, 9, 25, 14, 32, 19)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            push_service.apply_transfer(order, "paid")
            order.transfer_paid_at = marked_at  # 模拟「标记已转账」发生在那个时刻
            finance_service.sync_transfer(s, order, commit=True)
        rows = self._rows(order_id)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].settled_at, marked_at)
        self.assertEqual(rows[SETTLEMENT_SUPPLIER].status, SETTLEMENT_SETTLED)

    def test_18c_totals_split_by_direction(self):
        """合计按方向拆：应收（+）与应付（-）不能混在一起加。"""
        self._order("FIN-T-1", sale=200, cost=100, shipping=10)
        paid_id = self._order("FIN-T-2", sale=100, cost=50)
        with SessionLocal() as s:
            order = s.get(Order, paid_id)
            push_service.apply_transfer(order, "paid")
            finance_service.sync_transfer(s, order, commit=True)
            data = finance_service.totals(s, finance_service.filters())
        # 应收：200 − 1.20 = 198.80；100 − 0.60 = 99.40
        self.assertEqual(data["amount_in"], 298.2)
        self.assertEqual(data["pending_in"], 298.2)
        self.assertEqual(data["settled_in"], 0.0)
        # 应付：110 + 50 = 160，其中已付 50
        self.assertEqual(data["amount_out"], 160.0)
        self.assertEqual(data["pending_out"], 110.0)
        self.assertEqual(data["settled_out"], 50.0)
        self.assertEqual(data["count_in"], 2)
        self.assertEqual(data["count_out"], 2)
        self.assertEqual(data["net"], round(298.2 - 160.0, 2))
        self.assertEqual(data["count"], 4)

    def test_18_set_transfer_service_settles_flow(self):
        """推单页 / 详情页切「已转账」（push.set_transfer）也要同步核销。"""
        order_id = self._order("FIN-SETT", sale=100, cost=60)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            push_service.set_transfer(s, order, "paid")
        self.assertEqual(self._rows(order_id)[SETTLEMENT_SUPPLIER].status, SETTLEMENT_SETTLED)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            push_service.set_transfer(s, order, "unpaid")
        self.assertEqual(self._rows(order_id)[SETTLEMENT_SUPPLIER].status, SETTLEMENT_PENDING)


class FinancePageTest(unittest.TestCase):
    """资金页面：渲染、筛选、结算操作。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                receiver_name="贲",
                sale_amount=kw.pop("sale", 300.0),
                deal_amount=0.0,
                cost_amount=kw.pop("cost", 200.0),
                shipping_fee=kw.pop("shipping", 15.0),
                sender_name=kw.pop("sender", "小A"),
                supplier_name=kw.pop("supplier", "华强北数码"),
                ordered_at=datetime.strptime(kw.pop("day", "2026-09-20"), "%Y-%m-%d"),
                msg_time=datetime.strptime("2026-09-20", "%Y-%m-%d"),
                **kw,
            )
            s.add(order)
            s.flush()
            finance_service.sync_order(s, order)
            s.commit()
            return order.id

    def test_20_page_lists_flows(self):
        self._order("PAGE-1", day="2026-09-21")
        page = self.client.get("/finance?granularity=order")
        self.assertEqual(page.status_code, 200)
        self.assertIn("资金结算", page.text)
        self.assertIn("公司自营应收", page.text)
        self.assertIn("公司自营应付", page.text)
        self.assertIn("PAGE-1", page.text)
        self.assertIn("298.20", page.text, "公司自营应收 = 销售金额 - 0.6% 技术服务费")
        self.assertIn("1.80", page.text, "平台技术服务费 = 300 × 0.6%")
        self.assertIn("215.00", page.text, "公司自营应付 = 成本 + 运费")
        self.assertIn("待结算", page.text)

    def test_19c_kpi_cards_order_and_new_card(self):
        """顶部 5 个数字：应收待结算 → 应收已收 → 应付待结算 → 应付已付 → 待结算净额。"""
        order_id = self._order("KPI-1", day="2026-09-20", sale=100, cost=60)
        with SessionLocal() as s:
            supplier_id = s.scalar(
                select(Settlement.id).where(
                    Settlement.order_id == order_id, Settlement.kind == SETTLEMENT_SUPPLIER
                )
            )
            finance_service.settle(s, ids=[supplier_id], actor="admin")
        page = self.client.get("/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="grid grid-5"', page.text, "5 个数字一排放")
        labels = [
            "待结算 · 公司自营应收",
            "公司自营应收已收",
            "待结算 · 公司自营应付",
            "公司自营应付已付",
            "公司自营净额",
        ]
        positions = [page.text.find(label) for label in labels]
        self.assertNotIn(-1, positions, f"5 个卡片标题都要在：{labels}")
        self.assertEqual(positions, sorted(positions), "卡片顺序：应收 → 应收已收 → 应付 → 应付已付 → 净额")
        self.assertNotIn("本月已结算", page.text, "原来的「本月已结算」已被「公司自营应付已付」取代")

    def test_19e_net_card_is_received_minus_paid(self):
        """「公司自营净额」= 公司自营应收已收 − 公司自营应付已付（不是待收 − 待付）。"""
        order_id = self._order("KPI-3", day="2026-09-18", sale=100, cost=60)
        with SessionLocal() as s:
            # 只结算应付（付给供应商 60），应收还没收回来 -> 净额为负
            supplier_id = s.scalar(
                select(Settlement.id).where(
                    Settlement.order_id == order_id, Settlement.kind == SETTLEMENT_SUPPLIER
                )
            )
            finance_service.settle(s, ids=[supplier_id], actor="admin")
            stat = finance_service.summary(s)
            self.assertEqual(stat["settled_in"], 0.0)
            self.assertEqual(stat["settled_out"], 75.0, "应付 = 成本 60 + 运费 15")
            self.assertEqual(stat["settled_net"], -75.0, "已收 − 已付")
        page = self.client.get("/finance")
        self.assertIn("公司自营净额", page.text)
        self.assertIn("-¥75.00", page.text, "负数显示成 -¥75.00，不是 ¥-75.00")
        self.assertNotIn("¥-75.00", page.text)

    def test_19d_settled_in_card_counts_received(self):
        """「公司自营应收已收」= 已经收到的钱：按已结算的应收流水统计。"""
        order_id = self._order("KPI-2", day="2026-09-19", sale=100, cost=60)
        with SessionLocal() as s:
            initiator_id = s.scalar(
                select(Settlement.id).where(
                    Settlement.order_id == order_id, Settlement.kind == SETTLEMENT_INITIATOR
                )
            )
            finance_service.settle(s, ids=[initiator_id], actor="admin")
            stat = finance_service.summary(s)
        self.assertEqual(stat["settled_in"], 99.4, "收 100 − 0.6% 技术服务费")
        self.assertEqual(stat["settled_count_in"], 1)
        page = self.client.get("/finance")
        self.assertIn("公司自营应收已收", page.text)
        self.assertIn("99.40", page.text)

    def test_20b_page_defaults_to_detail_view(self):
        """默认进来就是按明细（逐笔）展示，不用先切一次聚合方式。"""
        self._order("PAGE-D1", day="2026-09-21")
        page = self.client.get("/finance")
        self.assertEqual(page.status_code, 200)
        self.assertIn("按笔明细", page.text)
        self.assertIn("平台技术服务费", page.text)
        # 明细视图才有「结算选中」按钮；聚合视图没有
        self.assertIn("结算选中", page.text)

    def test_20c_detail_row_shows_tech_fee(self):
        """明细里技术服务费只出现在应收那一行，应付行不计。"""
        order_id = self._order("PAGE-D2", day="2026-09-21", sale=1000, cost=700)
        with SessionLocal() as s:
            detail = finance_service.order_rows(s, finance_service.filters(), limit=10)
        by_kind = {r["kind"]: r for r in detail if r["order_id"] == order_id}
        self.assertEqual(by_kind[SETTLEMENT_INITIATOR]["tech_fee"], 6.0, "1000 × 0.6%")
        self.assertEqual(by_kind[SETTLEMENT_INITIATOR]["amount"], 994.0)
        self.assertEqual(by_kind[SETTLEMENT_SUPPLIER]["tech_fee"], 0.0, "应付行不涉及技术服务费")

    def test_20d_detail_row_shows_order_created_at(self):
        """按笔明细最后多一列「订单创建时间」（查订单问题用）。"""
        order_id = self._order("PAGE-CREATED", day="2026-09-20")
        page = self.client.get("/finance?granularity=order")
        self.assertIn("订单创建时间", page.text)
        with SessionLocal() as s:
            created = s.get(Order, order_id).created_at
        from app.utils import human_time

        self.assertIn(human_time(created), page.text)

    def test_20e_totals_chips_split_in_and_out(self):
        """合计按应收 / 应付分开统计：不再把两个方向的钱加成一个「待结算」。"""
        self._order("PAGE-INOUT", sale=300, cost=200, shipping=15)
        page = self.client.get("/finance?granularity=order")
        self.assertIn("应收 +¥", page.text)
        self.assertIn("应付 -¥", page.text)
        self.assertIn("净额", page.text)
        # 旧的「合计 ¥…」是把应收与应付加在一起的混合口径，不应再出现
        self.assertNotIn("合计 ¥", page.text)

    def test_20f_totals_chips_include_tech_fee(self):
        """合计在表格上方（表头位置，横向滑动也一直在视野里），并统计「平台技术服务费」。"""
        self._order("PAGE-FOOT", sale=300, cost=200, shipping=15)
        html = self.client.get("/finance?granularity=order").text
        head = html[: html.index("<tbody>")]
        self.assertIn("当前筛选", head)
        self.assertIn('<span class="n">2</span>', head, "一单两条流水")
        self.assertIn("应收 +¥298.20", head, "300 − 300×0.6% = 298.20")
        self.assertIn("应付 -¥215.00", head, "成本 200 + 运费 15")
        self.assertIn("技术服务费 ¥1.80", head, "平台技术服务费 = 300 × 0.6%")
        self.assertIn("待结算 ¥", head, "待结算 / 已结算仍跟着合计走")
        self.assertNotIn("<tfoot>", html, "合计不放在表格底部")

    def test_20g_header_totals_follow_filters(self):
        """合计按当前筛选结果算：筛掉的单不进合计。"""
        self._order("PAGE-F1", day="2026-09-21", sale=300, cost=200, shipping=15)
        self._order("PAGE-F2", day="2026-09-22", sale=100, cost=50)
        html = self.client.get(
            "/finance?granularity=order&day_from=2026-09-22&day_to=2026-09-22"
        ).text
        head = html[: html.index("<tbody>")]
        self.assertIn('<span class="n">2</span>', head, "一单两条流水")
        self.assertIn("应收 +¥99.40", head, "100 − 100×0.6%")
        self.assertNotIn("298.20", head)

    def test_20h_aggregate_view_has_header_totals(self):
        """按天 / 按月聚合视图同样在表格上方给合计。"""
        self._order("PAGE-AGG", day="2026-09-21")
        html = self.client.get("/finance?granularity=day").text
        head = html[: html.index("<tbody>")]
        self.assertIn("当前筛选", head)
        self.assertIn("应收 +¥", head)
        self.assertIn("应付 -¥", head)
        self.assertIn("技术服务费 ¥", head)

    def test_21_page_aggregates_by_day_and_month(self):
        self._order("PAGE-2", day="2026-09-21")
        self._order("PAGE-3", day="2026-08-31")
        day_page = self.client.get("/finance?granularity=day")
        self.assertIn("2026-09-21", day_page.text)
        self.assertIn("2026-08-31", day_page.text)
        month_page = self.client.get("/finance?granularity=month")
        self.assertIn("2026-09", month_page.text)
        self.assertIn("2026-08", month_page.text)
        self.assertNotIn("2026-09-21", month_page.text, "按月视图只显示月份")

    def test_22_page_filter_by_kind_and_day(self):
        self._order("PAGE-4", day="2026-09-21", sender="小A")
        self._order("PAGE-5", day="2026-09-22", sender="小B")
        page = self.client.get("/finance?granularity=order&kind=supplier&day_from=2026-09-22&day_to=2026-09-22")
        self.assertIn("PAGE-5", page.text)
        self.assertNotIn("PAGE-4", page.text)
        # 只看供应商结算：发起人流水（结算对象是订单发送人）不会出现
        self.assertNotIn("小A", page.text, "只看供应商结算时不应出现发起人流水")
        self.assertIn("华强北数码", page.text)

    def test_23_settle_single_and_undo_via_page(self):
        order_id = self._order("PAGE-6")
        with SessionLocal() as s:
            rows = s.query(Settlement).filter(Settlement.order_id == order_id).all()
            target = rows[0].id
        resp = self.client.post(f"/finance/settle", data={"ids": str(target)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Settlement, target).status, SETTLEMENT_SETTLED)
        resp = self.client.post("/finance/unsettle", data={"ids": str(target)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Settlement, target).status, SETTLEMENT_PENDING)

    def test_24_settle_whole_day_via_page(self):
        self._order("PAGE-7", day="2026-09-23")
        resp = self.client.post(
            "/finance/settle", data={"day": "2026-09-23"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已结算", resp.text)
        page = self.client.get("/finance?granularity=day&status=pending")
        self.assertNotIn("2026-09-23", page.text, "整期结算后不再有待结算")

    def test_25_settle_without_scope_is_rejected(self):
        self._order("PAGE-8")
        resp = self.client.post("/finance/settle", data={}, follow_redirects=True)
        self.assertIn("请先选中要结算的流水", resp.text)

    def test_26_recalled_order_flow_is_void_on_page(self):
        order_id = self._order("PAGE-9")
        self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        page = self.client.get("/finance?granularity=order&status=void")
        self.assertIn("PAGE-9", page.text)
        self.assertIn("已作废", page.text)
        default_page = self.client.get("/finance?granularity=order")
        self.assertNotIn("PAGE-9", default_page.text, "作废流水默认不进统计")

    def test_27_nav_has_finance_entry(self):
        page = self.client.get("/")
        self.assertIn('href="/finance"', page.text, "侧边导航要有「资金结算」入口")
        self.assertIn("资金结算", page.text)
        self.assertIn("待结算", page.text)
        # 进到资金页时，顶部标题跟着走（不能还显示「数据总览」）
        ledger = self.client.get("/finance")
        self.assertEqual(ledger.status_code, 200)
        self.assertIn("资金结算", ledger.text)

    def test_28_html_is_not_cached(self):
        """页面 HTML 不缓存：升级后刷新就能看到新菜单，不会一直显示旧的。"""
        page = self.client.get("/")
        self.assertEqual(page.headers.get("cache-control"), "no-store, must-revalidate")
        login = self.client.get("/login")
        self.assertIn("no-store", login.headers.get("cache-control", ""))

    def test_29_date_input_uses_shared_style(self):
        """日期控件要和其它输入框同一套外观（之前通用样式漏了 input[type=date]）。"""
        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
        shared = css.split("input[type=text],", 1)[1].split("{", 1)[0]
        self.assertIn("input[type=date]", shared, "通用输入框样式要带上日期控件")
        self.assertIn("input[type=date]", css.split("@media (max-width: 560px)", 1)[1])
        self.assertIn("appearance: auto", css, "日期控件要保留浏览器自带的日历按钮")
        page = self.client.get("/finance")
        self.assertIn('type="date"', page.text)
        self.assertIn("平台技术服务费", page.text)


class RefundFlowTest(unittest.TestCase):
    """退款流水（2026-10-01 加）：退单受理后要退给买家的钱单独记一类，净额扣退款。

    为什么单独一类：退款方向与「应付给供应商」一样是流出，但对象是买家 ——
    混在应付里就对不出「这笔钱到底给谁了」。
    """

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str = "REF-1", *, sale: float = 200.0, deal: float = 0.0,
               cost: float = 180.0, **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="徕芬吹风机LF03",
                receiver_name="蓝曼波",
                sale_amount=sale,
                deal_amount=deal,
                cost_amount=cost,
                sender_name="小A",
                supplier_name="BH001 华强北",
                ordered_at=datetime(2026, 9, 20),
                msg_time=datetime(2026, 9, 20),
                **kw,
            )
            s.add(order)
            s.flush()
            finance_service.sync_order(s, order)
            s.commit()
            return order.id

    def _rows(self, order_id: int) -> dict[str, Settlement]:
        with SessionLocal() as s:
            return {
                r.kind: r
                for r in s.query(Settlement).filter(Settlement.order_id == order_id).all()
            }

    def test_20_refund_flow_created_on_refund(self):
        """退单受理：应收应付作废，另生成一条退款流水（对象 = 买家、方向 out、待结算）。"""
        order_id = self._order("REF-1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
        rows = self._rows(order_id)
        self.assertEqual(
            set(rows), {SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER, SETTLEMENT_REFUND}
        )
        refund = rows[SETTLEMENT_REFUND]
        self.assertEqual(refund.amount, 200.0, "按成交价退（没成交价退回售价）")
        self.assertEqual(refund.direction, "out")
        self.assertEqual(refund.counterparty, "蓝曼波", "退款对象是买家，不是供应商")
        self.assertEqual(refund.status, SETTLEMENT_PENDING, "钱还没退出去：待结算")

    def test_21_refund_settles_when_refunded(self):
        """订单落到「已退款」：退款流水自动核销，结算人标「退款自动核销」。"""
        order_id = self._order("REF-2")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.refund_status = "refunded"
            order.refund_paid_at = datetime(2026, 9, 25)
            finance_service.sync_order(s, order, commit=True)
        refund = self._rows(order_id)[SETTLEMENT_REFUND]
        self.assertEqual(refund.status, SETTLEMENT_SETTLED)
        self.assertEqual(refund.settled_by, "退款自动核销")
        self.assertEqual(refund.settled_at, datetime(2026, 9, 25), "结算时间取实际退款时间")

    def test_22_recall_voids_refund_flow(self):
        """撤单（群里撤回、订单只留档）：退款流水作废 —— 没退钱就不该有这条账。"""
        order_id = self._order("REF-3")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_recalled = True
            finance_service.sync_order(s, order, commit=True)
        self.assertEqual(
            self._rows(order_id)[SETTLEMENT_REFUND].status, SETTLEMENT_VOID
        )

    def test_23_net_minus_refund(self):
        """净额 = 应收已收 − 应付已付 − 退款已退：退款是真金白银出去的。"""
        order_id = self._order("REF-4", sale=100.0, deal=100.0, cost=60.0)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
            order.refund_status = "refunded"
            finance_service.sync_order(s, order, commit=True)
            stat = finance_service.summary(s)
        self.assertEqual(stat["settled_refund"], 100.0)
        self.assertEqual(stat["settled_in"], 0.0, "退单后应收流水作废，不再算已收")
        self.assertEqual(stat["settled_net"], -100.0, "已收 0 − 已付 0 − 已退 100")

    def test_24_finance_page_shows_refund(self):
        """资金页看得见退款这一类：类型标签、合计、卡片都能筛出来。"""
        order_id = self._order("REF-5", sale=100.0, deal=100.0, cost=60.0)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, None, "此单退")
        page = self.client.get("/finance?kind=refund")
        self.assertIn("退款 · 退买家", page.text)
        self.assertIn("退款 -¥100.00", page.text, "退款按流出显示，合计与明细都要有")
        # 三处：筛选下拉的可选项、总览卡片、明细里的类型标签
        self.assertEqual(page.text.count("退款 · 退买家"), 3)


class SettlementRefundColumnTest(RefundFlowTest):
    """资金结算里的退款金额（2026-10-01）：明细单列一列，聚合按类型分开并参与净额。

    以前退款只在流水里另起一行：明细行上看不到这单退了多少，对账得去别处翻；
    按天 / 按月聚合更是把应收 / 应付 / 退款三类金额直接相加 —— 要付出去的钱、
    退给买家的钱都被算成了收入。
    """

    def test_order_rows_carry_refund_amount(self):
        order_id = self._order("REF-3", cost=120.0)
        with SessionLocal() as s:
            refund_service.mark_refund(s, s.get(Order, order_id), None, "此单退")
        with SessionLocal() as s:
            rows = [r for r in finance_service.order_rows(s, []) if r["order_id"] == order_id]
        self.assertTrue(rows, "这一单要有流水")
        for row in rows:
            self.assertAlmostEqual(row["refund_amount"], 200.0, places=2,
                                   msg="明细每一行都要带这单的退款金额（退款流水就是按它记的）")
        page = self.client.get("/finance")
        self.assertIn("退款金额", page.text, "明细表要有「退款金额」这一列")

    def test_aggregate_rows_split_in_out_refund(self):
        order_id = self._order("REF-4", cost=120.0)
        with SessionLocal() as s:
            refund_service.mark_refund(s, s.get(Order, order_id), None, "此单退")
        with SessionLocal() as s:
            rows = finance_service.aggregate_rows(s, [], "day")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["amount_out"], 120.0, places=2, msg="应付 = 采购成本")
        self.assertAlmostEqual(row["amount_refund"], 200.0, places=2,
                               msg="退款单独汇总，不混进「应付给供应商」")
        self.assertAlmostEqual(
            row["net"], row["amount_in"] - row["amount_out"] - row["amount_refund"], places=2
        )
        self.assertLess(row["net"], row["amount_in"] - row["amount_out"], "净额不扣退款会虚高")
        page = self.client.get("/finance?granularity=day")
        self.assertIn("退款", page.text, "按天聚合要能看到退款这一列")


if __name__ == "__main__":
    unittest.main()
