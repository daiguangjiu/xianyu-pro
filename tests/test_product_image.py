"""商品图：从订单截图里自动裁出商品缩略图，并展示到订单详情与订单列表。

业务口径（2026-09）：
  1 详情页左侧保留群里的订单截图原图，商品图是从这张截图里自动裁出的方图；
  2 裁剪不依赖 OCR 供应商：先按「彩色内容」锁定商品主体，再向卡片白底扩展出
    缩略图真实边界，最后取正方形；定位不到就退回 OCR 词框，仍失败则留空；
  3 订单列表新增「商品图」列，直接复用裁出的这张图；
  4 历史订单可用 `order-hub crop-images [--force]` 或详情页「重新裁取」补图。
"""
from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, PushBatchItem, RawMessage, VoiceEvent  # noqa: E402
from app.parser.ocr import _locate_product_thumb, crop_product_image  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
WORK_DIR = ROOT / "data" / "test_product_images"

# 合成截图里的商品缩略图位置（原图坐标），供断言比对
THUMB_BOX = (60, 400, 260, 580)


def _make_screenshot(path: Path, box: tuple[int, int, int, int] = THUMB_BOX) -> Path:
    """造一张「很像闲鱼订单详情」的截图：白底 + 黑色文字行 + 彩色商品缩略图。"""
    width, height = 720, 1560
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    # 顶部标题 / 收货人 / 地址：深色文字（通道差很小，不该被当成商品图）
    for y in (60, 120, 178, 232):
        draw.rectangle((40, y, 40 + width // 3, y + 26), fill=(70, 70, 70))
    # 商品行：左侧彩色商品缩略图 + 右侧商品名
    x0, y0, x1, y1 = box
    palette = [(232, 62, 62), (58, 178, 92), (66, 112, 232), (240, 188, 58)]
    step = 20
    for i in range(0, x1 - x0, step):
        for j in range(0, y1 - y0, step):
            color = palette[((i // step) + (j // step)) % len(palette)]
            draw.rectangle(
                (x0 + i, y0 + j, min(x0 + i + step - 1, x1), min(y0 + j + step - 1, y1)),
                fill=color,
            )
    draw.rectangle((x1 + 30, y0 + 10, x1 + 280, y0 + 46), fill=(60, 60, 60))
    draw.rectangle((x1 + 30, y0 + 70, x1 + 220, y0 + 104), fill=(60, 60, 60))
    # 成交价 / 商品总价 / 运费
    for y in (y1 + 40, y1 + 100, y1 + 160):
        draw.rectangle((40, y, 260, y + 26), fill=(96, 96, 96))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    return path


class ProductImageCropTest(unittest.TestCase):
    def setUp(self) -> None:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.src = _make_screenshot(WORK_DIR / "shot.png")

    def tearDown(self) -> None:
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    def test_locate_product_thumb(self):
        """定位到的方块要覆盖合成截图里的商品缩略图（中心 / 边长都在容差内）。"""
        with Image.open(self.src) as raw:
            box = _locate_product_thumb(raw.convert("RGB"))
        self.assertIsNotNone(box, "应能从截图里定位到商品缩略图")
        left, top, right, bottom = box
        side = right - left
        self.assertEqual(side, bottom - top, "裁出来的应该是正方形")
        expect = THUMB_BOX
        expect_side = max(expect[2] - expect[0], expect[3] - expect[1])
        expect_cx = (expect[0] + expect[2]) / 2
        expect_cy = (expect[1] + expect[3]) / 2
        self.assertLess(abs((left + right) / 2 - expect_cx), 30, "横向定位偏差过大")
        self.assertLess(abs((top + bottom) / 2 - expect_cy), 30, "纵向定位偏差过大")
        self.assertLess(abs(side - expect_side), expect_side * 0.35, "边长偏差过大")

    def test_crop_saves_square_png(self):
        out = WORK_DIR / "shot_product.png"
        result = crop_product_image(self.src, out)
        self.assertEqual(result, str(out))
        with Image.open(out) as crop:
            self.assertEqual(crop.size[0], crop.size[1], "商品图应为方图")
            self.assertGreaterEqual(crop.size[0], 40)

    def test_crop_skips_blank_screenshot(self):
        """纯白截图（没版式）不该裁出商品图，宁缺勿错。"""
        blank = Image.new("RGB", (720, 1560), "white")
        blank_path = WORK_DIR / "blank.png"
        blank.save(blank_path, "PNG")
        self.assertIsNone(crop_product_image(blank_path, WORK_DIR / "blank_product.png"))
        self.assertFalse((WORK_DIR / "blank_product.png").exists())


class ProductImageBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.shot = _make_screenshot(WORK_DIR / "backfill_shot.png")
        with SessionLocal() as session:
            session.query(PushBatchItem).delete()
            session.query(OrderImage).delete()
            session.query(RawMessage).delete()
            session.query(VoiceEvent).delete()
            session.query(Order).delete()
            session.commit()
            order = Order(platform="xianyu", order_no="P-1", product_name="充电宝")
            session.add(order)
            session.flush()
            session.add(
                OrderImage(
                    order_id=order.id,
                    message_id="im_1",
                    local_path=str(self.shot),
                    ocr_status="done",
                )
            )
            session.commit()
            self.order_id = order.id

    def tearDown(self) -> None:
        with SessionLocal() as session:
            session.query(OrderImage).delete()
            session.query(Order).delete()
            session.commit()
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    def test_backfill_then_recrop(self):
        with SessionLocal() as session:
            stats = ingest_service.backfill_product_images(session)
        self.assertEqual(stats["cropped"], 1, "缺商品图的订单应被补上")
        with SessionLocal() as session:
            order = session.get(Order, self.order_id)
            self.assertTrue(order.product_image_path)
            self.assertTrue(Path(order.product_image_path).exists())

        # 再跑一次：已有商品图，默认跳过
        with SessionLocal() as session:
            stats = ingest_service.backfill_product_images(session)
            self.assertEqual(stats["scanned"], 0)
            # force: 覆盖重裁
            stats = ingest_service.backfill_product_images(session, force=True)
            self.assertEqual(stats["cropped"], 1)

    def test_recrop_endpoint_and_list_column(self):
        client = TestClient(app)
        ensure_default_admin()
        resp = client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

        page = client.post(
            f"/orders/{self.order_id}/product-image", follow_redirects=False
        )
        self.assertEqual(page.status_code, 303)
        with SessionLocal() as session:
            order = session.get(Order, self.order_id)
            self.assertTrue(order.product_image_path)
            file_name = Path(order.product_image_path).name

        listing = client.get("/orders")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("<th>商品图</th>", listing.text, "订单列表要有商品图列")
        self.assertIn(file_name, listing.text, "列表里要显示裁出的商品图")


if __name__ == "__main__":
    unittest.main()
