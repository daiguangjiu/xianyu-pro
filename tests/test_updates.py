"""本轮更新：更新日志、列表不换行、回单 @ 员工、飞书真实姓名、物流单号同步回写。

业务口径（2026-09）：
  1 新增「更新日志」页，记录每次上线的核心能力（config/changelog.json）；
  2 回单（物流单号）同步到飞书群时必须 @ 同步该订单的员工；
  3 支持读取飞书员工真实姓名（通讯录 / 群成员），并可一键补全历史数据；
  4 供应链推单维护物流单号后自动同步到群，同时回写订单与批次明细的物流单号。
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
from app.models import Order, PushBatchItem, RawMessage, VoiceEvent  # noqa: E402
from app.services import changelog as changelog_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class FakeClient:
    """假飞书客户端：记录发送内容，并可按 open_id 返回员工姓名。"""

    enabled = True

    def __init__(self, names: dict[str, str] | None = None):
        self.replies: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.names = names or {}

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}

    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> str:
        return self.names.get(user_id, "")

    def get_chat_members(self, chat_id: str, page_size: int = 100) -> dict[str, str]:
        return {k: v for k, v in self.names.items() if k.startswith("ou_")}


class UpdateTest(unittest.TestCase):
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

    # ---------------------------------------------------------------- 更新日志
    def test_01_changelog_page(self):
        entries = changelog_service.load()
        self.assertGreater(len(entries), 1)
        latest = entries[0]["version"]
        # 渔集从 v1.0.1 起用三段号（v1.0.11），渔枢 / 渔货栈仍是两段（v2.65），两种都要放行
        self.assertRegex(latest, r"^v\d+(\.\d+)*$", "最新版本号形如 v1.10 或 v1.0.11")
        self.assertTrue(entries[0]["items"])

        page = self.client.get("/changelog")
        self.assertEqual(page.status_code, 200)
        self.assertIn("更新日志", page.text)
        self.assertIn(latest, page.text, "最新版本要能在更新日志里看到")
        self.assertIn("回单（物流单号）同步到飞书群时 @ 同步该订单的员工", page.text)
        self.assertIn("v1.5", page.text, "要能看到历史更新")
        self.assertIn("更新日志", self.client.get("/").text, "导航里要有更新日志入口")

    def test_02_changelog_now_on_backups_page(self):
        """更新日志已从首页移到「数据备份」页（首页改版为科技感看板）。"""
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertNotIn("最近更新", home.text)
        self.assertIn("更新日志", home.text, "导航里保留入口")

        page = self.client.get("/backups")
        self.assertEqual(page.status_code, 200)
        self.assertIn("更新日志", page.text)
        # 展示的是「最新几条」：按版本号数值排序取当前版本，v1.9 不会被排到 v1.20 前面
        self.assertIn(changelog_service.current_version(), page.text)
        self.assertIn("/changelog", page.text)

    def test_02b_changelog_version_order(self):
        """版本号按数值比大小：v1.20 比 v1.9 新，不能被字符串排序顶到后面去。"""
        self.assertGreater(
            changelog_service.version_key("v1.20"), changelog_service.version_key("v1.9")
        )
        self.assertGreater(
            changelog_service.version_key("v1.20"), changelog_service.version_key("v1.19")
        )
        versions = [e["version"] for e in changelog_service.latest(3)]
        self.assertIn(changelog_service.current_version(), versions)
        entries = changelog_service.load(force=True)
        self.assertEqual(entries[0]["version"], changelog_service.current_version())

    def test_02d_changelog_splits_by_system(self):
        """更新日志按子系统划开：渔集（检测仓）的条目不再被算成渔枢系统（2026-10-08）。"""
        all_rows = changelog_service.load_all("")
        wms_rows = changelog_service.load_all("wms")
        hub_rows = changelog_service.load_all("hub")
        self_rows = changelog_service.load_all("self")
        self.assertTrue(wms_rows, "渔集的更新要单独成一端")
        self.assertTrue(all(r["portal"] == "wms" for r in wms_rows))
        self.assertTrue(all(r["portal_name"] == "渔集" for r in wms_rows))
        self.assertTrue(all(r["portal"] == "hub" for r in hub_rows))
        self.assertEqual(len(all_rows), len(wms_rows) + len(hub_rows) + len(self_rows))

        # 渔集的记录用自己的一套版本号：2026-10-08 起从 v1.0.1 单独编号
        # （靠条目上的 system 字段认，没写的老记录仍算渔枢系统）
        wms_versions = [r["version"] for r in wms_rows]
        self.assertIn("v1.0.1", wms_versions)
        self.assertIn("v1.0.9", wms_versions)
        self.assertNotIn("v1.0.1", [r["version"] for r in hub_rows], "别把渔集的版本算给渔枢系统")

        page = self.client.get("/changelog?portal=wms")
        self.assertEqual(page.status_code, 200)
        self.assertIn(">v1.0.9<", page.text)
        self.assertIn("渔集", page.text)
        # 注意别拿最新版本号断言（静态资源的 ?v= 就是最新版本号，页面上到处都有）。
        # 用 badge 形式 >vX< 断言：asset_v 现在是 v1.0.11，粗着搜 "v1.0.1" 会命中它。
        self.assertNotIn(">v1.0.1<", self.client.get("/changelog?portal=hub").text,
                         "渔枢系统那一档里不该出现渔集的版本")
        self.assertIn(">全部 ", self.client.get("/changelog").text, "分端按钮上带条数")

    def test_02c_sidebar_shows_current_version(self):
        """侧边栏底部的版本号跟着更新日志走（之前硬编码 v1.0，升级后一直显示旧版本）。"""
        version = changelog_service.current_version()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'class="env-pill">{version}<', page.text)
        self.assertNotIn('class="env-pill">v1.0<', page.text)

    # ---------------------------------------------------------------- 回单 @ 员工
    def test_03_tracking_sync_mentions_source_sender(self):
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_up_1",
                    chat_id="oc_up",
                    sender_id="ou_sender_1",
                    msg_time=now(),
                )
            )
            s.add(
                Order(
                    platform="xianyu",
                    order_no="UP-1",
                    source_message_id="om_up_1",
                    chat_id="oc_up",
                    sender_id="ou_sender_1",
                    tracking_no="SF111",
                )
            )
            s.commit()
            order = s.scalar(select(Order).where(Order.order_no == "UP-1"))

        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            status, error = notify_service.sync_tracking_to_group(order, client=fake)
        self.assertEqual(status, "sent")
        self.assertTrue(fake.replies)
        self.assertTrue(
            fake.replies[0][1].startswith('<at user_id="ou_sender_1"></at>'),
            fake.replies[0][1],
        )

    def test_04_tracking_sync_mentions_even_without_source_message(self):
        """没有来源消息时，也要按订单上的发送人 @ 员工。"""
        with SessionLocal() as s:
            s.add(
                Order(
                    platform="xianyu",
                    order_no="UP-2",
                    chat_id="oc_up",
                    sender_id="ou_sender_2",
                    tracking_no="SF222",
                )
            )
            s.commit()
            order = s.scalar(select(Order).where(Order.order_no == "UP-2"))

        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"):
            status, _ = notify_service.sync_tracking_to_group(order, client=fake)
        self.assertEqual(status, "sent")
        self.assertEqual(len(fake.sent), 1, "没有来源消息时直接发到群里")
        self.assertIn('<at user_id="ou_sender_2"></at>', fake.sent[0][1])

    # ---------------------------------------------------------------- 物流单号回写
    def test_05_tracking_update_writes_back_batch_items(self):
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="UP-3",
                supplier_name="华强北数码",
                supplier_status="pushed",
                pushed_at=now(),
            )
            s.add(order)
            s.commit()
            batch = supplier_service.create_batch(s, "华强北数码", [order.id])
            batch_id, order_id = batch.id, order.id

        resp = self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "SF333", "sync": "0", "back": "/batches"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).tracking_no, "SF333")
            item = s.scalar(
                select(PushBatchItem).where(
                    PushBatchItem.batch_id == batch_id, PushBatchItem.order_id == order_id
                )
            )
            self.assertEqual(item.tracking_no, "SF333", "批次明细的运单号要一起更新")
        detail = self.client.get(f"/orders/{order_id}")
        self.assertIn("SF333", detail.text, "订单详情要能看到最新物流单号")

    # ---------------------------------------------------------------- 飞书真实姓名
    def test_06_backfill_sender_names(self):
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_up_4",
                    chat_id="oc_up",
                    sender_id="ou_xiaoyu",
                    sender_name="",
                    msg_time=now(),
                )
            )
            s.add(
                Order(
                    platform="xianyu",
                    order_no="UP-4",
                    chat_id="oc_up",
                    sender_id="ou_xiaoyu",
                    sender_name="",
                )
            )
            s.commit()

        from app.services.ingest import backfill_sender_names

        fake = FakeClient(names={"ou_xiaoyu": "小鱼"})
        with SessionLocal() as s:
            filled = backfill_sender_names(s, client=fake, limit=50)
        self.assertEqual(filled, 2)
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(RawMessage).where(RawMessage.message_id == "om_up_4")).sender_name,
                "小鱼",
            )
            self.assertEqual(
                s.scalar(select(Order).where(Order.order_no == "UP-4")).sender_name, "小鱼"
            )

        orders_page = self.client.get("/orders")
        self.assertIn("小鱼", orders_page.text, "订单列表显示真实姓名")

    def test_07_settings_fill_sender_names_route(self):
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_up_5",
                    chat_id="oc_up",
                    sender_id="ou_zhang",
                    sender_name="",
                    msg_time=now(),
                )
            )
            s.commit()

        fake = FakeClient(names={"ou_zhang": "张三"})
        with mock.patch.object(settings, "feishu_app_id", "cli_test"), mock.patch.object(
            settings, "feishu_app_secret", "secret"
        ), mock.patch("app.services.ingest.get_client", return_value=fake):
            resp = self.client.post("/settings/fill-sender-names", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(RawMessage).where(RawMessage.message_id == "om_up_5")).sender_name,
                "张三",
            )
        page = self.client.get("/settings")
        self.assertIn("一键补全发送人姓名", page.text)
        self.assertIn("contact:user.base:readonly", page.text)


if __name__ == "__main__":
    unittest.main()
