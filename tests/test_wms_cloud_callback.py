"""渔集 · 芯烨云打印**回调**（公网入站）：平台把打印结果推回来这一头。

官方《回调对接文档》的两条硬要求就是本文件的验收标准：
  · 收到后必须回 ``{"data": "ok"}`` —— 回别的、或超时，平台就当地址无效；
  · 响应必须 3 秒内回来 —— 所以接口只落库，一个外部请求都不发。

另外这接口是**免登录**的（平台服务器不会带我们的 Cookie），要验：
  · 不带 Cookie 也能 200（这是最容易漏的一道门：子系统守卫 + 平台中间件两道）；
  · 认不出的报文照样留痕，绝不因为解析失败就回 500（回 500 = 地址被判死）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import cloud_callback  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms import print_settings  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsCloudPrintEvent, WmsLabelPrint, WmsPrintConfig  # noqa: E402

PATH = cloud_callback.CALLBACK_PATH


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsCloudPrintEvent).delete(synchronize_session=False)
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsPrintConfig).delete(synchronize_session=False)
        s.commit()


def _mk_print(order_no: str = "SMI-1", status: str = "sent") -> int:
    with SessionLocal() as s:
        row = WmsLabelPrint(
            barcode="BC0001", display_code="QC-HA-2610-0001",
            status=status, order_no=order_no, printer_sn="XPY123",
        )
        s.add(row)
        s.commit()
        return row.id


def _last_event() -> WmsCloudPrintEvent | None:
    with SessionLocal() as s:
        return s.scalar(select(WmsCloudPrintEvent).order_by(WmsCloudPrintEvent.id.desc()))


class CallbackParseTest(unittest.TestCase):
    """报文解析：字段叫法千奇百怪，先把常见的都认下来（纯函数，不开服务）。"""

    def test_60_common_key_names(self):
        for payload, order, sn, state in (
            ({"orderId": "A1", "sn": "S1", "state": "1"}, "A1", "S1", "printed"),
            ({"orderNo": "A2", "printerSn": "S2", "printState": "0"}, "A2", "S2", "queued"),
            ({"taskId": "A3", "deviceSn": "S3", "status": "fail"}, "A3", "S3", "failed"),
            ({"order_id": "A4", "machineCode": "S4", "result": "success"}, "A4", "S4", "printed"),
        ):
            got = cloud_callback.parse(payload)
            self.assertEqual((got["order_no"], got["printer_sn"], got["state"]),
                             (order, sn, state), payload)

    def test_61_wrapped_in_data(self):
        """很多平台把正文裹在 data 里：{"code":0,"data":{...}}。"""
        got = cloud_callback.parse({"code": 0, "msg": "ok",
                                    "data": {"orderId": "W1", "state": "1"}})
        self.assertEqual(got["order_no"], "W1")
        self.assertEqual(got["state"], "printed")

    def test_62_printer_only_and_unknown(self):
        got = cloud_callback.parse({"sn": "S9", "status": "1"})
        self.assertEqual(got["event_type"], "printer", "只有机器号 = 机器状态推送")
        self.assertEqual(cloud_callback.parse({})["event_type"], "unknown")


class CallbackApiTest(unittest.TestCase):
    """接口：免登录 + 无条件回 ok + 落库 + 回写打印任务。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.web = TestClient(app)      # 带登录 Cookie：看设置页用
        cls.anon = TestClient(app)     # 不带 Cookie：模拟芯烨云平台推过来

    def setUp(self):
        self.web.cookies.set(COOKIE_NAME, make_session(1)[0])
        _wipe()

    def test_63_anonymous_ok(self):
        """最关键的一条：不带 Cookie 也要 200 + {"data":"ok"}（两道门都得开）。"""
        r = self.anon.post(PATH, json={"orderId": "SMI-1", "state": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"data": "ok"})
        # GET 也回 ok：平台后台「测试回调地址」多半发的是 GET
        self.assertEqual(self.anon.get(PATH).json(), {"data": "ok"})

    def test_64_printed_writes_back(self):
        pid = _mk_print("SMI-1")
        r = self.anon.post(PATH, json={"orderId": "SMI-1", "sn": "XPY123", "state": "1"})
        self.assertEqual(r.json(), {"data": "ok"})
        ev = _last_event()
        self.assertEqual(ev.state, "printed")
        self.assertEqual(ev.matched_print_id, pid)
        self.assertTrue(ev.applied)
        with SessionLocal() as s:
            row = s.get(WmsLabelPrint, pid)
            self.assertEqual(row.status, "printed")
            self.assertIsNotNone(row.printed_at)

    def test_65_failed_keeps_task_retryable(self):
        """失败不改状态（页面还能补打），把原因写到 error 上。"""
        pid = _mk_print("SMI-2")
        self.anon.post(PATH, json={"orderId": "SMI-2", "state": "-1", "msg": "缺纸"})
        with SessionLocal() as s:
            row = s.get(WmsLabelPrint, pid)
            self.assertEqual(row.status, "sent", "失败仍挂在「已下发」，可以继续补打")
            self.assertIn("缺纸", row.error)

    def test_66_unmatched_still_recorded(self):
        """对不上订单号：报文照样留着，响应照样 ok（绝不回 4xx / 5xx）。"""
        r = self.anon.post(PATH, json={"orderId": "NOPE", "state": "1"})
        self.assertEqual(r.json(), {"data": "ok"})
        ev = _last_event()
        self.assertEqual(ev.order_no, "NOPE")
        self.assertFalse(ev.applied)

    def test_67_form_body_and_garbage(self):
        """表单报文、以及根本不是 JSON 的玩意儿：都要收下并回 ok。"""
        self.assertEqual(
            self.anon.post(PATH, data="orderId=SMI-3&state=1",
                           headers={"Content-Type": "application/x-www-form-urlencoded"}).json(),
            {"data": "ok"},
        )
        self.assertEqual(_last_event().order_no, "SMI-3")
        self.assertEqual(self.anon.post(PATH, content=b"not-json-at-all").json(), {"data": "ok"})
        self.assertIn("not-json-at-all", _last_event().payload)

    def test_68_token_gate(self):
        with SessionLocal() as s:
            print_settings.save_config(s, {"callback_token": "tk-123"})
        before = len(cloud_callback.recent(SessionLocal()))
        # 不带令牌 / 带错令牌：只回 ok，不落库（防别人乱推）
        self.assertEqual(self.anon.post(PATH, json={"orderId": "X"}).json(), {"data": "ok"})
        self.assertEqual(self.anon.post(PATH + "?token=wrong",
                                        json={"orderId": "X"}).json(), {"data": "ok"})
        with SessionLocal() as s:
            self.assertEqual(len(cloud_callback.recent(s)), before)
        # 令牌对上：正常落库
        self.anon.post(PATH + "?token=tk-123", json={"orderId": "SMI-9", "state": "1"})
        with SessionLocal() as s:
            self.assertEqual(len(cloud_callback.recent(s)), before + 1)


class CallbackPageTest(unittest.TestCase):
    """打印设置页：回调地址要能一眼看到、能改。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.web = TestClient(app)

    def setUp(self):
        self.web.cookies.set(COOKIE_NAME, make_session(1)[0])
        _wipe()

    def test_69_page_shows_callback_url(self):
        page = self.web.get("/wms/print-settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn(cloud_callback.CALLBACK_PATH, page.text, "页面上要有回调地址")
        # 填了公网域名就用域名拼（芯烨只认域名形式）—— 回调设置有自己的路由
        self.web.post("/wms/print-settings/callback",
                      data={"public_base_url": "https://gw.example.com/release"})
        page = self.web.get("/wms/print-settings").text
        self.assertIn("https://gw.example.com/release" + cloud_callback.CALLBACK_PATH, page)
