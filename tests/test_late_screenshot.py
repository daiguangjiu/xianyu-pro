"""补发截图回填 + 消息「过滤原因」（重复发送相同内容）。

业务口径（2026-09）：
  1 群里先发了订单模板但没有截图 -> 归档成临时编号订单并回群催图；
  2 发送人后来补发的截图 -> 按「收货人」认出是哪一单，归档到那笔订单并补齐字段，
    临时编号升级成截图里的真实编号；认不出来才按老规矩过滤（并写明原因）；
  3 完全重复的消息不重复归档，消息页要能看出「为什么没归档」（重复发送相同内容）。
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

import warnings  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

warnings.filterwarnings("ignore", message="fields may not start with 'st'")

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, RawMessage  # noqa: E402
from app.services.ingest import (  # noqa: E402
    TEMP_PREFIX,
    attach_images_to_text,
    process_pending,
    process_pending_images,
    upsert_raw_message,
)
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

TEMPLATE = """产品名称： 倍思Q2 20000毫安30W自带线充电宝
收货人： 高艳鑫
手机号码： 18640345632
地址信息： 浙江省杭州市萧山区新街街道天明国际产业园2号楼1单元438室
售价：59
所属供应链：华强北数码
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""

# 补发的截图 OCR：收货人与模板一致（这就是认单依据）
OCR_TEXT = """高艳鑫 18640345632
浙江省杭州市萧山区新街街道天明国际产业园2号楼1单元438室
倍思Q2 20000毫安30W自带 ¥59.00
成交价 (在支付宝担保账户中)             ¥50.00
订单编号       5127743052126267544 | 复制
"""

# 另一单的截图：收货人对不上，应该被过滤掉而不是乱补
OCR_TEXT_OTHER = """李雷 <no-reply> 13900000000
广东省深圳市南山区科技园1号楼
某商品 ¥199.00
订单编号       8888888888888888 | 复制
"""

REAL_ORDER_NO = "5127743052126267544"


class FakeClient:
    """假飞书客户端：不下载真图、不发真消息。"""

    enabled = True

    def __init__(self) -> None:
        self.replies: list = []
        self.sent: list = []

    def download_resource(self, message_id: str, file_key: str, resource_type: str = "image") -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}


def _item(message_id: str, text: str = "", msg_type: str = "text",
          image_keys: tuple = (), minutes_ago: int = 10, chat_id: str = "oc_late") -> dict:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "msg_type": msg_type,
        "text": text,
        "created_at": now() - timedelta(minutes=minutes_ago),
        "sender_id": "ou_sender",
        "sender_type": "user",
        "image_keys": list(image_keys),
    }


def _patch_ocr(text: str = OCR_TEXT):
    return (
        mock.patch("app.services.ingest.ocr_image", return_value=(text, "done")),
        mock.patch("app.services.ingest.crop_product_image", return_value=None),
    )


class LateScreenshotTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = FakeClient()
        self._orig_backfill = settings.screenshot_backfill_enabled
        settings.screenshot_backfill_enabled = True

    def tearDown(self) -> None:
        settings.screenshot_backfill_enabled = self._orig_backfill
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _add(self, message_id: str, **kw) -> None:
        with SessionLocal() as s:
            upsert_raw_message(s, _item(message_id, **kw))
            s.commit()

    def _archive_template_without_image(self) -> Order:
        """先发模板、没截图：订单落成临时编号。"""
        self._add("om_tpl", text=TEMPLATE, minutes_ago=40)
        with SessionLocal() as s:
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)
            order = s.query(Order).one()
            s.refresh(order)
            return order

    # ---------------------------------------------------------------- 补图回填
    def test_01_late_screenshot_backfills_by_receiver(self):
        order = self._archive_template_without_image()
        self.assertTrue(order.order_no.startswith(TEMP_PREFIX), "没截图时是临时编号")

        # 过了配对窗口才补发截图（老流程这里会被直接过滤）
        self._add("om_img_late", msg_type="image", image_keys=("img_late",), minutes_ago=5)
        with SessionLocal() as s:
            self.assertEqual(attach_images_to_text(s, "oc_late"), 0, "超出配对窗口，不会自动挂接")
            a, b = _patch_ocr()
            with a, b:
                stats = process_pending_images(s, client=self.client)
            self.assertEqual(stats["backfilled"], 1, "补图被回填")
            self.assertEqual(stats["filtered"], 0)

            order = s.query(Order).one()
            self.assertEqual(order.order_no, REAL_ORDER_NO, "临时编号升级成截图里的真实编号")
            self.assertTrue(order.temp_order_no.startswith(TEMP_PREFIX), "原临时编号保留备查")
            self.assertEqual(order.deal_amount, 50.0, "OCR 出来的成交价补齐")
            self.assertEqual(order.receiver_phone, "18640345632")
            self.assertFalse(order.needs_review)

            imgs = s.query(OrderImage).all()
            self.assertEqual(len(imgs), 1)
            self.assertEqual(imgs[0].order_id, order.id)
            self.assertEqual(imgs[0].message_id, "om_img_late")

            img_msg = (
                s.query(RawMessage)
                .filter(RawMessage.message_id == "om_img_late")
                .one()
            )
            self.assertEqual(img_msg.parse_status, "parsed")
            self.assertEqual(img_msg.kind, "screenshot")
            self.assertEqual(img_msg.order_id, order.id)
            self.assertEqual(img_msg.parent_message_id, "om_tpl", "挂到那条缺图模板下")

    def test_02_unmatched_receiver_keeps_filtered_with_reason(self):
        self._archive_template_without_image()
        self._add("om_img_other", msg_type="image", image_keys=("img_other",), minutes_ago=5)
        with SessionLocal() as s:
            a, b = _patch_ocr(OCR_TEXT_OTHER)
            with a, b:
                stats = process_pending_images(s, client=self.client)
            self.assertEqual(stats["backfilled"], 0)
            self.assertEqual(stats["filtered"], 1)
            self.assertEqual(s.query(OrderImage).count(), 0, "认不出来就不落盘")
            msg = s.query(RawMessage).filter(RawMessage.message_id == "om_img_other").one()
            self.assertEqual(msg.parse_status, "filtered")
            self.assertIn("也没认出是哪笔订单的补图", msg.ignore_reason, "过滤要写明原因")
            order = s.query(Order).one()
            self.assertTrue(order.order_no.startswith(TEMP_PREFIX), "原订单不动")

    def test_03_backfill_can_be_switched_off(self):
        self._archive_template_without_image()
        self._add("om_img_off", msg_type="image", image_keys=("img_off",), minutes_ago=5)
        settings.screenshot_backfill_enabled = False
        try:
            with SessionLocal() as s:
                a, b = _patch_ocr()
                with a, b:
                    stats = process_pending_images(s, client=self.client)
                self.assertEqual(stats["backfilled"], 0)
                self.assertEqual(stats["filtered"], 1)
                self.assertEqual(s.query(OrderImage).count(), 0)
        finally:
            settings.screenshot_backfill_enabled = True

    def test_04_order_with_screenshot_is_not_stolen(self):
        """已经有截图的订单不会被补图抢走。"""
        self._add("om_tpl_full", text=TEMPLATE, minutes_ago=40)
        self._add("om_img_full", msg_type="image", image_keys=("img_full",), minutes_ago=39)
        with SessionLocal() as s:
            self.assertEqual(attach_images_to_text(s, "oc_late"), 1)
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)
            self.assertEqual(s.query(OrderImage).count(), 1)
        # 再补一张：订单已有截图，不抢图
        self._add("om_img_second", msg_type="image", image_keys=("img_second",), minutes_ago=1)
        with SessionLocal() as s:
            a, b = _patch_ocr()
            with a, b:
                stats = process_pending_images(s, client=self.client)
            self.assertEqual(stats["backfilled"], 0)
            self.assertEqual(s.query(OrderImage).count(), 1)

    # ---------------------------------------------------------------- 重复消息 + 过滤原因
    def test_05_duplicate_message_has_reason(self):
        self._add("om_dup_1", text=TEMPLATE, minutes_ago=30)
        with SessionLocal() as s:
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)
        self._add("om_dup_2", text=TEMPLATE, minutes_ago=3)
        with SessionLocal() as s:
            a, b = _patch_ocr()
            with a, b:
                stats = process_pending(s, client=self.client)
            self.assertEqual(stats.get("duplicate"), 1)
            self.assertEqual(s.query(Order).count(), 1, "重复内容不重复归档")
            msg = s.query(RawMessage).filter(RawMessage.message_id == "om_dup_2").one()
            self.assertEqual(msg.parse_status, "duplicate")
            self.assertIn("重复发送相同内容", msg.ignore_reason)

    def test_06_messages_page_shows_reason_and_duplicate_filter(self):
        ensure_default_admin()
        self.test_05_duplicate_message_has_reason()
        client = TestClient(app)
        resp = client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text
        page = client.get("/messages")
        self.assertEqual(page.status_code, 200)
        self.assertIn("过滤原因", page.text, "消息页新增「过滤原因」列")
        self.assertIn("重复发送相同内容", page.text, "重复消息直接显示原因")
        self.assertIn('value="duplicate"', page.text, "状态筛选可按「重复内容」过滤")

        dup_page = client.get("/messages?status=duplicate")
        self.assertEqual(dup_page.status_code, 200)
        self.assertIn("om_dup_2", dup_page.text, "能单独筛出重复消息")
        self.assertIn("重复发送相同内容", dup_page.text)


if __name__ == "__main__":
    unittest.main()
