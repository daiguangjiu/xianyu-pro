"""物流轨迹独立服务（三端共用 + 本地存档）回归。

口径（2026-10-13）：渔货栈 / 渔枢 / 渔集查的是同一个菜鸟接口、同一批运单号，
所以查询收成一个服务，查完在本地留一份存档：
  · 在途单 1 小时之内再来查 → 直接给存档，不打菜鸟；
  · 终态单（签收 / 退回 / 拦截）→ 永不再查；
  · 查不到轨迹 → 空结果短缓存（30 分钟）；
  · 查询失败 → **保留**上次轨迹（stale=True），不把轨迹清空。

另外锁住两个解析坑（同一天修的，不锁就会悄悄退化成「永远查不到轨迹」）：
  · 菜鸟把轨迹放在 result.data 里，而 data 又是一层 JSON 字符串，必须拆开；
  · 节点字段叫 opMessage / opTime / cityName，不是 desc / time / station。
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

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import LogisticsTrace  # noqa: E402
from app.services import logistics_trace as trace_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
MAIL = "TEST7903912345"


def _cainiao_body(state: str = "TRANSPORT", count: int = 2) -> str:
    """菜鸟真实响应结构：轨迹在 result.data 里，data 本身是 JSON 字符串。"""
    detail = [
        {
            "cpCode": "ZTO",
            "cityName": "深圳市",
            "opMessage": f"【深圳市】快件已到达第 {i} 站",
            "opTime": f"2026-10-13 0{i}:38:51",
        }
        for i in range(1, count + 1)
    ]
    data = json.dumps(
        {"lastTime": "2026-10-13T04:38:51", "lastState": state,
         "lastStateDesc": "运输中", "detail": detail},
        ensure_ascii=False,
    )
    return json.dumps({"result": {"code": "0", "data": data}}, ensure_ascii=False)


class _Resp:
    status_code = 200

    def __init__(self, text: str):
        self.text = text


class TraceTest(unittest.TestCase):
    def setUp(self):
        init_db()
        ensure_default_admin()
        self.db = SessionLocal()
        self.db.query(LogisticsTrace).filter(
            LogisticsTrace.mail_no == MAIL
        ).delete()
        self.db.commit()
        # 当启用菜鸟（凭据用假值，请求会被 mock 掉，不会真联网）
        self._saved = {
            "enabled": settings.cainiao_enabled,
            "secret": settings.cainiao_app_secret,
            "cp": settings.cainiao_cp_code,
            "gateway": settings.cainiao_gateway_url,
            "msg": settings.cainiao_msg_type,
            "app": settings.cainiao_app_code,
        }
        settings.cainiao_enabled = True
        settings.cainiao_app_secret = "s" * 32
        settings.cainiao_cp_code = "c" * 32
        settings.cainiao_gateway_url = "http://cainiao.test/gateway/link.do"
        settings.cainiao_msg_type = "CNTECH_LV_LOGISTICS_DETAIL_GET"
        settings.cainiao_app_code = "XHHD"

    def tearDown(self):
        for key, val in self._saved.items():
            setattr(settings, f"cainiao_{key}" if key != "enabled" else "cainiao_enabled", val)
        self.db.query(LogisticsTrace).filter(
            LogisticsTrace.mail_no == MAIL
        ).delete()
        self.db.commit()
        self.db.close()

    def _call(self, body: str):
        return mock.patch(
            "app.services.logistics.requests.post",
            return_value=_Resp(body),
        )

    # ---------- 解析层 ----------
    def test_10_parses_cainiao_detail(self):
        """能从 result.data（JSON 字符串）里解析出 opMessage 轨迹。"""
        from app.services.logistics import parse_traces, _parse_response

        ok, msg, payload = _parse_response(_cainiao_body())
        self.assertTrue(ok, msg)
        nodes = parse_traces(payload)
        self.assertEqual(len(nodes), 2)
        self.assertIn("快件已到达", nodes[0]["desc"])
        self.assertEqual(nodes[0]["station"], "深圳市")

    def test_12_maps_status_from_last_state(self):
        """状态码在 result.data.lastState，不是响应顶层。"""
        from app.services.logistics import map_status, _parse_response

        ok, _, payload = _parse_response(_cainiao_body(state="SIGN"))
        self.assertTrue(ok)
        self.assertEqual(map_status(payload), "signed")

    def test_14_error_response_not_treated_as_success(self):
        """result.code=S13 要当失败，不能被当成「成功但没轨迹」。"""
        from app.services.logistics import _parse_response

        body = json.dumps(
            {"result": {"code": "S13", "msg": "Source must not be null"}},
            ensure_ascii=False,
        )
        ok, msg, _ = _parse_response(body)
        self.assertFalse(ok)
        self.assertIn("S13", msg)

    # ---------- 存档层 ----------
    def test_20_second_lookup_uses_cache(self):
        """第一次真查落存档，第二次直接给存档（不再调菜鸟）。"""
        with self._call(_cainiao_body()):
            first = trace_service.lookup(self.db, MAIL)
            self.assertFalse(first["from_cache"])
            self.assertEqual(len(first["nodes"]), 2)
            self.db.commit()
            second = trace_service.lookup(self.db, MAIL)
        self.assertTrue(second["from_cache"])
        row = self.db.query(LogisticsTrace).filter_by(mail_no=MAIL).one()
        self.assertEqual(row.query_count, 1)  # 只真查了一次
        self.assertGreaterEqual(row.hit_count, 1)

    def test_30_force_skips_cache(self):
        """force=1 绕过存档实时查。"""
        with self._call(_cainiao_body()) as post:
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
            trace_service.lookup(self.db, MAIL, force=True)
            self.assertEqual(post.call_count, 2)

    def test_40_final_status_never_refreshes(self):
        """终态（已签收）不再自动刷新。"""
        with self._call(_cainiao_body(state="SIGN")) as post:
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
            out = trace_service.lookup(self.db, MAIL)
            self.assertTrue(out["is_final"])
            self.assertTrue(out["from_cache"])
            self.assertEqual(post.call_count, 1)
        row = self.db.query(LogisticsTrace).filter_by(mail_no=MAIL).one()
        self.assertIsNone(row.next_refresh_at)

    def test_50_failure_keeps_old_trace(self):
        """查询失败：保留上次轨迹（stale=True），不把轨迹清空。"""
        with self._call(_cainiao_body()):
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
        with self._call(json.dumps({"result": {"code": "S01", "msg": "服务不可用"}})):
            out = trace_service.lookup(self.db, MAIL, force=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(len(out["nodes"]), 2)  # 旧轨迹还在
        row = self.db.query(LogisticsTrace).filter_by(mail_no=MAIL).one()
        self.assertEqual(len(row.nodes), 2)
        self.assertTrue(row.last_error)

    def test_60_empty_trace_short_cache(self):
        """查得到单号但没轨迹：空结果短缓存，不会每次都刷接口。"""
        empty = json.dumps(
            {"result": {"code": "0", "data": json.dumps({"detail": []})}},
            ensure_ascii=False,
        )
        with self._call(empty) as post:
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
            after_first = post.call_count
            trace_service.lookup(self.db, MAIL)
            self.assertEqual(post.call_count, after_first)  # 第二次没再打接口
        row = self.db.query(LogisticsTrace).filter_by(mail_no=MAIL).one()
        self.assertFalse(row.nodes)
        self.assertIsNotNone(row.next_refresh_at)

    def test_70_stats_counts_saved_calls(self):
        """stats 能看出省了多少次接口调用。"""
        with self._call(_cainiao_body()):
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
            trace_service.lookup(self.db, MAIL)
            self.db.commit()
        st = trace_service.stats(self.db)
        self.assertGreaterEqual(st["api_calls"], 1)
        self.assertGreaterEqual(st["cache_hits"], 1)

    # ---------- HTTP 入口 ----------
    def test_80_api_endpoint(self):
        """GET /api/logistics/trace 三端 / 外部调用入口。"""
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        with self._call(_cainiao_body()):
            r = client.get(f"/api/logistics/trace?mail_no={MAIL}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["nodes"]), 2)
        self.assertFalse(body["from_cache"])

    def test_82_api_needs_mail_no(self):
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        r = client.get("/api/logistics/trace?mail_no=")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
