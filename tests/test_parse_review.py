"""字段值换行后的识别与「存疑提示」落库（2026-09-26）。

业务口径：
  1 群里是复制粘贴的模板，字段值常被换行（商品名一行、规格一行）：解析要把值取全，
    不能只取「标签: 到行尾」；
  2 自动合并是启发式，并进来的内容看着不对（像手机号 / 金额 / 聊天）时，
    订单要进「待人工确认」，并把「要核对哪里」写清楚，展示在订单列表与详情页；
  3 运营核对完在详情页把「需要人工确认」改成「否」，提示就消失。
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

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.services.ingest import (  # noqa: E402
    process_pending,
    reparse_truncated,
    upsert_raw_message,
)
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 群里真实发过的版式：商品名被折成两行
MULTILINE = (
    "产品名称：倍思Q2电充电宝 \n"
    "20000毫安 22.5w q2电 自带线（C+L）\n"
    "收货人： 梁程伟\n"
    "手机号码： 13088070863\n"
    "地址信息： 四川省成都市龙泉驿区大面街道青台山路泰华锦城11栋1单元405\n"
    "备注： 紫色\n"
    "\n"
    "售价：50\n"
    "\n"
    "成本价：45\n"
    "所属供应链：BH001\n"
    "出货闲鱼号：允川不吃大米饭\n"
    "回款支付宝归属人：范志强"
)
# 换行的第二行混进了手机号：合并后要能被识别为「有问题」
SUSPICIOUS = (
    "产品名称：充电宝\n"
    "13800138000\n"
    "收货人： 王五\n"
    "手机号码： 13900139000\n"
    "地址信息： 浙江省杭州市西湖区文三路1号\n"
    "售价：30\n"
    "成本价：20\n"
    "所属供应链：BH001"
)


class FakeClient:
    """假飞书客户端：不联网。"""

    enabled = True

    def reply_text(self, message_id: str, text: str) -> dict:
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        return {"ok": True}

    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> str:
        return ""

    def download_resource(self, *args, **kwargs):
        raise RuntimeError("测试环境不下载资源")


class ParseReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.feishu = FakeClient()
        self.web = TestClient(app)
        resp = self.web.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _archive(self, message_id: str, text: str) -> Order:
        with SessionLocal() as s:
            upsert_raw_message(
                s,
                {
                    "message_id": message_id,
                    "chat_id": "oc_test",
                    "msg_type": "text",
                    "text": text,
                    "created_at": now() - timedelta(minutes=5),
                    "sender_id": "ou_sender",
                    "sender_type": "user",
                    "image_keys": [],
                },
            )
            s.commit()
        with SessionLocal() as s:
            a = mock.patch("app.services.ingest.ocr_image", return_value=("", "skipped"))
            b = mock.patch("app.services.ingest.crop_product_image", return_value=None)
            with a, b:
                process_pending(s, client=self.feishu)
            orders = s.query(Order).all()
            self.assertEqual(len(orders), 1, "应归档成一笔订单")
            return orders[0]

    # ---------------------------------------------------------------- 多行取值
    def test_01_multiline_value_is_complete(self):
        order = self._archive("om_multi", MULTILINE)
        self.assertIn("20000毫安", order.product_name or "", "商品名要取全，不能只取第一行")
        self.assertTrue((order.product_name or "").startswith("倍思Q2电充电宝"))
        self.assertEqual(order.receiver_name, "梁程伟")
        self.assertEqual(order.receiver_phone, "13088070863")
        self.assertEqual(order.sale_amount, 50.0)
        self.assertEqual(order.cost_amount, 45.0)

    def test_02_multiline_ok_order_has_no_doubt(self):
        """合并靠谱时不要诬告：不该出现「存疑」，只说明「已合并」。"""
        order = self._archive("om_multi2", MULTILINE)
        self.assertNotIn("存疑", order.review_reason, f"不该报存疑：{order.review_reason}")
        self.assertIn("已合并", order.review_reason, "要说明系统把换行的值合并了")

    # ---------------------------------------------------------------- 存疑提示
    def test_03_suspicious_merge_is_flagged(self):
        order = self._archive("om_sus", SUSPICIOUS)
        self.assertTrue(order.needs_review, "并错的内容要提示人工核对")
        self.assertIn("存疑", order.review_reason)
        self.assertIn("产品名称", order.review_reason, "要说清是哪个字段")

    def test_04_reason_shows_on_list_and_detail(self):
        order = self._archive("om_sus2", SUSPICIOUS)
        listing = self.web.get("/orders")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("待确认", listing.text)
        self.assertIn("存疑", listing.text, "列表要能看到要核对什么")
        detail = self.web.get(f"/orders/{order.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("需要人工核对", detail.text)
        self.assertIn("产品名称", detail.text)

    def test_05_confirm_clears_reason(self):
        """运营核对完改成「否」，提示就消失。"""
        order = self._archive("om_sus3", SUSPICIOUS)
        self.assertTrue(order.needs_review)
        resp = self.web.post(
            f"/orders/{order.id}/edit",
            data={"needs_review": "0"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text)
        with SessionLocal() as s:
            saved = s.get(Order, order.id)
            self.assertFalse(saved.needs_review)
            self.assertEqual(saved.review_reason, "", "核对完不再显示提示")


    # ---------------------------------------------------------------- 历史订单修复
    def test_06_reparse_fills_truncated_history(self):
        """旧解析只取了前半截的历史订单，跑 reparse 后补齐。"""
        order = self._archive("om_old", MULTILINE)
        with SessionLocal() as s:
            saved = s.get(Order, order.id)
            saved.product_name = "倍思Q2电充电宝"        # 模拟老版本被截断的值
            saved.receiver_address = "四川省成都市龙泉驿区大面街道青台山路泰华锦城11栋1单元405"
            s.commit()
        with SessionLocal() as s:
            out = reparse_truncated(s)
            saved = s.get(Order, order.id)
            self.assertGreaterEqual(out["fields"], 1)
            self.assertIn("20000毫安", saved.product_name, "被截断的商品名要补齐")

    def test_07_reparse_keeps_manual_edits(self):
        """人工改过的值不能被 reparse 覆盖。"""
        order = self._archive("om_manual", MULTILINE)
        with SessionLocal() as s:
            saved = s.get(Order, order.id)
            saved.product_name = "充电宝（运营改的简称）"
            s.commit()
        with SessionLocal() as s:
            reparse_truncated(s)
            saved = s.get(Order, order.id)
            self.assertEqual(saved.product_name, "充电宝（运营改的简称）", "人工改过的不动")


if __name__ == "__main__":
    unittest.main()
