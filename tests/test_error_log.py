"""异常日志（2026-10-09）：线上报错要留痕 —— 服务端未捕获异常 / 前端非 JSON 响应。

**为什么要这些测试**：这个机制的起因是用户上传图片失败，页面上只显示
「上传失败，请重试」，截图里那句 ``Unexpected token '<'`` 谁都读不懂 ——
那是后端 500 返回了 HTML 错误页、前端却按 JSON 解析。所以要盯住四件事：

1. 未捕获异常 → 该给 JSON 的**必须给 JSON**（前端再也不会拿到 HTML），并且落一条带堆栈的记录；
2. 页面导航出错 → 给人话 500 页面（带编号），不是一屏堆栈；
3. 上传接口的**解析阶段**出错也要回 JSON 400（以前只兜住了写盘）；
4. 前端上报接口 + 「异常日志」页（管理员专属）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi import Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ErrorLog  # noqa: E402
from app.services import error_log as error_log_service  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402


def _wipe() -> None:
    with SessionLocal() as s:
        for row in s.scalars(select(ErrorLog)).all():
            s.delete(row)
        s.commit()


async def _boom(request: Request):  # noqa: ARG001
    """故意炸一个：模拟「某个接口里抛了没被接住的异常」。"""
    raise RuntimeError("故意炸一个（异常日志测试）")


def _last() -> ErrorLog:
    with SessionLocal() as s:
        return s.scalars(select(ErrorLog).order_by(ErrorLog.id.desc())).first()


class ErrorLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        # 未捕获异常要真的能拿到 500 响应（默认 TestClient 会把异常再抛出来）
        cls.client = TestClient(app, raise_server_exceptions=False)
        # 两个临时路由：一个走接口前缀、一个走页面路径
        cls.routes = [
            APIRoute("/api/__boom_test", _boom, methods=["GET"]),
            APIRoute("/__boom_page_test", _boom, methods=["GET"]),
        ]
        app.router.routes.extend(cls.routes)

    @classmethod
    def tearDownClass(cls):
        for route in cls.routes:
            try:
                app.router.routes.remove(route)
            except ValueError:
                pass

    def setUp(self):
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def test_10_api_error_is_json_and_logged(self):
        """接口炸了：返回 JSON（不是 HTML），并落一条带堆栈的记录。"""
        resp = self.client.get("/api/__boom_test", headers={"accept": "*/*"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("application/json", resp.headers["content-type"])
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["log_id"], "要给用户一个能报出来的编号")
        with SessionLocal() as s:
            row = s.get(ErrorLog, int(body["log_id"]))
        self.assertEqual(row.exc_type, "RuntimeError")
        self.assertEqual(row.kind, "server")
        self.assertEqual(row.scope, "app")
        self.assertIn("RuntimeError", row.traceback, "堆栈要留下来，不然查不出是哪一行")
        self.assertIn("__boom_test", row.path)
        self.assertEqual(row.username, "admin", "要记下是谁点的")
        self.assertEqual(row.status_code, 500)

    def test_11_api_path_gives_json_even_if_accept_html(self):
        """接口路径一律 JSON —— fetch 的 Accept 千奇百怪，不能让它拿到 HTML。"""
        resp = self.client.get("/api/__boom_test", headers={"accept": "text/html"})
        self.assertIn("application/json", resp.headers["content-type"])

    def test_12_page_error_is_a_friendly_html_page(self):
        """页面导航出错：给人话 + 编号，不甩堆栈。（fetch 来要还是 JSON）"""
        page = self.client.get("/__boom_page_test", headers={"accept": "text/html"})
        self.assertEqual(page.status_code, 500)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("系统出了点问题", page.text)
        self.assertIn("异常日志", page.text)
        self.assertNotIn("Traceback", page.text, "堆栈不能给用户看")
        # 同一个地址，fetch 来的（Accept 不含 text/html）必须给 JSON
        fetch = self.client.get("/__boom_page_test", headers={"accept": "*/*"})
        self.assertIn("application/json", fetch.headers["content-type"])

    def test_13_scope_of_paths(self):
        """三个子系统共一张表，靠路径分 scope。"""
        self.assertEqual(error_log_service.scope_of("/api/__boom"), "app")
        self.assertEqual(error_log_service.scope_of("/orders"), "app")
        self.assertEqual(error_log_service.scope_of("/wms/items/1"), "wms")
        self.assertEqual(error_log_service.scope_of("/self/products"), "hub")
        self.assertEqual(error_log_service.scope_of("/api/v1/orders"), "hub")
        self.assertTrue(error_log_service.scope_label("wms"))

    def test_14_upload_bad_multipart_still_returns_json(self):
        """上传解析阶段出错（图片没传完整 / 被网关截断）也要给 JSON 人话，不能是 HTML。"""
        resp = self.client.post(
            "/products/upload-image",
            content=b"this-is-not-a-real-multipart-body",
            headers={"Content-Type": "multipart/form-data; boundary=----x",
                     "accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/json", resp.headers["content-type"])
        self.assertIn("图片没传完整", resp.json()["error"])
        self.assertIsNotNone(_last(), "解析失败也要记一条（含 traceback）")

    def test_15_upload_still_works(self):
        """正常上传不能被这层兜底破坏。"""
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        resp = self.client.post(
            "/products/upload-image",
            files={"file": ("t.png", png, "image/png")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["url"].startswith("/media/products/"))

    def test_16_upload_over_size_limit_is_413(self):
        """超过单张上限要拒（直连 8000 时没有 nginx 那层限制，全靠这道闸）。"""
        from app.config import settings

        limit_mb = int(getattr(settings, "upload_image_max_mb", 5) or 0)
        self.assertTrue(limit_mb > 0, "测试环境默认要有上限")
        big = b"\x89PNG\r\n\x1a\n" + b"0" * (limit_mb * 1024 * 1024 + 1024)
        resp = self.client.post(
            "/products/upload-image",
            files={"file": ("big.png", big, "image/png")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 413)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn(f"{limit_mb}MB", body["error"], "要说清上限是多少")
        self.assertIn("MB", body["error"], "要告诉他这张图实际多大")
        # 超限的不该落盘
        folder = Path(settings.media_path) / "products"
        self.assertFalse(
            [p for p in folder.glob("*.png") if p.stat().st_size > 1024 * 1024],
            "超限的图不能写进磁盘",
        )

    def test_17_upload_format_must_be_real_image(self):
        """改后缀蒙混不了：内容不是图片就拒。"""
        resp = self.client.post(
            "/products/upload-image",
            files={"file": ("evil.png", b"<script>alert(1)</script>" * 10, "image/png")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不是真图片", resp.json()["error"])
        # 扩展名不在白名单里也拒
        pdf = self.client.post(
            "/products/upload-image",
            files={"file": ("x.pdf", b"%PDF-1.4" + b"0" * 64, "application/pdf")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(pdf.status_code, 400)
        self.assertIn("只支持", pdf.json()["error"])

    def test_18_upload_limit_shows_on_page(self):
        """发布页要写明单张上限（前端拿同一份值做本地校验）。"""
        html = self.client.get("/products/new").text
        self.assertIn("UPLOAD_MAX_MB", html)
        self.assertIn("单张 ≤", html, "按钮旁要写明上限，别让人传完才知道")
        self.assertIn("checkFile", html, "选完图要在本地先过一遍")

    def test_20_client_report_is_logged(self):
        """前端上报：把「拿到的非 JSON 响应原文 + 状态码」存下来 —— 这是最有用的一手线索。"""
        resp = self.client.post(
            "/api/client-error",
            json={"url": "/products/upload-image", "status": 502,
                  "body": "<html><head><title>502 Bad Gateway</title></head>",
                  "message": "页面收到非 JSON 响应（HTTP 502）"},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["log_id"])
        row = _last()
        self.assertEqual(row.kind, "client")
        self.assertEqual(row.status_code, 502)
        self.assertIn("502 Bad Gateway", row.detail)
        self.assertIn("upload-image", row.detail)

    def test_21_client_report_never_500(self):
        """上报接口自己绝不能炸（否则前端重试会把日志刷爆）。"""
        resp = self.client.post("/api/client-error", content=b"not json at all",
                                headers={"accept": "*/*"})
        self.assertEqual(resp.status_code, 200)

    def test_30_page_is_admin_only(self):
        """异常日志页：管理员能进，看得到记录与堆栈。"""
        error_log_service.record(kind="server", path="/wms/items",
                                 exc=ValueError("测试记录"),
                                 traceback_text="Traceback (most recent call last): ...")
        page = self.client.get("/errors")
        self.assertEqual(page.status_code, 200)
        self.assertIn("异常日志", page.text)
        self.assertIn("测试记录", page.text)
        self.assertIn("展开现场", page.text, "堆栈要能展开看")
        # 非管理员（不存在 / 非 admin 的会话）只能被退回首页
        self.client.cookies.set(COOKIE_NAME, make_session(9999)[0])
        denied = self.client.get("/errors", follow_redirects=False)
        self.assertIn(denied.status_code, (302, 303, 307))

    def test_31_prune_drops_old_rows(self):
        """清理按钮：只清 30 天前的，近期的必须留着。"""
        from datetime import timedelta

        from app.timeutil import now

        with SessionLocal() as s:
            s.add(ErrorLog(scope="app", kind="server", path="/old", message="老记录",
                           created_at=now() - timedelta(days=40)))
            s.add(ErrorLog(scope="app", kind="server", path="/new", message="新记录",
                           created_at=now()))
            s.commit()
            removed = error_log_service.prune(s, 30)
        self.assertEqual(removed, 1)
        with SessionLocal() as s:
            paths = [r.path for r in s.scalars(select(ErrorLog)).all()]
        self.assertEqual(paths, ["/new"])
