import unittest

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Order, OrderLog, ParseCorrection
from app.parser.engine import ParseResult
from app.services import cost_guard, learn, order_log
from app.services.learn import LEARNABLE_FIELDS
from app.timeutil import now


class CostGuardTest(unittest.TestCase):
    """成本护栏：自动流程写进来的成本「比售价还高」时不写、标待核对、留痕。

    背景：售价 55 的充电宝被截图 OCR 写成成本 133，毛利变负数且不留痕。
    人工在页面上自己填的不受这条限制。
    """

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Order).delete()
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, sale: float = 55.0, cost: float = 45.0) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                sale_amount=sale,
                deal_amount=sale,
                cost_amount=cost,
                product_name="测试商品",
                msg_time=now(),
                created_at=now(),
            )
            s.add(order)
            s.commit()
            return order.id

    def test_10_cost_higher_than_sale_is_blocked(self):
        order_id = self._order("CG-1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = cost_guard.guard_cost(s, order, 133.0, source="截图 OCR")
            self.assertFalse(ok)
            self.assertIn("比售价", reason)
            # 成本没被写进去，订单标上了「待核对」并留了痕
            self.assertEqual(order.cost_amount, 45.0)
            self.assertTrue(order.needs_review)
            self.assertIn("成本异常", order.review_reason or "")
            s.commit()
            logs = s.scalars(
                select(OrderLog).where(OrderLog.order_id == order_id)
            ).all()
            self.assertTrue(
                any(x.action == order_log.ACTION_COST_BLOCKED for x in logs),
                [x.action for x in logs],
            )

    def test_20_normal_cost_passes(self):
        order_id = self._order("CG-2")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = cost_guard.guard_cost(s, order, 40.0, source="解析")
            self.assertTrue(ok, reason)
            self.assertEqual(reason, "")
            self.assertFalse(order.needs_review)

    def test_30_zero_cost_is_blocked(self):
        order_id = self._order("CG-3")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = cost_guard.guard_cost(s, order, 0, source="解析")
            self.assertFalse(ok)
            self.assertIn("没取到值", reason)

    def test_40_scan_finds_cost_anomalies(self):
        self._order("CG-OK", sale=55.0, cost=45.0)
        self._order("CG-BAD", sale=55.0, cost=133.0)
        with SessionLocal() as s:
            rows = cost_guard.scan_cost_anomalies(s)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["order_no"], "CG-BAD")
            self.assertEqual(rows[0]["cost"], 133.0)
            self.assertIn("比售价", rows[0]["label"])


class CostLearnGuardTest(unittest.TestCase):
    """纠错样本护栏（2026-10-01）：脏样本不要学进来，也不要拿来改别的单。

    背景：某一单的成本被人工从 45 改成 133（误会或手滑），系统当成「人工结论」存成了纠错样本
    （指纹 = 「成本价：45」那一行）。此后凡是群里写着「成本价：45」的单，成本全被改成 133 ——
    118 / 122 / 124 / 130 连续中招。用户看到的「截图把成本认成 133」其实是这条样本干的：
    那几张截图上既没有「成本」字样，也没有 133。
    """

    def setUp(self) -> None:
        init_db()
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(ParseCorrection).delete()
            s.query(OrderLog).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, sale: float = 58.0, cost: float = 45.0) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                sale_amount=sale,
                deal_amount=sale,
                cost_amount=cost,
                product_name="测试商品",
                msg_time=now(),
                created_at=now(),
            )
            s.add(order)
            s.commit()
            return order.id

    def _sample(self, key: str, machine: str, human: str, line: str) -> None:
        with SessionLocal() as s:
            s.add(
                ParseCorrection(
                    field_key=key,
                    field_label=LEARNABLE_FIELDS.get(key, key),
                    machine_value=machine,
                    human_value=human,
                    source_line=line,
                    operator="admin",
                )
            )
            s.commit()

    def _apply(self, raw_text: str, **fields) -> ParseResult:
        result = ParseResult(fields=dict(fields))
        with SessionLocal() as s:
            learn.apply_known_corrections(s, result, raw_text)
        return result

    def test_50_dirty_cost_sample_is_ignored(self):
        """样本里的成本比售价还高（133 > 58）：当它不存在 —— 保留原文取到的 45，并写明原因。"""
        line = "成本价：45"
        self._sample("cost_amount", "45", "133", line)
        result = self._apply(
            f"商品：MagSafe 充电宝\n{line}\n成交价：58\n", cost_amount=45.0, sale_amount=58.0
        )
        self.assertEqual(result.fields["cost_amount"], 45.0, "脏样本不该改成本")
        self.assertTrue(result.warnings, "要写明这条样本为什么没被采用")

    def test_51_good_cost_sample_still_used(self):
        """样本合理（45 < 售价 58）就照旧采用：护栏不能误伤正常学习。"""
        line = "成本价：40"
        self._sample("cost_amount", "40", "45", line)
        result = self._apply(f"{line}\n成交价：58\n", cost_amount=40.0, sale_amount=58.0)
        self.assertEqual(result.fields["cost_amount"], 45.0)

    def test_60_bad_human_cost_is_not_learned(self):
        """人工把成本改成比售价还高：不存样本 —— 这种「纠正」本身有问题，学了会污染后面的单。"""
        order_id = self._order("CL-1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.cost_amount = 133.0
            saved = learn.record_corrections(
                s, order, before={"cost_amount": "45"}, operator="admin", commit=True
            )
        self.assertEqual(saved, [], "成本比售价高的改动不学习")

    def test_61_normal_human_cost_is_learned(self):
        """正常纠正（45 → 30）照旧存样本。"""
        order_id = self._order("CL-2")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.cost_amount = 30.0
            saved = learn.record_corrections(
                s, order, before={"cost_amount": "45"}, operator="admin", commit=True
            )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].human_value, "30")


if __name__ == "__main__":
    unittest.main()
