"""解析策略（人工标记学习）：通告类消息不再误判成订单，人工标记能改数据并学到话术。

业务背景（2026-09 线上出过的事）：
群里有人发「戴森的产品大家注意下，现在只能发全新未拆封，无理由退货会有30的费用」，
老规则里商品名的正则允许「产品」后面不带冒号，于是这整句通知被当成商品名、归档成了订单。
现在：① 商品名必须带冒号；② 消息要有模板结构（标签行 / 收货信息）才算订单；
③ 万一还判错，运营可以在飞书消息页标「不是订单」——订单撤单留档，同时学到话术，
下一条同类通告不用再人工标。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试环境（NOTIFY_MODE=off，不发真实回执）

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    ParseFeedback,
    ParsePhrase,
    RawMessage,
    Settlement,
    VoiceEvent,
)
from app.services import ingest as ingest_service  # noqa: E402
from app.services import policy as policy_service  # noqa: E402
from app.services.filter import is_template_message  # noqa: E402
from app.services.ingest import process_pending  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 线上误判的那条通告（提到「产品」，但整条是通知）
ANNOUNCE = "戴森的产品大家注意下，现在只能发全新未拆封，无理由退货会有30的费用"

TEMPLATE = (
    "产品：亚朵四代单枕头\n"
    "收货人:贲\n"
    "手机号码:14790021215\n"
    "地址信息: 安徽省 滁州市 琅琊区 清流街道\n"
    "售价：115\n"
    "出货闲鱼号：卖货的小姑娘\n"
    "回款支付宝归属人：卫玉珍\n"
)

# 简版模板：只有标签行、没有收货信息（老规则会漏判，靠人工标「其实是订单」补回来）
SHORT_TEMPLATE = "产品：戴森吹风机\n备注：客户要黑色"

# 群里发的退单消息：首行「此单退」，其余字段与正常订单一致 —— 这是退单，不是新订单
REFUND_TEMPLATE = (
    "此单退\n"
    "产品名称：倍思Q2电充电宝 20000毫安 30w  q2电 自带线（双C口）\n"
    "收货人： 李女士\n"
    "手机号码： 18303136964\n"
    "地址信息： 河北省张家口市万全区孔家庄镇同城雅居1号楼一单元\n"
    "备注： 蓝色\n"
    "售价：50\n"
    "成本价：45\n"
    "所属供应链：BH001\n"
    "出货闲鱼号：允川不吃大米饭\n"
    "回款支付宝归属人：范志强\n"
)
# 同一笔订单的正常模板消息（退单要匹配到的原订单）
REFUND_ORIGINAL = REFUND_TEMPLATE.replace("此单退\n", "")
# 规则词库里没有的退单说法（第一次会被当成新订单，人工标一次之后就学会了）
UNKNOWN_REFUND_WORDING = "这单申请售后"


class AnnounceTest(unittest.TestCase):
    """通告 / 闲聊不能被判成订单。"""

    def setUp(self) -> None:
        init_db()

    def test_01_announcement_is_not_order(self):
        self.assertFalse(is_template_message(ANNOUNCE))

    def test_02_template_is_order(self):
        self.assertTrue(is_template_message(TEMPLATE))

    def test_03_product_without_colon_is_not_order(self):
        # 「产品」后面没有冒号：后面那串是聊天内容，不是商品名
        self.assertFalse(is_template_message("戴森的产品现在只能发全新未拆封"))
        # 带冒号 + 有第二个标签行：正常订单
        self.assertTrue(
            is_template_message("产品：戴森吹风机\n售价：300")
        )

    def test_04_pipeline_ignores_announcement(self):
        """端到端：通告走一遍解析流程后不产生订单，也按非订单处理。"""
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
            s.add(
                RawMessage(
                    message_id="om_announce_1",
                    chat_id="oc_policy",
                    msg_type="text",
                    sender_id="ou_niulei",
                    sender_name="牛磊",
                    raw_text=ANNOUNCE,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
            process_pending(s, limit=5)
            s.commit()
            self.assertEqual(s.scalar(select(Order.id)) is None, True, "通告不该归档成订单")
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_announce_1"))
            # 通告里带「无理由退货」字样，可能被当成弱退单意图留痕（ignored），
            # 但绝不能归档成订单；人工标记过一次之后连意图都不会再识别（见 MarkFlowTest）
            self.assertIn(msg.parse_status, ("filtered", "ignored"))
            self.assertIsNone(msg.order_id)


class MarkFlowTest(unittest.TestCase):
    """消息页的人工标记：改数据 + 学策略。"""

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
            s.query(ParsePhrase).delete()
            s.query(ParseFeedback).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        policy_service.invalidate()

    # ---------------------------------------------------------------- 工具
    def _wrong_order(self, *, message_id: str = "om_dyson_1", text: str = ANNOUNCE) -> tuple[int, int]:
        """造一条「误判归档」的消息 + 订单（模拟老规则把通告当订单）。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=f"TMP-{message_id}-X",
                product_name=text[:40],
                sale_amount=0.0,
                cost_amount=0.0,
            )
            s.add(order)
            s.flush()
            msg = RawMessage(
                message_id=message_id,
                chat_id="oc_policy",
                msg_type="text",
                sender_id="ou_niulei",
                sender_name="牛磊",
                raw_text=text,
                parse_status="parsed",
                kind="template",
                order_id=order.id,
                msg_time=now(),
            )
            s.add(msg)
            s.commit()
            return order.id, msg.id

    def _filtered_message(self, *, message_id: str = "om_short_1", text: str = SHORT_TEMPLATE) -> int:
        with SessionLocal() as s:
            msg = RawMessage(
                message_id=message_id,
                chat_id="oc_policy",
                msg_type="text",
                sender_id="ou_seller",
                sender_name="小A",
                raw_text=text,
                parse_status="filtered",
                kind="other",
                ignore_reason="不是订单模板消息，也没有识别到退单 / 撤单意图（闲聊 / 通知 / 无关文本）",
                msg_time=now(),
            )
            s.add(msg)
            s.commit()
            return msg.id

    def _msg(self, message_id: str) -> RawMessage:
        with SessionLocal() as s:
            return s.scalar(select(RawMessage).where(RawMessage.message_id == message_id))

    # ---------------------------------------------------------------- 不是订单
    def test_10_mark_not_order_recalls_order_and_learns(self):
        order_id, _ = self._wrong_order()
        resp = self.client.post(
            "/messages/om_dyson_1/mark-not-order", follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order.is_recalled, "误判归档的订单要撤单留档")
            self.assertEqual(order.recall_reason, "人工标记：这条消息不是订单（解析误判归档）")
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_dyson_1"))
            self.assertEqual(msg.parse_status, "filtered")
            self.assertEqual(msg.kind, "other")
            self.assertIn("人工标记", msg.ignore_reason)
            # 学到话术：短句进排除词，通告类提示词也进
            phrases = [
                p.phrase
                for p in s.scalars(select(ParsePhrase).where(ParsePhrase.kind == "exclude")).all()
            ]
            self.assertIn("现在只能发全新未拆封", phrases)
            self.assertIn("大家注意", phrases)
            self.assertEqual(
                s.scalar(select(ParseFeedback).where(ParseFeedback.message_id == "om_dyson_1")) is not None,
                True,
            )

    def test_11_learned_sample_blocks_same_message(self):
        """同一条消息（重复同步 / 转发）直接按人工结论处理。"""
        self._wrong_order()
        self.client.post("/messages/om_dyson_1/mark-not-order", follow_redirects=False)
        with SessionLocal() as s:
            verdict, reason, exact = policy_service.decide(s, ANNOUNCE)
        self.assertEqual(verdict, policy_service.VERDICT_EXCLUDE)
        self.assertTrue(exact, "同一句话属于精确样本")
        self.assertIn("人工标记过", reason)

    def test_12_learned_phrase_blocks_two_line_announcement(self):
        """学到的话术能泛化：两行「产品 + 备注」的广告消息也被过滤。

        （三行以上 / 带收货信息的真模板不受影响 —— 见 test_13）
        """
        with SessionLocal() as s:
            s.add(ParsePhrase(phrase="全新未拆封", kind=policy_service.KIND_EXCLUDE, source="learn"))
            s.commit()
        policy_service.invalidate()
        self.assertFalse(is_template_message("产品：戴森吹风机\n备注：全新未拆封"))
        # 排除词是泛化推测，只在消息不像订单模板时才生效：真模板的备注里写了同一句也不受影响
        self.assertTrue(
            is_template_message(
                "产品：戴森吹风机\n收货人：张三\n手机号码：13800138000\n备注：全新未拆封"
            )
        )

    def test_13_learned_exclude_skips_intent_detection(self):
        """标记过一次之后，同类通告直接过滤，连「退单意图」都不会再识别。"""
        self._wrong_order()
        self.client.post("/messages/om_dyson_1/mark-not-order", follow_redirects=False)
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_dyson_2",
                    chat_id="oc_policy",
                    msg_type="text",
                    sender_id="ou_niulei",
                    sender_name="牛磊",
                    # 换了说法的同类通告：命中「大家注意」「退货会有」等学到的话术
                    raw_text="大家注意，戴森的产品无理由退货会有30的费用",
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
            process_pending(s, limit=5)
            s.commit()
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_dyson_2"))
            self.assertEqual(msg.parse_status, "filtered")
            self.assertEqual(msg.intent, "")
            self.assertIn("人工策略排除", msg.ignore_reason)

    # ---------------------------------------------------------------- 其实是订单
    def test_20_mark_order_archives_and_learns(self):
        self._filtered_message()
        resp = self.client.post("/messages/om_short_1/mark-order", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        msg = self._msg("om_short_1")
        self.assertIsNotNone(msg.order_id, "人工确认是订单后要重新解析归档")
        self.assertEqual(msg.parse_status, "parsed")
        with SessionLocal() as s:
            order = s.get(Order, msg.order_id)
            self.assertEqual(order.product_name, "戴森吹风机")
            # 这条只有「产品 / 备注」两个弱标签，学不到特征词，靠精确样本生效
            force = [p.phrase for p in s.scalars(select(ParsePhrase)).all() if p.kind == "force"]
        self.assertNotIn("产品", force)
        self.assertTrue(is_template_message(SHORT_TEMPLATE))

    def test_21_mark_order_learns_strong_label_only(self):
        """订单侧只学强结构标签：闲聊里也会出现的「产品」不能当特征词。"""
        self._filtered_message(message_id="om_short_2", text="产品：戴森吹风机\n收货人：张三")
        self.client.post("/messages/om_short_2/mark-order", follow_redirects=False)
        with SessionLocal() as s:
            force = [p.phrase for p in s.scalars(select(ParsePhrase)).all() if p.kind == "force"]
        self.assertIn("收货人", force)
        self.assertNotIn("产品", force)

    # ---------------------------------------------------------------- 策略维护
    def test_30_manual_phrase_add_and_delete(self):
        # 两行简版消息（没有收货信息）：规则会当订单，命中排除词后要过滤
        text = "产品：戴森吹风机\n备注：无理由退货会有30的费用"
        self.assertTrue(is_template_message(text))
        resp = self.client.post(
            "/messages/policy/phrase",
            data={"phrase": "无理由退货会有30的费用", "kind": "exclude"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.scalar(select(ParsePhrase).where(ParsePhrase.kind == "exclude"))
            self.assertIsNotNone(row)
            self.assertEqual(row.source, "manual")
            phrase_id = row.id
        self.assertFalse(is_template_message(text), "命中学到的排除词要过滤")
        # 删掉后不再生效
        self.client.post(f"/messages/policy/phrase/{phrase_id}/delete", follow_redirects=False)
        with SessionLocal() as s:
            self.assertIsNone(s.get(ParsePhrase, phrase_id))
        self.assertTrue(is_template_message(text))

    def test_31_template_label_cannot_be_exclude_phrase(self):
        """「产品」这种模板标签不能当排除词，否则真订单会被全过滤掉。"""
        resp = self.client.post(
            "/messages/policy/phrase",
            data={"phrase": "产品", "kind": "exclude"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.scalar(select(ParsePhrase)))

    def test_32_undo_feedback(self):
        self._wrong_order()
        self.client.post("/messages/om_dyson_1/mark-not-order", follow_redirects=False)
        with SessionLocal() as s:
            feedback = s.scalar(select(ParseFeedback))
            feedback_id = feedback.id
        self.client.post(
            f"/messages/policy/feedback/{feedback_id}/delete", follow_redirects=False
        )
        with SessionLocal() as s:
            self.assertIsNone(s.get(ParseFeedback, feedback_id))
            verdict, _reason, exact = policy_service.decide(s, ANNOUNCE)
        self.assertFalse(exact, "撤销后不再是精确样本")

    # ---------------------------------------------------------------- 页面
    def test_40_message_page_shows_policy_and_buttons(self):
        self._wrong_order()
        page = self.client.get("/messages")
        self.assertEqual(page.status_code, 200)
        self.assertIn("解析策略（人工学习）", page.text)
        self.assertIn("不是订单", page.text)
        self.assertIn("其实是订单", page.text)

        # 标记后：订单显示已撤单，策略区展示学到的话术与标记记录
        self.client.post("/messages/om_dyson_1/mark-not-order", follow_redirects=False)
        page = self.client.get("/messages")
        self.assertIn("已撤单", page.text)
        self.assertIn("现在只能发全新未拆封", page.text)
        self.assertIn("牛磊", page.text)


class RefundFlowTest(unittest.TestCase):
    """模板消息 + 退单意图：按退单处理存量订单，不新建订单；人工标记后学会新说法。"""

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
            s.query(ParsePhrase).delete()
            s.query(ParseFeedback).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        policy_service.invalidate()

    def _push(self, message_id: str, text: str) -> RawMessage:
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id=message_id,
                    chat_id="oc_refund",
                    msg_type="text",
                    sender_id="ou_seller",
                    sender_name="小A",
                    raw_text=text,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
            process_pending(s, limit=5)
            s.commit()
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
            s.expunge(msg)
            return msg

    def _msg(self, message_id: str) -> RawMessage:
        with SessionLocal() as s:
            return s.scalar(select(RawMessage).where(RawMessage.message_id == message_id))

    def test_50_refund_matches_original_order(self):
        """「此单退」的模板消息：按手机号 / 收货人 / 地址找到原订单退掉，不新建订单。"""
        self._push("om_orig", REFUND_ORIGINAL)
        with SessionLocal() as s:
            origin = s.scalar(select(Order))
            self.assertIsNotNone(origin)
            origin_id = origin.id
        msg = self._push("om_refund_1", REFUND_TEMPLATE)
        self.assertEqual(msg.order_id, origin_id, "要匹配到原订单")
        self.assertEqual(msg.intent, "refund")
        self.assertEqual(msg.parse_status, "parsed")
        with SessionLocal() as s:
            self.assertEqual(len(s.scalars(select(Order)).all()), 1, "退单不能新建订单")
            origin = s.get(Order, origin_id)
            self.assertTrue(origin.is_refund)

    def test_51_refund_without_original_creates_nothing(self):
        """没有原订单可匹配：不新建订单，消息留痕等人工核对。"""
        msg = self._push("om_refund_2", REFUND_TEMPLATE)
        self.assertEqual(msg.parse_status, "ignored")
        self.assertIsNone(msg.order_id)
        self.assertEqual(msg.intent, "refund")
        self.assertIn("没匹配到原订单", msg.ignore_reason)
        with SessionLocal() as s:
            self.assertEqual(len(s.scalars(select(Order)).all()), 0)

    def _refund_text(self, word: str, product: str) -> str:
        """退单消息：换掉说法与产品名写法（产品名不同 => 不会被判成同一单）。"""
        return (word + "\n" + REFUND_ORIGINAL).replace(
            "倍思Q2电充电宝 20000毫安 30w  q2电 自带线（双C口）", product
        )

    def test_60_mark_refund_recalls_wrong_order_and_refunds_original(self):
        """人工标「其实是退单」：撤掉误建的订单 + 给原订单标记退单 + 学会这句话的说法。"""
        self._push("om_orig2", REFUND_ORIGINAL)
        with SessionLocal() as s:
            origin_id = s.scalar(select(Order)).id
        # 词库里没有的退单说法：第一次被当成新订单（产品名写法不同）
        wrong_msg = self._push(
            "om_refund_3", self._refund_text(UNKNOWN_REFUND_WORDING, "倍思Q2充电宝 20000毫安")
        )
        wrong_order_id = wrong_msg.order_id
        self.assertIsNotNone(wrong_order_id, "新说法会被当成新订单归档")
        self.assertNotEqual(wrong_order_id, origin_id)

        resp = self.client.post("/messages/om_refund_3/mark-refund", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            wrong = s.get(Order, wrong_order_id)
            self.assertTrue(wrong.is_recalled, "误建的订单要撤单留档")
            origin = s.get(Order, origin_id)
            self.assertTrue(origin.is_refund, "原订单要标记退单")
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "om_refund_3"))
            self.assertEqual(msg.order_id, origin_id)
            self.assertEqual(msg.intent, "refund")
            refund_phrases = [
                p.phrase for p in s.scalars(select(ParsePhrase)).all() if p.kind == "refund"
            ]
        self.assertIn(UNKNOWN_REFUND_WORDING, refund_phrases, "要记住这句退单说法")

    def test_61_learned_refund_wording_is_reused(self):
        """学会之后：同样的说法再发一条，直接按退单处理原订单，不再新建订单。"""
        self._push("om_orig3", REFUND_ORIGINAL)
        with SessionLocal() as s:
            origin_id = s.scalar(select(Order)).id
        self._push(
            "om_refund_4", self._refund_text(UNKNOWN_REFUND_WORDING, "倍思Q2充电宝 20000毫安")
        )
        self.client.post("/messages/om_refund_4/mark-refund", follow_redirects=False)
        # 换一条同说法的退单消息：这次不靠人工标记，靠学到的策略
        msg = self._push(
            "om_refund_5",
            self._refund_text(UNKNOWN_REFUND_WORDING, "倍思Q2充电宝 20000毫安 30w"),
        )
        self.assertEqual(
            msg.order_id,
            origin_id,
            f"学到的退单话术要能直接匹配原订单（{msg.parse_status} / {msg.intent} / {msg.ignore_reason} / {msg.intent_note}）",
        )
        self.assertEqual(msg.intent, "refund")
        with SessionLocal() as s:
            alive = [o for o in s.scalars(select(Order)).all() if not o.is_recalled]
            self.assertEqual(len(alive), 1, "退单不新建订单（误建的那笔已撤单留档）")
            self.assertTrue(s.get(Order, origin_id).is_refund)


if __name__ == "__main__":
    unittest.main()
