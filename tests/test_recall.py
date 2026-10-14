"""消息撤回（2026-09）：撤回的群消息 -> 对应订单标记「撤回」 -> 只留档。

业务口径：
  1 群里撤回一条订单模板消息或订单截图，对应订单打上「撤回」标记；
  2 撤回订单只留档：不参与任何数据统计（看板 / 供应商 / 日报），也不参与后续流程
    （待推单发货、未推送批次、缺图提醒）；
  3 页面一眼可辨：置灰 + 文字删除线 + 「撤回订单」徽标 / 水印；
  4 误撤回可撤销：撤销后订单重新参与统计与流程。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.feishu import events as events_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, PushBatch, PushBatchItem, RawMessage  # noqa: E402
from app.parser.engine import parse_text  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import recall as recall_service  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

ORDER_TEXT = (
    "产品： 徕芬吹风机LF03 深灰色\n"
    "收货人： 蓝曼波\n"
    "手机号码： 15872093763\n"
    "地址信息： 浙江省 杭州市 西湖区 三墩镇 五幸家园南区2幢2001\n"
    "售价：200\n"
    "成本价：180\n"
    "所属供应链：BH001\n"
)
TEMPLATE_ID = "om_recall_tpl_1"
IMAGE_ID = "om_recall_img_1"


class RecallTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _make_order(self, order_no: str = "RC-1") -> int:
        """建一笔「模板消息 + 订单截图」都齐的订单，返回订单 id。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="吹风机",
                sale_amount=199.0,
                cost_amount=120.0,
                supplier_status="pending",
                msg_time=now(),
                ordered_at=now(),
            )
            s.add(order)
            s.flush()
            tpl = RawMessage(
                message_id=TEMPLATE_ID,
                chat_id="oc_recall",
                msg_type="text",
                kind="template",
                parse_status="parsed",
                raw_text="产品： 吹风机",
                msg_time=now(),
                order_id=order.id,
            )
            img = RawMessage(
                message_id=IMAGE_ID,
                chat_id="oc_recall",
                msg_type="image",
                kind="screenshot",
                parse_status="parsed",
                parent_message_id=TEMPLATE_ID,
                msg_time=now(),
            )
            s.add_all([tpl, img])
            order.source_message_id = TEMPLATE_ID
            s.add(
                OrderImage(
                    order_id=order.id,
                    message_id=IMAGE_ID,
                    file_key="img_key_1",
                    filename="shot.png",
                )
            )
            s.commit()
            return order.id

    def _order(self, order_id: int) -> Order:
        with SessionLocal() as s:
            return s.get(Order, order_id)

    def _recall(self, message_id: str) -> dict:
        with SessionLocal() as s:
            return recall_service.handle_message_recalled(s, message_id, chat_id="oc_recall")

    # ---------------------------------------------------------------- 撤回识别
    def test_template_message_recalled_marks_order(self):
        """撤回模板消息 -> 订单标记撤回，消息自己也记上撤回。"""
        order_id = self._make_order()
        result = self._recall(TEMPLATE_ID)
        self.assertEqual(result["orders"], [order_id])
        self.assertEqual(result["marked"], 1)

        order = self._order(order_id)
        self.assertTrue(order.is_recalled, "撤回模板消息后订单要标记撤回")
        self.assertEqual(order.recalled_message_id, TEMPLATE_ID)
        self.assertIsNotNone(order.recalled_at)
        self.assertEqual(order.recalled_label, "撤回订单")
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == TEMPLATE_ID))
            self.assertTrue(msg.is_recalled, "被撤回的消息也要打标记")

    def test_shipped_order_not_recalled(self):
        """已回填物流单号（已发货）的订单：消息撤回只留档，不置为已撤单。

        群里撤回常常是「规格写错后重发」，订单还在正常履约，不能因此作废。
        """
        order_id = self._make_order()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.tracking_no = "YT0711254313477"
            s.commit()
        result = self._recall(TEMPLATE_ID)
        self.assertEqual(result["orders"], [order_id])
        self.assertEqual(result["marked"], 0, "已发货的订单不撤单")
        self.assertEqual(result["skipped"], [order_id])
        order = self._order(order_id)
        self.assertFalse(order.is_recalled, "已发货订单不能被消息撤回打上撤单标记")
        self.assertNotEqual(order.order_state, "canceled", "已发货订单不能被置为已撤单")

    def test_refunded_order_not_recalled(self):
        """已走退单流程的订单：消息撤回不改写撤单状态，退单状态保持。"""
        order_id = self._make_order()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.refund_status = "pending"
            order.refund_keyword = "此单退"
            s.commit()
        result = self._recall(TEMPLATE_ID)
        self.assertEqual(result["marked"], 0, "已退单的订单不再因消息撤回而撤单")
        order = self._order(order_id)
        self.assertFalse(order.is_recalled)
        self.assertEqual(order.refund_status, "pending", "退单处理状态不被覆盖")

    def test_screenshot_message_recalled_marks_order(self):
        """撤回订单截图 -> 通过挂靠的模板消息找到订单并标记撤回。"""
        order_id = self._make_order()
        result = self._recall(IMAGE_ID)
        self.assertEqual(result["orders"], [order_id])
        order = self._order(order_id)
        self.assertTrue(order.is_recalled, "撤回订单截图后订单同样要标记撤回")
        self.assertEqual(order.recalled_message_id, IMAGE_ID)

    def test_unknown_message_noop(self):
        """本地没有的消息撤回：不报错、不误伤订单。"""
        order_id = self._make_order()
        result = self._recall("om_not_exists")
        self.assertFalse(result["found_message"])
        self.assertEqual(result["marked"], 0)
        self.assertFalse(self._order(order_id).is_recalled)

    def test_feishu_recalled_event(self):
        """飞书 im.message.recalled_v1 事件 -> 直接把订单标记成撤回。"""
        order_id = self._make_order()
        payload = {
            "header": {"event_type": "im.message.recalled_v1", "event_id": "e_1"},
            "event": {
                "message_id": TEMPLATE_ID,
                "chat_id": "oc_recall",
                "recaller_id": {"open_id": "ou_recaller"},
            },
        }
        out = events_service.handle_event(payload)
        self.assertTrue(out["ok"])
        self.assertEqual(out["orders"], [order_id])
        order = self._order(order_id)
        self.assertTrue(order.is_recalled)
        self.assertEqual(order.recalled_by, "ou_recaller")

    # ---------------------------------------------------------------- 不参与统计与流程
    def test_recalled_order_out_of_stats_and_push(self):
        """撤回订单不进统计、不进待推单发货、不再催截图。"""
        order_id = self._make_order()
        with SessionLocal() as s:
            before = stats_service.summary(s)
            pending_before = [o.id for o in push_service.pending_orders(s)]
        self.assertEqual(before["total_orders"], 1)
        self.assertIn(order_id, pending_before)

        self._recall(TEMPLATE_ID)

        with SessionLocal() as s:
            after = stats_service.summary(s)
            pending_after = [o.id for o in push_service.pending_orders(s)]
            pushed_after = [o.id for o in push_service.pushed_orders(s)]
        self.assertEqual(after["total_orders"], 0, "撤回订单不能计入订单数")
        self.assertEqual(after["total_sale"], 0.0, "撤回订单不能计入销售额")
        self.assertEqual(after["pending_push"], 0)
        self.assertNotIn(order_id, pending_after, "撤回订单不能出现在待推单发货里")
        self.assertNotIn(order_id, pushed_after)

    def test_recalled_order_dropped_from_draft_batch(self):
        """撤回订单从「未推送」批次里撤出来，已推送的批次保留历史。"""
        order_id = self._make_order()
        with SessionLocal() as s:
            draft = PushBatch(batch_no="B-DRAFT", supplier_name="GYS001", status="draft")
            pushed = PushBatch(batch_no="B-PUSHED", supplier_name="GYS001", status="pushed")
            s.add_all([draft, pushed])
            s.flush()
            s.add_all(
                [
                    PushBatchItem(batch_id=draft.id, order_id=order_id),
                    PushBatchItem(batch_id=pushed.id, order_id=order_id),
                ]
            )
            s.commit()

        self._recall(TEMPLATE_ID)

        with SessionLocal() as s:
            items = s.scalars(select(PushBatchItem).where(PushBatchItem.order_id == order_id)).all()
            kept = {s.get(PushBatch, i.batch_id).status for i in items}
        self.assertEqual(kept, {"pushed"}, "未推送批次里的撤回订单要撤出来，已推送的保留")

    def test_resend_after_recall_restores_order(self):
        """撤回后群里重发同一单：自动撤销撤回标记（不会丢单，也不用手动恢复）。"""
        order_id = self._make_order()
        self._recall(TEMPLATE_ID)
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == TEMPLATE_ID))
            ingest_service.save_order(s, msg, parse_text(msg.raw_text), None)
        self.assertFalse(
            self._order(order_id).is_recalled, "重发后要自动撤销撤回标记，订单继续参与流程"
        )

    def test_recalled_message_not_archived(self):
        """已撤回的消息不再归档成订单（撤回的只留档，不参与流程）。"""
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_recalled_pending",
                    chat_id="oc_recall",
                    msg_type="text",
                    raw_text=ORDER_TEXT,
                    parse_status="pending",
                    msg_time=now(),
                    is_recalled=True,
                )
            )
            s.commit()
        with SessionLocal() as s:
            stats = ingest_service.process_pending(s, client=None, limit=5)
        self.assertEqual(stats.get("filtered"), 1)
        with SessionLocal() as s:
            self.assertEqual(s.scalar(select(func.count(Order.id))), 0, "撤回消息不能建单")

    def test_undo_recalled_restores(self):
        """撤销撤回后订单重新参与统计与流程。"""
        order_id = self._make_order()
        self._recall(TEMPLATE_ID)
        with SessionLocal() as s:
            recall_service.undo_recalled(s, s.get(Order, order_id))
            summary = stats_service.summary(s)
            pending = [o.id for o in push_service.pending_orders(s)]
        self.assertFalse(self._order(order_id).is_recalled)
        self.assertEqual(summary["total_orders"], 1)
        self.assertIn(order_id, pending)

    # ---------------------------------------------------------------- 页面
    def test_pages_show_recalled_mark(self):
        """列表页置灰 + 「撤回订单」徽标/水印；详情页有水印与撤回提示。"""
        order_id = self._make_order()
        self._recall(TEMPLATE_ID)

        # 订单归档默认只看在途订单：撤单的都进了「撤单回收站」
        listing = self.client.get("/orders")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn("row-recalled", listing.text, "默认列表不再显示撤单订单")
        self.assertIn("撤单回收站（1）", listing.text, "右上角回收站角标显示撤单数")

        # 回收站子页面：撤单订单都在这里，可一键恢复
        bin_page = self.client.get("/orders/recycle")
        self.assertEqual(bin_page.status_code, 200)
        self.assertIn("RC-1", bin_page.text)
        self.assertIn("row-recalled", bin_page.text, "回收站里撤单行要置灰")
        self.assertIn(f"/orders/{order_id}/restore", bin_page.text, "回收站里可恢复正常")

        # 显式筛选：只看撤回订单 / 隐藏撤回订单
        only = self.client.get("/orders?recalled=1")
        self.assertIn("RC-1", only.text)
        hidden = self.client.get("/orders?recalled=0")
        self.assertNotIn("row-recalled", hidden.text)

        detail = self.client.get(f"/orders/{order_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("recall-watermark", detail.text, "详情页要打「撤回订单」水印")
        self.assertIn("不参与任何数据统计", detail.text)
        self.assertIn("撤销撤回", detail.text)

    def test_manual_recall_and_undo_endpoints(self):
        """手动标记撤回 / 撤销撤回（群里撤回事件没收到时的兜底）。"""
        order_id = self._make_order()
        resp = self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self._order(order_id).is_recalled)

        resp = self.client.post(f"/orders/{order_id}/recall/undo", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(self._order(order_id).is_recalled)


if __name__ == "__main__":
    unittest.main()
