"""推单与上行消息维度：发送人/发送时间补全、备注字段、单笔推单文案、复制即已推单。

业务口径：
  1 上行消息的「发送人 + 发送时间 + 群名」要落到订单字段里，列表与详情都能查；
  2 上行模板支持「备注」（记录下单备注），推单信息里带出来；
  3 推单先做单笔：订单详情顶部展示推单信息，复制即标记「已推单」；
  4 「今日第 x 单」按供应商分别计数（同一家供应商当天产生的订单按发送时间排队，
    撤回的订单不占号 —— 详见 tests/test_push_index.py）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    OperationLog,
    Order,
    OrderImage,
    PushBatch,
    PushBatchItem,
    RawMessage,
)
from app.services import push as push_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.services.ingest import process_pending, upsert_raw_message  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

TEMPLATE = """产品：亚朵四代单枕头
收货人:贲
手机号码:14790021215
地址信息: 安徽省 滁州市 琅琊区 清流街道 凤凰湖畔18栋
备注： 要黑色，尽快发货
售价：115
成本价：60
所属供应链：华强北数码
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍"""

CHAT_NAME = "闲鱼接单群"


class PushFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _ingest(
        self,
        message_id: str,
        sender_name: str = "小鱼",
        at: datetime | None = None,
        text: str | None = None,
    ):
        """模拟一条上行模板消息入库并解析（不发飞书、不下载图片）。"""
        at = at or now()
        with SessionLocal() as s:
            msg, _ = upsert_raw_message(
                s,
                {
                    "message_id": message_id,
                    "chat_id": "oc_push",
                    "msg_type": "text",
                    "text": text or TEMPLATE,
                    "created_at": at,
                    "sender_id": "ou_push_1",
                    "sender_name": sender_name,
                    "sender_type": "user",
                    "image_keys": [],
                },
            )
            msg.chat_name = CHAT_NAME
            s.commit()
            process_pending(s)
            order = s.scalar(select(Order).where(Order.source_message_id == message_id))
            order_id = order.id
        return order_id

    def test_01_message_meta_fills_order(self):
        """上行消息的发送人、发送时间、群名要补全到订单字段。"""
        at = now() - timedelta(minutes=5)
        order_id = self._ingest("om_push_1", at=at)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.sender_name, "小鱼")
            self.assertEqual(order.sender_id, "ou_push_1")
            self.assertEqual(order.chat_name, CHAT_NAME)
            self.assertIsNotNone(order.msg_time)
            self.assertAlmostEqual(
                (order.msg_time - at).total_seconds(), 0, delta=2
            )
            # 上行模板里的「备注」落到订单备注
            self.assertEqual(order.note, "要黑色，尽快发货")
            self.assertEqual(order.receiver_name, "贲")
            self.assertEqual(order.receiver_phone, "14790021215")

        # 消息页要能看到发送人、群名
        page = self.client.get("/messages")
        self.assertEqual(page.status_code, 200)
        self.assertIn("小鱼", page.text)
        self.assertIn(CHAT_NAME, page.text)

        # 订单列表能看到收货人信息
        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        self.assertIn("14790021215", page.text)
        self.assertIn("小鱼", page.text)

    def test_02_order_detail_shows_source_and_remark(self):
        order_id = self._ingest("om_push_2")
        page = self.client.get(f"/orders/{order_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("飞书发送人", page.text)
        self.assertIn("发送时间", page.text)
        self.assertIn("要黑色，尽快发货", page.text)
        self.assertIn(CHAT_NAME, page.text)

    def test_03_push_text_format(self):
        """推单文案：产品/收货人/手机/地址/备注/成本价/是否转账/今日第 x 单。"""
        order_id = self._ingest("om_push_3")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            index = push_service.supplier_daily_index(s, order)
            text = push_service.push_text(order, index)
            label = push_service.day_label(push_service.index_day(order))
            self.assertEqual(index, 1)

        self.assertIn("产品：亚朵四代单枕头", text)
        self.assertIn("收货人：贲", text)
        self.assertIn("手机号码：14790021215", text)
        self.assertIn("地址信息: 安徽省", text)
        self.assertIn("备注：要黑色，尽快发货", text)
        self.assertIn("成本价：60", text)
        self.assertIn("是否转账：未转账", text)
        # 「今日第 x 单」带日期与周几，供应商一眼看出是哪天的第几单
        self.assertTrue(text.rstrip().endswith(f"今日{label} 第1单，烦请及时发货！"))

        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("推单信息", page.text)
        self.assertIn("复制并标记已推单", page.text)

    def test_04_copy_marks_pushed_and_counts_by_supplier(self):
        """复制即已推单；同一供应商同一天按顺序计数。"""
        first = self._ingest("om_push_4")
        # 第二单换收货人与手机号：内容一致的消息会被判为「重复同步」不再建单
        second = self._ingest(
            "om_push_5",
            text=TEMPLATE.replace("收货人:贲", "收货人:李四").replace(
                "手机号码:14790021215", "手机号码:13800001111"
            ),
        )

        resp = self.client.post(
            f"/orders/{first}/push",
            data={"transfer_status": "paid"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

        with SessionLocal() as s:
            order = s.get(Order, first)
            self.assertEqual(order.supplier_status, "pushed")
            self.assertIsNotNone(order.pushed_at)
            self.assertEqual(order.transfer_status, "paid")
            # 推单动作要进操作日志
            self.assertIsNotNone(
                s.scalar(select(OperationLog).where(OperationLog.action == "推单给供应商"))
            )
            # 第二单：同供应商、同天 -> 今日第 2 单
            nxt = s.get(Order, second)
            self.assertEqual(push_service.supplier_daily_index(s, nxt), 2)
            text = push_service.push_text(nxt, 2)
            day = push_service.day_label(push_service.index_day(nxt))
            self.assertIn(f"今日{day} 第2单", text)
            self.assertIn("是否转账：未转账", text)

        # 已推单的订单详情页显示已推单状态
        page = self.client.get(f"/orders/{first}")
        self.assertIn("已推单", page.text)
        self.assertIn("是否转账：已转账", page.text)

    def test_05_batch_push_entry_is_hidden(self):
        """当前只做单笔推单：列表与批次页不再暴露批量组批入口。"""
        self._ingest("om_push_6")
        orders_page = self.client.get("/orders")
        self.assertNotIn("生成推单批次", orders_page.text)
        self.assertNotIn("按供应商自动组批", orders_page.text)

        batches_page = self.client.get("/batches")
        self.assertEqual(batches_page.status_code, 200)
        self.assertIn("已推单记录", batches_page.text)
        self.assertIn("待填回单", batches_page.text)   # 顶部两张统计卡：已推单 / 待填回单
        self.assertNotIn("按供应商自动组批", batches_page.text)

    def test_06_canceled_order_is_not_pending_push(self):
        """已撤单的订单：推单状态写「已撤回，无需推单」，不再显示「待推单发货」。"""
        self._ingest("om_push_7")
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_push_7"))
            order_id = order.id
            self.assertEqual(push_service.supplier_status_label(order), "待推单发货")
        self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order.is_recalled)
            self.assertEqual(push_service.supplier_status_label(order), "用户撤单，无需推单")
        page = self.client.get("/orders?recalled=1")
        self.assertIn("用户撤单，无需推单", page.text)
        self.assertIn("该订单已撤单，无需再推给供应商", page.text)
        detail = self.client.get(f"/orders/{order_id}")
        self.assertIn("用户撤单，无需推单", detail.text)

    def test_07_transfer_time_is_recorded(self):
        """标记「已转账」要记下时间：资金结算按它算结算时间，改回未转账清空。"""
        self._ingest("om_push_8")
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_push_8"))
            self.assertIsNone(order.transfer_paid_at)
            push_service.apply_transfer(order, "paid")
            paid_at = order.transfer_paid_at
            s.commit()
            order_id = order.id
        self.assertIsNotNone(paid_at)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.transfer_paid_at, paid_at, "重复标记不该改掉第一次的时间")
            push_service.apply_transfer(order, "unpaid")
            s.commit()
        with SessionLocal() as s:
            self.assertIsNone(s.get(Order, order_id).transfer_paid_at)


if __name__ == "__main__":
    unittest.main()
