"""消息过滤与回执：只处理订单模板消息和闲鱼订单截图，其余过滤不处理。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401  先加载测试环境（NOTIFY_MODE=off，不发真实回执）

from app.db import init_db, session_scope  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.services.filter import is_order_screenshot, is_template_message  # noqa: E402
from app.services.ingest import process_pending, upsert_raw_message  # noqa: E402
from app.services.notify import build_receipt  # noqa: E402
from app.timeutil import now  # noqa: E402

TEMPLATE = """产品：亚朵四代单枕头
收货人:贲
手机号码:14790021215
地址信息: 安徽省 滁州市 琅琊区 清流街道
售价：115
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""

CHATTER = "今天中午吃什么？顺便看下闲鱼有没有人下单"
ORDER_OCR = """买家已付款，请尽快发货
雷鸟 32U8A Pro | 31.5英寸 ¥2599.00
运费 ¥0.00
订单编号 3316901665047047870 复制
"""


class ClassifyTest(unittest.TestCase):
    def test_template_message(self):
        self.assertTrue(is_template_message(TEMPLATE))

    def test_chatter_is_not_template(self):
        # 含「闲鱼」二字但没有订单必填字段，应被过滤
        self.assertFalse(is_template_message(CHATTER))

    def test_empty_text(self):
        self.assertFalse(is_template_message(""))

    def test_screenshot_by_order_no(self):
        self.assertTrue(is_order_screenshot(ORDER_OCR))

    def test_screenshot_by_keywords(self):
        self.assertTrue(is_order_screenshot("运费 ¥0.00\n收货人 张三 13800138000"))

    def test_random_image_filtered(self):
        self.assertFalse(is_order_screenshot("今天天气不错 出去玩"))


class PipelineTest(unittest.TestCase):
    def test_chatter_filtered_in_pipeline(self):
        init_db()
        item = {
            "message_id": "om_chatter_1",
            "chat_id": "oc_test",
            "msg_type": "text",
            "created_at": now(),
            "sender_id": "ou_test",
            "text": CHATTER,
            "image_keys": [],
        }
        with session_scope() as session:
            upsert_raw_message(session, item)
            session.commit()
            stats = process_pending(session)
            msg = (
                session.query(RawMessage)
                .filter(RawMessage.message_id == "om_chatter_1")
                .first()
            )
            self.assertEqual(msg.parse_status, "filtered")
            self.assertEqual(msg.kind, "other")
            self.assertIsNone(msg.order_id)
            self.assertEqual(stats["filtered"], 1)
            self.assertEqual(stats["parsed"], 0)

    def test_template_message_processed(self):
        init_db()
        item = {
            "message_id": "om_tpl_1",
            "chat_id": "oc_test",
            "msg_type": "text",
            "created_at": now(),
            "sender_id": "ou_test",
            "text": TEMPLATE,
            "image_keys": [],
        }
        with session_scope() as session:
            upsert_raw_message(session, item)
            session.commit()
            process_pending(session)
            msg = (
                session.query(RawMessage).filter(RawMessage.message_id == "om_tpl_1").first()
            )
            self.assertEqual(msg.parse_status, "parsed")
            self.assertEqual(msg.kind, "template")
            self.assertIsNotNone(msg.order_id)
            # 未配置飞书凭证/关闭回执时不应标记为已发送
            self.assertNotEqual(msg.notify_status, "sent")


class ReceiptTest(unittest.TestCase):
    def test_receipt_content(self):
        msg = RawMessage(message_id="om_1", sender_id="ou_abc")
        order = Order(
            id=7,
            order_no="3316901665047047870",
            product_name="雷鸟 32U8A Pro",
            spec="31.5英寸",
            sale_amount=2599.0,
            receiver_name="腾飞",
            receiver_phone="18326839658",
        )
        text = build_receipt(msg, order)
        self.assertIn("已收到并处理", text)
        self.assertIn("3316901665047047870", text)
        self.assertIn("雷鸟 32U8A Pro", text)
        self.assertIn("183****9658", text)      # 手机号脱敏
        self.assertIn('user_id="ou_abc"', text)  # 群内回复 @ 发送人

    def test_receipt_auto_order_no_hidden(self):
        msg = RawMessage(message_id="om_2", sender_id="ou_abc")
        order = Order(id=8, order_no="AUTO-om2", product_name="枕头", sale_amount=115.0)
        text = build_receipt(msg, order)
        self.assertNotIn("AUTO-om2", text)


if __name__ == "__main__":
    unittest.main()
