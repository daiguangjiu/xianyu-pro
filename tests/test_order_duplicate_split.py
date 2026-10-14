"""同一买家同款下两单（2026-09-29 版本）。

背景：判「重复发送同一笔订单」的业务指纹只认「产品 + 收货人 + 手机号」，
同一个买家买两件同款、收货信息一样时指纹必然相同 —— 以前会把两笔真实订单
并成一笔（少发一件货、少算一笔成本、供应商少下一单）。

现在的口径（见 `ingest._looks_like_separate_order`）—— 命中任意一条就算两笔单：
  1. 本次消息带了订单截图，而那个订单已有截图、两张图内容不一样（下一次单一张截图）；
  2. 两张截图都读到了疑似订单编号，且互不相同；
  3. 那个订单已经是正式单号（非 TMP-），本次又从截图读到另一个正式单号。
比不了（缺截图 / 老数据没落盘 / 两边都没号）就维持原来的「重复同步」口径，
避免把真正的重复发送（同一单补发）拆成两单。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, OrderImage  # noqa: E402
from app.parser.engine import parse_text  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import dedupe as dedupe_service  # noqa: E402
from app.timeutil import now  # noqa: E402

# 同一买家同一款商品的两条群消息（内容一模一样，只是发了两次）
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


class _Shot:
    """测试用截图：落一个临时文件，跟 OrderImage 的 local_path 对得上。"""

    _tmp: tempfile.TemporaryDirectory | None = None

    def __init__(self, content: bytes, hint: str = "") -> None:
        if _Shot._tmp is None:
            _Shot._tmp = tempfile.TemporaryDirectory(prefix="order_shots_")
        self.path = Path(_Shot._tmp.name) / f"shot_{abs(hash((content, hint)))}.png"
        self.path.write_bytes(content)
        self.hint = hint


class SeparateOrderEvidenceTest(unittest.TestCase):
    """反向证据判定（不依赖 Feishu，直接调判定函数）。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            db.query(OrderImage).delete()
            db.query(Order).delete()
            db.commit()

    def _order_with_shot(self, order_no: str, shot: _Shot, hint: str = "") -> int:
        with SessionLocal() as db:
            order = Order(platform="xianyu", order_no=order_no, product_name="亚朵四代枕头全套",
                          receiver_name="王小萌", receiver_phone="18561274331")
            db.add(order)
            db.flush()
            db.add(
                OrderImage(
                    order_id=order.id, message_id="om_1", file_key="fk_1",
                    filename=shot.path.name, local_path=str(shot.path),
                    order_no_hint=hint or shot.hint,
                )
            )
            db.commit()
            return order.id

    def _new_shot_row(self, shot: _Shot, hint: str = "") -> OrderImage:
        return OrderImage(
            message_id="om_2", file_key="fk_2", filename=shot.path.name,
            local_path=str(shot.path), order_no_hint=hint or shot.hint,
        )

    def test_different_screenshots_mean_two_orders(self) -> None:
        """一张截图 = 一次下单：两张图不一样就是两笔单。"""
        old, new = _Shot(b"order-3316454115216161182"), _Shot(b"order-3316902457144020685")
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("TMP-A", old))
            self.assertTrue(
                ingest_service._looks_like_separate_order(  # noqa: SLF001
                    order, [self._new_shot_row(new)], ""
                )
            )

    def test_same_screenshot_still_duplicate(self) -> None:
        """同一张图重复发（同一单补发）：不算两笔单，维持重复口径。"""
        content = b"same-screenshot-bytes"
        old, new = _Shot(content), _Shot(content)
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("TMP-A", old))
            self.assertFalse(
                ingest_service._looks_like_separate_order(  # noqa: SLF001
                    order, [self._new_shot_row(new)], ""
                )
            )

    def test_different_order_no_hints_mean_two_orders(self) -> None:
        """图比不了（老数据没落盘）时，看截图上的疑似订单编号。"""
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("TMP-A", _Shot(b"x"), hint="3316454115216161182"))
            row = self._new_shot_row(_Shot(b"x"), hint="3316902457144020685")
            row.local_path = ""          # 老数据：文件没留档，比不了图
            self.assertTrue(
                ingest_service._looks_like_separate_order(order, [row], "")  # noqa: SLF001
            )

    def test_formal_order_numbers_differ(self) -> None:
        """旧单已是正式单号、本次又读到另一个正式单号 -> 两笔单。"""
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("3316454115216161182", _Shot(b"y")))
            row = self._new_shot_row(_Shot(b"y"))
            row.local_path = ""
            self.assertTrue(
                ingest_service._looks_like_separate_order(  # noqa: SLF001
                    order, [row], "3316902457144020685"
                )
            )

    def test_temp_order_no_upgrade_is_not_split(self) -> None:
        """旧单还是临时编号、本次读到真号：那是补图后拿到真号，仍算同一单。"""
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("TMP-B", _Shot(b"z")))
            row = self._new_shot_row(_Shot(b"z"))
            row.local_path = ""
            self.assertFalse(
                ingest_service._looks_like_separate_order(  # noqa: SLF001
                    order, [row], "3316454115216161182"
                )
            )

    def test_no_screenshot_keeps_old_behaviour(self) -> None:
        """没有截图可比的：只按单号判，旧单临时号时仍按重复处理。"""
        with SessionLocal() as db:
            order = db.get(Order, self._order_with_shot("TMP-C", _Shot(b"w")))
            self.assertFalse(ingest_service._looks_like_separate_order(order, [], ""))  # noqa: SLF001


class DuplicateOrderIngestTest(unittest.TestCase):
    """走一遍归档：同一买家同款发两次（都没有截图）仍然是「重复发送」。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            db.query(OrderImage).delete()
            db.query(Order).delete()
            db.commit()

    def _ingest(self, message_id: str, text: str) -> Order:
        from app.models import RawMessage

        with SessionLocal() as db:
            msg = RawMessage(
                message_id=message_id, chat_id="oc_dup", msg_type="text",
                raw_text=text, parse_status="pending", msg_time=now(),
            )
            db.add(msg)
            db.commit()
            order = ingest_service.save_order(db, msg, parse_text(text), None)
            db.refresh(order)
            return order

    def test_same_buyer_same_item_twice_without_screenshot_is_duplicate(self) -> None:
        first = self._ingest("om_dup_1", ORDER_TEXT)
        second = self._ingest("om_dup_2", ORDER_TEXT)
        self.assertEqual(first.id, second.id, "内容完全一致且没有截图，仍判为重复发送")
        with SessionLocal() as db:
            self.assertEqual(db.query(Order).count(), 1, "只归档一笔订单")
            self.assertEqual(
                dedupe_service.identity_fingerprint(order=first),
                dedupe_service.identity_fingerprint(order=second),
                "两边的身份指纹本来就一样（这正是要小心的地方）",
            )


if __name__ == "__main__":
    unittest.main()
