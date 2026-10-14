"""菜鸟物流查询（LINK 网关）：签名、报文、轨迹解析、配置页与查询页。

签名算法是 2026-10-01 用真实凭据实测定的：
    data_digest = base64(md5(报文 + APP 秘钥).digest())
拼错一律回 S02 验签失败 —— 这条测试就是护栏，别再改成别的拼法。
"""
import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order  # noqa: E402
from app.services import logistics, settings_store as store  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
BACKUP = store.ENV_PATH.read_text(encoding="utf-8") if store.ENV_PATH.exists() else ""
ORIG = (
    settings.cainiao_enabled,
    settings.cainiao_gateway_url,
    settings.cainiao_msg_type,
    settings.cainiao_app_secret,
    settings.cainiao_cp_code,
    settings.cainiao_app_code,
    settings.cainiao_timeout_sec,
    settings.cainiao_interface_style,
)


def _restore() -> None:
    store.ENV_PATH.write_text(BACKUP, encoding="utf-8")
    (
        settings.cainiao_enabled,
        settings.cainiao_gateway_url,
        settings.cainiao_msg_type,
        settings.cainiao_app_secret,
        settings.cainiao_cp_code,
        settings.cainiao_app_code,
        settings.cainiao_timeout_sec,
        settings.cainiao_interface_style,
    ) = ORIG


def _enable() -> None:
    settings.cainiao_enabled = True
    settings.cainiao_gateway_url = "http://link.cainiao.com/gateway/link.do"
    settings.cainiao_msg_type = "CNTECH_LV_LOGISTICS_DETAIL_GET"
    settings.cainiao_app_secret = "unit-test-secret"
    settings.cainiao_cp_code = "unit-test-cpcode"
    settings.cainiao_app_code = ""
    settings.cainiao_timeout_sec = 15
    settings.cainiao_interface_style = "arg0"


class SignTest(unittest.TestCase):
    def test_sign_is_base64_of_md5_content_plus_secret(self):
        """签名 = base64(md5(报文+秘钥)的二进制)：md5 的十六进制串是错的。"""
        content = '{"mailNo":"SF123"}'
        secret = "tH1YQ7R6HoC87UErZ91W3THRsm5SG3G7"
        self.assertEqual(
            logistics.sign(content, secret),
            base64.b64encode(hashlib.md5((content + secret).encode()).digest()).decode(),
        )
        self.assertNotEqual(logistics.sign(content, secret), hashlib.md5((content + secret).encode()).hexdigest())

    def test_interface_and_phone_tail(self):
        # 默认按设置里的结构（arg0）：菜鸟 LINK 网关的服务端堆栈
        # （LinkServiceSupport.getRequestParam → BeanUtils.copyProperties → Source must not be null）
        # 显示 S13 就是缺 arg0，所以默认包一层
        # 手机号后四位两个名字都发（phone / receiverPhone）：菜鸟各接口叫法不一
        self.assertEqual(
            logistics.build_interface("SF123", cp_code="SF", phone="6267"),
            '{"arg0":{"mailNo":"SF123","cpCode":"SF","phone":"6267","receiverPhone":"6267"}}',
        )
        self.assertEqual(logistics.build_interface("SF123"), '{"arg0":{"mailNo":"SF123"}}')
        # 平铺：设置里选 flat 就发平铺（2026-10-01 实测它能进到菜鸟业务层）
        self.assertEqual(
            logistics.build_interface("SF123", cp_code="SF", phone="6267", style="flat"),
            '{"mailNo":"SF123","cpCode":"SF","phone":"6267","receiverPhone":"6267"}',
        )
        # 空字段不发：phone 没填就不带 phone
        self.assertEqual(
            logistics.build_interface("SF123", cp_code="SF", style="flat"),
            '{"mailNo":"SF123","cpCode":"SF"}',
        )
        self.assertEqual(logistics.phone_tail("13800138000"), "8000")
        self.assertEqual(logistics.phone_tail("138 0013 8000"), "8000")
        self.assertEqual(logistics.phone_tail("123"), "")


class ParseTest(unittest.TestCase):
    def test_parse_json_trace(self):
        payload = {
            "success": "true",
            "status": "SIGNED",
            "traceList": [
                {"time": "2026-10-01 10:00:00", "desc": "已签收", "station": "深圳"},
                {"time": "2026-09-30 09:00:00", "desc": "运输中", "station": "广州"},
            ],
        }
        nodes = logistics.parse_traces(payload)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["desc"], "已签收")  # 最新的在最上面
        self.assertEqual(logistics.map_status(payload, nodes), "signed")

    def test_parse_nested_and_other_field_names(self):
        """菜鸟各接口层级与字段名不一样：藏在 result 里、字段名是 acceptTime/standerdDesc 也要认。"""
        payload = {
            "success": "true",
            "result": {
                "mailNo": "YT1",
                "logisticsTraceDetailList": [
                    {"acceptTime": "2026-10-01 08:00", "standerdDesc": "派送中", "nodeName": "杭州"},
                ],
            },
        }
        nodes = logistics.parse_traces(payload)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["time"], "2026-10-01 08:00")
        self.assertEqual(nodes[0]["station"], "杭州")

    def test_parse_xml_error(self):
        xml = (
            "<response><success>false</success><errorCode>S18</errorCode>"
            "<errorMsg>找不到接口订阅信息 traceId:abc</errorMsg></response>"
        )
        ok, msg, _ = logistics._parse_response(xml)
        self.assertFalse(ok)
        self.assertIn("没订阅", msg)

    def test_error_codes_are_explained(self):
        for code, keyword in (("S02", "APP_SECRET"), ("S18", "订阅"), ("S23", "开通"), ("500", "网关")):
            ok, msg, _ = logistics._parse_response(
                json.dumps({"success": "false", "errorCode": code, "errorMsg": f"raw {code}"})
            )
            self.assertFalse(ok)
            self.assertIn(keyword, msg, f"{code} 的提示要说出该改什么")


class QueryTest(unittest.TestCase):
    def tearDown(self) -> None:
        _restore()

    def test_disabled_gives_actionable_hint(self):
        settings.cainiao_enabled = False
        result = logistics.query("SF123")
        self.assertFalse(result["ok"])
        self.assertIn("系统设置", result["msg"])

    def test_query_posts_signed_form(self):
        _enable()
        captured = {}

        class FakeResp:
            text = json.dumps(
                {
                    "success": "true",
                    "status": "DELIVERING",
                    "traceList": [{"time": "2026-10-01 10:00", "desc": "派送中", "station": "深圳"}],
                }
            )

        def fake_post(url, data=None, timeout=None):
            captured.update({"url": url, "data": data, "timeout": timeout})
            return FakeResp()

        with mock.patch.object(logistics.requests, "post", fake_post):
            result = logistics.query("SF123", cp_code="SF", phone="8000")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "delivering")
        self.assertEqual(result["nodes"][0]["desc"], "派送中")
        self.assertEqual(captured["url"], "http://link.cainiao.com/gateway/link.do")
        self.assertEqual(captured["data"]["msg_type"], "CNTECH_LV_LOGISTICS_DETAIL_GET")
        self.assertEqual(captured["data"]["logistic_provider_id"], "unit-test-cpcode")
        self.assertEqual(
            captured["data"]["data_digest"],
            base64.b64encode(
                hashlib.md5(
                    (captured["data"]["logistics_interface"] + "unit-test-secret").encode()
                ).digest()
            ).decode(),
        )
        self.assertNotIn("app_code", captured["data"])  # 没配 appCode 就不发

    def test_no_mail_no(self):
        _enable()
        self.assertFalse(logistics.query("")["ok"])

    def test_falls_back_to_other_interface_style(self):
        """首选结构查不到（S13）就自动换另一种：换过去能查到就算成功。

        菜鸟到底认 arg0 还是平铺，两边说法打架（实测 500 vs 服务端堆栈说缺 arg0），
        这条测试保证「换结构重试」这条兜底一直在，谁通算谁。
        """
        _enable()
        sent: list[str] = []

        class FakeResp:
            def __init__(self, text: str):
                self.text = text
                self.status_code = 200

            # 第一次（arg0）回 S13，第二次（平铺）回轨迹
        def fake_post(url, data=None, timeout=None):
            sent.append(data["logistics_interface"])
            if data["logistics_interface"].startswith('{"arg0"'):
                return FakeResp(
                    json.dumps(
                        {
                            "success": "false",
                            "errorCode": "S13",
                            "errorMsg": "Source must not be null traceId:x",
                        }
                    )
                )
            return FakeResp(
                json.dumps(
                    {
                        "success": "true",
                        "status": "SIGNED",
                        "traceList": [{"time": "2026-10-02 10:00", "desc": "已签收"}],
                    }
                )
            )

        with mock.patch.object(logistics.requests, "post", fake_post):
            result = logistics.query_debug("SF123", cp_code="SF")

        self.assertEqual(len(sent), 2, "首选结构失败后要换另一种重试一次")
        self.assertTrue(sent[0].startswith('{"arg0"'))
        self.assertFalse(sent[1].startswith('{"arg0"'))
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(result["interface_style"], "flat")
        self.assertEqual(result["nodes"][0]["desc"], "已签收")
        self.assertEqual(len(result["tried"]), 2)

    def test_both_styles_fail_keeps_first_raw(self):
        """两种都失败：留首选那次的原文（排查要看原文），提示里说清另一种也试过。"""
        _enable()

        class FakeResp:
            status_code = 200
            text = json.dumps(
                {"success": "false", "errorCode": "S13", "errorMsg": "Source must not be null"}
            )

        with mock.patch.object(logistics.requests, "post", lambda *a, **k: FakeResp()):
            result = logistics.query_debug("SF123")

        self.assertFalse(result["ok"])
        self.assertIn("S13", result["msg"])
        self.assertIn("重试", result["msg"])
        self.assertEqual(len(result["tried"]), 2)
        self.assertIn("S13", result["raw"], "失败时保留首选那次的菜鸟原文")
        self.assertTrue(
            result["request"]["logistics_interface"].startswith('{"arg0"'),
            "失败时留首选结构的报文给排查看",
        )


class LogisticsPageTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        self.assertEqual(self.client.post("/login", data=ADMIN, follow_redirects=False).status_code, 303)

    def tearDown(self) -> None:
        _restore()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, tracking_no: str = "") -> int:
        with SessionLocal() as s:
            order = Order(
                order_no=order_no,
                product_name="测试商品",
                receiver_name="张三",
                receiver_phone="13800138000",
                sale_amount=100,
                cost_amount=60,
                supplier_name="测试供应商",
                tracking_no=tracking_no,
                order_state="shipped",
                supplier_status="done",
                pushed_at=now(),
            )
            s.add(order)
            s.commit()
            return order.id

    def test_settings_page_has_cainiao_card(self):
        html = self.client.get("/settings").text
        for field in (
            "cainiao_gateway_url",
            "cainiao_msg_type",
            "cainiao_cp_code",
            "cainiao_app_secret",
            "cainiao_interface_style",
        ):
            self.assertIn(field, html, f"系统设置页缺菜鸟配置项 {field}")

    def test_save_cainiao_settings(self):
        resp = self.client.post(
            "/settings/logistics",
            data={
                "cainiao_enabled": "1",
                "cainiao_gateway_url": "https://link.cainiao.com/gateway/link.do",
                "cainiao_msg_type": "CNTECH_LV_LOGISTICS_DETAIL_GET",
                "cainiao_cp_code": "cp-test",
                "cainiao_app_secret": "secret-test",
                "cainiao_timeout_sec": "20",
                "cainiao_interface_style": "flat",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(settings.cainiao_enabled)
        self.assertEqual(settings.cainiao_cp_code, "cp-test")
        self.assertEqual(settings.cainiao_app_secret, "secret-test")
        self.assertEqual(settings.cainiao_timeout_sec, 20)
        self.assertEqual(settings.cainiao_interface_style, "flat")
        self.assertTrue(logistics.is_enabled())

    def test_tracking_page_without_config(self):
        """没配菜鸟时打开轨迹页：页面照常打开，给出「去哪儿配」的提示，不能报错。"""
        settings.cainiao_enabled = False
        order_id = self._order("LG-1", tracking_no="SF1234567890")
        resp = self.client.get(f"/orders/{order_id}/tracking")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("SF1234567890", resp.text)
        self.assertIn("系统设置", resp.text)

    def test_tracking_page_shows_trace(self):
        _enable()
        order_id = self._order("LG-2", tracking_no="SF9876543210")
        fake_payload = {
            "success": "true",
            "status": "SIGNED",
            "traceList": [
                {"time": "2026-10-01 12:00:00", "desc": "已签收，签收人：本人", "station": "深圳市南山区"},
                {"time": "2026-09-30 08:00:00", "desc": "已发货", "station": "深圳转运中心"},
            ],
        }

        class FakeResp:
            text = json.dumps(fake_payload)

        with mock.patch.object(logistics.requests, "post", lambda *a, **k: FakeResp()):
            html = self.client.get(f"/orders/{order_id}/tracking").text
        self.assertIn("已签收，签收人：本人", html)
        self.assertIn("深圳市南山区", html)
        self.assertIn("已签收", html)  # 状态标签
        self.assertIn("同步为", html)  # 可以一键同步到订单

    def test_tracking_page_needs_mail_no(self):
        _enable()
        order_id = self._order("LG-3", tracking_no="")
        html = self.client.get(f"/orders/{order_id}/tracking").text
        self.assertIn("还没填运单号", html)


if __name__ == "__main__":
    unittest.main()
