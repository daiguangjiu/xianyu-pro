"""库存为 0 自动下架 + 飞书通知（2026-10-11 用户拍板第 7 条）。

用户原话：「如果库存是0，则系统自动下架处理，并通知飞书群 @牛磊 @胡子萍
xxx商品库存为0，请知晓，做好商品上架事宜。」

锁住的口径：

1. 在架且库存 <= 0（含 0 / 负数 / 空）的货源被下架，写 is_listed / unlisted_at /
   unlisted_reason / unlisted_by；有库存的一款**不能被动**；
2. 幂等：已经下架的再跑一次查不到，不会重复通知（后台线程每 10 分钟跑也不会刷群）；
3. 每条都留一条 exception_logs（type=stock-zero-unlist，resolved=True）；
4. 文案里必须有「请知晓，做好商品上架事宜」，并 @ 到配置的人（默认 牛磊 / 胡子萍）；
5. 没配群 / 没配飞书凭证时不报错，只回错误说明（巡检不能拖垮服务）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包：测试库配置要在任何 app 模块之前生效

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from src.goods import stock_guard  # noqa: E402
from src.models import ExceptionLog, SupplierItem  # noqa: E402

ZERO_ID = "T-STOCK-ZERO"
MINUS_ID = "T-STOCK-MINUS"
OK_ID = "T-STOCK-OK"
ALL_IDS = (ZERO_ID, MINUS_ID, OK_ID)


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(ExceptionLog).filter(ExceptionLog.type == "stock-zero-unlist").delete(
            synchronize_session=False
        )
        s.query(SupplierItem).filter(SupplierItem.item_id.in_(ALL_IDS)).delete(
            synchronize_session=False
        )
        s.commit()


def _make(item_id: str, *, stock: int | None, listed: bool = True, title: str = "测试-零库存货源") -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=item_id,
                supplier_id="sup-1",
                title=title,
                category_id="ht_bedding",
                main_image="http://127.0.0.1:9/a.jpg",
                cost_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                market_price=Decimal("169.00"),
                stock=stock,
                sku_list={"skus": [{"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": 45.5, "stock": 0}]},
                source_type="third_party",
                audit_status="approved",
                is_listed=listed,
            )
        )
        s.commit()


def _item(item_id: str) -> SupplierItem | None:
    with SessionLocal() as s:
        return s.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()


class _FakeClient:
    """假飞书客户端：只记下发出去的文案。"""

    def __init__(self) -> None:
        self.enabled = True
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((chat_id, text))
        return {"code": 0}


class StockGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        _wipe()
        _make(ZERO_ID, stock=0, title="测试-库存归零")
        _make(MINUS_ID, stock=-3, title="测试-库存负数")
        _make(OK_ID, stock=8, title="测试-还有货")

    def tearDown(self) -> None:
        _wipe()

    # ------------------------------------------------------------ 下架
    def test_10_zero_stock_items_get_unlisted(self):
        with SessionLocal() as s:
            out = stock_guard.unlist_zero_stock(s, notify=False)

        ids = [b["item_id"] for b in out["unlisted"]]
        self.assertIn(ZERO_ID, ids)
        self.assertIn(MINUS_ID, ids)
        self.assertNotIn(OK_ID, ids, "还有库存的一款不能被动")

        for item_id in (ZERO_ID, MINUS_ID):
            row = _item(item_id)
            self.assertFalse(row.is_listed, f"{item_id} 应该已下架")
            self.assertEqual(row.unlisted_reason, stock_guard.AUTO_UNLIST_REASON)
            self.assertEqual(row.unlisted_by, stock_guard.AUTO_UNLIST_BY)
            self.assertIsInstance(row.unlisted_at, datetime)

        ok = _item(OK_ID)
        self.assertTrue(ok.is_listed)
        self.assertIsNone(ok.unlisted_reason)

    def test_15_unlisted_items_do_not_show_up_again(self):
        """幂等：下架后再巡检查不到，所以后台每 10 分钟跑一次也不会重复 @ 人。"""
        with SessionLocal() as s:
            first = stock_guard.unlist_zero_stock(s, notify=False)
            second = stock_guard.unlist_zero_stock(s, notify=False)
        self.assertEqual(len(first["unlisted"]), 2)
        self.assertEqual(second["unlisted"], [], "已经下架的不该再被处理一遍")

    def test_20_every_unlist_writes_an_exception_log(self):
        with SessionLocal() as s:
            stock_guard.unlist_zero_stock(s, notify=False)
        with SessionLocal() as s:
            logs = (
                s.query(ExceptionLog)
                .filter(ExceptionLog.type == "stock-zero-unlist")
                .all()
            )
        self.assertEqual(len(logs), 2, "每条自动下架都要留痕")
        self.assertTrue(all(x.resolved for x in logs), "下架就是处理动作，不用运营再点")
        titles = {str(x.payload.get("title")) for x in logs}
        self.assertIn("测试-库存归零", titles)
        self.assertTrue(all(x.error == stock_guard.AUTO_UNLIST_REASON for x in logs))

    # ------------------------------------------------------------ 文案
    def test_30_text_says_the_agreed_sentence(self):
        text = stock_guard.build_text(
            [{"item_id": "A-1", "title": "某某商品", "stock": 0}],
            ["ou_niu", "ou_hu"],
        )
        self.assertIn("请知晓，做好商品上架事宜", text)
        self.assertIn("库存为 0", text)
        self.assertIn("某某商品", text)
        self.assertIn('<at user_id="ou_niu">', text)
        self.assertIn('<at user_id="ou_hu">', text)
        self.assertEqual(stock_guard.build_text([]), "", "没内容就不发")

    def test_32_default_cc_is_the_two_names(self):
        """默认 @ 的就是用户点名的两个人（可在配置里改）。"""
        names = list(settings.stock_out_cc_names)
        self.assertIn("牛磊", names)
        self.assertIn("胡子萍", names)

    # ------------------------------------------------------------ 通知
    def test_40_notify_sends_to_group_with_at(self):
        client = _FakeClient()
        with mock.patch.object(stock_guard, "target_chat_ids", return_value=["oc-test"]), mock.patch(
            "app.services.notify.open_ids_by_names", return_value=["ou_niu", "ou_hu"]
        ):
            ok, errors = stock_guard.notify_zero_stock(
                [{"item_id": "A-1", "title": "某某商品", "stock": 0}], client=client
            )
        self.assertTrue(ok, errors)
        self.assertEqual(len(client.sent), 1)
        chat_id, text = client.sent[0]
        self.assertEqual(chat_id, "oc-test")
        self.assertIn('<at user_id="ou_niu">', text)
        self.assertIn("请知晓，做好商品上架事宜", text)

    def test_45_notify_without_group_does_not_raise(self):
        """没配群：只回一句人话，绝不能把巡检线程搞崩。"""
        with mock.patch.object(stock_guard, "target_chat_ids", return_value=[]):
            ok, errors = stock_guard.notify_zero_stock(
                [{"item_id": "A-1", "title": "某某商品", "stock": 0}], client=_FakeClient()
            )
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertIn("没有可推送的群", errors[0])

    def test_48_notify_without_feishu_credentials(self):
        client = _FakeClient()
        client.enabled = False
        with mock.patch.object(stock_guard, "target_chat_ids", return_value=["oc-test"]):
            ok, errors = stock_guard.notify_zero_stock(
                [{"item_id": "A-1", "title": "某某商品", "stock": 0}], client=client
            )
        self.assertFalse(ok)
        self.assertIn("飞书", errors[0])

    # ------------------------------------------------------------ 开关 / 巡检
    def test_50_run_once_respects_the_switch(self):
        with mock.patch.object(settings, "zero_stock_auto_unlist", False):
            out = stock_guard.run_once()
        self.assertIn("skipped", out)
        self.assertEqual(out["unlisted"], [])
        self.assertTrue(_item(ZERO_ID).is_listed, "开关关着时一个都不动")

    def test_55_run_once_unlists_with_its_own_session(self):
        with mock.patch.object(stock_guard, "notify_zero_stock", return_value=(False, [])) as notify:
            out = stock_guard.run_once()
        self.assertEqual(len(out["unlisted"]), 2)
        self.assertTrue(notify.called, "默认要发群通知")

    def test_60_manual_trigger_endpoint(self):
        """运维手动口子 GET/POST /api/v1/goods/unlist-zero-stock（notify=0 不打扰群）。

        直接调路由函数（这个口子在 /api/v1 下，要登录态，页面级测试不划算）。
        """
        import asyncio

        from src.goods.router import unlist_zero_stock as endpoint

        with SessionLocal() as s:
            out = asyncio.run(endpoint(notify=False, db=s))
        self.assertEqual(len(out["unlisted"]), 2)
        self.assertFalse(_item(ZERO_ID).is_listed)


if __name__ == "__main__":
    unittest.main()
