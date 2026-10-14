"""群消息意图识别：不看固定格式，按动作意图处理订单状态。

业务口径（2026-09）：
  1 退单：群里发「此单退 / 退货 / 不要了 / 拒收」等表达「要退」的消息即按退单处理，
    消息里能解析出订单信息就精确匹配，只有一句人话时按订单号 / 手机尾号 / 产品名 /
    收货人打分，再兜底到「同一发送人最近的在途订单」；未发货 -> 已退单，已发货 -> 不同意退单；
  2 撤单：群里发「撤单 / 取消订单 / 作废」等，未发货的订单置「已撤单」（只留档）；
  3 撤回消息：群里把订单消息撤回，按被撤回的消息定位订单 -> 已撤单（本地没存过也能反查）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (
    ORDER_STATE_CANCELED,
    ORDER_STATE_NEW,
    ORDER_STATE_PUSHED,
    ORDER_STATE_REFUND_REQUEST,
    SETTLEMENT_VOID,
    Order,
    RawMessage,
    Settlement,
    VoiceEvent,
)  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import intent as intent_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import recall as recall_service  # noqa: E402
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
ORDER_TEXT_2 = (
    "产品： 亚朵四代单枕头\n"
    "收货人： 小鱼儿\n"
    "手机号码： 14790021215\n"
    "地址信息： 安徽省 滁州市 琅琊区 清流街道 1 号\n"
    "售价：115\n"
    "成本价：60\n"
    "所属供应链：BH002\n"
)


class FakeClient:
    """假飞书客户端：记录下发内容，不真正联网。"""

    enabled = True

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.sent: list[str] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append(text)
        return {"ok": True}

    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> str:
        return ""

    def download_resource(self, *args, **kwargs):
        raise RuntimeError("测试环境不下载资源")


class IntentDetectTest(unittest.TestCase):
    """意图词表与判定规则（纯函数）。"""

    def _kind(self, text: str) -> str:
        return intent_service.classify(text).kind

    def test_01_refund_intent(self):
        for text in ["此单退了", "这单不要了", "申请退款", "买家拒收，麻烦退货", "不想要了谢谢"]:
            self.assertEqual(self._kind(text), "refund", text)

    def test_02_cancel_intent(self):
        for text in ["撤单", "取消订单", "此单取消", "作废了吧", "这单不发了"]:
            self.assertEqual(self._kind(text), "cancel", text)

    def test_03_plain_chat_has_no_intent(self):
        for text in ["今天的天气不错", "早上好", "供应商回单了吗", "数据出来了"]:
            self.assertEqual(self._kind(text), "", text)

    def test_04_weak_words_need_context(self):
        weak = intent_service.classify("退了吧")
        self.assertEqual(weak.kind, "refund")
        self.assertTrue(weak.needs_context, "「退了吧」要能确认是在说某笔订单才动手")
        strong = intent_service.classify("不要了")
        self.assertFalse(strong.needs_context, "「不要了」是强意图词")

    def test_05_template_only_looks_at_first_line(self):
        """模板消息只看第一行：正文里出现「七天无理由退货」不算退单。"""
        self.assertEqual(intent_service.classify(ORDER_TEXT, template=True).kind, "")
        text = "产品： 吹风机\n备注：七天无理由退货"
        self.assertEqual(intent_service.classify(text, template=True).kind, "")
        # 同一句话在非模板场景（一句人话）里就是退单意图
        self.assertEqual(intent_service.classify(text).kind, "refund")

    def test_06_order_context_hint(self):
        self.assertTrue(intent_service.has_order_context("刚才那单"))
        self.assertFalse(intent_service.has_order_context("谢谢"))

    def test_07_extract_order_no(self):
        self.assertEqual(
            intent_service.extract_order_no("订单号 2026010199990001 退了"),
            "2026010199990001",
        )
        self.assertEqual(intent_service.extract_order_no("这单退了"), "")


class IntentFlowTest(unittest.TestCase):
    """消息 -> 订单 -> 状态流转（端到端）。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _push(
        self,
        message_id: str,
        text: str,
        *,
        sender_id: str = "ou_1",
        sender_name: str = "小A",
        parent_message_id: str = "",
        msg_type: str = "text",
    ) -> tuple[dict, FakeClient]:
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id=message_id,
                    chat_id="oc_intent",
                    msg_type=msg_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    parent_message_id=parent_message_id,
                    raw_text=text,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            with SessionLocal() as s:
                stats = ingest_service.process_pending(s, client=fake, limit=10)
        return stats, fake

    def _order(self, order_no: str = "", source: str = "") -> Order:
        with SessionLocal() as s:
            stmt = select(Order).order_by(Order.id)
            if order_no:
                stmt = stmt.where(Order.order_no == order_no)
            if source:
                stmt = stmt.where(Order.source_message_id == source)
            return s.scalars(stmt).first()

    def _message(self, message_id: str) -> RawMessage:
        with SessionLocal() as s:
            return s.scalar(select(RawMessage).where(RawMessage.message_id == message_id))

    def _order_of(self, source: str) -> Order:
        """按来源消息取订单（重新查一次，拿到最新状态）。"""
        with SessionLocal() as s:
            return s.scalar(select(Order).where(Order.source_message_id == source))

    # ---------------------------------------------------------------- 退单
    def test_10_natural_refund_by_product_name(self):
        """一句人话「徕芬吹风机那单不要了」：靠产品名匹配到订单并标记已退单。"""
        self._push("om_i_1", ORDER_TEXT)
        stats, fake = self._push("om_i_2", "徕芬吹风机那单不要了")
        self.assertEqual(stats.get("refund"), 1)
        order = self._order_of("om_i_1")
        self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
        self.assertTrue(order.is_refund)
        self.assertEqual(order.refund_keyword, "不要了")
        self.assertTrue(fake.replies, "退单要回群告知已识别")
        self.assertIn("退单", fake.replies[0])

    def test_11_natural_refund_by_sender_fallback(self):
        """消息里没有任何订单信息：兜底到该发送人在途的订单。"""
        self._push("om_i_3", ORDER_TEXT)
        stats, _ = self._push("om_i_4", "麻烦退了吧")
        self.assertEqual(stats.get("refund"), 1)
        order = self._order_of("om_i_3")
        self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
        message = self._message("om_i_4")
        self.assertEqual(message.intent, "refund")
        self.assertIn("发送人", message.intent_note)

    def test_12_refund_by_phone_tail(self):
        """人话里只给了手机尾号：也能匹配到对应订单。"""
        self._push("om_i_5", ORDER_TEXT)
        self._push("om_i_6", ORDER_TEXT_2)
        stats, _ = self._push("om_i_7", "尾号 21215 这单退了")
        self.assertEqual(stats.get("refund"), 1)
        self.assertEqual(self._order_of("om_i_6").order_state, ORDER_STATE_REFUND_REQUEST)
        self.assertEqual(self._order_of("om_i_5").order_state, ORDER_STATE_NEW, "另一单不受影响")

    def test_13_refund_after_shipping_is_accepted(self):
        """已发货要求退单：**受理**（未收货同步物流拦截、已收货走退货退款）。"""
        self._push("om_i_8", ORDER_TEXT)
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_i_8"))
            order.tracking_no = "SF123456"
            order.supplier_status = "pushed"
            order.pushed_at = now()
            order.order_state = "shipped"
            s.commit()
        stats, fake = self._push("om_i_9", "这个单不要了")
        self.assertEqual(stats.get("refund"), 1)
        order = self._order_of("om_i_8")
        self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
        self.assertEqual(order.refund_status, "pending")
        self.assertEqual(
            push_service.refund_push_status(order), push_service.PUSH_STATUS_INTERCEPT
        )
        self.assertTrue(fake.replies)

    def test_13b_refund_by_tracking_no(self):
        """群里甩一个物流单号说退单：按物流单号认订单，不误伤同一发送人的其它在途订单。"""
        self._push("om_i_8b", ORDER_TEXT)  # 同一发送人名下还有一笔在途订单
        self._push("om_i_8c", ORDER_TEXT_2)
        with SessionLocal() as s:
            target = s.scalar(select(Order).where(Order.source_message_id == "om_i_8c"))
            target.tracking_no = "SF5155067424154"
            target.order_state = "shipped"
            s.commit()
        stats, fake = self._push("om_i_8d", "SF5155067424154 这个退单")
        self.assertEqual(stats.get("refund"), 1)
        target = self._order_of("om_i_8c")
        self.assertEqual(target.refund_status, "pending", "已发货也受理：待拦截（同步物流拦截）")
        self.assertEqual(
            self._order_of("om_i_8b").order_state, ORDER_STATE_NEW, "另一单不受影响"
        )
        self.assertTrue(any("物流单号" in t for t in fake.replies), fake.replies)

    def test_13c_refund_by_bare_order_no(self):
        """群里只甩一串订单号说退单（不带「订单号」前缀、不引用模板）：也能认出来。"""
        self._push("om_i_8e", ORDER_TEXT)
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_i_8e"))
            order.order_no = "3317077020114001399"
            s.commit()
        stats, _ = self._push("om_i_8f", "3317077020114001399 退单")
        self.assertEqual(stats.get("refund"), 1)
        self.assertEqual(self._order_of("om_i_8e").order_state, ORDER_STATE_REFUND_REQUEST)

    # ---------------------------------------------------------------- 撤单
    def test_14_natural_cancel_marks_canceled(self):
        self._push("om_i_10", ORDER_TEXT)
        stats, fake = self._push("om_i_11", "这单先取消订单")
        self.assertEqual(stats.get("cancel"), 1)
        order = self._order_of("om_i_10")
        self.assertEqual(order.order_state, ORDER_STATE_CANCELED)
        self.assertTrue(order.is_recalled)
        self.assertIn("群里发消息撤单", order.recall_reason)
        self.assertTrue(any("撤单" in t for t in fake.replies), fake.replies)
        # 撤单的订单不再结算
        with SessionLocal() as s:
            rows = s.scalars(select(Settlement).where(Settlement.order_id == order.id)).all()
            self.assertTrue(rows)
            self.assertTrue(all(r.status == SETTLEMENT_VOID for r in rows))

    def test_15_shipped_order_cannot_be_canceled(self):
        self._push("om_i_12", ORDER_TEXT)
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_i_12"))
            order.tracking_no = "SF999"
            order.order_state = "shipped"
            s.commit()
        stats, fake = self._push("om_i_13", "把这个订单撤单")
        self.assertEqual(stats.get("ignored"), 1, "撤单请求未受理，订单不动")
        order = self._order_of("om_i_12")
        self.assertEqual(order.order_state, "shipped", "已发货不能撤单")
        self.assertFalse(order.is_recalled)
        self.assertTrue(any("不能撤单" in t for t in fake.replies), fake.replies)

    # ---------------------------------------------------------------- 边界
    def test_16_weak_intent_without_context_is_not_acted_on(self):
        """一句话「退了」既没指向订单、发送人名下也没有在途订单：只留痕，不误伤。"""
        self._push("om_i_14", ORDER_TEXT)  # 别的同事发的订单
        stats, _ = self._push("om_i_15", "退了", sender_id="ou_9", sender_name="小C")
        self.assertEqual(stats.get("ignored"), 1)
        self.assertEqual(self._order_of("om_i_14").order_state, ORDER_STATE_NEW)
        message = self._message("om_i_15")
        self.assertEqual(message.intent, "refund")
        self.assertIn("没有指向具体订单", message.intent_note)

    def test_16b_weak_intent_with_single_in_flight_order_is_acted_on(self):
        """一句话「退了」+ 该发送人只有一笔在途订单：敢动，按退单处理。"""
        self._push("om_i_14b", ORDER_TEXT)
        stats, _ = self._push("om_i_15b", "退了")
        self.assertEqual(stats.get("refund"), 1)
        self.assertEqual(self._order_of("om_i_14b").order_state, ORDER_STATE_REFUND_REQUEST)

    def test_16c_unknown_number_is_not_guessed(self):
        """消息里带了单号但库里没这笔：不猜、不兜底 —— 别把正常订单误判成退单。"""
        self._push("om_i_14c", ORDER_TEXT)
        stats, _ = self._push("om_i_15c", "SF0000000000000 这个退单")
        self.assertEqual(stats.get("ignored"), 1)
        self.assertEqual(self._order_of("om_i_14c").order_state, ORDER_STATE_NEW)
        note = self._message("om_i_15c").intent_note
        self.assertIn("订单", note, "要写明为什么没动手、让人补什么信息")

    def test_17_quote_reply_matches_quoted_order(self):
        """回复/引用某条订单消息说「这单退了」：直接认那条消息的订单。"""
        self._push("om_i_16", ORDER_TEXT)
        self._push("om_i_17", ORDER_TEXT_2)
        stats, _ = self._push("om_i_18", "这单退了", parent_message_id="om_i_17")
        self.assertEqual(stats.get("refund"), 1)
        self.assertEqual(self._order_of("om_i_17").order_state, ORDER_STATE_REFUND_REQUEST)
        self.assertEqual(self._order_of("om_i_16").order_state, ORDER_STATE_NEW)

    def test_17b_refund_by_quoted_info_without_order_no(self):
        """引用一段「收货人 / 手机号」的信息（不是模板、没建单）再说「退单」：
        用被引用内容里的手机号定位订单 —— 这是群里最常见的沟通方式。"""
        self._push("om_i_30", ORDER_TEXT)
        # 被引用的那条：不是订单模板，不会被归档成订单，只带手机号
        self._push("om_i_31", "新的收货信息：收货人 蓝曼波 手机 15872093763")
        stats, _ = self._push("om_i_32", "退单", parent_message_id="om_i_31")
        self.assertEqual(stats.get("refund"), 1, "应按被引用消息里的手机号匹配到订单")
        self.assertEqual(
            self._order_of("om_i_30").order_state, ORDER_STATE_REFUND_REQUEST
        )

    def test_17c_feishu_event_keeps_quote_relation(self):
        """飞书事件的「引用 / 回复」关系必须存下来：
        否则「引用原订单 + 退单」就丢了最硬的定位证据。"""
        from app.feishu.events import event_to_message
        from app.feishu.messages import normalize

        raw = event_to_message(
            {
                "message": {
                    "message_id": "om_x_quote",
                    "chat_id": "oc_intent",
                    "message_type": "text",
                    "content": '{"text":"退单"}',
                    "create_time": "1790500877497",
                    "parent_id": "om_i_30",
                },
                "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user"},
            }
        )
        self.assertEqual(normalize(raw)["parent_message_id"], "om_i_30")

    def test_18_ambiguous_multi_order_marks_review(self):
        """同一发送人（运营常同时挂多单）有多笔在途：拿不准是哪一笔 ——
        一笔都不许动，回群问清楚（带订单号或引用原订单消息）。"""
        self._push("om_i_19", ORDER_TEXT)
        self._push("om_i_20", ORDER_TEXT_2)
        with SessionLocal() as s:
            before = {
                o.id: (o.order_state, o.needs_review)
                for o in s.scalars(select(Order).order_by(Order.id)).all()
            }
        stats, fake = self._push("om_i_21", "退单")
        self.assertEqual(stats.get("ignored"), 1, "拿不准就不落订单，只留痕")
        message = self._message("om_i_21")
        self.assertIsNone(message.order_id, "拿不准就不该挂到某笔订单上")
        self.assertTrue(any("引用原订单消息" in t for t in fake.replies), fake.replies)
        with SessionLocal() as s:
            after = {
                o.id: (o.order_state, o.needs_review)
                for o in s.scalars(select(Order).order_by(Order.id)).all()
            }
        self.assertEqual(before, after, "拿不准是哪一笔时，一笔都不许动")

    def test_19_no_match_records_and_asks_for_order_no(self):
        stats, fake = self._push("om_i_22", "这个订单退款")
        self.assertEqual(stats.get("ignored"), 1)
        message = self._message("om_i_22")
        self.assertEqual(message.intent, "refund")
        self.assertIn("没匹配到对应订单", message.intent_note)
        self.assertTrue(any("订单号" in t for t in fake.replies), fake.replies)

    def test_20_refund_by_explicit_order_no(self):
        self._push("om_i_23", ORDER_TEXT)
        order_no = self._order_of("om_i_23").order_no
        self._push("om_i_24", f"订单号 {order_no} 退了")
        self.assertEqual(self._order_of("om_i_23").order_state, ORDER_STATE_REFUND_REQUEST)

    def test_20b_stale_history_message_is_not_acted_on(self):
        """同步进来的历史消息（几十天前发的「不要了」）只留痕：避免误伤现在在途的订单。"""
        self._push("om_i_27", ORDER_TEXT)  # 现在在途的订单
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_i_old",
                    chat_id="oc_intent",
                    msg_type="text",
                    sender_id="ou_1",
                    sender_name="小A",
                    raw_text="这个订单退款",  # 只有发送人兜底，把握不足
                    parse_status="pending",
                    msg_time=now() - timedelta(days=30),
                )
            )
            s.commit()
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            with SessionLocal() as s:
                stats = ingest_service.process_pending(s, client=fake, limit=10)
        self.assertEqual(stats.get("ignored"), 1)
        message = self._message("om_i_old")
        self.assertIn("已过去 30 天", message.intent_note)
        self.assertEqual(message.parse_status, "ignored")
        self.assertEqual(self._order_of("om_i_27").order_state, ORDER_STATE_NEW)

    def test_20c_stale_message_with_order_no_is_still_handled(self):
        """历史消息但写明了订单号（很确定）：照常处理。"""
        self._push("om_i_30", ORDER_TEXT)
        order_no = self._order_of("om_i_30").order_no
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_i_old_2",
                    chat_id="oc_intent",
                    msg_type="text",
                    sender_id="ou_1",
                    sender_name="小A",
                    raw_text=f"订单号 {order_no} 退了",
                    parse_status="pending",
                    msg_time=now() - timedelta(days=20),
                )
            )
            s.commit()
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            with SessionLocal() as s:
                ingest_service.process_pending(s, client=fake, limit=10)
        self.assertEqual(self._order_of("om_i_30").order_state, ORDER_STATE_REFUND_REQUEST)

    def test_21_message_page_shows_intent(self):
        self._push("om_i_25", ORDER_TEXT)
        self._push("om_i_26", "这个单不要了")
        page = self.client.get("/messages?kind=intent")
        self.assertEqual(page.status_code, 200)
        self.assertIn("意图消息", page.text)
        self.assertIn("退单意图", page.text)
        self.assertIn("识别到退单意图", page.text)


class RecallByMessageTest(unittest.TestCase):
    """撤回消息 -> 定位订单 -> 已撤单。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(
        self, order_no: str = "RC-INT-1", source: str = "om_rc_1", with_message: bool = True, **kw
    ) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="吹风机",
                sale_amount=199.0,
                cost_amount=120.0,
                supplier_status="pushed",
                pushed_at=now(),
                source_message_id=source,
                msg_time=now(),
                ordered_at=now(),
                **kw,
            )
            s.add(order)
            s.flush()
            if with_message:
                s.add(
                    RawMessage(
                        message_id=source,
                        chat_id="oc_intent",
                        msg_type="text",
                        sender_id="ou_1",
                        kind="template",
                        parse_status="parsed",
                        raw_text="产品： 吹风机",
                        msg_time=now(),
                        order_id=order.id,
                    )
                )
            s.commit()
            return order.id

    def test_30_recall_marks_order_canceled(self):
        order_id = self._order()
        with SessionLocal() as s:
            out = recall_service.handle_message_recalled(s, "om_rc_1", "oc_intent", "ou_1")
        self.assertEqual(out["marked"], 1)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order.is_recalled)
            self.assertEqual(order.order_state, ORDER_STATE_CANCELED)
            self.assertEqual(order.recall_reason, "群里撤回消息")
        with SessionLocal() as s:
            rows = s.scalars(select(Settlement).where(Settlement.order_id == order_id)).all()
            self.assertTrue(all(r.status == SETTLEMENT_VOID for r in rows), "撤单后不再结算")

    def test_31_recall_without_local_message_still_finds_order(self):
        """本地没存过那条消息（系统上线前发的）：按订单表反查也能定位。"""
        order_id = self._order(
            order_no="RC-INT-2", source="om_rc_never_saved", with_message=False
        )
        with SessionLocal() as s:
            out = recall_service.handle_message_recalled(s, "om_rc_never_saved", "oc_intent", "ou_1")
        self.assertFalse(out["found_message"])
        self.assertEqual(out["marked"], 1)
        with SessionLocal() as s:
            self.assertTrue(s.get(Order, order_id).is_recalled)

    def test_32_recalled_screenshot_child_maps_to_order(self):
        """撤回的是挂在模板消息下的订单截图：也能找到对应订单。"""
        from app.models import OrderImage

        order_id = self._order(order_no="RC-INT-3", source="om_rc_3")
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_rc_3_img",
                    chat_id="oc_intent",
                    msg_type="image",
                    kind="screenshot",
                    parse_status="parsed",
                    parent_message_id="om_rc_3",
                    msg_time=now(),
                )
            )
            s.add(
                OrderImage(
                    order_id=order_id,
                    message_id="om_rc_3_img",
                    file_key="k1",
                    filename="shot.png",
                )
            )
            s.commit()
        with SessionLocal() as s:
            out = recall_service.handle_message_recalled(s, "om_rc_3_img", "oc_intent", "ou_1")
        self.assertEqual(out["marked"], 1)
        with SessionLocal() as s:
            self.assertTrue(s.get(Order, order_id).is_recalled)

    def test_33_pushed_order_recall_keeps_state_only_archived(self):
        """撤回的订单只留档：不参与统计（已推单的撤回后不参与任何流程）。"""
        order_id = self._order(order_no="RC-INT-4", source="om_rc_4")
        with SessionLocal() as s:
            recall_service.handle_message_recalled(s, "om_rc_4", "oc_intent", "ou_1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order.is_recalled)
        # 撤销撤回：状态按事实回到「已推单」，结算流水恢复
        with SessionLocal() as s:
            recall_service.undo_recalled(s, s.get(Order, order_id))
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertFalse(order.is_recalled)
            self.assertEqual(order.order_state, ORDER_STATE_PUSHED)
            self.assertEqual(order.recall_reason, "")


if __name__ == "__main__":
    unittest.main()
