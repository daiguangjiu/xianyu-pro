"""两件事（2026-10-13 用户拍板）：

**① 时间全站统一按服务端时区（默认北京时间）显示。**
以前页面时间跟着浏览器 cookie 里的时区走 —— 同一条数据，坐在不同时区的电脑上看
是两个点，这就是「上传商品的时间戳不是北京时间」的根因。现在：

  - 库里**仍然存 UTC**（内部一致，历史数据不用搬）；
  - 展示 / 传输 / 导出一律按 ``settings.display_tz``（默认 Asia/Shanghai），
    **浏览器带进来的时区一律不认**；
  - 真要按各自时区看，把 ``TZ_FOLLOW_BROWSER`` 设成 1（门留着，默认关）；
  - 模板里不许再直接 ``.strftime()`` —— 那样出来的是库里的 UTC 值，会比北京早 8 小时。

**② 商家编码（商品流水 ID）可以手改。**
系统仍然自动出号（XYSK + 日期 + 序号），但编辑页不再锁死 —— 可以改成自己的编号
（比如渔集条码 QCHA26100366）。改完 SKU 编码（商家编码-01 / -02）会跟着重排，
已上架到渔货栈的会在保存后自动回传。
**唯一的例外**是从渔集扫码带出来的条码：那是渔集与渔枢的关联键，JS 会把它锁上。
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductSku  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.timeutil import (  # noqa: E402
    display_tz,
    fmt,
    start_of_local_day,
    to_iso,
    to_local,
)
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class DisplayTzTest(unittest.TestCase):
    """展示时区：全站统一，不看浏览器脸色。"""

    def test_10_default_is_beijing(self):
        """默认就是北京时间，且不认外面带进来的时区。"""
        self.assertEqual(display_tz(), "Asia/Shanghai")
        value = datetime(2026, 10, 13, 7, 58)  # UTC = 北京 15:58
        self.assertEqual(to_local(value), datetime(2026, 10, 13, 15, 58))
        self.assertEqual(fmt(value), "2026-10-13 15:58")
        # 外面塞个纽约时区进来也不算数（TZ_FOLLOW_BROWSER 默认关）
        self.assertEqual(fmt(value, "%Y-%m-%d %H:%M", "America/New_York"), "2026-10-13 15:58")

    def test_20_iso_carries_beijing_offset(self):
        """接口 / 导出出来的 ISO 串带 +08:00，跟页面显示的是同一个点。"""
        self.assertEqual(to_iso(datetime(2026, 10, 13, 7, 58)), "2026-10-13T15:58:00+08:00")

    def test_30_config_can_move_the_whole_site(self):
        """改 DISPLAY_TZ 全站跟着变（真有海外团队时靠它）。"""
        saved = settings.display_tz
        settings.display_tz = "Asia/Tokyo"
        try:
            self.assertEqual(fmt(datetime(2026, 10, 13, 7, 58)), "2026-10-13 16:58")
        finally:
            settings.display_tz = saved
        self.assertEqual(fmt(datetime(2026, 10, 13, 7, 58)), "2026-10-13 15:58", "还原回北京")

    def test_40_day_boundary_follows_display_tz(self):
        """按天统计的边界（日报 / 今天）也按统一时区，不按浏览器的。"""
        # 北京 10-13 00:00 = UTC 10-12 16:00
        self.assertEqual(
            start_of_local_day(at=datetime(2026, 10, 13, 3, 0)), datetime(2026, 10, 12, 16, 0)
        )
        self.assertEqual(
            start_of_local_day("America/New_York", datetime(2026, 10, 13, 3, 0)),
            datetime(2026, 10, 12, 16, 0),
            "传别的时区也不生效，统一按北京",
        )


class ProductCodeEditableTest(unittest.TestCase):
    """商家编码可以手改（改完 SKU 编码跟着重排）。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ProductSku).delete()
            s.query(Product).delete()
            s.commit()

    def tearDown(self) -> None:
        self.setUp()

    def _product(self, session, name="编码可改测试", with_skus=True):
        payload = {"name": name, "category": "家用电器 / 耳机音响", "price": "199"}
        if with_skus:
            payload["skus_json"] = (
                '[{"spec":"黑","price":"199","stock":"2"},'
                '{"spec":"白","price":"219","stock":"3"}]'
            )
        return product_service.create_product(session, payload)

    def test_60_edit_page_code_input_is_editable(self):
        """编辑页的商家编码框**不再是 readonly**（以前锁死，改了也存不进去）。"""
        with SessionLocal() as s:
            product = self._product(s, with_skus=False)
            pid = product.id
        html = self.client.get(f"/products/{pid}/edit").text
        tag = re.search(r'<input[^>]*id="codeInput"[^>]*>', html)
        self.assertIsNotNone(tag, "编辑页要有商家编码输入框")
        self.assertNotIn("readonly", tag.group(0), "编码框不能再锁死")

    def test_70_change_code_and_skus_follow(self):
        """改编码：落库 + SKU 编码按新编码重排（商家编码-01 / -02）。"""
        with SessionLocal() as s:
            product = self._product(s)
            old = product.code
            self.assertTrue(old.startswith("XYSK"))
            self.assertEqual(
                [x.sku_code for x in product_service.list_skus(s, product.id)],
                [f"{old}-01", f"{old}-02"],
            )
            product_service.update_product(s, product, {"code": "QCHA26100366"})
            self.assertEqual(product.code, "QCHA26100366")
            self.assertEqual(
                [x.sku_code for x in product_service.list_skus(s, product.id)],
                ["QCHA26100366-01", "QCHA26100366-02"],
                "SKU 编码跟着商家编码重排",
            )

    def test_75_change_through_page(self):
        """走页面保存同样生效（编辑页提交 form → 落库）。"""
        with SessionLocal() as s:
            product = self._product(s, with_skus=False)
            pid = product.id
        resp = self.client.post(
            f"/products/{pid}/edit",
            data={"name": "编码可改测试", "code": "MY-CODE-001"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, pid).code, "MY-CODE-001")

    def test_80_clash_is_rejected(self):
        """编码撞了别拦下并说清楚被谁占了（不能闷声保存成重复的号）。"""
        with SessionLocal() as s:
            first = self._product(s, "先来的", with_skus=False)
            second = self._product(s, "后到的", with_skus=False)
            with self.assertRaises(ValueError) as ctx:
                product_service.update_product(s, second, {"code": first.code})
            self.assertIn("已被商品", str(ctx.exception))
            self.assertIn("先来的", str(ctx.exception))

    def test_90_wms_barcode_still_locked(self):
        """渔集扫码带出来的条码仍然锁死 —— 那是两边的关联键，改了库存同步不回来。"""
        with SessionLocal() as s:
            product = self._product(s, with_skus=False)
            pid = product.id
        html = self.client.get(f"/products/{pid}/edit").text
        self.assertIn("setAttribute('readonly', 'readonly')", html, "扫码带出时要由 JS 锁上")
        self.assertIn("关联键", html)


if __name__ == "__main__":
    unittest.main()
