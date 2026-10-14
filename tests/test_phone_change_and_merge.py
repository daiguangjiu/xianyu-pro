"""改手机号的消息不能再建一笔新订单 + 人工合并订单的健壮性（2026-10-05 线上问题）。

问题一（截图实证）：群里先发了订单模板，随后有人**回复那条消息**说「这个改一下手机号码」，
再贴一遍完整订单 —— 只有手机号从 13812681375 变成 13912681375。
身份指纹含手机号（dedupe.IDENTITY_KEYS），改了号就认不出是同一单，于是白建了第二笔订单。

现在口径：精确指纹没命中时，再按「产品 + 收货人一致、手机号确实不同」宽松认单
（dedupe.find_by_identity_change），认成同一单的变更 → 手机号更新到原单上，不新建。
「同买家同款两笔真单」的防护照旧（截图内容 / 订单编号对不上就拆成两单）。

问题二：运营在订单详情页点「合并订单」直接报错（500）。真凶是
merge.find_order 对纯数字键无条件 int()：闲鱼订单号是 16~24 位纯数字，
SQLite / PostgreSQL 的 64 位整型装不下 → OverflowError / bigint out of range。
现在只有位数像订单 id（<= 9 位）的才当 id 查；另外资产搬迁后先 flush 再删源单，
避免 delete-orphan 把截图连带删掉 / 抛 StaleDataError。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    OrderImage,
    OrderLog,
    ParseCorrection,
    PushBatchItem,
    RawMessage,
    VoiceEvent,
)
from app.services import dedupe as dedupe_service  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import merge as merge_service  # noqa: E402
from app.services import order_log as order_log_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 线上那条消息的脱敏版本：先发的订单模板
ORDER_V1 = (
    "产品名称：树莓派HAUTE 42 T16 Pro V2 格斗键盘\n"
    "收货人:vege\n"
    "手机号码:13812681375\n"
    "地址信息: 江苏省 苏州市 昆山市 巴城镇 马鞍山西路1058号 昆山登云科技职业学院6号楼\n"
    "货号412412\n"
    "售价： 149\n"
    "\n"
    "成本价： 140\n"
    "所属供应链： JD001\n"
    "出货闲鱼号： 珊瑚家的令狐冲\n"
    "回款支付宝归属人： 王雨\n"
)
# 回复原消息说「这个改一下手机号码」，再贴一遍完整信息（只有手机号变了）
ORDER_V2 = ORDER_V1.replace("13812681375", "13912681375")
ORDER_V2_TEXT = "这个改一下手机号码\n" + ORDER_V2


class FakeClient:
    """假飞书客户端：只记下发内容，不联网。"""

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


class PhoneChangeTest(unittest.TestCase):
    """回复原单说「改一下手机号」→ 更新原单，不再多出一笔订单。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(ParseCorrection).delete()
            s.query(VoiceEvent).delete()
            s.query(OrderImage).delete()
            s.query(PushBatchItem).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _push(self, message_id: str, text: str, parent: str = "") -> FakeClient:
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id=message_id,
                    chat_id="oc_phone",
                    msg_type="text",
                    sender_id="ou_1",
                    sender_name="卫玉珍",
                    parent_message_id=parent,
                    raw_text=text,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
        fake = FakeClient()
        with SessionLocal() as s:
            ingest_service.process_pending(s, client=fake, limit=10)
        return fake

    def _orders(self) -> list[Order]:
        with SessionLocal() as s:
            return list(s.scalars(select(Order).order_by(Order.id)).all())

    # ------------------------------------------------------------ 主场景
    def test_10_phone_change_updates_original_order(self):
        """改手机号的回复消息：更新原单手机号，**不**新建第二笔订单。"""
        self._push("om_p_1", ORDER_V1)
        self._push("om_p_2", ORDER_V2_TEXT, parent="om_p_1")

        orders = self._orders()
        self.assertEqual(len(orders), 1, "改手机号不能再多出一笔订单")
        order = orders[0]
        self.assertEqual(
            dedupe_service.norm_digits(order.receiver_phone),
            "13912681375",
            "新手机号要落到原单上",
        )
        self.assertTrue(order.order_no.startswith("TMP-"), "还没配到截图，仍是临时单号")
        with SessionLocal() as s:
            msg = s.scalar(
                select(RawMessage).where(RawMessage.message_id == "om_p_2")
            )
            self.assertEqual(msg.order_id, order.id, "第二条消息要挂到同一笔订单上")
            logs = s.scalars(
                select(OrderLog).where(
                    OrderLog.order_id == order.id,
                    OrderLog.action == order_log_service.ACTION_IDENTITY_CHANGE,
                )
            ).all()
            self.assertTrue(logs, "按身份信息变更更新要留一条处理日志")
            self.assertIn("13912681375", logs[0].detail)

    def test_15_same_phone_resend_still_no_new_order(self):
        """一模一样再贴一遍（手机号没变）：还走原来的重复口径，也不新建订单。"""
        self._push("om_p_3", ORDER_V1)
        self._push("om_p_4", ORDER_V1)
        self.assertEqual(len(self._orders()), 1)

    def test_20_address_and_phone_change_keep_same_order(self):
        """地址 + 手机号一起改：产品 / 收货人一致，仍按同一单更新，不新建。"""
        self._push("om_p_5", ORDER_V1)
        changed = ORDER_V2.replace("马鞍山西路1058号", "马鞍山西路9999号")
        self._push("om_p_6", changed, parent="om_p_5")
        orders = self._orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].receiver_phone, "13912681375")
        self.assertIn("9999", orders[0].receiver_address, "地址变更要写回原单")

    def test_25_new_person_same_product_makes_new_order(self):
        """产品一样但**收货人和手机号都不是同一人**：不能硬并成一单，照旧新建。"""
        self._push("om_p_7", ORDER_V1)
        other = ORDER_V1.replace("收货人:vege", "收货人:另一个人").replace(
            "13812681375", "15900000000"
        )
        self._push("om_p_8", other)
        self.assertEqual(len(self._orders()), 2, "换了人就是另一笔订单")


class MergeRobustTest(unittest.TestCase):
    """人工合并订单：长数字单号不能崩，资产搬迁不能丢。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        self._clean()
        with SessionLocal() as s:
            self.dst = Order(
                platform="xianyu", order_no="33169016650470478780",
                product_name="树莓派键盘", cost_amount=140.0, order_state="pushed",
            )
            self.src = Order(
                platform="xianyu", order_no="TMP-20261005120000-AAAAAA",
                product_name="树莓派键盘", receiver_name="vege",
                receiver_phone="13912681375", order_state="new",
            )
            s.add_all([self.dst, self.src])
            s.commit()
            self.dst_id, self.src_id = self.dst.id, self.src.id

    def tearDown(self):
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(ParseCorrection).delete()
            s.query(VoiceEvent).delete()
            s.query(OrderImage).delete()
            s.query(PushBatchItem).delete()
            s.query(Order).delete()
            s.commit()

    # ------------------------------------------------------------ 找单
    def test_40_long_digit_order_no_does_not_overflow(self):
        """20 位纯数字订单号：以前 int() 溢出直接 500，现在要正常查到这一单。"""
        with SessionLocal() as s:
            row = merge_service.find_order(s, "33169016650470478780")
            self.assertIsNotNone(row, "长数字订单号要能按订单号查到")
            self.assertEqual(row.id, self.dst_id)

    def test_45_short_digit_still_finds_by_id(self):
        """短数字仍然按订单 id 找（页面上填 id 的老习惯不能丢）。"""
        with SessionLocal() as s:
            row = merge_service.find_order(s, str(self.src_id))
            self.assertEqual(row.id, self.src_id)

    def test_50_bad_key_returns_none_and_page_hints(self):
        """乱填（超长数字 / 不存在的号）：不报 500，页面给「没找到订单」提示。"""
        resp = self.client.post(
            f"/orders/{self.dst_id}/merge",
            data={"merge_order_no": "99999999999999999999"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, "不能是 500")

    # ------------------------------------------------------------ 资产搬迁
    def test_60_merge_moves_assets_without_losing_them(self):
        """源单的截图 / 语音事件 / 纠错样本都要跟着搬到主单，不能丢也不能悬空。"""
        with SessionLocal() as s:
            src = s.get(Order, self.src_id)
            s.add(
                OrderImage(
                    order_id=self.src_id, message_id="om_shot", file_key="fk_1",
                    filename="shot.png", local_path="/tmp/not-exist-shot.png",
                )
            )
            s.add(
                VoiceEvent(
                    kind="order", order_id=self.src_id, order_no=src.order_no, text="新订单"
                )
            )
            s.add(
                ParseCorrection(
                    order_id=self.src_id, order_no=src.order_no, platform="xianyu",
                    field_key="cost_amount", machine_value="0", human_value="140",
                    source_line="成本价： 140",
                )
            )
            s.commit()

        ok, msg = None, ""
        with SessionLocal() as s:
            src = s.get(Order, self.src_id)
            dst = s.get(Order, self.dst_id)
            ok, msg = merge_service.merge_orders(s, src, dst, actor="测试")
            s.commit()
        self.assertTrue(ok, msg)
        with SessionLocal() as s:
            self.assertIsNone(s.get(Order, self.src_id), "源单要删掉")
            dst = s.get(Order, self.dst_id)
            self.assertEqual(dst.receiver_phone, "13912681375", "收货信息并过来")
            imgs = s.scalars(
                select(OrderImage).where(OrderImage.order_id == self.dst_id)
            ).all()
            self.assertEqual(len(imgs), 1, "截图要搬到主单，不能被 delete-orphan 删掉")
            voice = s.scalars(select(VoiceEvent).where(VoiceEvent.order_id == self.dst_id)).all()
            self.assertEqual(len(voice), 1, "语音事件不能留下指向已删订单的悬空引用")
            fixes = s.scalars(
                select(ParseCorrection).where(ParseCorrection.order_id == self.dst_id)
            ).all()
            self.assertEqual(len(fixes), 1, "纠错样本要跟着订单走")

    def test_65_merge_page_returns_friendly_message(self):
        """整条页面路径跑通：填被合并掉的临时单号，也能追到主单并给出提示。"""
        with SessionLocal() as s:
            src = s.get(Order, self.src_id)
            dst = s.get(Order, self.dst_id)
            merge_service.merge_orders(s, src, dst, actor="测试")
            s.commit()
        # 再用同一个临时单号去合并：应提示「不能和自己合并」，而不是抛异常
        resp = self.client.post(
            f"/orders/{self.dst_id}/merge",
            data={"merge_order_no": "TMP-20261005120000-AAAAAA"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)


if __name__ == "__main__":
    unittest.main()
