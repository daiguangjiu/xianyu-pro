"""供应链发货页（已推单记录）：物流单号维护 + 群同步 + 转账文案实时刷新。

业务口径（2026-09）：
  1 供应链发货页展示「已推单记录」，可直接维护物流单号；
  1.1 搜索关键词覆盖 订单号 / 产品名称 / 收货人 / 手机号 / 物流单号；
  1.2 已填好的物流单号最多只能再改 1 次（首次填写不算），改满就锁成只读；
  2 更新物流单号后自动在该订单的飞书群里 @ 订单发送人同步；
  3 推单模块的「是否转账」一改就刷新推单文案，复制出去的就是最新文案；
  4 订单没有指定供应商时，推单模块要给最醒目的提醒；
  5 系统设置页不再展示「最近收到的模板与截图」。
"""
from __future__ import annotations

import re
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
from app.models import Order, OrderImage, PushBatchItem, RawMessage  # noqa: E402
from app.services import logistics as logistics_service  # noqa: E402
from app.services import logistics_trace  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web import nav  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

RAW = """产品名称： 亚朵四代单枕头
收货人： 贲
手机号码： 14790021215
地址信息： 安徽省 滁州市 琅琊区 清流街道
售价：115
成本价：60
所属供应链：华强北数码
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""


class FakeClient:
    """假飞书客户端：只记录调用，不发真消息。"""

    enabled = True

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}


class TrackingBase(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text
        self.order_ids: list[int] = []

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _make_order(
        self,
        order_no: str,
        *,
        supplier: str = "华强北数码",
        pushed: bool = True,
        cost: float = 60.0,
        transfer: str = "unpaid",
        tracking_no: str = "",
        with_source: bool = False,
    ) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                receiver_name="贲",
                receiver_phone="14790021215",
                receiver_address="安徽省 滁州市 琅琊区 清流街道",
                sale_amount=115.0,
                cost_amount=cost,
                supplier_name=supplier,
                xianyu_account="卖货的小姑娘",
                payee_name="卫玉珍",
                transfer_status=transfer,
                tracking_no=tracking_no,
                msg_time=now(),
                source_message_id=f"om_{order_no}" if with_source else "",
            )
            if pushed:
                order.supplier_status = "pushed"
                order.pushed_at = now()
            s.add(order)
            if with_source:
                s.add(
                    RawMessage(
                        message_id=f"om_{order_no}",
                        chat_id="oc_tracking_test",
                        chat_name="发货推单群",
                        sender_id="ou_sender_1",
                        sender_name="小鱼",
                        msg_time=now(),
                        raw_text=RAW,
                        kind="template",
                        parse_status="parsed",
                    )
                )
            s.commit()
            self.order_ids.append(order.id)
            return order.id


class PushedRecordsPageTest(TrackingBase):
    """已推单记录列表。"""

    def test_lists_only_pushed_orders(self):
        pushed = self._make_order("TRACK-1")
        pending = self._make_order("TRACK-2", pushed=False)

        page = self.client.get("/batches")
        self.assertEqual(page.status_code, 200)
        self.assertIn("已推单", page.text)
        self.assertIn("TRACK-1", page.text)
        self.assertNotIn("TRACK-2", page.text)
        self.assertIn("物流单号", page.text)

        # 未填写物流单号筛选
        with SessionLocal() as s:
            s.get(Order, pushed).tracking_no = "SF123"
            s.commit()
        page = self.client.get("/batches?tracking=none")
        self.assertNotIn("TRACK-1", page.text)
        page = self.client.get("/batches?tracking=has")
        self.assertIn("TRACK-1", page.text)
        self.assertIn("SF123", page.text)
        self.assertNotIn("TRACK-2", page.text, "未推单的订单不进已推单记录")
        self.assertIn("TRACK-2", self.client.get(f"/orders/{pending}").text)

    def test_history_view_keeps_batches(self):
        self._make_order("TRACK-3")
        page = self.client.get("/batches?view=history")
        self.assertEqual(page.status_code, 200)
        self.assertIn("历史批次", page.text)
        self.assertIn("供应链发货 · 历史批次", page.text)

    def test_supplier_filter(self):
        self._make_order("TRACK-4", supplier="华强北数码")
        self._make_order("TRACK-5", supplier="义乌小商品城")
        page = self.client.get("/batches?supplier=义乌小商品城")
        self.assertIn("TRACK-5", page.text)
        self.assertNotIn("TRACK-4", page.text)


class TrackingEditLimitTest(TrackingBase):
    """已填好的物流单号：最多只能再改 1 次（首次填写不算），改满就锁死。"""

    def _save(self, order_id: int, tracking_no: str):
        return self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": tracking_no, "back": "/batches"},
            follow_redirects=False,
        )

    def test_filled_tracking_can_only_be_edited_once(self):
        order_id = self._make_order("TRACK-EDIT-1")
        resp = self._save(order_id, "SF-AAA")
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.tracking_no, "SF-AAA")
            self.assertEqual(order.tracking_edit_count, 0, "首次填写不占改动次数")

        self._save(order_id, "SF-BBB")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.tracking_no, "SF-BBB", "填错一次要能改回来")
            self.assertEqual(order.tracking_edit_count, 1)

        self._save(order_id, "SF-CCC")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.tracking_no, "SF-BBB", "改满 1 次后不能再改")
            self.assertEqual(order.tracking_edit_count, 1)

        page = self.client.get("/batches")
        self.assertIn("不能再改", page.text)
        self.assertNotIn('name="tracking_no" value="SF-BBB"', page.text, "锁死后不该再给输入框")

    def test_empty_tracking_can_always_be_filled(self):
        """没填过单号的订单不受限制：随时可以填。"""
        order_id = self._make_order("TRACK-EDIT-2")
        self._save(order_id, "SF-FIRST")
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).tracking_edit_count, 0)
        page = self.client.get("/batches")
        self.assertIn("还能再改 1 次", page.text)


class TrackingSearchTest(TrackingBase):
    """搜索关键词支持物流单号。"""

    def test_search_by_tracking_no(self):
        hit = self._make_order("TRACK-Q-1", tracking_no="SF-Q-111")
        self._make_order("TRACK-Q-2", tracking_no="SF-Q-222")
        page = self.client.get("/batches?q=SF-Q-111")
        self.assertIn("TRACK-Q-1", page.text)
        self.assertNotIn("TRACK-Q-2", page.text)
        self.assertIn(f"/orders/{hit}/shipping", page.text, "有单号就能直接看发货详情与轨迹")
        self.assertIn("发货详情", page.text)

    def test_shipped_order_shows_tracking_no_in_order_list(self):
        """订单列表：已发货的状态后面直接跟物流单号。"""
        order_id = self._make_order("TRACK-LIST-1", tracking_no="SF-LIST-9")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.order_state = "shipped"
            s.commit()
        page = self.client.get("/orders")
        self.assertIn("SF-LIST-9", page.text)


class TrackingSyncTest(TrackingBase):
    """物流单号维护后自动同步到群里 @ 发送人。"""

    def test_update_tracking_syncs_to_group(self):
        order_id = self._make_order("TRACK-SYNC", with_source=True)
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"), mock.patch(
            "app.services.notify.get_client", return_value=fake
        ):
            resp = self.client.post(
                f"/batches/tracking/{order_id}",
                data={"tracking_no": "SF7788990011", "sync": "1", "back": "/batches"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)

        self.assertEqual(len(fake.replies), 1, "应在原消息下回复一条物流同步")
        message_id, text = fake.replies[0]
        self.assertEqual(message_id, "om_TRACK-SYNC")
        self.assertIn("物流单号：SF7788990011", text)
        self.assertIn('<at user_id="ou_sender_1"></at>', text, "群里要 @ 订单发送人")
        self.assertIn("华强北数码", text)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.tracking_no, "SF7788990011")
            self.assertIsNotNone(order.tracking_synced_at)
            self.assertIsNotNone(order.updated_at)

    def test_update_tracking_without_sync(self):
        order_id = self._make_order("TRACK-NOSYNC", with_source=True)
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"), mock.patch(
            "app.services.notify.get_client", return_value=fake
        ):
            self.client.post(
                f"/batches/tracking/{order_id}",
                data={"tracking_no": "SF-ONLY-SAVE", "back": "/batches"},
                follow_redirects=False,
            )
        self.assertEqual(fake.replies, [], "取消勾选后不应发群")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.tracking_no, "SF-ONLY-SAVE")
            self.assertIsNone(order.tracking_synced_at, "未同步就不记同步时间")

    def test_sync_skipped_when_notify_off(self):
        order_id = self._make_order("TRACK-OFF", with_source=True)
        fake = FakeClient()
        with mock.patch("app.services.notify.get_client", return_value=fake):
            resp = self.client.post(
                f"/batches/tracking/{order_id}",
                data={"tracking_no": "SF-OFF-1", "sync": "1", "back": "/batches"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(fake.replies, [], "关闭下发时不应该发群")
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).tracking_no, "SF-OFF-1", "单号仍要保存")

    def test_notice_text_and_empty_guard(self):
        order_id = self._make_order("TRACK-TEXT")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.tracking_no = "SF-TEXT-9"
            text = push_service.tracking_notice(order)
            self.assertIn("物流单号：SF-TEXT-9", text)
            self.assertIn("147****1215", text, "手机号要脱敏")
            # 空单号不同步
            order.tracking_no = ""
            from app.services.notify import sync_tracking_to_group

            with mock.patch.object(settings, "notify_mode", "reply"):
                status, error = sync_tracking_to_group(order, client=FakeClient(), session=s)
        self.assertEqual(status, "skipped")
        self.assertIn("物流单号为空", error)


class TransferRefreshTest(TrackingBase):
    """改「是否转账」立即刷新推单文案，复制出去就是最新。"""

    def test_transfer_change_refreshes_text_without_marking_pushed(self):
        order_id = self._make_order("TRACK-TRANSFER", pushed=False)
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("是否转账：未转账", page.text)

        resp = self.client.post(
            f"/orders/{order_id}/push/transfer",
            data={"transfer_status": "paid"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["transfer"], "paid")
        self.assertEqual(data["transfer_label"], "已转账")
        self.assertIn("是否转账：已转账", data["text"])
        self.assertIn("成本价：60", data["text"])

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.transfer_status, "paid")
            self.assertIsNone(order.pushed_at, "只改转账不应标记已推单")
            self.assertEqual(order.supplier_status, "pending")

        detail = self.client.get(f"/orders/{order_id}")
        self.assertIn("是否转账：已转账", detail.text)
        self.assertIn('data-transfer-url="/orders/%d/push/transfer"' % order_id, detail.text)

    def test_transfer_change_on_pushed_order_returns_json(self):
        """已推单的订单也要能改转账（pushed_at 不能被塞进 JSON 导致 500）。"""
        order_id = self._make_order("TRACK-TRANSFER-PUSHED", pushed=True, transfer="unpaid")
        resp = self.client.post(
            f"/orders/{order_id}/push/transfer",
            data={"transfer_status": "paid"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pushed"])
        self.assertIn("是否转账：已转账", data["text"])
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.transfer_status, "paid")
            self.assertIsNotNone(order.pushed_at)
            self.assertEqual(order.supplier_status, "pushed", "改转账不应影响已推单状态")

    def test_transfer_change_rejects_illegal_value(self):
        order_id = self._make_order("TRACK-BAD")
        resp = self.client.post(
            f"/orders/{order_id}/push/transfer",
            data={"transfer_status": "whatever"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])


class SupplierMissingAlertTest(TrackingBase):
    """订单没有供应商时，推单模块要最醒目地提醒。"""

    def test_alert_shown_when_supplier_missing(self):
        order_id = self._make_order("TRACK-NOSUP", supplier="")
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("该订单没有指定供应商", page.text)
        self.assertIn("push-card-alert", page.text)
        self.assertIn("供应商：未指定（务必先补全）", page.text)
        self.assertIn("供应商未指定，仍要复制推单文案吗？", page.text)

        with SessionLocal() as s:
            s.get(Order, order_id).supplier_name = "华强北数码"
            s.commit()
        page = self.client.get(f"/orders/{order_id}")
        self.assertNotIn("该订单没有指定供应商", page.text)
        self.assertNotIn("push-card-alert", page.text)

    def test_alert_shown_for_placeholder_supplier(self):
        order_id = self._make_order("TRACK-PLACEHOLDER", supplier="未指定")
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("该订单没有指定供应商", page.text)


class SettingsCleanupTest(TrackingBase):
    """系统设置页去掉「最近收到的模板与截图」。"""

    def test_recent_samples_removed(self):
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="om_settings_sample",
                    chat_id="oc_tracking_test",
                    chat_name="发货推单群",
                    sender_id="ou_sender_1",
                    msg_time=now(),
                    raw_text=RAW,
                    kind="template",
                    parse_status="parsed",
                )
            )
            s.commit()
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("最近收到的模板与截图", page.text)
        self.assertNotIn("还没有收到模板消息", page.text)
        # 上行模板说明保留
        self.assertIn("上行模板与截图样例", page.text)
        self.assertIn("标准模板（群里上行格式）", page.text)


class PushedOrdersQueryTest(TrackingBase):
    """已推单记录查询（含关键词 / 转账 / 物流单号筛选）。"""

    def test_filters(self):
        self._make_order("TRACK-Q1", transfer="paid", tracking_no="SF1")
        self._make_order("TRACK-Q2", transfer="unpaid")
        with SessionLocal() as s:
            def nos(**kw) -> list[str]:
                return [o.order_no for o in push_service.pushed_orders(s, **kw)]

            self.assertEqual(sorted(nos(q="TRACK-Q")), ["TRACK-Q1", "TRACK-Q2"], "关键词命中两单")
            self.assertEqual(nos(transfer="paid"), ["TRACK-Q1"], "只看已转账")
            self.assertEqual(nos(tracking="has"), ["TRACK-Q1"], "只看已填物流单号")
            self.assertEqual(nos(tracking="none"), ["TRACK-Q2"], "只看未填物流单号")
            # 未推单的不出现在已推单记录里
            self.assertEqual(push_service.pushed_orders(s, q="TRACK-NOTHING"), [])
            self.assertIsNotNone(s.scalar(select(Order.id).where(Order.order_no == "TRACK-Q1")))


class CompanyGuessTest(unittest.TestCase):
    """按运单号自动认物流公司。"""

    def test_guess_company(self):
        self.assertEqual(logistics_service.guess_company("SF1234567890"), "顺丰速运")
        self.assertEqual(logistics_service.guess_company("YT0712394919262"), "圆通速递")
        self.assertEqual(logistics_service.guess_company("JD0123456789"), "京东物流")
        self.assertEqual(logistics_service.guess_company("12345"), "", "认不出就留空")
        self.assertEqual(logistics_service.guess_company(""), "")
        self.assertEqual(logistics_service.guess_cp_code("SF1234567890"), "SF")
        self.assertEqual(logistics_service.guess_cp_code("12345"), "")


class ShippingDetailTest(TrackingBase):
    """订单发货详情（不是订单详情）：单号 + 物流公司 + 轨迹明细 + 发货日志。"""

    def test_shipping_page_shows_company_tracks_and_logs(self):
        order_id = self._make_order("SHIP-1")
        fake_result = {
            "ok": True,
            "msg": "",
            "status": "delivering",
            "nodes": [
                {
                    "time": "2026-10-01 10:00",
                    "station": "深圳中转部",
                    "desc": "已发出",
                    "status": "delivering",
                }
            ],
        }
        # 页面现在走三端共用的轨迹服务（带本地存档），mock 它而不是裸的菜鸟调用
        with mock.patch.object(
            logistics_trace, "lookup", return_value=fake_result
        ) as query_mock:
            self.client.post(
                f"/batches/tracking/{order_id}",
                data={"tracking_no": "SF1234567890", "back": "/batches"},
                follow_redirects=False,
            )
            page = self.client.get(f"/orders/{order_id}/shipping")

        self.assertEqual(page.status_code, 200)
        self.assertIn("订单发货详情", page.text)
        self.assertIn("SF1234567890", page.text)
        self.assertIn("顺丰速运", page.text, "按单号自动认出的物流公司要显示")
        self.assertIn("深圳中转部", page.text, "轨迹明细要显示")
        self.assertIn("回填物流单号", page.text, "发货操作日志要记录")
        self.assertIn(">订单详情<", page.text, "要有回订单详情的入口")
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).tracking_company, "顺丰速运")
        self.assertEqual(query_mock.call_count, 1, "进页面就按单号查一次轨迹")

    def test_filled_tracking_shows_edit_button_in_list(self):
        """已填单号：默认只读显示 +「编辑」按钮，输入框藏着等点击。"""
        order_id = self._make_order("SHIP-2")
        self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "YT0712394919262", "back": "/batches"},
            follow_redirects=False,
        )
        page = self.client.get("/batches")
        self.assertIn("圆通速递", page.text)
        self.assertIn("js-edit-tracking", page.text, "给「编辑」按钮，点开才是输入框")
        self.assertIn(f'href="/orders/{order_id}/shipping"', page.text, "发货详情入口")


class TrackingRecordsTest(TrackingBase):
    """列表里的「保存记录 / 通知记录」，以及改单号时群里的「物流单号更新」文案。"""

    def _post(self, order_id: int, **data):
        return self.client.post(
            f"/batches/tracking/{order_id}", data=data, follow_redirects=False
        )

    def test_records_show_in_list(self):
        order_id = self._make_order("REC-1", with_source=True)
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"), mock.patch(
            "app.services.notify.get_client", return_value=fake
        ):
            self._post(order_id, tracking_no="SF7000000001", sync="1", back="/batches")

        page = self.client.get("/batches")
        self.assertIn("保存记录：", page.text)
        self.assertIn("通知记录：", page.text)
        self.assertIn("已通知发货群：SF7000000001", page.text)
        self.assertIn("js-edit-tracking", page.text, "已填单号给「编辑」按钮")
        self.assertIn('style="display:none"', page.text, "编辑态默认藏起来，别和显示态一起冒出来")
        self.assertIn("请及时填写物流单号", page.text)

        text = fake.replies[0][1]
        self.assertIn("物流单号：SF7000000001", text)
        self.assertIn("顺丰速运", text, "通知里要带上自动认出的物流公司")
        self.assertNotIn("物流单号有更新", text, "第一次填写不是「更新」")

    def test_edit_again_notifies_as_update(self):
        order_id = self._make_order("REC-2", with_source=True)
        fake = FakeClient()
        with mock.patch.object(settings, "notify_mode", "reply"), mock.patch(
            "app.services.notify.get_client", return_value=fake
        ):
            self._post(order_id, tracking_no="YT1234567890", back="/batches")
            fake.replies.clear()
            self._post(order_id, tracking_no="SF8000000002", sync="1", back="/batches")

        self.assertEqual(len(fake.replies), 1)
        text = fake.replies[0][1]
        self.assertIn("物流单号有更新，请以本条为准", text, "改单号要在群里说明是更新")
        self.assertIn("物流单号更新：SF8000000002", text)
        page = self.client.get("/batches")
        self.assertIn("已通知发货群（单号更新）：SF8000000002", page.text)

    def test_no_sync_records_why(self):
        order_id = self._make_order("REC-3")
        self._post(order_id, tracking_no="SF9000000003", back="/batches")
        page = self.client.get("/batches")
        self.assertIn("未勾选同步到群，只存档：SF9000000003", page.text)


class CainiaoTestQueryTest(TrackingBase):
    """系统设置 → 菜鸟物流查询：测试查询按钮（填单号当场把链路走一遍）。"""

    def test_settings_page_has_test_form(self):
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("测试查询", page.text)
        self.assertIn('action="/settings/logistics/test"', page.text)

    def test_test_query_calls_gateway_and_shows_raw(self):
        import json as _json

        fake = mock.Mock()
        fake.status_code = 200
        fake.text = _json.dumps(
            {
                "success": "true",
                "traces": [{"time": "2026-10-01 10:00", "desc": "已签收", "station": "深圳"}],
            }
        )
        with mock.patch.object(settings, "cainiao_gateway_url", "https://example.com/link.do"), mock.patch.object(
            settings, "cainiao_enabled", True
        ), mock.patch("app.services.logistics.requests.post", return_value=fake) as post:
            resp = self.client.post(
                "/settings/logistics/test",
                data={"test_mail_no": "YT1134420815964"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        page = self.client.get(resp.headers["location"])
        self.assertIn("调用成功", page.text)
        self.assertIn("已签收", page.text, "轨迹要展示出来")
        self.assertIn("请求与响应原文", page.text)

        sent = post.call_args.kwargs.get("data") or post.call_args[1]["data"]
        for key in ("msg_type", "logistic_provider_id", "logistics_interface", "data_digest"):
            self.assertIn(key, sent, "四个网关参数一个都不能少")
        self.assertIn("YTO", sent["logistics_interface"], "cpCode 按单号自动识别成圆通")

    def test_test_query_failure_shows_reason_and_raw(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.text = '{"success":"false","errorCode":"S02","errorMsg":"验签失败"}'
        with mock.patch.object(settings, "cainiao_gateway_url", "https://example.com/link.do"), mock.patch(
            "app.services.logistics.requests.post", return_value=fake
        ):
            resp = self.client.post(
                "/settings/logistics/test",
                data={"test_mail_no": "SF1234567890"},
                follow_redirects=False,
            )
        page = self.client.get(resp.headers["location"])
        self.assertIn("调用失败", page.text)
        self.assertIn("S02", page.text, "错误码要翻成人话并回显")
        self.assertIn("验签失败", page.text)


def card_value(html: str, label: str) -> int:
    """从页面 HTML 里抠出统计卡片的数字（卡片结构：label 的 div 后紧跟 value 的 div）。"""
    found = re.search(rf"{label}</div>\s*<div class=\"value\">(\d+)", html)
    assert found is not None, f"页面找不到「{label}」卡片"
    return int(found.group(1))


class BatchesOverviewTest(TrackingBase):
    """供应链发货页顶部三张卡片：已推单记录 / 供应商已发货 / 待填回单。

    这三张卡片是运营看整体进度用的，数字走**全量口径**（不随筛选变）——
    以前用当前列表行数，点了「待填回单」后「已推单」跟着从 93 掉到 21，
    看着像数据丢了（「为什么有两个已推单数量」就是这么来的）。
    """

    def test_cards_show_overview_and_ignore_filter(self):
        self._make_order("TRACK-A", tracking_no="SF1234567890")
        self._make_order("TRACK-B")
        self._make_order("TRACK-C")

        page = self.client.get("/batches")
        self.assertEqual(card_value(page.text, "已推单记录"), 3)
        self.assertEqual(card_value(page.text, "供应商已发货"), 1)
        self.assertEqual(card_value(page.text, "待填回单"), 2)

        # 点了「待填回单」：列表少了，但卡片数字不许跟着缩水
        filtered = self.client.get("/batches?tracking=none")
        self.assertEqual(card_value(filtered.text, "已推单记录"), 3)
        self.assertEqual(card_value(filtered.text, "供应商已发货"), 1)
        self.assertEqual(card_value(filtered.text, "待填回单"), 2)
        self.assertIn("当前筛选出", filtered.text, "筛选后要说清列表里有多少条")
        self.assertIn("清除筛选", filtered.text)

    def test_tracking_cell_does_not_repeat_synced_time(self):
        """单号那一格不再重复「已同步群 时间」—— 下面的「通知记录」里已经有一条了。"""
        oid = self._make_order("TRACK-D", tracking_no="SF1227771046910")
        with SessionLocal() as s:
            s.get(Order, oid).tracking_synced_at = now()
            s.commit()
        page = self.client.get("/batches")
        self.assertIn("SF1227771046910", page.text)
        self.assertEqual(page.text.count("已同步群"), 1, "同步时间只该出现一次（通知记录里那条）")


class BatchesRefundFilterTest(TrackingBase):
    """退单 / 撤回的单不进供应链发货页：它们不发货、也不用供应商回单。

    以前没排掉，页面上会混着几条「没单号的退单」，看着像漏催的回单，
    也让人以为「退单是不是该是关闭状态」——它们该在退单页跟进，不该在这。
    """

    def test_refund_orders_not_listed(self):
        self._make_order("SUP-KEEP-1")
        refunded = self._make_order("SUP-REFUND-1")
        with SessionLocal() as s:
            order = s.get(Order, refunded)
            order.refund_status = "pending"
            order.order_state = "refund_synced"
            s.commit()
            listed = [o.order_no for o in push_service.pushed_orders(s)]
        self.assertIn("SUP-KEEP-1", listed)
        self.assertNotIn("SUP-REFUND-1", listed, "退单不该出现在供应链发货列表")

        page = self.client.get("/batches")
        self.assertNotIn("SUP-REFUND-1", page.text)
        # 卡片一起排掉：已推单记录 = 供应商已发货 + 待填回单，不再有退单搅在里面
        self.assertEqual(card_value(page.text, "已推单记录"), 1)
        self.assertEqual(card_value(page.text, "待填回单"), 1)
        self.assertIn("退单、撤回的单不在这里", page.text)


class CrumbAndNavOrderTest(TrackingBase):
    """顶部面包屑归属 + 菜单板块顺序（2026-10-01 调整）。"""

    def test_shipping_detail_crumb_belongs_to_supply(self):
        order_id = self._make_order("SUP-CRUMB-1")
        page = self.client.get(f"/orders/{order_id}/shipping")
        crumb = page.text.split('class="topbar-crumb"', 1)[1].split("</nav>", 1)[0]
        self.assertIn("供应链", crumb)
        self.assertIn("供应链发货", crumb)
        self.assertIn("发货详情", crumb)
        self.assertNotIn("订单管理", crumb, "发货详情属于供应链，不该指到交易 / 订单管理")

    def test_supply_before_goods_before_finance(self):
        """菜单板块顺序（2026-10-01）：交易 → 供应链 → 货源 → 资金。"""
        titles = [section.title for section in nav.NAV]
        self.assertLess(titles.index("供应链"), titles.index("货源"))
        self.assertLess(titles.index("货源"), titles.index("资金"))

    def test_pending_push_item_renamed(self):
        """「待推单」改成「待推单发货」：这批单是等着推给供应商去发货的。"""
        labels = [
            child.label
            for section in nav.NAV
            for item in section.items
            for child in item.children
        ]
        self.assertIn("待推单发货", labels)


if __name__ == "__main__":
    unittest.main()
