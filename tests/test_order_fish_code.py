"""订单详情「关联渔货号」（2026-10-15）。

报单的人把渔集的渔货号（YS + 日期 + 流水）抄进飞书订单模板，
订单详情页从来源消息原文里把它抠出来展示，点进去是渔集货品台账。

覆盖：
  1 消息里写了渔货号 → 详情页显示并可跳 /wms/goods/<渔货号>，多个去重保序；
  2 没写 → 显示兜底文案，不渲染链接。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage, User  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402


class OrderFishCodeTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        with SessionLocal() as s:
            user = s.query(User).order_by(User.id).first()
            uid = user.id
        self.client.cookies.set(COOKIE_NAME, make_session(uid)[0])

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _order_with_msg(self, order_no: str, raw_text: str) -> int:
        with SessionLocal() as s:
            msg = RawMessage(
                message_id=order_no,
                raw_text=raw_text,
                sender_id="ou_test",
                sender_name="报单人",
                chat_name="报单群",
                msg_time=datetime(2026, 10, 15, 10, 0),
            )
            s.add(msg)
            s.flush()
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="测试商品",
                receiver_name="测试",
                xianyu_account=" Fish01 ",
                sale_amount=100.0,
                ordered_at=datetime(2026, 10, 15, 10, 1),
                source_message_id=msg.message_id,
            )
            s.add(order)
            s.commit()
            return order.id

    def test_10_detail_shows_fish_codes(self) -> None:
        """消息里的 YS 渔货号显示在详情页，去重保序，链接指向渔集台账。"""
        raw = (
            "闲鱼订单已支付\n订单编号 XY20261015001\n"
            "关联渔货号 YS202610140001\n备注：含 YS202610140001 与 YS202610150002 两款\n"
        )
        oid = self._order_with_msg("FT202610150001", raw)
        page = self.client.get(f"/orders/{oid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("关联渔货号", page.text)
        # 去重保序：先 140001 再 150002
        self.assertLess(
            page.text.find("YS202610140001"), page.text.find("YS202610150002"),
            "渔货号应按消息里出现的先后排",
        )
        self.assertIn('/wms/goods/YS202610140001', page.text)

    def test_20_no_fish_code_shows_fallback(self) -> None:
        """消息里没写渔货号 → 兜底文案，不出 /wms/goods 链接。"""
        oid = self._order_with_msg("FT202610150002", "闲鱼订单已支付\n订单编号 XY20261015002")
        page = self.client.get(f"/orders/{oid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("没写 YS 开头的渔货号", page.text)
        self.assertNotIn('/wms/goods/', page.text)


if __name__ == "__main__":
    unittest.main()
