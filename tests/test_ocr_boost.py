"""截图订单号识别增强：临时单号为什么一直没更新，以及怎么补上。

线上实例（2026-09-27，订单 TMP-20260927110052-61CAAE）：
  模板消息已经解析出临时单号，截图也配上了，但截图 OCR 只认出「订单编号」四个字，
  后面的 19 位数字整串丢掉 —— 于是订单号一直停在临时编号。

这里覆盖的四重机制：
  1 订单号文本提取：标签同行 / 标签下一行 / 长数字兜底；
  2 认不准时不写死：落成「疑似订单号」，列表与详情给提示；
  3 详情页一键采用（人工确认后才升级）；
  4 兜底重试：临时单号的订单可以重新识别截图（定时任务与手动按钮共用）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, RawMessage  # noqa: E402
from app.parser import ocr as ocr_mod  # noqa: E402
from app.parser.ocr import OcrText  # noqa: E402
from app.parser.order_no import order_no_from_text, order_no_hints_from_text  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services.ingest import order_no_hint_of  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
REAL_NO = "5127743052126267544"
TEMP_NO = "TMP-20260927110052-61CAAE"
CHAT = "oc_ocr_boost"
# 真实故障现场：手机号认出来了，订单号那串丢了
OCR_BAD = """10:59
焦传艮  13739259130
安徽省合肥市蜀山区高新技术产业开发区科学大道8号兴园小区南区75#407室
苹果磁吸充电宝 5000毫安
订单编号
闲鱼手机节
"""
# 订单号专项识别救回来的样子
OCR_FIXED = OCR_BAD.replace("闲鱼手机节", REAL_NO)
TEMPLATE = """产品名称：苹果磁吸充电宝
苹果5000毫安充电宝
收货人： 焦传艮
手机号码： 13739259130
地址信息： 安徽省合肥市蜀山区高新技术产业开发区科学大道8号兴园小区南区75#407室
售价：46
"""


class OcrBoostTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OrderImage).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self._saved_provider = settings.ocr_provider
        settings.ocr_provider = "tesseract"
        self._tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        settings.ocr_provider = self._saved_provider
        self._tmp.cleanup()

    # ------------------------------------------------------------ 工具
    def _order(self, order_no: str = TEMP_NO, **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                temp_order_no=order_no if order_no.startswith("TMP-") else "",
                chat_id=CHAT,
                receiver_name="焦传艮",
                receiver_phone="13739259130",
                product_name="苹果磁吸充电宝 苹果5000毫安充电宝",
                raw_text=TEMPLATE,
                needs_review=True,
                review_reason="还没配上订单号（临时编号），请核对",
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _image(self, order_id: int, ocr_text: str = OCR_BAD, hint: str = "", attempts: int = 0) -> int:
        path = Path(self._tmp.name) / f"shot_{order_id}_{attempts}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        with SessionLocal() as s:
            img = OrderImage(
                order_id=order_id,
                message_id="om_shot",
                file_key="img_1",
                filename=path.name,
                local_path=str(path),
                ocr_status="done",
                ocr_text=ocr_text,
                order_no_hint=hint,
                ocr_attempts=attempts,
            )
            s.add(img)
            s.commit()
            return img.id

    def _patch_ocr(self, text: str, hints: tuple[str, ...] = ()):
        return mock.patch(
            "app.services.ingest.ocr_image_ex",
            return_value=OcrText(text=text, status="done", order_no_hints=list(hints)),
        )

    # ------------------------------------------------------------ 1 文本提取
    def test_01_order_no_from_text(self):
        """标签同行 / 标签下一行 / 长数字兜底，三种写法都要能取到。"""
        self.assertEqual(order_no_from_text(f"订单编号   {REAL_NO}  复制"), (REAL_NO, "标签「订单编号」同行"))
        self.assertEqual(order_no_from_text(f"订单编号\n{REAL_NO}"), (REAL_NO, "标签下一行"))
        self.assertEqual(order_no_from_text("订单号 33169016 47870")[0], "3316901647870", "断开的数字拼回来")
        self.assertEqual(order_no_from_text("10:59 2026-09-27 13739259130"), ("", ""), "时间手机号不当订单号")
        self.assertIn(REAL_NO, order_no_hints_from_text(f"某单 {REAL_NO} 已付款"))

    # ------------------------------------------------------------ 2 值在标签下一行
    def test_02_value_on_next_line_upgrades_temp_no(self):
        """OCR 把订单号挤到「订单编号」下一行：照样能升级临时单号。"""
        order_id = self._order()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            merged = ingest_service.parse_text(TEMPLATE, [OCR_FIXED])
            order = ingest_service.apply_real_order_no(s, order, merged.fields.get("order_no"))
            s.commit()
            self.assertEqual(order.order_no, REAL_NO)
            self.assertEqual(order.temp_order_no, TEMP_NO, "原临时编号留档")

    # ------------------------------------------------------------ 3 疑似候选
    def test_03_hint_keeps_temp_no_but_warns(self):
        """没认准时：不写死单号，落成「疑似订单号」并在待确认里说明。"""
        order_id = self._order()
        self._image(order_id, ocr_text=OCR_BAD, hint=REAL_NO)  # 截图识别到的疑似候选
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            result = ingest_service.parse_text(TEMPLATE, [OCR_BAD])
            ingest_service._mark_review(order, result, hint=order_no_hint_of(s, order))
            s.commit()
            self.assertEqual(order.order_no, TEMP_NO, "认不准不能自动改单号")
            self.assertTrue(order.needs_review)
            self.assertIn(f"疑似订单号 {REAL_NO}", order.review_reason)
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("采用疑似单号", page.text, "详情页给一键采用入口")

    # ------------------------------------------------------------ 4 一键采用
    def test_04_apply_hint_route(self):
        """人工确认后一键采用疑似单号：临时编号留档、待确认标记清掉。"""
        order_id = self._order()
        self._image(order_id, hint=REAL_NO)
        resp = self.client.post(
            f"/orders/{order_id}/apply-order-no",
            data={"order_no": REAL_NO},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.order_no, REAL_NO)
            self.assertEqual(order.temp_order_no, TEMP_NO)
            self.assertFalse(order.needs_review)
            self.assertEqual(order.review_reason, "")

    # ------------------------------------------------------------ 5 手动重新识别
    def test_05_reocr_upgrades_order(self):
        """「截图已配上、单号没更新」：重新识别后自动升级。"""
        order_id = self._order()
        self._image(order_id, ocr_text=OCR_BAD)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            with self._patch_ocr(OCR_FIXED):
                upgraded, note = ingest_service.reocr_order(s, order)
            self.assertTrue(upgraded, note)
            self.assertEqual(order.order_no, REAL_NO)
            self.assertEqual(order.temp_order_no, TEMP_NO)
            self.assertIn(REAL_NO, note)

    def test_06_reocr_route_from_detail_page(self):
        """详情页按钮：POST /orders/{id}/reocr 也能把单号升级掉。"""
        order_id = self._order()
        self._image(order_id, ocr_text=OCR_BAD)
        with self._patch_ocr(OCR_FIXED):
            resp = self.client.post(f"/orders/{order_id}/reocr", follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).order_no, REAL_NO)

    # ------------------------------------------------------------ 6 定时兜底
    def test_07_reocr_pending_scans_temp_orders(self):
        """定时兜底：只挑还是临时编号、且隔了足够久的订单重试。"""
        order_id = self._order()
        self._image(order_id, ocr_text=OCR_BAD)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.updated_at = now().replace(year=2020)  # 早就该重试了
            s.commit()
            with self._patch_ocr(OCR_FIXED):
                stats = ingest_service.reocr_pending(s)
            self.assertEqual(stats["checked"], 1)
            self.assertEqual(stats["upgraded"], 1)
            self.assertEqual(s.get(Order, order_id).order_no, REAL_NO)

    def test_08_reocr_pending_skips_fresh_order(self):
        """刚处理过的订单不立刻重试（避免空转、也避免反复占 CPU）。"""
        order_id = self._order()
        self._image(order_id, ocr_text=OCR_BAD)
        with SessionLocal() as s:
            with self._patch_ocr(OCR_FIXED):
                stats = ingest_service.reocr_pending(s)
            self.assertEqual(stats["checked"], 0, "更新时间很近的订单先跳过")

    # ------------------------------------------------------------ 7 认单打分
    def test_09_late_screenshot_matches_by_phone_despite_bad_name(self):
        """截图里姓名被 OCR 认成乱码，但手机号对得上：仍然认得出是哪一单。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=TEMP_NO,
                chat_id=CHAT,
                receiver_name="焦传艮",
                receiver_phone="13739259130",
                product_name="苹果磁吸充电宝 苹果5000毫安充电宝",
                created_at=now(),
            )
            s.add(order)
            s.commit()
            fields = {
                "receiver_name": "CAI RARE",  # OCR 乱码
                "receiver_phone": "13739259130",
                "receiver_address": "安徽省合肥市蜀山区科学大道8号兴园小区南区75",
                "deal_amount": 46.0,
            }
            found = ingest_service.find_order_for_late_screenshot(s, fields, CHAT)
            self.assertIsNotNone(found, "手机号一致就该认出来")
            self.assertEqual(found.id, order.id)

    def test_11_review_reason_keeps_refund_notice(self):
        """重新识别会重算待确认原因，但不能把「缺退单理由」这类提醒清掉。"""
        order_id = self._order()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.needs_review = True
            order.review_reason = "缺退单理由：请在群里补充（回复「退单理由：xxx」）"
            s.commit()
            result = ingest_service.parse_text(TEMPLATE, [OCR_FIXED])
            ingest_service._mark_review(order, result)
            s.commit()
            self.assertIn("缺退单理由", order.review_reason, "退单提醒不能被解析结果顶掉")

    def test_10_late_screenshot_rejects_stranger(self):
        """手机号、姓名都对不上：宁可认不出，也不挂到别的单上。"""
        with SessionLocal() as s:
            s.add(
                Order(
                    platform="xianyu",
                    order_no=TEMP_NO,
                    chat_id=CHAT,
                    receiver_name="张三",
                    receiver_phone="13900000000",
                    created_at=now(),
                )
            )
            s.commit()
            fields = {"receiver_name": "李四", "receiver_phone": "13739259130"}
            self.assertIsNone(ingest_service.find_order_for_late_screenshot(s, fields, CHAT))

    # ------------------------------------------------------------ 8 OCR 超时不连坐（2026-10-16 线上故障）
    def _variants(self) -> list[Path]:
        vdir = Path(self._tmp.name) / "variants"
        vdir.mkdir(exist_ok=True)
        out = []
        for name in ("x_gray.png", "x_sharp.png"):
            p = vdir / name
            p.write_bytes(b"png")
            out.append(p)
        return out

    def test_20_variant_timeout_does_not_kill_rest(self):
        """单个预处理版本超时不再连坐：剩下的版本结果照常合并返回。

        线上故障（2026-10-16）：手机长截图 + 并发 OCR，tesseract 180 秒超时 →
        整次 OCR 判 failed、文本一个字不留 → 订单号读不出 → 临时单号一直配不上。
        """
        variants = self._variants()

        def fake_run(target, lang, extra):
            if target.name.endswith("_sharp.png"):
                raise ocr_mod.OcrError("Command timed out after 180 seconds")
            return f"订单编号   {REAL_NO}"

        with mock.patch.object(ocr_mod, "_prepare_variants", return_value=variants), \
                mock.patch.object(ocr_mod, "_run_tesseract", side_effect=fake_run):
            text = ocr_mod._ocr_tesseract(Path("fake.png"))
        self.assertIn(REAL_NO, text, "没超时的版本结果要留下来")

    def test_21_all_variants_failed_raises(self):
        """所有版本都失败才报错（保持 failed 状态语义不变）。"""
        variants = self._variants()
        with mock.patch.object(ocr_mod, "_prepare_variants", return_value=variants), \
                mock.patch.object(
                    ocr_mod, "_run_tesseract", side_effect=ocr_mod.OcrError("timed out")
                ):
            with self.assertRaises(ocr_mod.OcrError):
                ocr_mod._ocr_tesseract(Path("fake.png"))

    def test_22_variants_capped_for_long_screenshots(self):
        """手机长截图：放大倍数压到像素上限内，且跳过更高倍的锐化版。"""
        from PIL import Image

        big = Path(self._tmp.name) / "big.png"
        Image.new("L", (1080, 2400)).save(big)  # 线上超时的那类 1080×2400 截图
        variants = ocr_mod._prepare_variants(big)
        names = [v.stem.rsplit("_", 1)[-1] for v in variants]
        self.assertNotIn("sharp", names, "大图不再跑更高倍的锐化版")
        limit = ocr_mod._VARIANT_MAX_PIXELS * 1.01
        for v in variants:
            with Image.open(v) as im:
                self.assertLessEqual(im.width * im.height, limit, f"{v.name} 放大后超像素上限")

    def test_23_small_image_keeps_all_variants(self):
        """小图不受影响：三个预处理版本照旧。"""
        from PIL import Image

        small = Path(self._tmp.name) / "small.png"
        Image.new("L", (480, 800)).save(small)
        variants = ocr_mod._prepare_variants(small)
        names = [v.stem.rsplit("_", 1)[-1] for v in variants]
        self.assertEqual(names, ["gray", "sharp", "binary"])

    # ------------------------------------------------------------ 9 假疑似单号过滤
    def test_24_hint_ignores_phone_with_tail(self):
        """「已复制收货信息」弹窗的「手机号-8893」连成一串不再是疑似订单号。

        线上案例（订单 #303）：hint=184482613358893（手机号 18448261335 + 弹窗尾巴 8893）。
        """
        self.assertEqual(
            order_no_hints_from_text("已复制收货信息\n韦丽宝[8893]\n18448261335-8893"),
            [],
            "手机号 + 尾巴不是订单号",
        )
        self.assertIn(REAL_NO, order_no_hints_from_text(f"订单编号 {REAL_NO} 复制"), "真订单号照常给出")
