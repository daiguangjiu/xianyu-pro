"""其余通知的模板：发货 / 推单 / 撤单 / 重复订单 / 缺截图 / 意图提示 / 合并摘要 / 每日汇总。

业务口径（2026-09）：
  1 系统下发的每一类通知都能在系统设置页改文案，**留空 = 内置格式**；
  2 模板写错（语法错误）不能让通知发不出去，统一退回内置格式；
  3 推单给供应商的文案里电话是完整号码（供应商要能打电话）；
  4 每日出单汇总（默认 24 点）是整群日报：开 @ 时按 @所有人 提醒，不逐个 @ 人。
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
from app.services import daily_summary as ds  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import settings_store  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
TPL_FIELDS = (
    # 下面这些字段会被用例改动，跑完必须恢复原样，免得污染其它测试
    "notify_receipt_template",
    "notify_refund_detail_template",
    "notify_receipt_at",
    "notify_refund_at",
    "refund_ack_text",
    "refund_cc_text",
    "refund_cc_names",
    "notify_duplicate_template",
    "notify_cancel_template",
    "notify_intent_template",
    "notify_missing_image_template",
    "notify_digest_template",
    "push_template",
    "tracking_template",
    "daily_summary_template",
    "notify_missing_image",
    "notify_mode",
    "daily_summary_at_users",
)
PAYLOAD = {
    "date": "2026-09-27",
    "people": [
        {
            "name": "张三",
            "open_id": "ou_zhang",
            "shops": 1,
            "orders": 2,
            "amount": 300.0,
            "refunds": 1,
            "refund_amount": 100.0,
        }
    ],
    "total": {
        "shops": 1,
        "orders": 2,
        "amount": 300.0,
        "refunds": 1,
        "refund_amount": 100.0,
    },
}


class _FakeClient:
    """只记录发出去的文案，不真调飞书。"""

    enabled = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def reply_text(self, message_id: str, text: str) -> bool:
        self.sent.append(text)
        return True

    def send_text(self, target: str, text: str, receive_id_type: str = "open_id") -> bool:
        self.sent.append(text)
        return True


class AllNotifyTemplatesTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.add(
                Order(
                    order_no="ALL-1",
                    product_name="倍思充电宝",
                    supplier_name="安徽梦想电商",
                    sale_amount=199.0,
                    cost_amount=120.0,
                    quantity=1,
                    receiver_name="张三",
                    receiver_phone="13800001111",
                    receiver_address="上海市杨浦区xx路1号",
                    note="要黑色",
                    tracking_no="SF1234567890",
                    xianyu_account="小鱼",
                    payee_name="小鱼",
                )
            )
            s.add(
                RawMessage(
                    message_id="om_all_1",
                    chat_id="oc_all",
                    sender_id="ou_all_sender",
                    msg_time=now(),
                )
            )
            s.commit()
        self._saved = {key: getattr(settings, key) for key in TPL_FIELDS}
        settings.notify_mode = "reply"
        settings.notify_missing_image = True
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(settings, key, value)
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _order_msg(self):
        with SessionLocal() as s:
            msg = s.query(RawMessage).filter_by(message_id="om_all_1").one()
            order = s.query(Order).filter_by(order_no="ALL-1").one()
            s.expunge(msg)
            s.expunge(order)
            return msg, order

    # ---------------- ② 推单与发货 ----------------
    def test_01_tracking_template_and_fallback(self):
        """发货同步：配了模板按模板来，留空用内置那句。"""
        _, order = self._order_msg()
        settings.tracking_template = ""
        self.assertIn("🚚 已发货", push_service.tracking_notice(order))
        settings.tracking_template = "发货啦 {{ 产品 }}｜单号 {{ 物流单号 }}｜{{ 供应商 }}"
        text = push_service.tracking_notice(order)
        self.assertEqual(text, "发货啦 倍思充电宝｜单号 SF1234567890｜安徽梦想电商")

    def test_02_tracking_bad_template_falls_back(self):
        """发货模板写错：退回内置格式，不影响同步。"""
        _, order = self._order_msg()
        settings.tracking_template = "{% if 物流单号 %}{{ 物流单号 }}"
        self.assertIn("🚚 已发货", push_service.tracking_notice(order))

    def test_03_push_template_with_full_phone(self):
        """推单文案：模板里 {{ 收货电话 }} 给的是完整号码（供应商要能打电话）。"""
        _, order = self._order_msg()
        settings.push_template = (
            "{{ 收货人 }} {{ 收货电话 }} {{ 收货地址 }}｜{{ 产品 }} x{{ 数量 }}｜"
            "成本 {{ 成本价 }}｜{{ 是否转账 }}｜今日{{ 今日日期 }} 第{{ 第几单 }}单"
        )
        text = push_service.push_text(order, 3)
        self.assertIn("张三 13800001111", text, "这里必须是完整号码，不能是 138****1111")
        self.assertIn("上海市杨浦区xx路1号", text)
        self.assertIn("第3单", text)
        settings.push_template = ""
        self.assertIn("地址信息:", push_service.push_text(order, 3), "留空回到内置格式")

    # ---------------- ③ 退单 / 撤单 ----------------
    def test_04_cancel_template_and_fallback(self):
        """撤单回执：模板 + 内置兜底 + @ 发送人。"""
        msg, order = self._order_msg()
        settings.notify_cancel_template = "已撤：{{ 订单号 }}（关键词 {{ 撤单关键词 }}）"
        text = notify_service.build_cancel_receipt(msg, order, keyword="取消订单")
        self.assertIn("已撤：ALL-1（关键词 取消订单）", text)
        self.assertIn('<at user_id="ou_all_sender"></at>', text)
        settings.notify_cancel_template = ""
        self.assertIn("🚫 已按撤单处理", notify_service.build_cancel_receipt(msg, order))

    # ---------------- ① 重复订单 / 缺截图 ----------------
    def test_05_duplicate_template_and_fallback(self):
        """重复订单提醒：模板 + 内置兜底。"""
        msg, order = self._order_msg()
        settings.notify_duplicate_template = "重复：{{ 产品 }} {{ 订单号 }}"
        self.assertIn("重复：倍思充电宝 ALL-1", notify_service.build_duplicate_receipt(msg, order))
        settings.notify_duplicate_template = ""
        self.assertIn("已收到过", notify_service.build_duplicate_receipt(msg, order))

    def test_06_missing_image_template(self):
        """缺截图提醒：配了模板按模板发（同一条只提醒一次）。"""
        settings.notify_missing_image_template = "请补截图：{{ 产品 }}（{{ 订单号 }}）"
        fake = _FakeClient()
        with SessionLocal() as s:
            msg = s.query(RawMessage).filter_by(message_id="om_all_1").one()
            order = s.query(Order).filter_by(order_no="ALL-1").one()
            self.assertTrue(
                ingest_service.notify_missing_screenshot(s, msg, order, fake), "第一次提醒"
            )
            # 第二次不再发，避免刷屏
            self.assertFalse(ingest_service.notify_missing_screenshot(s, msg, order, fake))
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("请补截图：倍思充电宝（ALL-1）", fake.sent[0])
        self.assertIn('<at user_id="ou_all_sender"></at>', fake.sent[0])

    # ---------------- ④ 提示类 ----------------
    def test_07_intent_template_and_fallback(self):
        """意图回执：{{ 标题 }} {{ 说明 }} {{ 提示 }}。"""
        msg, _ = self._order_msg()
        settings.notify_intent_template = "⚠️ {{ 标题 }}\n{{ 说明 }}\n{% if 提示 %}→ {{ 提示 }}{% endif %}"
        text = notify_service.build_intent_receipt(
            msg, "识别到「退单」意图", "这笔要人工确认", "请回复订单号"
        )
        self.assertIn("⚠️ 识别到「退单」意图", text)
        self.assertIn("这笔要人工确认", text)
        self.assertIn("→ 请回复订单号", text)
        settings.notify_intent_template = ""
        text = notify_service.build_intent_receipt(msg, "识别到「退单」意图", "", "请回复订单号")
        self.assertIn("⚠️ 识别到「退单」意图", text)
        self.assertIn("请回复订单号", text)

    def test_08_digest_template(self):
        """静默期合并摘要：{{ 条数 }} {{ 明细 }} {{ 更多 }}。"""
        msg, order = self._order_msg()
        entries = [(msg, order), (msg, order)]
        settings.notify_digest_template = "合并 {{ 条数 }} 条\n{{ 明细 }}{% if 更多 %}\n还有 {{ 更多 }} 条{% endif %}"
        text = notify_service.build_digest(entries)
        self.assertIn("合并 2 条", text)
        self.assertIn("ALL-1", text, "明细里带订单号")
        self.assertIn("2. ", text)
        settings.notify_digest_template = ""
        self.assertIn("📦 已收到并处理 2 条订单", notify_service.build_digest(entries))

    # ---------------- ⑤ 每日汇总（24 点） ----------------
    def test_09_daily_summary_template_with_at_all(self):
        """汇总：文案可改 + 开 @ 时是 @所有人（不是逐个 @ 人）。"""
        settings.daily_summary_template = (
            "日报 {{ 日期 }}：{{ 人数 }} 人 / 出单 {{ 出单数 }} 单 {{ 出单金额 }}\n{{ 明细 }}\n{{ 合计 }}"
        )
        text = ds.build_text(PAYLOAD, at_users=True)
        self.assertTrue(text.startswith('<at user_id="all"></at> '), "整群日报 @所有人")
        self.assertNotIn("ou_zhang", text, "不再逐个 @ 出单人")
        self.assertIn("日报 2026-09-27：1 人 / 出单 2 单 300.00", text)
        self.assertIn("张三", text)
        text_off = ds.build_text(PAYLOAD, at_users=False)
        self.assertNotIn("<at ", text_off)

        settings.daily_summary_template = ""
        builtin = ds.build_text(PAYLOAD, at_users=True)
        self.assertIn("📊 今日（2026-09-27）出单情况汇总", builtin)
        self.assertIn("合计：店铺 1 家", builtin)

    def test_10_empty_summary_and_bad_template(self):
        """没出单 / 模板写错时也能正常发出。"""
        empty = {"date": "2026-09-27", "people": [], "total": PAYLOAD["total"]}
        settings.daily_summary_template = "{% if 明细 %}{{ 明细 }}"
        text = ds.build_text(empty, at_users=False)
        self.assertIn("当天没有出单", text, "模板写错退回内置格式")
        settings.daily_summary_template = "{% if 无出单 %}今天（{{ 日期 }}）没人出单{% endif %}"
        self.assertEqual(
            ds.build_text(empty, at_users=False), "今天（2026-09-27）没人出单"
        )

    # ---------------- 设置页 ----------------
    def test_11_page_saves_all_templates(self):
        """设置页：这些模板都跟回执模板一起保存，并立即生效。"""
        # 关掉写 .env：只验证运行时生效，避免把测试数据写进配置文件
        with mock.patch.object(settings_store, "is_writable", return_value=False):
            resp = self.client.post(
                "/settings/templates",
                data={
                    "notify_receipt_template": "回执 {{ 产品 }}",
                    "tracking_template": "发货 {{ 物流单号 }}",
                    "push_template": "推单 {{ 收货人 }}",
                    "notify_cancel_template": "撤单 {{ 订单号 }}",
                    "notify_duplicate_template": "重复 {{ 订单号 }}",
                    "notify_missing_image_template": "补图 {{ 订单号 }}",
                    "notify_intent_template": "提示 {{ 标题 }}",
                    "notify_digest_template": "摘要 {{ 条数 }}",
                    "daily_summary_template": "日报 {{ 日期 }}",
                    "refund_ack_text": "收到",
                    "refund_cc_text": "处理中",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303, resp.text)
        for field, value in {
            "tracking_template": "发货 {{ 物流单号 }}",
            "push_template": "推单 {{ 收货人 }}",
            "notify_cancel_template": "撤单 {{ 订单号 }}",
            "notify_duplicate_template": "重复 {{ 订单号 }}",
            "notify_missing_image_template": "补图 {{ 订单号 }}",
            "notify_intent_template": "提示 {{ 标题 }}",
            "notify_digest_template": "摘要 {{ 条数 }}",
            "daily_summary_template": "日报 {{ 日期 }}",
        }.items():
            self.assertEqual(getattr(settings, field), value, f"{field} 保存后立即生效")
        _, order = self._order_msg()
        self.assertEqual(
            push_service.tracking_notice(order), "发货 SF1234567890", "发货模板马上用上"
        )
