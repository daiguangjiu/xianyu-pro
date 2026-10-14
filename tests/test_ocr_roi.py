"""订单截图 ROI 专项识别（ocr_roi）的纠错口径（2026-09-29 新增）。

线上实例（2026-09-29 实测真实闲鱼订单截图，1080x2400）：
  · 收货人姓名整图糊成 "FINASHR18796323478"（姓名那几个字全丢）；
  · 商品总价认成 "商品总价(E7tBY¥9.00)¥50.00"（两个金额粘一行）；
  · 而订单号、地址两项早就改成「裁区域 -> 放大 -> 单独认」，明显更准。

本模块把同样的做法补到金额、收货人上，但口径是保守的：
  ROI 只能纠正「整图明显不对」的值，认不出来就维持原样（宁缺勿错），
  所以这里测的重点不是「能不能认出来」（那要真图），而是「什么时候该动、
  什么时候不该动」——把对的改错比认不出来更糟。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from app.config import settings  # noqa: E402
from app.parser import ocr_roi  # noqa: E402


class MoneyFromTextTest(unittest.TestCase):
    """整图文本里的金额取值：只认两位小数的钱，孤立的整数不算。"""

    def test_reads_labeled_money(self):
        got = ocr_roi.money_from_text("成交价(在支付宝担保账户中)¥396.00\n商品总价¥396.00")
        self.assertEqual(got, {"成交价": 396.0, "商品总价": 396.0})

    def test_ignores_plain_integers(self):
        """「运费 4」这种孤零零的整数多半是界面上的别的东西，不认。"""
        self.assertEqual(ocr_roi.money_from_text("运费 4"), {})

    def test_glued_two_money_in_one_line(self):
        """实测乱码：一行里粘了两个金额，取到的是第一个（后面靠 ROI 纠正）。"""
        got = ocr_roi.money_from_text("商品总价(E7tBY¥9.00)¥50.00")
        self.assertEqual(got, {"商品总价": 9.0})


class MoneyNeedFixTest(unittest.TestCase):
    """金额纠错的触发条件：版式上「成交价 == 商品总价」，对不上才动。"""

    def test_no_roi_value_keeps_original(self):
        self.assertEqual(ocr_roi._money_need_fix({"商品总价": 396.0}, None), (False, ""))

    def test_consistent_values_not_touched(self):
        """两处一致、也和 ROI 一致 —— 说明整图认对了，不动。"""
        self.assertEqual(
            ocr_roi._money_need_fix({"成交价": 396.0, "商品总价": 396.0}, 396.0), (False, "")
        )

    def test_conflict_triggers_fix(self):
        need, why = ocr_roi._money_need_fix({"成交价": 50.0, "商品总价": 9.0}, 50.0)
        self.assertTrue(need)
        self.assertIn("对不上", why)

    def test_missing_money_triggers_fix(self):
        need, _ = ocr_roi._money_need_fix({}, 69.0)
        self.assertTrue(need)

    def test_different_from_roi_triggers_fix(self):
        """只有一个金额、又和 ROI 不一致 —— 多半整图认错了。"""
        need, why = ocr_roi._money_need_fix({"商品总价": 3863.2}, 69.0)
        self.assertTrue(need)
        self.assertIn("不一致", why)


class NameNeedFixTest(unittest.TestCase):
    """收货人纠错：整图那串不像姓名才动（实测 "FINASHR" 这种）。"""

    def test_reads_labeled_name(self):
        self.assertEqual(ocr_roi.name_from_text("收货人：王小萌 13800138000"), "王小萌")

    def test_missing_name_triggers_fix(self):
        self.assertTrue(ocr_roi._name_need_fix("", "王小萌")[0])

    def test_valid_name_not_touched(self):
        """整图已经是像样的中文姓名，哪怕 ROI 认成别的也不改。"""
        self.assertEqual(ocr_roi._name_need_fix("王小萌", "张三"), (False, ""))

    def test_garbled_name_triggers_fix(self):
        need, why = ocr_roi._name_need_fix("FINASHR", "付娜")
        self.assertTrue(need)
        self.assertIn("FINASHR", why)


class AugmentGuardTest(unittest.TestCase):
    """兜底：开关关掉、图片不存在、图片上没有这些区域 —— 都不能改坏原文。"""

    def test_disabled_returns_original(self):
        original = settings.ocr_roi
        settings.ocr_roi = False
        try:
            text, notes = ocr_roi.augment(Path("/tmp/not-exist.png"), "商品总价¥396.00")
        finally:
            settings.ocr_roi = original
        self.assertEqual(text, "商品总价¥396.00")
        self.assertEqual(notes, [])

    def test_missing_image_returns_original(self):
        text, notes = ocr_roi.augment(Path("/tmp/definitely-not-here.png"), "成交价¥69.00")
        self.assertEqual(text, "成交价¥69.00")
        self.assertEqual(notes, [])


class VoteTest(unittest.TestCase):
    """多阈值投票：次数多的优先，同票取更长的。"""

    def test_picks_most_common(self):
        self.assertEqual(ocr_roi._vote(["付娜", "付娜", "付好"]), "付娜")

    def test_tie_prefers_longer(self):
        self.assertEqual(ocr_roi._vote(["付娜", "王小萌"]), "王小萌")

    def test_empty(self):
        self.assertEqual(ocr_roi._vote([]), "")

    def test_requires_min_votes(self):
        """四档各认各的 -> 这块根本没看清，宁可不要（姓名没有第二个数据源可校验）。"""
        self.assertEqual(ocr_roi._vote(["张三"], min_votes=2), "")
        self.assertEqual(ocr_roi._vote(["张三", "张三"], min_votes=2), "张三")


class NameFromTextGluedTest(unittest.TestCase):
    """整图姓名取值：闲鱼截图上姓名常与手机号粘成一行，标签常常没被认出来。"""

    def test_glued_name_and_phone(self):
        self.assertEqual(ocr_roi.name_from_text("冰本研15022405112"), "冰本研")

    def test_status_line_not_treated_as_name(self):
        """「买家已付款，请尽快发货」是订单状态，不能当收货人（实测踩过）。"""
        self.assertEqual(ocr_roi.name_from_text("买家已付款，请尽快发货"), "")


class NameCleanupTest(unittest.TestCase):
    """姓名候选里的界面文案要清掉：实测认出过「去发货」「人孙本阁」。"""

    def test_strips_label_prefix(self):
        self.assertEqual(ocr_roi._strip_noise("人孙本阁"), "孙本阁")

    def test_ui_words_filtered(self):
        picked = [
            t for t in ("去发货", "去上发贷") if not any(w in t for w in ocr_roi._NAME_STOPWORDS)
        ]
        self.assertEqual(picked, [])


class MoneyConsistentTest(unittest.TestCase):
    """整图金额能不能直接信（决定了要不要多跑一次 ROI）。"""

    def test_two_equal_values(self):
        self.assertTrue(ocr_roi._money_consistent({"成交价": 50.0, "商品总价": 50.0}))

    def test_single_value_needs_roi(self):
        self.assertFalse(ocr_roi._money_consistent({"商品总价": 3863.2}))

    def test_conflict_needs_roi(self):
        self.assertFalse(ocr_roi._money_consistent({"成交价": 50.0, "商品总价": 9.0}))


if __name__ == "__main__":
    unittest.main()
