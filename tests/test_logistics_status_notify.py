"""物流状态体系 + 状态通知 + 独立定时服务。

口径（2026-10-13 用户拍板）：
    · 物流状态六个正向：待揽收 → 已揽收 → 运输中 → 派送中 → 待取件 → 已签收；
      三个异常分支：派送失败 / 退回中 / 已退回。状态从菜鸟状态码（result.data.lastState）
      或最新一条轨迹描述里认，认不出就留空。
    · **状态一变就通知订单发送人**（飞书私聊，收件人是 Order.sender_id 这个 open_id），
      同一单号的同一个状态**只通知一次**。
    · 刷新轨迹的活交给**独立进程**（scripts/logistics_worker.py），它挂了只是状态不自动
      更新，不影响订单 / 发货这些日常业务。
    · 顺丰这类「菜鸟这边查不到」的，给人指一条官网查询的路（实测顺丰能查，所以只在
      真查不到时才提示）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import LogisticsNotify, LogisticsTrace, Order  # noqa: E402
from app.services import logistics as cainiao  # noqa: E402
from app.services import logistics_notify as notify  # noqa: E402
from app.services import logistics_trace as trace_service  # noqa: E402

MAIL = "SF1234567890"


def _body(state: str, desc: str = "快件已到达") -> str:
    data = json.dumps(
        {"lastState": state, "detail": [{"opTime": "2026-10-13 12:00:00", "opMessage": desc}]},
        ensure_ascii=False,
    )
    return json.dumps({"result": {"code": "0", "data": data}}, ensure_ascii=False)


class _Resp:
    status_code = 200

    def __init__(self, text: str):
        self.text = text


class StatusTest(unittest.TestCase):
    """状态怎么认出来的（状态码 + 描述兜底）。"""

    def test_10_labels_cover_all_states(self):
        """六个正向 + 三个异常，一个都不能少（页面与通知都靠它）。"""
        for key in ("pending_pickup", "accepted", "transit", "delivering",
                    "waiting_pickup", "signed", "delivery_failed", "returning", "returned"):
            self.assertIn(key, cainiao.STATUS_LABELS)
            self.assertTrue(cainiao.status_label(key))
        self.assertEqual(cainiao.status_label("signed"), "已签收")
        self.assertEqual(cainiao.status_label("waiting_pickup"), "待取件")

    def test_20_abnormal_flags(self):
        self.assertTrue(cainiao.is_abnormal("delivery_failed"))
        self.assertTrue(cainiao.is_abnormal("returning"))
        self.assertTrue(cainiao.is_abnormal("returned"))
        self.assertFalse(cainiao.is_abnormal("signed"))
        self.assertFalse(cainiao.is_abnormal("transit"))

    def test_30_status_code_mapping(self):
        """菜鸟状态码 → 我们的状态（含新加的待揽收 / 已揽收 / 待取件 / 派送失败）。"""
        cases = {
            "CREATED": "pending_pickup",
            "NOT_SEND": "pending_pickup",
            "GOT": "accepted",
            "ACCEPTED": "accepted",
            "TRANSIT": "transit",
            "DELIVERING": "delivering",
            "WAIT_PICKUP": "waiting_pickup",
            "STATION": "waiting_pickup",
            "SIGN": "signed",
            "DELIVERY_FAILED": "delivery_failed",
            "RETURNING": "returning",
            "RETURNED": "returned",
        }
        for raw, want in cases.items():
            self.assertEqual(cainiao.STATUS_MAP.get(raw), want, raw)

    def test_40_map_status_from_last_state(self):
        ok, msg, payload = cainiao._parse_response(_body("SIGN"))
        self.assertTrue(ok, msg)
        self.assertEqual(cainiao.map_status(payload), "signed")

    def test_50_desc_fallback_order(self):
        """描述兜底：**派送失败**不能被「派送」吃掉，「已退回」不能被「退回」吃掉。"""
        self.assertEqual(cainiao.status_from_desc("派送失败，联系不上收件人"), "delivery_failed")
        self.assertEqual(cainiao.status_from_desc("快件已退回"), "returned")
        self.assertEqual(cainiao.status_from_desc("退回中"), "returning")
        self.assertEqual(cainiao.status_from_desc("请到菜鸟驿站取件"), "waiting_pickup")
        self.assertEqual(cainiao.status_from_desc("快递员已揽收"), "accepted")
        self.assertEqual(cainiao.status_from_desc("商品已下单，等待揽收"), "pending_pickup")
        self.assertEqual(cainiao.status_from_desc("正在派送中"), "delivering")

    def test_60_official_site_fallback(self):
        """菜鸟查不到时给官网入口（顺丰有，认不出的没有）。"""
        name, url = cainiao.official_site("SF")
        self.assertEqual(name, "顺丰速运")
        self.assertIn("sf-express.com", url)
        self.assertEqual(cainiao.official_site("", "顺丰速运")[0], "顺丰速运")
        self.assertEqual(cainiao.official_site("XXX", "没听过的快递"), ("", ""))


class NotifyTest(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self._wipe()
        self.db.add(
            Order(order_no="T-NOTIFY-1", tracking_no=MAIL, sender_id="ou_sender_1",
                  sender_name="张三")
        )
        self.db.commit()
        # 飞书是否启用是 settings 上的只读属性，测试里直接放行发送环节
        self._ready = mock.patch.object(notify, "_feishu_ready", return_value=True)
        self._ready.start()
        self._on = settings.logistics_notify_enabled
        settings.logistics_notify_enabled = True

    def tearDown(self):
        settings.logistics_notify_enabled = self._on
        self._ready.stop()
        self._wipe()
        self.db.close()

    def _wipe(self):
        self.db.query(LogisticsNotify).delete()
        self.db.query(LogisticsTrace).delete()
        self.db.query(Order).filter(Order.order_no.like("T-NOTIFY-%")).delete()
        self.db.commit()

    def test_70_notifies_sender_on_change(self):
        """状态变了 → 飞书发给订单发送人（open_id）。"""
        with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
            out = notify.notify_change(
                self.db, MAIL, "transit", "delivering",
                nodes=[{"time": "2026-10-13 12:00", "desc": "派送中"}], cp_name="顺丰速运",
            )
        self.assertTrue(out["sent"])
        self.assertEqual(send.call_args[0][0], "ou_sender_1")  # 发给发送人本人
        self.assertIn("运输中 → 派送中", out["text"])
        self.assertIn("物流更新", out["text"])
        self.assertEqual(self.db.query(LogisticsNotify).count(), 1)

    def test_72_abnormal_title(self):
        """异常分支的标题要让人一眼看出出事了。"""
        with mock.patch.object(notify, "_send", return_value=(True, "")):
            out = notify.notify_change(self.db, MAIL, "delivering", "delivery_failed")
        self.assertIn("物流异常", out["text"])
        row = self.db.query(LogisticsNotify).one()
        self.assertTrue(row.abnormal)

    def test_74_notifies_only_once_per_status(self):
        """同一单号的同一状态只通知一次（刷新一百遍也不重复发）。"""
        with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
            notify.notify_change(self.db, MAIL, "transit", "delivering")
            notify.notify_change(self.db, MAIL, "transit", "delivering")
            self.assertEqual(send.call_count, 1)
            # 换成别的状态就该再发
            notify.notify_change(self.db, MAIL, "delivering", "signed")
            self.assertEqual(send.call_count, 2)

    def test_78_text_carries_receiver_and_blocks(self):
        """通知文案（2026-10-13 改版）：带收件人信息，订单 / 快递 / 轨迹三块分开排。"""
        self.db.add(
            Order(
                order_no="T-NOTIFY-3",
                tracking_no="YT-NOTIFY-1",
                sender_id="ou_sender_1",
                product_name="摩飞电器破壁机",
                spec="白色",
                receiver_name="李四",
                receiver_phone="13800000001",
                receiver_address="广东省深圳市南山区科技园路 1 号 A 座 1203",
            )
        )
        self.db.commit()
        with mock.patch.object(notify, "_send", return_value=(True, "")):
            out = notify.notify_change(
                self.db,
                "YT-NOTIFY-1",
                "transit",
                "signed",
                nodes=[
                    {"time": "2026-10-13 15:20:00", "desc": "已签收，签收人：本人"},
                    {"time": "2026-10-13 09:12:00", "desc": "快件已到达深圳南山中转场"},
                    {"time": "2026-10-12 21:03:00", "desc": "已揽收"},
                ],
                cp_name="中通快递",
            )
        text = out["text"]
        # ① 订单本体：产品 / 收货人 / 手机号 / 地址，顺序照用户给的模板
        self.assertIn("产品：摩飞电器破壁机（白色）", text)
        self.assertIn("收货人：李四", text)
        self.assertIn("手机号码：13800000001", text)
        self.assertIn("地址信息：广东省深圳市南山区科技园路 1 号 A 座 1203", text)
        self.assertLess(text.index("产品："), text.index("收货人："))
        self.assertLess(text.index("收货人："), text.index("手机号码："))
        # ② 快递那块跟订单信息之间空一行（别堆一起）
        self.assertIn("中通快递 YT-NOTIFY-1", text)
        self.assertIn("状态：运输中 → 已签收", text)
        self.assertIn("\n\n中通快递", text)
        # ③ 轨迹：一行一条，时间是短的（不带年份）
        self.assertIn("物流轨迹：", text)
        self.assertIn("10-13 15:20  已签收，签收人：本人", text)
        self.assertIn("10-12 21:03  已揽收", text)
        self.assertNotIn("2026-10-13 15:20:00", text, "轨迹时间要缩短")

    def test_79_text_without_order_still_readable(self):
        """没有关联订单（比如渔集出库单）时，文案照样通顺，不留半截冒号。"""
        text = notify.build_text(
            order_no="", mail_no="SF0001", cp_name="顺丰速运",
            from_status="", to_status="transit",
            nodes=[{"time": "2026-10-13 12:00:00", "desc": "运输中"}],
        )
        self.assertIn("【物流更新】", text)
        self.assertIn("顺丰速运 SF0001", text)
        self.assertIn("状态：未查询 → 运输中", text)
        self.assertNotIn("产品：", text)
        self.assertNotIn("收货人：", text)

    def test_76_no_sender_skips(self):
        """订单没记发送人（比如渔集出库单）→ 记一笔原因，不发。"""
        self.db.add(Order(order_no="T-NOTIFY-2", tracking_no="NO-SENDER-1"))
        self.db.commit()
        with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
            # 用异常件测（首次正向状态本来就不通知）
            out = notify.notify_change(self.db, "NO-SENDER-1", "", "delivery_failed")
        self.assertFalse(out["sent"])
        self.assertEqual(send.call_count, 0)
        self.assertIn("发送人", self.db.query(LogisticsNotify).one().error)

    def test_78_switch_off(self):
        settings.logistics_notify_enabled = False
        with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
            out = notify.notify_change(self.db, MAIL, "", "transit")
        self.assertFalse(out["sent"])
        self.assertEqual(send.call_count, 0)

    def test_79_first_seen_normal_status_keeps_quiet(self):
        """第一次跟踪到历史单（已签收）不该打扰人；异常件要立刻通知。"""
        with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
            out = notify.notify_change(self.db, MAIL, "", "signed")
            self.assertFalse(out["sent"])
            self.assertIn("首次跟踪", out["reason"])
            self.assertEqual(send.call_count, 0)
            # 异常件（派送失败）第一次就得通知
            out2 = notify.notify_change(self.db, MAIL, "", "delivery_failed")
            self.assertTrue(out2["sent"])
            self.assertEqual(send.call_count, 1)

    def test_80_trace_triggers_notify(self):
        """走真实链路：状态变了才通知，同一状态再查不再发。"""
        self._wipe()
        self.db.add(
            Order(order_no="T-NOTIFY-1", tracking_no=MAIL, sender_id="ou_sender_1",
                  sender_name="张三")
        )
        self.db.commit()
        saved = (settings.cainiao_enabled, settings.cainiao_app_secret,
                 settings.cainiao_cp_code, settings.cainiao_gateway_url,
                 settings.cainiao_msg_type)
        settings.cainiao_enabled = True
        settings.cainiao_app_secret = "s" * 32
        settings.cainiao_cp_code = "c" * 32
        settings.cainiao_gateway_url = "http://cainiao.test/link.do"
        settings.cainiao_msg_type = "CNTECH_LV_LOGISTICS_DETAIL_GET"
        try:
            with mock.patch(
                "app.services.logistics.requests.post",
                side_effect=[_Resp(_body("SIGN")), _Resp(_body("DELIVERING"))],
            ):
                with mock.patch.object(notify, "_send", return_value=(True, "")) as send:
                    out = trace_service.lookup(self.db, MAIL)
                    self.db.commit()
                    self.assertEqual(out["status"], "signed")
                    self.assertEqual(send.call_count, 0)  # 历史已签收单：首次不打扰
                    out2 = trace_service.lookup(self.db, MAIL, force=True)
                    self.db.commit()
                    self.assertEqual(out2["status"], "delivering")
                    self.assertEqual(send.call_count, 1)  # 状态真的变了 → 通知
        finally:
            (settings.cainiao_enabled, settings.cainiao_app_secret,
             settings.cainiao_cp_code, settings.cainiao_gateway_url,
             settings.cainiao_msg_type) = saved


class WorkerTest(unittest.TestCase):
    """独立定时服务：只验证调度逻辑，不打真实接口。"""

    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self.db.query(LogisticsTrace).delete()
        self.db.commit()

    def tearDown(self):
        self.db.query(LogisticsTrace).delete()
        self.db.commit()
        self.db.close()

    def test_90_run_once_refreshes_overdue(self):
        """过期在途单会被挑出来刷一遍。"""
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib

        worker = importlib.import_module("logistics_worker")
        from datetime import timedelta

        from app.timeutil import now

        self.db.add(
            LogisticsTrace(mail_no="W-1", next_refresh_at=now() - timedelta(minutes=1))
        )
        self.db.commit()
        # 收单环节跳过（库里可能还有别的用例留下的订单，会干扰计数）
        with mock.patch.object(worker, "_pending_mail_nos", return_value=[]):
            with mock.patch.object(worker.trace_service, "lookup",
                                   return_value={"ok": True}) as look:
                stat = worker.run_once(self.db)
        self.assertEqual(stat["refreshed"], 1)
        self.assertEqual(stat["ok"], 1)
        self.assertEqual(look.call_count, 1)

    def test_92_worker_survives_errors(self):
        """单单失败不拖垮整轮（独立进程不许因为一条单挂掉）。"""
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib

        worker = importlib.import_module("logistics_worker")
        self.db.add(LogisticsTrace(mail_no="W-2", next_refresh_at=None))
        self.db.commit()
        with mock.patch.object(worker, "_pending_mail_nos", return_value=[]):
            with mock.patch.object(worker.trace_service, "lookup",
                                   side_effect=RuntimeError("boom")):
                stat = worker.run_once(self.db)
        self.assertEqual(stat["refreshed"], 0)  # 没到期的不刷
        self.assertIn("refreshed", stat)


if __name__ == "__main__":
    unittest.main()
