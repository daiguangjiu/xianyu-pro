"""重复订单去重 + 逆向退单。

业务口径（2026-09）：
  1 群里重复发同一笔订单：内容没变 -> 不重复归档/推单/播报，只回群提醒「已收到，内容无变化」；
    内容有变更 -> 按正常流程更新并回执、播报。
  2 逆向退单：群里同步「此单退 / 退单」等消息，匹配原订单后标记为退单待拦截，
    列表与详情最醒目红色标记，并语音播报「有退单订单请及时处理拦截！」，
    订单支持维护拦截处理结果（已拦截 / 已发货无法拦截 / 已退款）。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
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
from app.models import Order, PushBatchItem, RawMessage, VoiceEvent  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402
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
    "出货闲鱼号：珊瑚家的令狐冲\n"
    "回款支付宝：王雨\n"
)

REFUND_TEXT = "此单退\n" + ORDER_TEXT


def _event(message_id: str, content: str) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
            "token": "",
            "app_id": "cli_test",
            "tenant_key": "t",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user", "tenant_key": "t"},
            "message": {
                "message_id": message_id,
                "create_time": str(int(time.time() * 1000)),
                "chat_id": "oc_refund",
                "chat_type": "group",
                "message_type": "text",
                "content": content,
            },
        },
    }


class FakeClient:
    """假飞书客户端：记录下发内容，不真正联网。"""

    enabled = True

    def __init__(self):
        self.replies: list[str] = []
        self.sent: list[str] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append(text)
        return {"ok": True}

    def download_resource(self, *args, **kwargs):
        raise RuntimeError("测试环境不下载资源")

    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> str:
        return ""

    def get_chat_members(self, chat_id: str, page_size: int = 100) -> dict[str, str]:
        # 群成员名单：退单回执靠它把「牛磊」这类姓名解析成 open_id
        return {"ou_1": "范志强", "ou_niulei": "牛磊"}


class RefundTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(PushBatchItem).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(PushBatchItem).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _push(self, message_id: str, text: str) -> tuple[dict, FakeClient]:
        """把一条群消息塞进库里走一遍入库流程，返回 (stats, fake client)。"""
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id=message_id,
                    chat_id="oc_refund",
                    msg_type="text",
                    sender_id="ou_1",
                    raw_text=text,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
        fake = FakeClient()
        # 测试环境默认关闭下发，这里打开以校验回执内容
        with mock.patch.object(settings, "notify_mode", "reply"):
            with SessionLocal() as s:
                stats = ingest_service.process_pending(s, client=fake, limit=10)
        return stats, fake

    def _orders(self):
        with SessionLocal() as s:
            return s.scalars(select(Order).order_by(Order.id)).all()

    # ---------------------------------------------------------------- 退单识别
    def test_01_detect_keywords(self):
        self.assertEqual(refund_service.detect(REFUND_TEXT), "此单退")
        self.assertEqual(refund_service.detect("退单\n产品： x"), "退单")
        self.assertEqual(refund_service.detect(ORDER_TEXT), "", "正常订单不能被判成退单")
        # 正文里出现「退货」不算退单（只看第一行）
        self.assertEqual(refund_service.detect("产品： 吹风机\n备注：七天无理由退货"), "")

    def test_02_refund_marks_original_order(self):
        stats, _ = self._push("om_r_1", ORDER_TEXT)
        self.assertEqual(stats["parsed"], 1)
        self.assertEqual(len(self._orders()), 1)

        stats, fake = self._push("om_r_2", REFUND_TEXT)
        self.assertEqual(stats.get("refund"), 1)
        orders = self._orders()
        self.assertEqual(len(orders), 1, "退单要匹配到原订单，不能新建一笔")

        order = orders[0]
        self.assertEqual(order.order_status, "refund")
        # 还没推给供应商：没有包裹可拦，不能记成「待拦截」（2026-10-02 口径）
        self.assertEqual(order.refund_status, "not_pushed")
        self.assertEqual(order.refund_keyword, "此单退")
        self.assertEqual(order.refund_source_message_id, "om_r_2")
        self.assertTrue(order.is_refund)
        self.assertEqual(order.refund_status_label, "无需拦截（未推单）")

        # 群里回执：@ 发送人确认收到 + @ 牛磊告知处理口径，空行后再列订单明细
        self.assertTrue(fake.replies)
        text = fake.replies[-1]
        lines = text.split("\n")
        self.assertEqual(lines[0], '<at user_id="ou_1"></at> 退单信息已收到！')
        self.assertEqual(
            lines[1],
            '<at user_id="ou_niulei"></at> '
            "这单还没推给供应商，无需继续推单发货，也请发起人安排退款给客户。"
            "如果已退款给客户，也请及时同步！",
        )
        self.assertEqual(lines[2], "", "两个 @ 之后要空一行再列明细")
        self.assertTrue(lines[3].startswith("订单号："), lines[3])
        self.assertIn("处理：", text)

    def test_02b_refund_cc_fallbacks(self):
        """@ 供应链对接人：没配名单就不 @；群里查不到 open_id 时只发文字、不能报错。"""
        self._push("om_r_2b", ORDER_TEXT)
        with mock.patch.object(settings, "refund_cc_names", []):
            _, fake = self._push("om_r_2c", REFUND_TEXT)
        text = fake.replies[-1]
        self.assertIn("退单信息已收到！", text)
        self.assertNotIn("拦截处理！有问题随时沟通", text)

        # 群里没有「牛磊」这个成员：退化为纯文字（@ 不到人也不能影响回执）
        self._push("om_r_2d", ORDER_TEXT)
        with mock.patch.object(settings, "refund_cc_names", ["张三"]):
            _, fake2 = self._push("om_r_2e", REFUND_TEXT)
        text2 = fake2.replies[-1]
        self.assertIn("退单信息已收到！", text2)

    def test_03_refund_voice_event(self):
        self._push("om_r_3", ORDER_TEXT)
        since = self.client.get("/api/voice/pending?since=0").json()["latest_id"]
        self._push("om_r_4", REFUND_TEXT)
        pending = self.client.get(f"/api/voice/pending?since={since}").json()
        kinds = [e["kind"] for e in pending["events"]]
        self.assertIn("refund", kinds)
        event = [e for e in pending["events"] if e["kind"] == "refund"][0]
        self.assertEqual(event["text"], "有退单订单请及时处理拦截！")
        self.assertEqual(
            pending["config"]["templates"]["refund"], "有退单订单请及时处理拦截！"
        )

    def test_04_refund_pages_and_handle(self):
        self._push("om_r_5", ORDER_TEXT)
        self._push("om_r_6", REFUND_TEXT)
        order = self._orders()[0]

        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        self.assertIn("已退单", page.text)
        self.assertIn("row-refund", page.text)
        self.assertIn("待拦截", page.text)

        detail = self.client.get(f"/orders/{order.id}")
        self.assertIn("此订单已退单，请及时处理拦截！", detail.text)

        # 维护拦截处理结果
        resp = self.client.post(
            f"/orders/{order.id}/refund",
            data={"refund_status": "intercepted", "refund_note": "已通知供应商拦截成功"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            saved = s.get(Order, order.id)
            self.assertEqual(saved.refund_status, "intercepted")
            self.assertEqual(saved.refund_note, "已通知供应商拦截成功")
            self.assertTrue(saved.refund_handled_at)
        detail = self.client.get(f"/orders/{order.id}")
        self.assertIn("已拦截", detail.text)

        # 退单订单不参与推单
        with SessionLocal() as s:
            batch = supplier_service.create_batch(s, "BH001", [order.id])
            items = s.scalars(
                select(PushBatchItem).where(PushBatchItem.batch_id == batch.id)
            ).all()
        self.assertEqual(items, [], "退单订单不能推给供应商")

    def test_05_refund_without_original_order_creates_nothing(self):
        """匹配不到原订单：**不新建订单**，消息留痕等人工核对。

        退单是对存量订单的处理，凭空建一笔只会多一笔要人工清理的脏数据。
        """
        stats, fake = self._push("om_r_7", REFUND_TEXT)
        self.assertEqual(self._orders(), [], "匹配不到原订单不能新建订单")
        self.assertEqual(stats.get("ignored"), 1)
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_r_7"))
            self.assertEqual(msg.intent, "refund")
            self.assertIsNone(msg.order_id)
            self.assertIn("没匹配到原订单", msg.ignore_reason)
        # 回执要说明没匹配到，请对方带订单号 / 手机号再发一次
        self.assertTrue(fake.replies)
        self.assertIn("没有匹配到对应订单", fake.replies[-1])
        self.assertIn("不会新建订单", fake.replies[-1])

    def test_05b_refund_matches_by_receiver_and_address(self):
        """按收货人 + 地址也能匹配原订单（产品名写法不同不影响）。"""
        _stats, _fake = self._push("om_r_8", ORDER_TEXT)
        with SessionLocal() as s:
            origin = s.scalar(select(Order))
            origin.product_name = "倍思Q2充电宝 20000毫安"  # 产品名写法与退单消息不同
            s.commit()
            origin_id = origin.id
        # 退单消息：同样的收货人 / 手机 / 地址，产品名写得不一样
        text = REFUND_TEXT.replace("徕芬吹风机LF03 深灰色", "倍思Q2电充电宝 20000毫安 30w")
        stats, _fake = self._push("om_r_9", text)
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_r_9"))
            self.assertEqual(msg.order_id, origin_id, "按手机号 / 收货人 / 地址要能匹配上")
            self.assertEqual(msg.parse_status, "parsed")
            origin = s.get(Order, origin_id)
            self.assertTrue(origin.is_refund)
        self.assertEqual(len(self._orders()), 1, "退单不能新建订单")
        self.assertEqual(stats.get("refund"), 1)

    # ---------------------------------------------------------------- 重复去重
    def test_06_duplicate_without_change_is_ignored(self):
        _stats, _fake = self._push("om_d_1", ORDER_TEXT)
        self.assertEqual(len(self._orders()), 1)

        stats, fake = self._push("om_d_2", ORDER_TEXT)
        orders = self._orders()
        self.assertEqual(len(orders), 1, "内容没变不能重复建单")
        self.assertEqual(stats.get("duplicate"), 1)
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_d_2"))
            self.assertEqual(msg.parse_status, "duplicate")
            self.assertEqual(msg.order_id, orders[0].id)
        # 只提醒「已收到过」，不重复归档
        self.assertTrue(fake.replies)
        self.assertIn("已收到过", fake.replies[-1])
        self.assertIn("未重复归档", fake.replies[-1])

        # 不产生新的播报事件
        pending = self.client.get("/api/voice/pending?since=0").json()
        self.assertEqual(len(pending["events"]), 1, "重复同步不能再播一次")

    def test_07_duplicate_with_change_is_processed(self):
        origin_interval = settings.notify_min_interval_seconds
        settings.notify_min_interval_seconds = 0  # 便于校验「有变更走正常回执」
        try:
            _stats, _fake = self._push("om_c_1", ORDER_TEXT)
            changed = ORDER_TEXT.replace("售价：200", "售价：260")
            stats, fake = self._push("om_c_2", changed)
        finally:
            settings.notify_min_interval_seconds = origin_interval
        self.assertNotIn("duplicate", stats)
        orders = self._orders()
        self.assertEqual(len(orders), 1, "有变更时更新原订单，不新建")
        self.assertAlmostEqual(orders[0].sale_amount, 260.0)
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_c_2"))
            self.assertEqual(msg.parse_status, "parsed")
        # 有变更 -> 正常回执 + 再播一次
        self.assertTrue(fake.replies)
        self.assertIn("已收到并处理", fake.replies[-1])
        pending = self.client.get("/api/voice/pending?since=0").json()
        self.assertEqual(len(pending["events"]), 2)

    def test_08_dedupe_disabled_by_config(self):
        """ORDER_DEDUPE_DAYS=0 时只按订单号比对（不同单不再合并）。"""
        origin = settings.order_dedupe_days
        settings.order_dedupe_days = 0
        try:
            self._push("om_e_1", ORDER_TEXT)
            self._push("om_e_2", ORDER_TEXT)
        finally:
            settings.order_dedupe_days = origin
        # 关掉指纹比对后，没有订单号的两条消息会各建一单（原有的临时编号行为）
        self.assertEqual(len(self._orders()), 2)


if __name__ == "__main__":
    unittest.main()
