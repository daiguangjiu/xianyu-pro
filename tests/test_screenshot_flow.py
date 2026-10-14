"""截图配对 / 缺图提醒 / 订单编号升级 的入库流程测试。

业务口径：
  1 条模板消息配 1 张订单截图；其他图片忽略；
  订单编号以截图里的为准（截图里的编号 > 临时编号），补到截图后把临时编号换成真实编号；
  模板消息没有截图时回群提醒发送人，同一条只提醒一次。
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, OrderImage, RawMessage  # noqa: E402
from app.parser.ocr import OcrText  # noqa: E402
from app.services.ingest import (  # noqa: E402
    attach_images_to_text,
    notify_missing_screenshots,
    process_pending,
    process_pending_images,
    upsert_raw_message,
)
from app.timeutil import now  # noqa: E402

TEMPLATE = """产品名称： 倍思Q2 20000毫安30W自带线充电宝
 收货人： 高艳鑫
手机号码： 18640345632
地址信息： 浙江省杭州市萧山区新街街道天明国际产业园2号楼1单元438室
售价：59
成本价：
所属供应链：华强北数码
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""

# 真实订单截图经 tesseract(chi_sim+eng) 识别出来的文本（地址被折成两行）
OCR_TEXT = """高艳鑫 18640345632
浙江省杭州市萧山区新街街道天明国际产业园2
号楼1单元438室
倍思Q2 20000毫安30W自带 ¥59.00
成交价 (在支付宝担保账户中)             ¥50.00
商品总价 (已优惠¥9.00)          ¥50.00
运费                         ¥0.00
订单编号       5127743052126267544 | 复制
"""

REAL_ORDER_NO = "5127743052126267544"


class FakeClient:
    """假飞书客户端：不下载真图、不发真消息，只记录调用。"""

    enabled = True

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    def download_resource(self, message_id: str, file_key: str, resource_type: str = "image") -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}

    def notice_count(self) -> int:
        return sum(1 for _, text in self.replies if "订单截图" in text)


def _item(
    message_id: str,
    text: str = "",
    msg_type: str = "text",
    image_keys: tuple[str, ...] = (),
    minutes_ago: int = 10,
) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_screenshot_test",
        "msg_type": msg_type,
        "text": text,
        "created_at": now() - timedelta(minutes=minutes_ago),
        "sender_id": "ou_sender",
        "sender_type": "user",
        "image_keys": list(image_keys),
    }


def _patch_ocr():
    """OCR 用真实识别文本，商品图裁剪在单测里跳过。"""
    return (
        mock.patch(
            "app.services.ingest.ocr_image_ex",
            return_value=OcrText(text=OCR_TEXT, status="done"),
        ),
        mock.patch("app.services.ingest.crop_product_image", return_value=None),
    )


class ScreenshotFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()
        self.client = FakeClient()

    def _add(self, message_id: str, **kw) -> None:
        with SessionLocal() as s:
            upsert_raw_message(s, _item(message_id, **kw))
            s.commit()

    def test_template_with_one_screenshot_fills_fields(self):
        """1 条模板 + 1 张截图：字段补全，订单编号取截图里的真实编号。"""
        self._add("om_tpl_1", text=TEMPLATE, minutes_ago=10)
        self._add("om_img_1", msg_type="image", image_keys=("img_1",), minutes_ago=9)

        with SessionLocal() as s:
            self.assertEqual(attach_images_to_text(s, "oc_screenshot_test"), 1)
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)

            order = s.query(Order).one()
            self.assertEqual(order.order_no, REAL_ORDER_NO)
            self.assertEqual(order.temp_order_no, "")
            self.assertFalse(order.needs_review, "有截图与真实编号时不应再标待确认")
            self.assertEqual(order.deal_amount, 50.0)
            self.assertEqual(order.total_amount, 50.0)
            self.assertEqual(order.shipping_fee, 0.0)
            self.assertEqual(order.sale_amount, 59.0)
            self.assertEqual(order.receiver_phone, "18640345632")
            self.assertEqual(
                order.receiver_address, "浙江省杭州市萧山区新街街道天明国际产业园2号楼1单元438室"
            )
            # 模板里的商品名完整，不被截断的 OCR 名称覆盖
            self.assertEqual(order.product_name, "倍思Q2 20000毫安30W自带线充电宝")
            self.assertEqual(s.query(OrderImage).count(), 1)

    def test_extra_images_are_ignored(self):
        """同一条模板的第二张图直接忽略：不挂接、不建单、不发回执。"""
        self._add("om_tpl_2", text=TEMPLATE, minutes_ago=10)
        self._add("om_img_a", msg_type="image", image_keys=("img_a",), minutes_ago=9)
        self._add("om_img_b", msg_type="image", image_keys=("img_b",), minutes_ago=8)

        with SessionLocal() as s:
            self.assertEqual(attach_images_to_text(s, "oc_screenshot_test"), 1, "只应挂第一张")
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)
                process_pending_images(s, client=self.client)

            self.assertEqual(s.query(Order).count(), 1, "多余的图不应单独建单")
            self.assertEqual(s.query(OrderImage).count(), 1)
            extra = s.query(RawMessage).filter(RawMessage.message_id == "om_img_b").one()
            self.assertEqual(extra.parent_message_id, "")
            self.assertEqual(extra.parse_status, "filtered", "窗口过期后多余图应被忽略")

    def test_missing_screenshot_notice_is_sent_once(self):
        """模板消息没配到截图：回群提醒一次，之后不再重复提醒。"""
        self._add("om_tpl_3", text=TEMPLATE, minutes_ago=10)
        with SessionLocal() as s:
            with mock.patch.object(settings, "notify_mode", "reply"), mock.patch.object(
                settings, "notify_missing_image", True
            ):
                a, b = _patch_ocr()
                with a, b:
                    process_pending(s, client=self.client)
                self.assertEqual(self.client.notice_count(), 1)
                self.assertIn("订单截图", self.client.replies[-1][1])
                # 幂等：再跑一轮不再提醒
                self.assertEqual(notify_missing_screenshots(s, self.client), 0)
                self.assertEqual(self.client.notice_count(), 1)

    def test_temp_order_no_is_upgraded_by_later_screenshot(self):
        """先收到模板（无图）落临时编号，之后补发截图要换成真实编号且不重复建单。"""
        self._add("om_tpl_4", text=TEMPLATE, minutes_ago=10)
        with SessionLocal() as s:
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)
            order = s.query(Order).one()
            self.assertTrue(order.order_no.startswith("TMP-"))
            self.assertTrue(order.needs_review)
            temp_no = order.order_no

        # 截图随后才发出来
        self._add("om_img_4", msg_type="image", image_keys=("img_4",), minutes_ago=9)
        with SessionLocal() as s:
            self.assertEqual(attach_images_to_text(s, "oc_screenshot_test"), 1)
            a, b = _patch_ocr()
            with a, b:
                process_pending(s, client=self.client)

            self.assertEqual(s.query(Order).count(), 1, "升级编号不应新建订单")
            order = s.query(Order).one()
            self.assertEqual(order.order_no, REAL_ORDER_NO)
            self.assertEqual(order.temp_order_no, temp_no, "原临时编号要留档备查")
            self.assertFalse(order.needs_review)


if __name__ == "__main__":
    unittest.main()
