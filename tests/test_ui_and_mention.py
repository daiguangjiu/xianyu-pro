"""界面与@口径调整（2026-09-25）。

业务口径：
  1 订单列表：来源（发送人 / 群）放第一列，不再显示收货地址（太长），所有字段不折叠不换行；
  2 回单（物流单号）同步到群必须 @ 到该订单的原发送人：订单的「来源消息 / 发送人」不能被
    机器人自己的回执消息覆盖（否则 sender_id 变成 cli_xxx，@ 不到人）；
  3 更新日志从首页移到「数据备份」页；
  4 首页科技感改版：指标数字带滚动动画。
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
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

TEMPLATE = (
    "产品： 亚朵四代单枕头\n"
    "收货人： 贲\n"
    "手机号码： 14790021215\n"
    "地址信息： 安徽省 滁州市 琅琊区 清流街道 凤凰湖畔18栋\n"
    "售价：115\n"
    "成本价：60\n"
    "所属供应链：华强北数码\n"
    "出货闲鱼号：卖货的小姑娘\n"
    "回款支付宝归属人：卫玉珍\n"
)

# 机器人自己的回执被转发回群里：带着订单号，会被解析成模板消息
ROBOT_FORWARD = (
    "产品： 亚朵四代单枕头\n"
    "收货人： 贲\n"
    "手机号码： 14790021215\n"
    "订单号： 8888888888\n"
)


class FakeClient:
    enabled = True

    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}

    def download_resource(self, *args, **kwargs):
        raise RuntimeError("测试环境不下载资源")


class UiAdjustmentTest(unittest.TestCase):
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
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _push(self, message_id: str, text: str, sender_id: str = "ou_person_1") -> None:
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id=message_id,
                    chat_id="oc_ui",
                    chat_name="闲鱼接单群",
                    msg_type="text",
                    sender_id=sender_id,
                    sender_name="小鱼",
                    raw_text=text,
                    parse_status="pending",
                    msg_time=now(),
                )
            )
            s.commit()
        with mock.patch.object(settings, "notify_mode", "off"):
            with SessionLocal() as s:
                ingest_service.process_pending(s, client=FakeClient(), limit=10)

    # ---------------------------------------------------------------- 订单列表
    def test_01_order_list_source_first_no_address(self):
        self._push("om_ui_1", TEMPLATE)
        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn("<th>发送人</th>", text, "来源列只显示发送人，不再展示群")
        self.assertIn("list-nowrap", text)
        self.assertNotIn('class="clip"', text, "列表不再做省略号折叠")
        self.assertNotIn('class="wrap"', text, "列表不再换行")
        self.assertNotIn("凤凰湖畔18栋", text, "列表不再显示收货地址")
        self.assertIn("14790021215", text, "收货人手机号仍要显示")
        # 列顺序：序号放最左边，然后是流水号（系统内订单 ID）、发送人、订单号
        thead = text.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertLess(thead.index("序号"), thead.index("流水号"), "序号列放最左边")
        self.assertLess(thead.index("流水号"), thead.index("发送人"), "流水号紧跟序号")
        self.assertLess(thead.index("发送人"), thead.index("订单号"))
        self.assertNotIn("<th></th>", thead, "操作列移到前面后，末尾不再留空列")
        self.assertIn("序号", thead, "列表第一列给出序号，方便口头报数")
        body = text.split("<tbody id=\"order-table\">", 1)[1]
        first_td = body.split("<td", 2)[1]
        self.assertIn("跨页连续", first_td, "第一列是序号（跨页连续）")
        second_td = body.split("<td", 3)[2]
        self.assertIn("订单流水号", second_td, "第二列是流水号（系统内订单 ID），点开就是详情")
        self.assertIn('href="/orders/', second_td, "流水号直接链到订单详情")
        third_td = body.split("<td", 4)[3]
        self.assertIn("小鱼", third_td, "第三列是发送人")
        self.assertNotIn("闲鱼接单群", third_td, "发送人列不展示来源群名")
        self.assertNotIn("oc_ui", third_td, "发送人列不展示群 ID")
        self.assertIn("跨页连续", body, "每行都带序号（跨页连续）")

    # ---------------------------------------------------------------- 更新日志位置
    def test_02_changelog_moved_to_backups(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertNotIn("最近更新", home.text, "首页不再放更新日志")
        self.assertIn("hero-tech", home.text)
        self.assertIn("data-count", home.text, "首页指标要有滚动动画标记")

        page = self.client.get("/backups")
        self.assertEqual(page.status_code, 200)
        self.assertIn("更新日志", page.text)
        self.assertIn("/changelog", page.text)

    # ---------------------------------------------------------------- @ 原发送人
    def test_03_source_and_sender_not_overwritten_by_robot_message(self):
        self._push("om_ui_2", TEMPLATE)
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.source_message_id == "om_ui_2"))
            order.order_no = "8888888888"
            s.commit()
            order_id = order.id

        # 机器人回执被转发回群：解析成功且按订单号匹配到同一单
        self._push("om_ui_3", ROBOT_FORWARD, sender_id="cli_bot_1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.sender_id, "ou_person_1", "发送人不能被机器人 id 顶掉")
            self.assertEqual(order.source_message_id, "om_ui_2", "来源消息不能被回执消息顶掉")
            order.tracking_no = "SF-UI-1"
            s.commit()

        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            status, error = notify_service.sync_tracking_to_group(order, client=fake)
        self.assertEqual(status, "sent")
        self.assertEqual(error, "", "能 @ 到人时不应有告警提示")
        self.assertTrue(fake.replies[0][1].startswith('<at user_id="ou_person_1"></at>'))

    def test_04_sync_warns_when_no_human_sender(self):
        with SessionLocal() as s:
            s.add(
                Order(
                    platform="xianyu",
                    order_no="UI-NO-MENTION",
                    chat_id="oc_ui",
                    sender_id="cli_bot_1",  # 只有机器人 id，@ 不了人
                    tracking_no="SF-UI-2",
                )
            )
            s.commit()
            order_id = s.scalar(select(Order.id).where(Order.order_no == "UI-NO-MENTION"))

        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"), SessionLocal() as s:
            status, note = notify_service.sync_tracking_to_group(
                s.get(Order, order_id), client=fake, session=s
            )
        self.assertEqual(status, "sent")
        self.assertIn("未 @ 到原发送人", note)
        self.assertTrue(fake.sent, "没有来源消息时直接发到群里")
        self.assertNotIn("<at ", fake.sent[0][1])

    def test_05_is_app_sender(self):
        self.assertTrue(notify_service.is_app_sender("cli_abc"))
        self.assertTrue(notify_service.is_app_sender("app_x"))
        self.assertFalse(notify_service.is_app_sender("ou_abc"))
        self.assertFalse(notify_service.is_app_sender(""))


if __name__ == "__main__":
    unittest.main()
