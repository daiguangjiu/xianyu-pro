"""地址防错：截图上的营销文案不能再被当成收货地址（2026-09-28 修复）。

线上实例（2026-09-28）：
  群里地址写「湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科技园」，
  截图 OCR 出来的「地址」却是「领券狂补省得Duo! 马上和领」（闲鱼截图上的营销文案）。

以前只在「两处都取到值」时比对，于是：
  · 群里有地址 -> 只抛一句「两处不一致，请核对」，噪声天天有，运营看到麻木；
  · 群里没地址 -> 交叉验证压根不跑，营销文案直接当成地址写进订单（真事故）。

现在的做法是源头拦：OCR 里那条内容不像地址就当作没取到 ——
不像地址的值既不参与比对（不打扰），也不写进订单（宁缺勿错），
并试着从截图其它行里把真地址挑出来。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from app.models import Order  # noqa: E402
from app.parser.engine import (  # noqa: E402
    best_address_line,
    looks_like_address,
    parse_text,
)
from app.parser.rules import get_rules  # noqa: E402
from app.services.ingest import _apply_parsed  # noqa: E402

GROUP_ADDR = "湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科技园"
# 闲鱼截图里地址标签下面那一行的营销文案（本次事故现场）
MARKETING = "领券狂补省得Duo! 马上和领"

TEMPLATE = f"""产品名称：苹果磁吸充电宝
收货人：张三
手机号码：13800138000
地址信息：{GROUP_ADDR}
售价：199
"""

TEMPLATE_NO_ADDR = """产品名称：苹果磁吸充电宝
收货人：张三
手机号码：13800138000
售价：199
"""


class LooksLikeAddressTest(unittest.TestCase):
    """形状校验本身：真地址放行，非地址一律挡下。"""

    def test_10_real_addresses(self):
        for value in (
            GROUP_ADDR,
            "安徽省阜阳市临泉县高塘镇范兴集街道",
            "广东省深圳市南山区科技园1号",
            "浙江省杭州市余杭区文一西路969号淘宝城5号楼",
            "北京市朝阳区建国路88号SOHO现代城A座1806室",
            # 线上实例（2026-10-03）：括号一律当噪声，整条地址被丢掉，地址变空要人工补
            "广东省 深圳市 宝安区 沙井街道 和一二路15号(满天下网咖这栋)704",
            "江苏省苏州市吴中区木渎镇金枫路216号（东创科技园）3幢",
        ):
            self.assertTrue(looks_like_address(value), value)

    def test_20_not_addresses(self):
        for value in (
            MARKETING,               # 闲鱼截图营销文案（本次事故）
            "闲鱼手机节",             # 活动名
            "",                      # 空
            "湖南省",                 # 太短
            "Please check out now",  # 纯英文
            "确认收货",               # 按钮文案
            "待发货 立即付款",        # 状态 + 按钮
            # 线上抽查到的：OCR 把「成交价」认成「成区价」，整条被当成地址
            "成区价 (在支付宝担保账户中)                x168.00",
            # 括号里是界面文案（不是楼栋名）：仍然挡下
            "湖南省长沙市岳麓区麓谷街道玉兰路2号(在支付宝担保账户中)",
            "浙江省杭州市余杭区文一西路969号(领券优惠)",
            "商品总价 ¥65.00",
            "订单编号 复制",
        ):
            self.assertFalse(looks_like_address(value), value)

    def test_30_pick_real_line_from_screenshot(self):
        """真地址在别的行上时，能把它挑出来。"""
        ocr = f"""10:59
张三 13800138000
{MARKETING}
{GROUP_ADDR}
闲鱼手机节
"""
        self.assertEqual(best_address_line([ocr]), GROUP_ADDR)
        self.assertIsNone(best_address_line([MARKETING]))


class WriteToOrderTest(unittest.TestCase):
    """落库那一层：不像地址的值写不进订单字段。"""

    def _order(self) -> Order:
        return Order(order_no="T-ADDR", product_name="苹果磁吸充电宝")

    def test_10_junk_not_written(self):
        """截图解析出营销文案：地址保持为空，等运营补。"""
        order = self._order()
        result = parse_text(TEMPLATE_NO_ADDR, [MARKETING], rules=get_rules())
        _apply_parsed(order, result, only_fill_empty=True)
        self.assertFalse(order.receiver_address, "不像地址就不能写进订单")

    def test_20_real_address_written(self):
        """截图里有真地址：照旧补进订单。"""
        order = self._order()
        result = parse_text(TEMPLATE_NO_ADDR, [f"地址信息：{MARKETING}\n{GROUP_ADDR}"], rules=get_rules())
        _apply_parsed(order, result, only_fill_empty=True)
        self.assertEqual(order.receiver_address, GROUP_ADDR)

    def test_30_existing_address_not_overwritten_by_junk(self):
        """订单上已有地址：截图解析出的垃圾不能覆盖它。"""
        order = self._order()
        order.receiver_address = GROUP_ADDR
        result = parse_text(TEMPLATE_NO_ADDR, [MARKETING], rules=get_rules())
        _apply_parsed(order, result)
        self.assertEqual(order.receiver_address, GROUP_ADDR)

    def test_40_bracketed_address_is_written(self):
        """线上实例（2026-10-03）：地址带括号被判「不像地址」，订单地址空着要人工补。"""
        bracketed = "广东省 深圳市 宝安区 沙井街道 和一二路15号(满天下网咖这栋)704"
        template = f"""产品名称：亚朵四代枕头+枕套
收货人：王慧娟
手机号码：18244792173
地址信息：{bracketed}
售价：165
"""
        order = self._order()
        result = parse_text(template, rules=get_rules())
        _apply_parsed(order, result)
        self.assertEqual(order.receiver_address, bracketed, "带楼栋括号的地址要照常落库")


class OcrAddressGuardTest(unittest.TestCase):
    """解析链路：营销文案既不写进订单，也不再抛「两处不一致」。"""

    def setUp(self):
        self.rules = get_rules()

    def test_10_marketing_no_longer_warns(self):
        """群里有地址 + 截图是营销文案：以群里为准，且不再打扰运营。"""
        result = parse_text(TEMPLATE, [MARKETING], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)
        self.assertNotIn(
            "两处", " ".join(result.warnings), "截图那行不是地址，不该抛「两处不一致」"
        )

    def test_20_marketing_not_written_when_group_has_none(self):
        """群里没地址 + 截图只有营销文案：地址留空，绝不能写进营销文案。"""
        result = parse_text(TEMPLATE_NO_ADDR, [MARKETING], rules=self.rules)
        self.assertIsNone(result.fields.get("receiver_address"), "不像地址就不写")

    def test_30_real_address_still_taken_from_screenshot(self):
        """群里没地址 + 截图里有真地址：照旧取截图里的。"""
        ocr = f"""张三 13800138000
{MARKETING}
{GROUP_ADDR}
"""
        result = parse_text(TEMPLATE_NO_ADDR, [ocr], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)

    def test_40_real_address_overrides_when_group_missing(self):
        """地址标签后面跟的是营销文案、真地址在另一行：挑真地址。"""
        ocr = f"地址信息：{MARKETING}\n{GROUP_ADDR}"
        result = parse_text(TEMPLATE_NO_ADDR, [ocr], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)

    def test_50_consistent_address_untouched(self):
        """两处地址一致：照旧取到，不产生任何告警（回归）。"""
        result = parse_text(TEMPLATE, [f"地址信息：{GROUP_ADDR}"], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)
        self.assertFalse(result.warnings)

    def test_60_same_address_with_ocr_typos_is_not_a_conflict(self):
        """截图把「岳麓区麓谷」认成「岳苯区艳谷」：同一个地址，不该报「两处不一样」。"""
        ocr_addr = "湖南省长沙市岳苯区艳谷街道玉兰路2号湘邮科技园"
        result = parse_text(TEMPLATE, [f"地址信息：{ocr_addr}"], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)
        self.assertFalse(result.warnings, "错一两个字是 OCR 常态，不该天天弹提示")

    def test_70_really_different_address_must_warn(self):
        """两处真是两个地址：必须提示，发货前核对（地址错了会发错货）。"""
        other = "安徽省合肥市蜀山区科学大道8号兴园小区"
        result = parse_text(TEMPLATE, [f"地址信息：{other}"], rules=self.rules)
        self.assertEqual(result.fields.get("receiver_address"), GROUP_ADDR)
        self.assertTrue(any("地址" in w and "核对" in w for w in result.warnings), result.warnings)


class OcrAddressLayerTest(unittest.TestCase):
    """截图 OCR 层：地址专项识别的拼接、过滤与接入（不依赖真实 OCR）。"""

    def test_10_folded_address_joined(self):
        """地址被折成两行（「…湘邮科」+「技园」）：拼回一条。"""
        from app.parser.ocr import _merge_address_rows

        rows = ["湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科", "技园"]
        self.assertEqual(
            _merge_address_rows(rows), ["湖南省长沙市岳麓区麓谷街道玉兰路2号湘邮科技园"]
        )

    def test_20_marketing_and_labels_dropped(self):
        """营销文案、字段标签、按钮文案都不是地址。"""
        from app.parser.ocr import _merge_address_rows

        rows = [MARKETING, "订单编号 复制", "免费领", "去发货", "买家已付款，请尽快发货"]
        self.assertEqual(_merge_address_rows(rows), [])

    def test_30_ui_noise_trimmed(self):
        """地址行被一起带出的界面文案要截掉（「…湘邮科技园 去发货」）。"""
        from app.parser.ocr import _merge_address_rows

        rows = [f"{GROUP_ADDR} 去发货"]
        self.assertEqual(_merge_address_rows(rows), [GROUP_ADDR])

    def test_40_augment_appends_when_missing(self):
        """整图没认出地址时：专项识别结果并进文本，并记在 address_hints 上。"""
        from unittest import mock

        from app.parser import ocr as ocr_mod

        with mock.patch.object(ocr_mod, "address_lines", return_value=[GROUP_ADDR]):
            text, hints = ocr_mod._augment_with_address(
                Path("shot.png"), "买家已付款，请尽快发货\n戴娟娟 13787068029"
            )
        self.assertIn(GROUP_ADDR, text)
        self.assertEqual(hints, [GROUP_ADDR])

    def test_50_augment_skipped_when_text_has_address(self):
        """整图已经认出地址：不再多跑几次 OCR（常规订单不变慢）。"""
        from unittest import mock

        from app.parser import ocr as ocr_mod

        with mock.patch.object(
            ocr_mod, "address_lines", side_effect=AssertionError("不该再跑专项识别")
        ):
            text, hints = ocr_mod._augment_with_address(Path("shot.png"), f"地址信息：{GROUP_ADDR}")
        self.assertIn(GROUP_ADDR, text)
        self.assertEqual(hints, [])

    def test_60_ocr_image_ex_carries_address_hints(self):
        """主入口：截图没读出地址时，落库用的文本里要带上专项识别的地址。"""
        from types import SimpleNamespace
        from unittest import mock

        from app.parser import ocr as ocr_mod

        fake_settings = SimpleNamespace(ocr_provider="tesseract", ocr_lang="chi_sim+eng", ocr_upscale=2)
        with mock.patch.object(ocr_mod, "settings", fake_settings), \
             mock.patch.object(ocr_mod, "_ocr_tesseract", return_value="买家已付款，请尽快发货"), \
             mock.patch.object(ocr_mod, "order_no_near_label", return_value=("", "")), \
             mock.patch.object(ocr_mod, "order_no_hints", return_value=[]), \
             mock.patch.object(ocr_mod, "address_lines", return_value=[GROUP_ADDR]):
            out = ocr_mod.ocr_image_ex(Path(__file__).resolve())
        self.assertEqual(out.status, "done")
        self.assertEqual(out.address_hints, [GROUP_ADDR])
        self.assertIn(GROUP_ADDR, out.text)


if __name__ == "__main__":
    unittest.main()
