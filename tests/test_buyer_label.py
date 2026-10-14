"""订单的「买家」一栏 = 收货人 + 手机号（2026-09-29 口径）。

群里同步的模板里没有买家昵称，闲鱼昵称也常读不到；收货人 + 手机号是「这个人」
的稳定标识，跨单能认出同一个买家，以后的用户管理（复购 / 退单率）按这一栏做。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.parser.engine import parse_text  # noqa: E402
from app.services import buyer as buyer_service  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.timeutil import now  # noqa: E402

ORDER_TEXT = """产品名称：亚朵四代枕头全套
收货人:王小萌
手机号码:18561274331
地址信息: 山东省 威海市 文登区 环山街道 豹山路邮政宾馆大院西北角2楼
售价：180
成本价：160
所属供应链：BH001
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""
LABEL = "王小萌 18561274331"


class BuyerLabelTest(unittest.TestCase):
    def test_label_is_name_plus_phone(self) -> None:
        self.assertEqual(buyer_service.buyer_label("王小萌", "18561274331"), LABEL)
        # 只有一项时只写那一项，不硬拼空格
        self.assertEqual(buyer_service.buyer_label("王小萌", ""), "王小萌")
        self.assertEqual(buyer_service.buyer_label("", "18561274331"), "18561274331")
        self.assertEqual(buyer_service.buyer_label(), "")

    def test_fill_only_empty_or_noise(self) -> None:
        order = Order(order_no="X1", receiver_name="王小萌", receiver_phone="18561274331")
        self.assertTrue(buyer_service.fill_buyer(order))
        self.assertEqual(order.buyer_nick, LABEL)
        self.assertFalse(buyer_service.fill_buyer(order), "已经补过的不重复写")

        noise = Order(order_no="X2", receiver_name="王小萌", buyer_nick="已付款")
        self.assertTrue(buyer_service.fill_buyer(noise), "解析噪声要被替换掉")
        self.assertEqual(noise.buyer_nick, "王小萌")

        manual = Order(order_no="X3", receiver_name="王小萌", buyer_nick="小萌萌")
        self.assertFalse(buyer_service.fill_buyer(manual), "运营手改过的不覆盖")
        self.assertEqual(manual.buyer_nick, "小萌萌")


class BuyerLabelBackfillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            db.query(Order).delete()
            db.query(RawMessage).delete()
            db.commit()

    def _add(self, order_no: str, name: str, phone: str, buyer: str = "") -> int:
        with SessionLocal() as db:
            order = Order(
                platform="xianyu", order_no=order_no, receiver_name=name,
                receiver_phone=phone, buyer_nick=buyer,
            )
            db.add(order)
            db.commit()
            return order.id

    def test_ensure_fills_history_and_is_idempotent(self) -> None:
        empty = self._add("H1", "王小萌", "18561274331")
        self._add("H2", "李四", "13800000000", buyer="已付款")
        self._add("H3", "王五", "13900000000", buyer="王五兄")   # 手改过的
        with SessionLocal() as db:
            self.assertEqual(buyer_service.ensure_buyer_labels(db), 2)
            self.assertEqual(buyer_service.ensure_buyer_labels(db), 0, "幂等：再调一次不重复补")
            self.assertEqual(db.get(Order, empty).buyer_nick, LABEL)
            rows = {o.order_no: o.buyer_nick for o in db.query(Order).all()}
        self.assertEqual(rows["H2"], "李四 13800000000")
        self.assertEqual(rows["H3"], "王五兄", "运营填过的不被覆盖")

    def test_archived_order_gets_buyer(self) -> None:
        """群里归档出来的订单，买家一栏直接是「收货人 + 手机号」。"""
        with SessionLocal() as db:
            db.add(
                RawMessage(
                    message_id="om_buyer", chat_id="oc_buyer", msg_type="text",
                    raw_text=ORDER_TEXT, parse_status="pending", msg_time=now(),
                )
            )
            db.commit()
            msg = db.scalar(select(RawMessage).where(RawMessage.message_id == "om_buyer"))
            order = ingest_service.save_order(db, msg, parse_text(ORDER_TEXT), None)
            self.assertEqual(order.buyer_nick, LABEL)


if __name__ == "__main__":
    unittest.main()
