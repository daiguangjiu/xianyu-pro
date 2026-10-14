"""解析引擎单元测试：python -m unittest discover tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from app.parser.engine import convert, extract_kv_lines, parse_text  # noqa: E402
from app.parser.rules import get_rules  # noqa: E402

SAMPLE = """【闲鱼订单】
订单号：2026010112345678
买家：小鱼儿
商品：iPhone 13 128G 午夜黑
规格：128G/午夜黑
数量：1
金额：¥2,999.00
成本：2400
运费：12
收货人：李四
电话：13800138000
地址：广东省深圳市南山区科技园1号
供应商：华强北数码
下单时间：2026-01-01 12:30:00
备注：尽快发货
"""

# 飞书群里的真实模板（无订单号）
REAL_GROUP_MESSAGE = """产品：亚朵四代单枕头

收货人:贲
手机号码:14790021215
地址信息: 安徽省 滁州市 琅琊区 清流街道 滁州市琅琊区凤凰湖畔18栋
                                                             售价：115
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""

# 对应订单截图经 tesseract(chi_sim+eng) 真实识别出来的文本（系统内已做灰度+放大预处理）
# 注意：OCR 会把长数字串断成 "33169016   47870"，交给规则的 normalize: strip_spaces 还原
REAL_OCR_TEXT = """21:16                           al 5G @)
<                                           eee

TRE, BRRAK. 。心 &

RS -& 18326839658

te
安徽省阜阳市临泉县高塘镇范兴集街道

雷鸟 32U8A Pro | 31.53% +2599.00
寸

支付宝担保账户中)                    ¥2599.00
商品总价                                           ¥2599.00
运费                                                          ¥0.00

订单编号                   3316901665047047870 | 复制

(150     卖出获150g蚂蚁森林能量
"""


# 2026-09 新版上行模板：新增「所属供应链」，且成本价/闲鱼号/回款人允许留空
NEW_GROUP_MESSAGE = """产品： AirPods Pro 3 新款苹果耳机
 收货人： 基拉
手机号码： 17842144173
地址信息： 浙江省 宁波市 北仑区 梅山街道 极氪山川生活区        售价：469

成本价：
所属供应链：
出货闲鱼号：
回款支付宝归属人：
"""

# 同上模板，但四个字段都已回填
NEW_GROUP_MESSAGE_FILLED = """产品： AirPods Pro 3 新款苹果耳机
收货人： 基拉
手机号码： 17842144173
地址信息： 浙江省 宁波市 北仑区 梅山街道 极氪山川生活区
售价：469
成本价：380
所属供应链：华强北数码
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""


class TestConvert(unittest.TestCase):
    def test_money(self):
        self.assertEqual(convert("¥2,999.00", "money")[0], 2999.0)
        self.assertEqual(convert("2400元", "money")[0], 2400.0)

    def test_int(self):
        self.assertEqual(convert("数量 3 件", "int")[0], 3)

    def test_phone(self):
        self.assertEqual(convert("13800138000", "phone")[0], "13800138000")
        self.assertEqual(convert("电话 138-0013-8000", "phone")[0], "13800138000")

    def test_datetime(self):
        value, ok = convert("2026-01-01 12:30:00", "datetime")
        self.assertTrue(ok)
        self.assertEqual(value.strftime("%Y-%m-%d"), "2026-01-01")


class TestKv(unittest.TestCase):
    def test_extract(self):
        kv = extract_kv_lines(SAMPLE)
        self.assertIn("订单号", kv)
        self.assertEqual(kv["订单号"], "2026010112345678")


class TestParse(unittest.TestCase):
    def test_full_message(self):
        rules = get_rules()
        result = parse_text(SAMPLE, rules=rules)
        self.assertEqual(result.platform, "xianyu")
        self.assertEqual(result.fields.get("order_no"), "2026010112345678")
        self.assertEqual(result.fields.get("buyer_nick"), "小鱼儿")
        self.assertEqual(result.fields.get("sale_amount"), 2999.0)
        self.assertEqual(result.fields.get("cost_amount"), 2400.0)
        self.assertEqual(result.fields.get("quantity"), 1)
        self.assertEqual(result.fields.get("receiver_phone"), "13800138000")
        self.assertEqual(result.fields.get("supplier_name"), "华强北数码")
        self.assertEqual(result.fields.get("receiver_address"), "广东省深圳市南山区科技园1号")
        self.assertFalse(result.missing_required)
        self.assertFalse(result.needs_review)

    def test_ocr_fallback(self):
        rules = get_rules()
        text = "【闲鱼订单】\n订单号：A20260101\n商品：耳机\n"
        result = parse_text(text, ["金额：199.00", "供应商：东莞声学"], rules=rules)
        self.assertEqual(result.fields.get("order_no"), "A20260101")
        self.assertEqual(result.fields.get("sale_amount"), 199.0)
        self.assertEqual(result.fields.get("supplier_name"), "东莞声学")
        self.assertEqual(result.sources.get("sale_amount", "").split(":")[0], "ocr")

    def test_non_order_message(self):
        rules = get_rules()
        result = parse_text("今天下午三点开会，大家记得参加", rules=rules)
        self.assertTrue(result.missing_required, "非订单消息应缺失必填字段")


class TestRealTemplate(unittest.TestCase):
    """真实飞书群模板：群消息 + 订单截图 OCR 互补。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = get_rules()

    def test_group_message_only(self):
        result = parse_text(REAL_GROUP_MESSAGE, rules=self.rules)
        f = result.fields
        self.assertEqual(f.get("product_name"), "亚朵四代单枕头")
        self.assertEqual(f.get("receiver_name"), "贲")
        self.assertEqual(f.get("receiver_phone"), "14790021215")
        self.assertIn("滁州市琅琊区凤凰湖畔18栋", f.get("receiver_address", ""))
        self.assertEqual(f.get("sale_amount"), 115.0)
        self.assertEqual(f.get("xianyu_account"), "卖货的小姑娘")
        self.assertEqual(f.get("payee_name"), "卫玉珍")
        self.assertEqual(f.get("quantity"), 1)
        self.assertFalse(result.missing_required, "群消息应命中必填字段 product_name")

    def test_with_screenshot_ocr(self):
        result = parse_text(REAL_GROUP_MESSAGE, [REAL_OCR_TEXT], rules=self.rules)
        f = result.fields
        # 群消息优先
        self.assertEqual(f.get("sale_amount"), 115.0, "售价应以群消息为准，不被截图成交价覆盖")
        self.assertEqual(f.get("receiver_phone"), "14790021215")
        # 截图补充
        self.assertEqual(f.get("order_no"), "3316901665047047870")
        # 该截图里买家昵称被 OCR 成噪声，应宁缺勿错
        self.assertNotIn("buyer_nick", f)
        self.assertEqual(f.get("shipping_fee"), 0.0)
        # 这份 OCR 文本其实是另一单（雷鸟电视 ¥2599，群里是亚朵枕头 ¥115，收货人与手机号也对不上）：
        # 手机号、地址、总价三处对不上，正是要摆给运营核对的，所以必须标存疑
        self.assertTrue(result.needs_review)
        self.assertEqual(result.sources.get("order_no", "").split(":")[0], "ocr")

    def test_screenshot_only(self):
        """只有截图（无群消息文本）时，也能从 OCR 文本建单。"""
        result = parse_text("", [REAL_OCR_TEXT], rules=self.rules)
        f = result.fields
        self.assertEqual(f.get("order_no"), "3316901665047047870")
        self.assertEqual(f.get("product_name"), "雷鸟 32U8A Pro")
        self.assertEqual(f.get("sale_amount"), 2599.0)
        self.assertEqual(f.get("shipping_fee"), 0.0)
        self.assertEqual(f.get("receiver_address"), "安徽省阜阳市临泉县高塘镇范兴集街道")
        self.assertFalse(result.missing_required)


class TestOcrNoise(unittest.TestCase):
    """截图 OCR 的两个高频坑：货币符号被认成 Y / x、「成交价」被认成「成区价」。

    2026-09-25 的真实数据里，就是因为这两个坑，成交价 / 商品总价一直是 0，
    同时「买家已付款」「买家了昵称」被当成买家昵称取了回来。
    """

    RULES = None

    @classmethod
    def setUpClass(cls):
        cls.RULES = get_rules()

    def test_money_symbol_variants(self):
        """¥ 被 OCR 认成 x / Y 时，成交价与商品总价也要取得到。"""
        ocr = (
            "买家已付款，请尽快发货\n"
            "成交价 (在支付宝担保账户中)                 x200.00\n"
            "商品总价                                              Y200.00\n"
            "运费                                      Y0.00\n"
        )
        f = parse_text("", [ocr], rules=self.RULES).fields
        self.assertEqual(f.get("deal_amount"), 200.0)
        self.assertEqual(f.get("total_amount"), 200.0)

    def test_deal_label_ocr_mistake(self):
        """「成交价」被认成「成区价」时照样取值。"""
        ocr = (
            "成区价 (在支付宝担保账户中)                ¥165.00\n"
            "商品总价                             Y165.00\n"
        )
        f = parse_text("", [ocr], rules=self.RULES).fields
        self.assertEqual(f.get("deal_amount"), 165.0)
        self.assertEqual(f.get("total_amount"), 165.0)

    def test_total_amount_takes_last_number(self):
        """商品总价要取「已优惠」之后的那个数，不能取括号里的优惠额。"""
        ocr = (
            "成交价 (在支付宝担保账户中)         ¥50.00\n"
            "商品总价 (已优惠#9.00)            Y50.00\n"
        )
        f = parse_text("", [ocr], rules=self.RULES).fields
        self.assertEqual(f.get("total_amount"), 50.0)

    def test_buyer_nick_noise_is_ignored(self):
        """截图里的「买家已付款」「买家了昵称」不能被当成买家昵称。"""
        for line in ("买家已付款，请尽快发货", "买家了昵称                           老琴友的琴房"):
            f = parse_text("", [line], rules=self.RULES).fields
            self.assertNotIn("buyer_nick", f, f"{line!r} 不该取出买家昵称")

    def test_explicit_buyer_nick_still_works(self):
        """群里明写「买家：小鱼儿」这种正常写法照旧能取到。"""
        f = parse_text("产品名称：枕头\n买家：小鱼儿\n", rules=self.RULES).fields
        self.assertEqual(f.get("buyer_nick"), "小鱼儿")


class TestNewTemplate(unittest.TestCase):
    """新版上行模板：留空字段不能污染相邻字段。"""

    def test_blank_fields_are_empty(self):
        f = parse_text(NEW_GROUP_MESSAGE, rules=get_rules()).fields
        self.assertEqual(f.get("product_name"), "AirPods Pro 3 新款苹果耳机")
        self.assertEqual(f.get("receiver_name"), "基拉")
        self.assertEqual(f.get("receiver_phone"), "17842144173")
        self.assertEqual(f.get("receiver_address"), "浙江省 宁波市 北仑区 梅山街道 极氪山川生活区")
        self.assertEqual(f.get("sale_amount"), 469.0)
        self.assertEqual(f.get("quantity"), 1)
        # 上行时留空的字段必须为空，不能被下一行标签填充
        for key in ("cost_amount", "supplier_name", "xianyu_account", "payee_name"):
            self.assertNotIn(key, f, f"{key} 应为空，不能吞掉后续字段")

    def test_two_line_layout(self):
        """同一模板的地址与售价分行写在两个版本里结果一致。"""
        text = NEW_GROUP_MESSAGE.replace(
            "极氪山川生活区        售价：469", "极氪山川生活区\n售价：469\n"
        )
        f = parse_text(text, rules=get_rules()).fields
        self.assertEqual(f.get("receiver_address"), "浙江省 宁波市 北仑区 梅山街道 极氪山川生活区")
        self.assertEqual(f.get("sale_amount"), 469.0)

    def test_filled_fields(self):
        f = parse_text(NEW_GROUP_MESSAGE_FILLED, rules=get_rules()).fields
        self.assertEqual(f.get("cost_amount"), 380.0)
        self.assertEqual(f.get("supplier_name"), "华强北数码")
        self.assertEqual(f.get("xianyu_account"), "卖货的小姑娘")
        self.assertEqual(f.get("payee_name"), "卫玉珍")


class TestMultilineValues(unittest.TestCase):
    """复制粘贴的模板经常把字段值换行：值要取全，并错的要提示人工确认。"""

    # 群里真实发过的两单（商品名被折成两行）
    CASE_A = (
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
    CASE_B = CASE_A.replace("梁程伟", "李女士").replace(
        "20000毫安 22.5w q2电 自带线（C+L）", "20000毫安 30w  q2电 自带线（双C口）"
    ).replace("13088070863", "18303136964").replace(
        "四川省成都市龙泉驿区大面街道青台山路泰华锦城11栋1单元405",
        "河北省张家口市万全区孔家庄镇同城雅居1号楼一单元",
    ).replace("紫色", "蓝色")

    def test_product_name_spans_two_lines(self):
        for name, text in (("A", self.CASE_A), ("B", self.CASE_B)):
            r = parse_text(text, rules=get_rules())
            self.assertIn("20000毫安", r.fields.get("product_name", ""), f"案例 {name} 商品名要取全")
            self.assertTrue(
                r.fields.get("product_name", "").startswith("倍思Q2电充电宝"),
                f"案例 {name} 商品名要从「倍思Q2电充电宝」开始",
            )
            # 合并是靠谱的：不该把正常订单打成「待确认」
            self.assertFalse(r.warnings, f"案例 {name} 不该报存疑：{r.warnings}")

    def test_other_fields_still_correct(self):
        f = parse_text(self.CASE_A, rules=get_rules()).fields
        self.assertEqual(f.get("receiver_name"), "梁程伟")
        self.assertEqual(f.get("receiver_phone"), "13088070863")
        self.assertEqual(
            f.get("receiver_address"),
            "四川省成都市龙泉驿区大面街道青台山路泰华锦城11栋1单元405",
        )
        self.assertEqual(f.get("note"), "紫色")
        self.assertEqual(f.get("sale_amount"), 50.0)
        self.assertEqual(f.get("cost_amount"), 45.0)
        self.assertEqual(f.get("supplier_name"), "BH001")

    def test_amount_on_next_line(self):
        """「成本价：」留了个空行、值在下一行时也要取到。"""
        text = (
            "产品名称：测试商品\n"
            "收货人：张三\n"
            "手机号码：13800138000\n"
            "地址信息：浙江省宁波市北仑区某路1号\n"
            "售价：\n50\n"
            "成本价：\n45\n"
            "所属供应链：BH001"
        )
        f = parse_text(text, rules=get_rules()).fields
        self.assertEqual(f.get("sale_amount"), 50.0)
        self.assertEqual(f.get("cost_amount"), 45.0)

    def test_address_spans_two_lines(self):
        text = (
            "产品名称：充电宝\n"
            "收货人：张三\n"
            "手机号码：13800138000\n"
            "地址信息：浙江省宁波市北仑区某路\n"
            "1号楼2单元303\n"
            "售价：50\n成本价：45"
        )
        r = parse_text(text, rules=get_rules())
        self.assertIn("1号楼2单元303", r.fields.get("receiver_address", ""))
        self.assertFalse(r.warnings)

    def test_suspicious_continuation_is_flagged(self):
        """并进来的内容看着像手机号 / 金额 / 聊天时，明确提示人工确认。"""
        base = (
            "收货人：张三\n手机号码：13800138000\n"
            "地址信息：浙江省宁波市北仑区某路1号\n售价：50\n成本价：45"
        )
        for extra in ("13800138000", "只要49.9元", "谢谢老板支持"):
            r = parse_text(f"产品名称：充电宝\n{extra}\n{base}", rules=get_rules())
            self.assertTrue(r.warnings, f"{extra} 应被判为存疑")
            self.assertTrue(r.needs_review, "存疑的订单要进「待人工确认」")

    def test_colon_line_is_not_merged(self):
        """续行自己带「标签:」时是另一个字段，不能并进上一个字段。"""
        text = (
            "产品名称：充电宝\n"
            "颜色：黑色\n"
            "收货人：张三\n手机号码：13800138000\n"
            "地址信息：浙江省宁波市北仑区某路1号\n售价：50\n成本价：45"
        )
        r = parse_text(text, rules=get_rules())
        self.assertEqual(r.fields.get("product_name"), "充电宝")
        self.assertFalse(r.warnings)

    def test_review_reason_text(self):
        """存疑原因要能直接给运营看。"""
        text = (
            "产品名称：充电宝\n13800138000\n"
            "收货人：张三\n手机号码：13800138000\n"
            "地址信息：浙江省宁波市北仑区某路1号\n售价：50\n成本价：45"
        )
        r = parse_text(text, rules=get_rules())
        self.assertIn("存疑：", r.review_reason())
        self.assertIn("产品名称", r.review_reason())


if __name__ == "__main__":
    unittest.main()
