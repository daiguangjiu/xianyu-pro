"""通知消息模板：系统设置页可自定义编辑（收到订单的回执 / 退单通知）+ @ 谁的规则。

业务口径：
  1 模板留空 = 用系统内置格式，改坏了也不会让通知发不出去（自动退回内置格式）；
  2 模板里用 {{ 订单号 }} 这类中文变量，不确定有没有值的行用 {% if %} 包起来；
  3 @ 谁是定好的规则（勾选）：@ 发消息的人 / @ 供应链对接人，不用在文案里写名字。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class NotifyTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()
            s.add(
                Order(
                    order_no="TPL-1",
                    product_name="倍思充电宝",
                    supplier_name="安徽梦想电商",
                    sale_amount=199.0,
                    cost_amount=120.0,
                    quantity=2,
                    receiver_name="张三",
                    receiver_phone="13800001111",
                    xianyu_account="小鱼",
                    payee_name="小鱼",
                )
            )
            s.add(
                RawMessage(
                    message_id="om_tpl_1",
                    chat_id="oc_tpl",
                    sender_id="ou_tpl_sender",
                    msg_time=now(),
                )
            )
            s.commit()
            self.order_id = s.query(Order).filter_by(order_no="TPL-1").one().id

        # 改配置只在本用例内生效，跑完恢复
        self._saved = {
            "receipt_template": settings.notify_receipt_template,
            "refund_detail_template": settings.notify_refund_detail_template,
            "receipt_at": settings.notify_receipt_at,
            "receipt_at_names": settings.notify_receipt_at_names,
            "refund_at": settings.notify_refund_at,
            "mode": settings.notify_mode,
            "ack_text": settings.refund_ack_text,
            "cc_text": settings.refund_cc_text,
            "cc_names": settings.refund_cc_names,
        }
        settings.notify_mode = "reply"
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if key == "receipt_template":
                settings.notify_receipt_template = value
            elif key == "refund_detail_template":
                settings.notify_refund_detail_template = value
            elif key == "receipt_at":
                settings.notify_receipt_at = value
            elif key == "refund_at":
                settings.notify_refund_at = value
            elif key == "receipt_at_names":
                settings.notify_receipt_at_names = value
            elif key == "ack_text":
                settings.refund_ack_text = value
            elif key == "cc_text":
                settings.refund_cc_text = value
            elif key == "cc_names":
                settings.refund_cc_names = value
            else:
                settings.notify_mode = value
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _msg_order(self):
        with SessionLocal() as s:
            msg = s.query(RawMessage).filter_by(message_id="om_tpl_1").one()
            order = s.query(Order).filter_by(order_no="TPL-1").one()
            s.expunge(msg)
            s.expunge(order)
            return msg, order

    def test_01_no_template_uses_builtin(self):
        """没配模板 = 内置格式，且群内模式默认 @ 发送人。"""
        settings.notify_receipt_template = ""
        settings.notify_receipt_at = "sender"
        msg, order = self._msg_order()
        text = notify_service.build_receipt(msg, order)
        self.assertIn("已收到并处理", text)
        self.assertIn("订单号：TPL-1", text)
        self.assertIn("倍思充电宝", text)
        self.assertTrue(text.startswith('<at user_id="ou_tpl_sender">'))

    def test_02_receipt_template_used(self):
        """配了模板就按模板渲染（变量替换 + 条件行）。"""
        settings.notify_receipt_template = (
            "收到 {{ 产品 }} 共 {{ 数量 }} 件\n"
            "{% if 供应商 %}供应商：{{ 供应商 }}{% endif %}"
        )
        msg, order = self._msg_order()
        text = notify_service.build_receipt(msg, order)
        self.assertIn("收到 倍思充电宝 x2 共 2 件", text, "{{ 产品 }} 带规格与件数")
        self.assertIn("供应商：安徽梦想电商", text)
        self.assertNotIn("已收到并处理", text, "配了模板就不再用内置格式")

    def test_03_bad_template_falls_back(self):
        """模板写错也不能影响下发：退回内置格式。"""
        settings.notify_receipt_template = "{% if 订单号 %}产品：{{ 产品 }}"
        msg, order = self._msg_order()
        text = notify_service.build_receipt(msg, order)
        self.assertIn("已收到并处理", text, "模板语法错误时退回内置格式")

    def test_04_receipt_at_can_be_off(self):
        """@ 谁可关：notify_receipt_at=none 时只发文字、不 @ 人。"""
        settings.notify_receipt_template = ""
        settings.notify_receipt_at = "none"
        msg, order = self._msg_order()
        text = notify_service.build_receipt(msg, order)
        self.assertNotIn("<at ", text)
        self.assertIn("订单号：TPL-1", text)

    def test_05_refund_detail_template_and_at(self):
        """退单通知：明细按模板渲染，@ 谁按规则（都不勾就不 @）。"""
        settings.notify_refund_detail_template = "单号 {{ 订单号 }}｜处理 {{ 处理 }}"
        settings.notify_refund_at = "none"
        msg, order = self._msg_order()
        text = notify_service.build_refund_receipt(msg, order, keyword="退单", client=None)
        self.assertIn("单号 TPL-1｜处理", text)
        self.assertNotIn("<at ", text, "退单通知 @ 谁关掉后不 @ 任何人")

    def test_06_settings_page_and_save(self):
        """设置页能编辑并保存（保存后即时生效，这里 mock 掉写 .env 避免污染本地配置）。"""
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("通知消息模板", page.text)
        self.assertIn("notify_receipt_template", page.text)

        with mock.patch("app.web.pages.is_writable", return_value=False):
            resp = self.client.post(
                "/settings/templates",
                data={
                    "notify_receipt_template": "新单：{{ 产品 }}",
                    "notify_refund_detail_template": "退单：{{ 订单号 }}",
                    "refund_ack_text": "退单已收到！",
                    "refund_cc_text": "请协助拦截",
                    "refund_cc_names": "牛磊,小鱼",
                    "notify_receipt_at_names": "牛磊,小鱼",
                    "receipt_at_sender": "1",
                    "receipt_at_names_on": "1",
                    "refund_at_sender": "1",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303, resp.text)
        self.assertEqual(settings.notify_receipt_template, "新单：{{ 产品 }}")
        self.assertEqual(settings.notify_refund_detail_template, "退单：{{ 订单号 }}")
        self.assertEqual(
            settings.notify_receipt_at, "sender,names", "回执 @ 发送人 + 名单里的人"
        )
        self.assertEqual(settings.notify_receipt_at_names, ["牛磊", "小鱼"], "名单可多人")
        self.assertEqual(settings.notify_refund_at, "sender", "只勾了 @ 发送人")
        self.assertEqual(settings.refund_cc_names, ["牛磊", "小鱼"], "抄送可多人")
        # 保存的模板立刻能用上
        msg, order = self._msg_order()
        self.assertIn("新单：倍思充电宝", notify_service.build_receipt(msg, order))

    def test_07_receipt_at_multiple_people(self):
        """回执 @ 多人：发送人 + 名单里的多个人一起 @。"""
        settings.notify_receipt_template = ""
        settings.notify_receipt_at = "sender,names"
        settings.notify_receipt_at_names = ["牛磊", "小鱼"]
        msg, order = self._msg_order()
        members = {"ou_niu": "牛磊", "ou_yu": "小鱼", "ou_tpl_sender": "小王"}
        with mock.patch.object(notify_service, "_chat_members", return_value=members):
            text = notify_service.build_receipt(msg, order)
        self.assertEqual(text.count("<at "), 3, "@ 发送人 + 名单里 2 个人")
        for open_id in ("ou_tpl_sender", "ou_niu", "ou_yu"):
            self.assertIn(f'<at user_id="{open_id}"></at>', text)
        self.assertIn("订单号：TPL-1", text)

    def test_08_receipt_at_no_duplicate(self):
        """名单里的人和发送人是同一个时，只 @ 一次。"""
        settings.notify_receipt_at = "sender,names"
        settings.notify_receipt_at_names = ["小王"]
        msg, order = self._msg_order()
        with mock.patch.object(
            notify_service, "_chat_members", return_value={"ou_tpl_sender": "小王"}
        ):
            text = notify_service.build_receipt(msg, order)
        self.assertEqual(text.count("<at "), 1, "同一个人不重复 @")

    def test_09_refund_at_multiple_cc(self):
        """退单通知：@ 发送人 + 多个供应链对接人。"""
        settings.notify_refund_at = "sender,cc"
        settings.refund_cc_names = ["牛磊", "小鱼"]
        msg, order = self._msg_order()
        members = {"ou_niu": "牛磊", "ou_yu": "小鱼", "ou_tpl_sender": "小王"}
        with mock.patch.object(notify_service, "_chat_members", return_value=members):
            text = notify_service.build_refund_receipt(
                msg, order, keyword="退单", client=None
            )
        self.assertEqual(text.count("<at "), 3, "@ 发送人 + 2 个对接人")
        # 这单还没推给供应商：口径是「直接不推，退款给客户」，不是「联系供应链拦截」
        self.assertGreaterEqual(
            text.count("还没推给供应商"), 2, "每个对接人都带一句处理口径"
        )

    def test_10_app_sender_fixed_name(self):
        """机器人 / 应用自己发的消息（cli_aa3f9f）固定叫「闲鱼订单Agent」。"""
        self.assertTrue(notify_service.is_app_sender("cli_aa3f9f"))
        self.assertEqual(notify_service.APP_SENDER_NAME, "闲鱼订单Agent")
        self.assertEqual(
            ingest_service.resolve_sender_name("cli_aa3f9f", None),
            "闲鱼订单Agent",
            "机器人不用查通讯录，也不算未识别员工",
        )
        self.assertEqual(
            notify_service.sender_display_name("cli_aa3f9f", ""), "闲鱼订单Agent"
        )

    def test_11_receipt_at_people_with_note(self):
        """回执：@ 的人可以各带一句话，写了 | 的人正文后再单独 @ 一次带上。"""
        settings.notify_receipt_template = ""
        settings.notify_receipt_at = "sender,names"
        settings.notify_receipt_at_names = ["牛磊|请确认库存", "小鱼"]
        msg, order = self._msg_order()
        members = {"ou_niu": "牛磊", "ou_yu": "小鱼", "ou_tpl_sender": "小王"}
        with mock.patch.object(notify_service, "_chat_members", return_value=members):
            text = notify_service.build_receipt(msg, order)
        self.assertIn('<at user_id="ou_niu"></at> 请确认库存', text, "给牛磊的那句话")
        self.assertIn('<at user_id="ou_tpl_sender"></at>', text, "@ 发送人")
        self.assertEqual(
            text.count('<at user_id="ou_yu"></at>'), 1, "没写文案的人只在开头 @ 一次"
        )

    def test_12_refund_cc_each_with_own_note(self):
        """退单：每个对接人可以有自己的一句文案，没写的用默认那句。"""
        settings.notify_refund_at = "sender,cc"
        settings.refund_cc_names = ["牛磊|请优先拦截这一单", "小鱼"]
        msg, order = self._msg_order()
        members = {"ou_niu": "牛磊", "ou_yu": "小鱼", "ou_tpl_sender": "小王"}
        with mock.patch.object(notify_service, "_chat_members", return_value=members):
            text = notify_service.build_refund_receipt(
                msg, order, keyword="退单", client=None
            )
        self.assertIn('<at user_id="ou_niu"></at> 请优先拦截这一单', text)
        self.assertIn(
            f'<at user_id="ou_yu"></at> {notify_service.REFUND_CC_TEXT_NOT_PUSHED}',
            text,
            "没单独写的人用默认文案（未推单的口径）",
        )
