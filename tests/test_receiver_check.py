"""收货信息「待核对」：地址 / 收货人 / 手机号不确定时必须明说（2026-09-28）。

背景（用户原话）：地址、收货人、手机号两边比对很重要，发错了会引起售后问题。
所以这三项只要出现下面任一情况，就在订单上标「待核对」并写进备注，
回群通知和推单文案里也要带上这句提醒 —— 不能只留一句笼统的
「部分字段待人工确认」，运营看不见到底该核对什么：
  · 群里没写、只有截图里有（OCR 本来就认不准）；
  · 两处都写了但说法对不上（必有一个是错的）；
  · 两边都没取到（发不了货）。

反过来，两处一致、或只有群里手打的值，都不标 —— 标多了运营会当成背景噪声。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from app.models import Order  # noqa: E402
from app.parser.engine import parse_text  # noqa: E402
from app.parser.rules import get_rules  # noqa: E402
from app.services.ingest import _mark_review  # noqa: E402
from app.services.notify import (  # noqa: E402
    DEFAULT_RECEIPT_TEMPLATE,
    _default_receipt_text,
    notify_context,
    pending_check_labels,
    pending_check_names,
    render_notify,
)
from app.services.push import push_text  # noqa: E402

ADDR = "湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科技园"
OTHER_ADDR = "安徽省合肥市蜀山区科学大道8号兴园小区"
OCR_TEXT = f"收货人：戴娟娟\n手机号码：13787068029\n地址信息：{ADDR}"
GROUP = (
    "产品名称：小米变焦支架自拍杆 2\n"
    "收货人：戴娟娟\n"
    "手机号码：13787068029\n"
    f"地址信息：{ADDR}\n"
    "售价：65"
)


def parse(group: str, ocr: str | None = None):
    return parse_text(group, [ocr] if ocr else None, rules=get_rules())


def names(result) -> list[str]:
    """待核对字段的纯名字（去掉括号里的原因）。"""
    return [label.split("（")[0] for label in result.review_fields]


class PendingCheckTest(unittest.TestCase):
    """哪些情况该标待核对（也顺便守住「别乱标」）。"""

    def setUp(self):
        self.rules = get_rules()

    def test_10_ocr_only_and_complete_is_fine(self):
        """群里没写、只有截图里有，但认全了（地址到区县、手机号 11 位）：可以直接用。

        群里模板消息本来就不含收货信息，收货三件套全靠截图 —— 一律都标等于每单都标，
        运营会当成背景噪声。认全了就信它。
        """
        result = parse("", OCR_TEXT)
        self.assertEqual(result.review_fields, [], result.review_fields)

    def test_15_ocr_only_and_truncated_is_uncertain(self):
        """群里没写、只有截图里有，而且只认出半截（缺区县、手机号位数不对）：必须标。"""
        result = parse("", "收货人：戴娟娟\n手机号码：137870\n地址信息：湖南省长沙市岳麓区")
        self.assertEqual(sorted(names(result)), ["地址", "手机号"])

    def test_20_two_sides_agree_is_fine(self):
        """两处写的一致：互为佐证，不该标（天天标就等于没标）。"""
        result = parse(GROUP, OCR_TEXT)
        self.assertEqual(result.review_fields, [])

    def test_30_group_only_is_fine(self):
        """只有群里手打的值（没有截图）：手打的比 OCR 可信，不标。"""
        result = parse(GROUP, None)
        self.assertEqual(result.review_fields, [])

    def test_40_conflict_must_be_marked(self):
        """两处对不上：必有一个是错的，必须标出来让运营核对。"""
        result = parse(GROUP.replace(ADDR, OTHER_ADDR), OCR_TEXT)
        self.assertEqual(names(result), ["地址"])
        self.assertIn("与群里写的不一致", result.review_fields[0])

    def test_50_missing_is_not_marked_here(self):
        """两边都没取到：不占「待核对」—— 缺值走缺字段提示，免得大面积误报。

        解析层分不清「这单真没有收货信息」和「订单上本来就有、只是这轮没解析到」，
        一律标会把真要核对的淹掉；空值在订单页一眼就看得到。
        """
        result = parse("产品名称：小米自拍杆\n售价：65", "订单号：123456789\n成交价：65")
        self.assertEqual(result.review_fields, [])

    def test_60_bad_phone_is_marked(self):
        """截图里认出来的手机号位数不对：照着它打过去就是空号，必须核对。"""
        result = parse(GROUP.replace("13787068029", "1378706"), "手机号码：13787\n地址信息：" + ADDR)
        self.assertIn("手机号", names(result))

    def test_70_address_typo_is_not_a_conflict(self):
        """OCR 认错一两个字（岳麓区 -> 岳苯区）不算对不上：不该标。"""
        typo = OCR_TEXT.replace(ADDR, "湖南省长沙市岳苯区艳谷街道玉兰路2号湘邮科技园")
        self.assertEqual(parse(GROUP, typo).review_fields, [])


class PendingCheckNoticeTest(unittest.TestCase):
    """待核对要一路带到备注、回群通知和推单文案里。"""

    def setUp(self):
        # 只有截图、且地址只认出半截：这是最典型的「不确定」，提醒必须发得出去
        self.result = parse("", "收货人：戴娟娟\n手机号码：13787068029\n地址信息：湖南省长沙市岳麓区")
        self.order = Order(
            id=1,
            product_name="小米变焦支架自拍杆 2",
            order_no="5127743052126267544",
            receiver_name=self.result.fields.get("receiver_name") or "",
            receiver_phone=self.result.fields.get("receiver_phone") or "",
            receiver_address=self.result.fields.get("receiver_address") or "",
            sale_amount=65.0,
        )
        _mark_review(self.order, self.result)

    def test_10_reason_written_to_order(self):
        """订单备注里写清楚待核对什么（运营在列表悬浮、详情页都能看到）。"""
        self.assertTrue(self.order.needs_review)
        self.assertIn("待核对：地址", self.order.review_reason)
        self.assertEqual(pending_check_names(self.order), ["地址"])
        self.assertTrue(all("（" in label for label in pending_check_labels(self.order)))

    def test_20_receipt_carries_the_warning(self):
        """回群回执里要点名核对哪些字段。"""
        text = _default_receipt_text(None, self.order)
        self.assertIn("地址", text)
        self.assertIn("待核对", text)
        self.assertIn("发货前请先核对", text)

    def test_30_receipt_template_carries_the_warning(self):
        """设置页改过的回执模板（内置模板）同样带这句提醒。"""
        text = render_notify(DEFAULT_RECEIPT_TEMPLATE, notify_context(self.order))
        self.assertIn("待核对", text)
        self.assertIn("地址", text)

    def test_40_push_text_carries_the_warning(self):
        """推单文案是发给供应商发货用的：地址不确定时也要写一句。"""
        text = push_text(self.order)
        self.assertIn("地址信息", text)
        self.assertIn("待核对", text)
        self.assertIn("请先确认再发货", text)

    def test_50_no_pending_no_warning(self):
        """没有待核对的订单：回执里不该凭空冒出「待核对」。"""
        clean = parse(GROUP, OCR_TEXT)
        order = Order(
            id=2,
            order_no="5127743052126267545",
            product_name="小米变焦支架自拍杆 2",
            receiver_name="戴娟娟",
            receiver_phone="13787068029",
            receiver_address=ADDR,
            sale_amount=65.0,
        )
        _mark_review(order, clean)
        self.assertEqual(pending_check_labels(order), [])
        self.assertNotIn("待核对", _default_receipt_text(None, order))
        self.assertNotIn("待核对", push_text(order))


if __name__ == "__main__":
    unittest.main()
