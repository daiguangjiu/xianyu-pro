"""退单登记（2026-10-06 口径）：群里发「原单号 + 退回单号 + 备注」的模板消息。

规则（业务口径）：
  1 这类消息**不是新订单**，是按「原单号」给已有订单补退单信息；
    退回单号写进订单的退单编号（return_tracking_no），页面订单信息里能直接看到；
  2 备注是**累计**的：群里可能分多次发，后发的追加成一行写在后面，历史备注不动；
    同一条内容重复推送不重复追加；
  3 匹配不到订单时跟退单一个口径：**不新建订单**，留痕 + 回群提示补信息；
  4 登记成功顺手把订单标记退单（有退回单号说明客户已经在退了）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from starlette.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderLog, RawMessage  # noqa: E402
from app.parser.engine import parse_text  # noqa: E402
from app.services import order_log as order_log_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.services.ingest import process_pending, upsert_raw_message  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

ORDER_NO = "YT1134546055340"
PHONE = "17396748268"
PRODUCT = "倍思Q2 20000毫安自带线充电宝"

RETURN_TEXT = f"""原单号：{ORDER_NO}

退回单号：73729433121621

买家姓名电话地址：黄仕宏 {PHONE}
广西壮族自治区崇左市天等县福新镇龙江屯

产品名称 ：{PRODUCT}

备注：无3c。顾客承担运费退回
"""

SECOND_TEXT = f"""原单号：{ORDER_NO}
退回单号：73729433121621
备注：客户已寄出，运费到付 12 元
"""


def _item(message_id: str, text: str, minutes_ago: int = 5) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_return",
        "msg_type": "text",
        "text": text,
        "created_at": now() - timedelta(minutes=minutes_ago),
        "sender_id": "ou_return",
        "sender_type": "user",
        "image_keys": [],
    }


class ReturnRegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    # ------------------------------------------------------------ 工具
    def _order(self, order_no: str = ORDER_NO, **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name=kw.pop("product_name", PRODUCT),
                receiver_name=kw.pop("receiver_name", "黄仕宏"),
                receiver_phone=kw.pop("receiver_phone", PHONE),
                deal_amount=kw.pop("deal_amount", 99.0),
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _send(self, message_id: str, text: str) -> None:
        with SessionLocal() as s:
            upsert_raw_message(s, _item(message_id, text))
            s.commit()
            process_pending(s)

    def _note(self, order_id: int) -> str:
        with SessionLocal() as s:
            return (s.get(Order, order_id).note or "").strip()

    # ------------------------------------------------------------ 1 解析
    def test_10_parser_reads_return_fields(self):
        """原单号 / 退回单号 / 备注都能单独抽出来，且不会被订单号字段抢走。"""
        result = parse_text(RETURN_TEXT)
        fields = result.fields
        self.assertEqual(fields.get("original_order_no"), ORDER_NO)
        self.assertEqual(fields.get("return_no"), "73729433121621")
        self.assertEqual(fields.get("note"), "无3c。顾客承担运费退回")
        self.assertFalse(fields.get("order_no"), "原单号不能掉进订单号字段")

    # ------------------------------------------------------------ 2 登记
    def test_20_register_writes_return_no_and_note(self):
        """退单登记按原单号落到已有订单：不新建脏单，退单编号与备注都写进去。"""
        order_id = self._order()
        self._send("om_reg_1", RETURN_TEXT)

        with SessionLocal() as s:
            orders = s.query(Order).all()
            self.assertEqual(len(orders), 1, "退单登记不能新建订单")
            order = s.get(Order, order_id)
            self.assertEqual(order.return_tracking_no, "73729433121621", "退单编号已登记")
            self.assertIn("无3c。顾客承担运费退回", order.note or "")
            self.assertTrue(order.is_received, "填了退回单号 = 客户已收货")
            self.assertTrue(bool((order.refund_status or "").strip()), "顺带标记退单")
            self.assertEqual(order.refund_keyword, refund_service.REGISTER_KEYWORD)
            msg = s.query(RawMessage).filter(RawMessage.message_id == "om_reg_1").one()
            self.assertEqual(msg.parse_status, "parsed")
            self.assertEqual(msg.order_id, order_id)
            actions = [r.action for r in s.query(OrderLog).filter(OrderLog.order_id == order_id)]
            self.assertIn(order_log_service.ACTION_NOTE_APPEND, actions)
            self.assertIn(order_log_service.ACTION_RETURN_TRACKING, actions)

    def test_30_note_accumulates(self):
        """备注多次发：追加成多行，历史备注一条不少。"""
        order_id = self._order()
        self._send("om_reg_a", RETURN_TEXT)
        first = self._note(order_id)
        self.assertEqual(len(first.splitlines()), 1, "第一次一条备注")

        self._send("om_reg_b", SECOND_TEXT)
        lines = self._note(order_id).splitlines()
        self.assertEqual(len(lines), 2, "第二次备注累加成两行")
        self.assertIn("无3c。顾客承担运费退回", lines[0], "第一次的备注还在最前面")
        self.assertIn("客户已寄出", lines[1])

    def test_40_duplicate_note_is_not_repeated(self):
        """同一条备注重复推送：不重复追加（群里重发同一句很常见）。"""
        order_id = self._order()
        self._send("om_reg_dup1", RETURN_TEXT)
        before = self._note(order_id)
        self._send("om_reg_dup2", RETURN_TEXT)
        self.assertEqual(self._note(order_id), before, "重复推送不再追加一行")

    def test_50_unmatched_does_not_create_order(self):
        """匹配不到订单：跟退单一个口径，不新建订单并写明原因。"""
        order_id = self._order()
        wrong = (
            RETURN_TEXT.replace(ORDER_NO, "YT9999999999999")
            .replace(PHONE, "13800001111")
            .replace("黄仕宏", "张三丰")
            .replace(PRODUCT, "完全不同的另一款商品")
            .replace("广西壮族自治区崇左市天等县福新镇龙江屯", "北京朝阳区某小区 1 号楼")
        )
        self._send("om_reg_miss", wrong)

        with SessionLocal() as s:
            self.assertEqual(s.query(Order).count(), 1, "没有新建脏单")
            msg = s.query(RawMessage).filter(RawMessage.message_id == "om_reg_miss").one()
            self.assertEqual(msg.parse_status, "ignored")
            self.assertIn("没有新建订单", msg.ignore_reason or "")
            self.assertFalse(s.get(Order, order_id).return_tracking_no, "原订单没动")

    def test_60_only_return_no_matches_by_receiver(self):
        """只写了退回单号、没写原单号：按手机号 + 产品也能认出是哪一笔。"""
        order_id = self._order()
        text = f"""退回单号：SF9876543210
买家姓名电话地址：黄仕宏 {PHONE}
产品名称 ：{PRODUCT}
备注：机器有一处磕碰
"""
        self._send("om_reg_nono", text)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.return_tracking_no, "SF9876543210")
            self.assertIn("机器有一处磕碰", order.note or "")
            self.assertEqual(s.query(Order).count(), 1)

    # ------------------------------------------------------------ 3 页面
    def test_70_detail_shows_return_no_and_note_lines(self):
        """订单详情页：退单编号在订单信息里，备注按行展示（历史备注一起）。"""
        order_id = self._order()
        self._send("om_reg_page", RETURN_TEXT)
        self._send("om_reg_page2", SECOND_TEXT)

        resp = self.client.get(f"/orders/{order_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("退单编号", html)
        self.assertIn("73729433121621", html, "退单编号要显示在订单信息里")
        self.assertIn("无3c。顾客承担运费退回", html)
        self.assertIn("客户已寄出，运费到付 12 元", html, "两次备注都要在页面上")
        self.assertIn("white-space:pre-wrap", html, "备注多条时要按行显示")


if __name__ == "__main__":
    unittest.main()
