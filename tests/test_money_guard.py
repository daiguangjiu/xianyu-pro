"""截图里的金额必须有货币符号或小数点才算数（2026-09-28）。

真实事故：订单 5127476906502025235（网球拍），截图里「运费 ¥0.00」被 OCR 认成
乱码，紧跟着的乱码行里有个孤零零的 4（电量 / 界面上的数字），结果运费被认成
¥4.00，回群通知里就多了一句「运费 ¥4.00」，运营看着莫名其妙。

根因：截图文本里「运费」是标签，merge_continuation_lines 会把下面那行乱码并回
「运费」这一行，运费的正则正好落在那个孤立数字上。运费 4 < 售价 129，
「运费比售价还高」那条兜底校验也拦不住。

所以：截图路径的金额字段（售价 / 成本 / 运费 / 手续费）要求匹配串里带 ¥ / ￥
或小数点；群消息是手打的，不受这条约束（「运费：8」照旧取 8）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401

from app.parser.engine import parse_text  # noqa: E402
from app.parser.rules import get_rules  # noqa: E402

GROUP = (
    "产品名称:N系列网球拍紫色\n"
    "收货人:钟蕾\n"
    "手机号码:18616658255\n"
    "地址信息: 上海 上海市 徐汇区 田林街道 古井路38弄7号1102室\n"
    "备注:紫色，单球拍\n"
    "售价：129\n"
    "\n"
    "成本价：95\n"
    "所属供应链：BH001"
)

# 事故现场：截图里运费明明是 ¥0.00，「运费」标签下面一行乱码里有个孤立的 4
OCR_BAD = """21:3106                  NS Tul Sul E49) 100%
价               ¥ 129.00
运费                  ¥0.00
eg ae
运费
s     4  | |
21:31 @CB
钟董 18616658255
上海上海市徐汇区田林街站古井路38弄7号
11022
"""

# 正常截图：运费带货币符号
OCR_OK_YEN = """成交价 (在支付宝担保账户中)    ¥129.00
商品总价                       ¥129.00
运费                           ¥8.00
订单编号                       5127476906502025235
"""

# 只带了小数点（OCR 没认全货币符号）也算数
OCR_OK_DOT = """商品总价  129.00
运费       8.00
"""


class MoneyGuardTest(unittest.TestCase):
    def test_10_stray_digit_is_not_shipping_fee(self):
        """事故复现：「运费」下面的孤立 4 不能当运费。"""
        result = parse_text(GROUP, [OCR_BAD], rules=get_rules())
        self.assertNotEqual(result.fields.get("shipping_fee"), 4.0, result.sources)
        self.assertEqual(result.fields.get("shipping_fee") or 0, 0.0)
        # 售价 / 成交价照旧认出来（它们都带 ¥）
        self.assertEqual(result.fields.get("sale_amount"), 129.0)

    def test_20_yen_value_is_kept(self):
        """截图里带 ¥ 的运费照旧取到。"""
        result = parse_text(GROUP, [OCR_OK_YEN], rules=get_rules())
        self.assertEqual(result.fields.get("shipping_fee"), 8.0)

    def test_30_decimal_value_is_kept(self):
        """只有小数点也算数：OCR 常把 ¥ 认丢，但 8.00 这种写法是明确的金额。"""
        result = parse_text(GROUP, [OCR_OK_DOT], rules=get_rules())
        self.assertEqual(result.fields.get("shipping_fee"), 8.0)

    def test_40_group_message_plain_number_still_works(self):
        """群消息手打的「运费：8」不受这条约束影响。"""
        result = parse_text(GROUP + "\n运费：8", None, rules=get_rules())
        self.assertEqual(result.fields.get("shipping_fee"), 8.0)

    def test_50_zero_fee_stays_zero(self):
        """截图里认出「运费 ¥0.00」就是 0，不是缺失也不是别的数。"""
        result = parse_text(GROUP, [OCR_BAD], rules=get_rules())
        self.assertEqual(result.fields.get("shipping_fee") or 0, 0.0)


if __name__ == "__main__":
    unittest.main()
