"""订单详情页维护订单截图 + 首页统计块可点击 + 趋势图撑满（2026-09-25 版本）。

业务口径：
  1 订单详情页支持上传 / 更新订单截图：系统重新 OCR 解析并把订单编号、成交价、
    收货信息补全到订单；如果解析出的收货人 / 手机号与订单本身对不上，直接拒绝并提示
    「订单截图上传不对，请重新上传」；
  2 首页「今日新增 / 待推单发货 / 待人工确认」统计块与「待供应商回单」一样可点击进列表；
  3 「近 30 天销售额趋势」柱状图撑满整个模块高度。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import timedelta
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, RawMessage  # noqa: E402
from app.parser.ocr import OcrText  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 与订单收货人一致的截图 OCR（正确截图）
OCR_OK = """高艳鑫 18640345632
浙江省杭州市萧山区新街街道天明国际产业园2号楼1单元438室
倍思Q2 20000毫安30W自带线充电宝 ¥59.00
成交价 (在支付宝担保账户中)             ¥50.00
订单编号       5127743052126267544 | 复制
"""

# 收货人是别人（上传错图）
OCR_WRONG = """李雷 <no-reply> 13900000000
广东省深圳市南山区科技园1号楼
某商品 ¥199.00
订单编号       8888888888888888 | 复制
"""

REAL_ORDER_NO = "5127743052126267544"


def _patch_ocr(text: str = OCR_OK):
    """固定 OCR 结果 + 跳过商品图裁剪（测试不依赖真实图片 / OCR 服务）。"""
    return (
        mock.patch(
            "app.services.ingest.ocr_image_ex", return_value=OcrText(text=text, status="done")
        ),
        mock.patch("app.services.ingest.crop_product_image", return_value=None),
    )


class ManualScreenshotTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        for path in settings.media_path.rglob("manual_*"):
            path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- 工具
    def _order(self, order_no: str = "TMP-20260925-0001", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                temp_order_no=order_no,
                receiver_name="高艳鑫",
                receiver_phone="18640345632",
                product_name="倍思Q2 20000毫安30W自带线充电宝",
                sale_amount=59.0,
                needs_review=True,
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _upload(self, order_id: int, data: bytes, filename: str = "shot.png"):
        return self.client.post(
            f"/orders/{order_id}/screenshot",
            files={"screenshot": (filename, data, "image/png")},
            follow_redirects=True,
        )

    def _manual_files(self) -> set[str]:
        return {p.name for p in settings.media_path.rglob("manual_*")}

    # ---------------------------------------------------------------- 上传解析
    def test_01_upload_parses_and_backfills_order(self):
        order_id = self._order()
        a, b = _patch_ocr()
        with a, b:
            resp = self._upload(order_id, b"\x89PNG\r\n\x1a\n-fake")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("订单截图已更新", resp.text)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.order_no, REAL_ORDER_NO, "临时编号升级成截图里的真实编号")
            self.assertEqual(order.temp_order_no, "TMP-20260925-0001", "原临时编号保留备查")
            self.assertEqual(order.deal_amount, 50.0, "成交价从截图补齐")
            self.assertFalse(order.needs_review, "解析完整后不再需要人工确认")
            imgs = s.query(OrderImage).filter(OrderImage.order_id == order_id).all()
            self.assertEqual(len(imgs), 1)
            self.assertTrue(pathlib.Path(imgs[0].local_path).exists(), "截图文件落盘")
            self.assertIn("订单编号", imgs[0].ocr_text)

    def test_02_wrong_receiver_is_rejected_with_hint(self):
        order_id = self._order()
        before = self._manual_files()
        a, b = _patch_ocr(OCR_WRONG)
        with a, b:
            resp = self._upload(order_id, b"\x89PNG\r\n\x1a\n-fake")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("订单截图上传不对", resp.text)
        self.assertIn("请重新上传", resp.text)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.order_no, "TMP-20260925-0001", "拒绝后订单不动")
            self.assertEqual(s.query(OrderImage).filter(OrderImage.order_id == order_id).count(), 0)
        self.assertEqual(self._manual_files(), before, "拒绝的截图不留在服务器上")

    def test_03_non_image_file_is_rejected(self):
        order_id = self._order()
        a, b = _patch_ocr()
        with a, b:
            resp = self._upload(order_id, b"hello", filename="note.txt")
        self.assertIn("只支持图片格式", resp.text)
        with SessionLocal() as s:
            self.assertEqual(s.query(OrderImage).count(), 0)

    def test_04_upload_replaces_existing_screenshot(self):
        order_id = self._order()
        old_file = settings.media_path / "202609" / "old_shot.png"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"\x89PNG\r\n\x1a\n-old")
        with SessionLocal() as s:
            s.add(
                OrderImage(
                    order_id=order_id,
                    message_id="om_old",
                    filename=old_file.name,
                    local_path=str(old_file),
                    ocr_status="done",
                    ocr_text="旧截图",
                )
            )
            s.commit()

        a, b = _patch_ocr()
        with a, b:
            resp = self._upload(order_id, b"\x89PNG\r\n\x1a\n-new")
        self.assertIn("订单截图已更新", resp.text)
        self.assertFalse(old_file.exists(), "更新截图后旧图不再保留")
        with SessionLocal() as s:
            imgs = s.query(OrderImage).filter(OrderImage.order_id == order_id).all()
            self.assertEqual(len(imgs), 1, "以最新一张为准")
            self.assertNotEqual(imgs[0].local_path, str(old_file))

    def test_05_ocr_disabled_still_keeps_uploaded_image(self):
        """没启用 OCR 时：图能上传，只是没法自动解析字段，提示说明清楚。"""
        order_id = self._order()
        with mock.patch(
            "app.services.ingest.ocr_image_ex", return_value=OcrText(status="skipped")
        ), mock.patch("app.services.ingest.crop_product_image", return_value=None):
            resp = self._upload(order_id, b"\x89PNG\r\n\x1a\n-fake")
        self.assertIn("订单截图已更新", resp.text)
        self.assertIn("没能自动解析", resp.text)
        with SessionLocal() as s:
            self.assertEqual(s.query(OrderImage).filter(OrderImage.order_id == order_id).count(), 1)

    # ---------------------------------------------------------------- 首页可点击
    def test_06_dashboard_stats_are_clickable(self):
        self._order(order_no="DASH-9", supplier_status="pending")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/orders?created=today", page.text, "今日新增点进去看今天的订单")
        self.assertIn("/orders?supplier_status=pending", page.text, "待推单发货点进去处理")
        self.assertIn("/orders?review=1", page.text, "待人工确认点进去逐单确认")
        self.assertIn("/orders?state=shipped", page.text, "已发货点进去看已发货列表")
        hero = page.text.split('class="hero-stats"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(hero.count("hero-stat"), 5, "顶部 5 个可点统计块")
        self.assertEqual(hero.count("href="), 5, "5 个统计块都带链接")

    def test_07_orders_filter_by_created_today(self):
        today_id = self._order(order_no="TODAY-1")
        with SessionLocal() as s:
            old = Order(
                platform="xianyu",
                order_no="OLD-1",
                created_at=now() - timedelta(days=3),
                msg_time=now() - timedelta(days=3),
            )
            s.add(old)
            s.commit()

        page = self.client.get("/orders?created=today")
        self.assertEqual(page.status_code, 200)
        self.assertIn("TODAY-1", page.text)
        self.assertNotIn("OLD-1", page.text, "3 天前的订单不算今日新增")
        self.assertIn("今日新增", page.text, "筛选下拉里有「今日新增」")

        with SessionLocal() as s:
            self.assertIsNotNone(s.get(Order, today_id))

    # ---------------------------------------------------------------- 趋势图
    def test_08_trend_chart_fills_card_height(self):
        """柱状图必须撑满整个区块高度（宽屏与右侧卡片等高、窄屏撑满自身卡片）。

        做法：卡片竖向 flex -> chart-body 吃掉剩余高度 -> .bars 绝对定位铺满它，
        柱子的百分比高度才有确定参照（各浏览器表现一致，不再留一大片空白）。
        """
        page = self.client.get("/")
        self.assertIn("chart-card", page.text, "趋势图卡片带 chart-card 用于撑满高度")
        self.assertIn("chart-body", page.text, "柱状区外层要有 chart-body 承接剩余高度")
        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".card.chart-card", css)
        self.assertIn(".card.chart-card .chart-body", css, "chart-body 撑满卡片剩余高度")
        self.assertIn(".card.chart-card .chart-body .bars", css, "柱状区绝对定位铺满 chart-body")
        self.assertIn("position: absolute; inset: 0", css, "柱状区高度确定，柱子才能撑满")


if __name__ == "__main__":
    unittest.main()
