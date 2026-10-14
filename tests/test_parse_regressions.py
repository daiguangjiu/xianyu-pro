"""解析 / 归档的历史错误回归集（2026-09-29）。

把线上真实踩过的坑固化成用例，同一个坑不再踩第二次。每一条都对应过一次人工纠错：

  1. 韩晓云那单：运营不按模板写标签，直接贴「姓名 / 手机号 / 地址」三行 ——
     以前整段收货信息全丢，商品名还被后面两行污染（收货人、地址只能人工改）；
  2. 闲鱼买家编号 `[4673]`：跟着姓名 / 地址一起复制过来，是闲鱼内部标识，
     不能进人名、地址、手机号；
  3. 王小萌两单：同群先后两条内容一致的模板消息，第二张截图被
     「最近的那条模板已经配过图」挡住 —— 第二单既没截图、又被指纹判成重复推送，
     直接丢单（少发一件货、少算一笔成本）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, OrderImage, RawMessage  # noqa: E402
from app.parser.engine import parse_text, strip_buyer_code  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.timeutil import now  # noqa: E402

CHAT = "oc_regression"

# 标准写法（模板标签齐全）
TEMPLATE_TEXT = """产品名称：亚朵四代枕头全套
收货人:王小萌
手机号码:18561274331
地址信息: 山东省 威海市 文登区 环山街道 豹山路邮政宾馆大院西北角2楼
售价：180
成本价：160
所属供应链：BH001
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍
"""

# 韩晓云那单：没有标签，姓名 / 手机号 / 地址三行裸贴（买家编号也跟着复制过来了）
UNLABELED_TEXT = """产品名称：N系列网球拍紫色+网球拍底座训练器和训练网球
韩晓云[4673] 
15781432531-4673
河北省石家庄市裕华区建通街道仓兴街15号盛邦花园3区3号楼1单元403[4673]
备注：紫色

售价：141

成本价：107
所属供应链：BH001
出货闲鱼号：哔哩叭啦
回款支付宝归属人：周玉萍"""

# 姓名与手机号写在同一行（也有人这么发）
NAME_PHONE_SAME_LINE = """产品名称：亚朵四代枕头全套
王小萌 18561274331
山东省威海市文登区环山街道豹山路邮政宾馆大院西北角2楼
售价：180
成本价：160"""

# 系统回执（机器人发的）：内容里复述了订单号 / 金额 / 收货，容易被误判成订单模板消息
RECEIPT_TEXT = """@_user_1 @_user_2 ✅ 你的订单已收到， 系统已经处理订单归档。

订单号：3316466210241024789
产品：亚朵四代枕头全套

金额：售价 ¥180.00 ｜ 成本 ¥160.00
收货：王小萌 185****4331
供应商：安徽梦想电商（BH001）

详情：http://example.com/orders/90

@_user_3 ⚠️请抓紧安排供应链采购和发货事宜！
"""


class UnlabeledContactParseTest(unittest.TestCase):
    """1、2：无标签收货信息 + 闲鱼买家编号。"""

    def test_three_lines_without_labels(self) -> None:
        r = parse_text(UNLABELED_TEXT)
        self.assertEqual(r.fields.get("product_name"), "N系列网球拍紫色+网球拍底座训练器和训练网球")
        self.assertEqual(r.fields.get("receiver_name"), "韩晓云")
        self.assertEqual(r.fields.get("receiver_phone"), "15781432531")
        self.assertEqual(
            r.fields.get("receiver_address"),
            "河北省石家庄市裕华区建通街道仓兴街15号盛邦花园3区3号楼1单元403",
        )
        # 后面几个标签字段照常解析，没有被收货信息带偏
        self.assertEqual(r.fields.get("sale_amount"), 141.0)
        self.assertEqual(r.fields.get("cost_amount"), 107.0)
        self.assertEqual(r.fields.get("note"), "紫色")
        self.assertEqual(r.fields.get("supplier_name"), "BH001")
        self.assertEqual(r.fields.get("xianyu_account"), "哔哩叭啦")
        self.assertEqual(r.fields.get("payee_name"), "周玉萍")

    def test_product_name_not_polluted_by_contact_lines(self) -> None:
        """商品名不能被「姓名 / 手机号」两行并进来（以前就是这样丢的收货信息）。"""
        r = parse_text(UNLABELED_TEXT)
        name = str(r.fields.get("product_name") or "")
        self.assertNotIn("韩晓云", name)
        self.assertNotIn("15781432531", name)

    def test_name_and_phone_on_same_line(self) -> None:
        r = parse_text(NAME_PHONE_SAME_LINE)
        self.assertEqual(r.fields.get("receiver_name"), "王小萌")
        self.assertEqual(r.fields.get("receiver_phone"), "18561274331")
        self.assertEqual(
            r.fields.get("receiver_address"),
            "山东省威海市文登区环山街道豹山路邮政宾馆大院西北角2楼",
        )

    def test_buyer_code_stripped_with_labels(self) -> None:
        """带标签的写法也要剥掉买家编号。"""
        r = parse_text(
            "产品名称：亚朵四代枕头全套\n"
            "收货人:韩晓云[4673]\n"
            "手机号码:15781432531-4673\n"
            "地址信息: 河北省石家庄市裕华区建通街道仓兴街15号盛邦花园3区3号楼1单元403[4673]\n"
            "售价：141\n"
        )
        self.assertEqual(r.fields.get("receiver_name"), "韩晓云")
        self.assertEqual(r.fields.get("receiver_phone"), "15781432531")
        self.assertEqual(
            r.fields.get("receiver_address"),
            "河北省石家庄市裕华区建通街道仓兴街15号盛邦花园3区3号楼1单元403",
        )

    def test_strip_buyer_code_helper(self) -> None:
        self.assertEqual(strip_buyer_code("韩晓云[4673]", "receiver_name"), "韩晓云")
        self.assertEqual(strip_buyer_code("韩晓云[", "receiver_name"), "韩晓云")
        self.assertEqual(
            strip_buyer_code("…3号楼1单元403[4673]", "receiver_address"), "…3号楼1单元403"
        )


class ScreenshotPairingTest(unittest.TestCase):
    """3：截图配对 —— 上一条模板已配图时，图要留给下一条还没配图的模板。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            db.query(OrderImage).delete()
            db.query(Order).delete()
            db.query(RawMessage).delete()
            db.commit()

    def _add(
        self, message_id: str, msg_type: str, when, parent: str = "", text: str = ""
    ) -> None:
        with SessionLocal() as db:
            db.add(
                RawMessage(
                    message_id=message_id,
                    chat_id=CHAT,
                    msg_type=msg_type,
                    raw_text=(text or TEMPLATE_TEXT) if msg_type == "text" else "",
                    parse_status="parsed" if msg_type == "text" else "pending",
                    msg_time=when,
                    parent_message_id=parent,
                )
            )
            db.commit()

    def test_image_goes_to_next_template_without_screenshot(self) -> None:
        base = now()
        self._add("om_t1", "text", base)
        self._add("om_shot1", "image", base + timedelta(seconds=5), parent="om_t1")
        self._add("om_t2", "text", base + timedelta(seconds=13))
        self._add("om_shot2", "image", base + timedelta(seconds=9))

        with SessionLocal() as db:
            self.assertEqual(ingest_service.attach_images_to_text(db, CHAT), 1)
            shot2 = db.query(RawMessage).filter_by(message_id="om_shot2").one()
            self.assertEqual(
                shot2.parent_message_id,
                "om_t2",
                "第一张图已配给第一条模板，第二张图必须留给紧随其后的第二条模板（否则丢单）",
            )

    def test_bot_receipt_does_not_take_screenshot(self) -> None:
        """回执里复述了订单号 / 金额 / 收货，很容易被当成模板消息 —— 图挂到回执上等于丢图。"""
        base = now()
        self._add("om_r1", "text", base)
        self._add("om_r2", "text", base + timedelta(seconds=35), text=RECEIPT_TEXT)
        self._add("om_r_shot", "image", base + timedelta(seconds=30))

        with SessionLocal() as db:
            self.assertEqual(ingest_service.attach_images_to_text(db, CHAT), 1)
            shot = db.query(RawMessage).filter_by(message_id="om_r_shot").one()
            self.assertEqual(shot.parent_message_id, "om_r1", "截图不能挂到机器人回执上")

    def test_receipt_text_detected_as_bot(self) -> None:
        from app.services.filter import is_bot_message

        self.assertTrue(is_bot_message("", RECEIPT_TEXT))
        self.assertTrue(is_bot_message("app", "任意文本"))
        self.assertFalse(is_bot_message("user", TEMPLATE_TEXT))

    def test_receipt_children_do_not_block_pairing(self) -> None:
        """回执把 parent 指向模板消息：不能因此认为「这条模板已经有截图了」。"""
        base = now()
        self._add("om_c1", "text", base)
        self._add(
            "om_c1_receipt",
            "text",
            base + timedelta(seconds=3),
            parent="om_c1",
            text=RECEIPT_TEXT,
        )
        self._add("om_c1_shot", "image", base + timedelta(seconds=5))

        with SessionLocal() as db:
            self.assertFalse(ingest_service._has_screenshot(db, "om_c1"))
            self.assertEqual(ingest_service.attach_images_to_text(db, CHAT), 1)
            shot = db.query(RawMessage).filter_by(message_id="om_c1_shot").one()
            self.assertEqual(shot.parent_message_id, "om_c1")


class IndependentResendTest(unittest.TestCase):
    """3：同买家同款两条消息，第二条另带自己的截图 —— 要建两单并标待复核。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            db.query(OrderImage).delete()
            db.query(Order).delete()
            db.query(RawMessage).delete()
            db.commit()

    def _text(self, db, message_id: str, when) -> RawMessage:
        msg = RawMessage(
            message_id=message_id,
            chat_id=CHAT,
            msg_type="text",
            raw_text=TEMPLATE_TEXT,
            parse_status="pending",
            msg_time=when,
            parent_message_id="",
        )
        db.add(msg)
        db.commit()
        return msg

    def test_second_same_text_with_own_shot_is_split(self) -> None:
        base = now()
        with SessionLocal() as db:
            first = self._text(db, "om_re_1", base)
            order1 = ingest_service.save_order(db, first, parse_text(TEMPLATE_TEXT), None)

            second = self._text(db, "om_re_2", base + timedelta(seconds=20))
            # 第二条消息名下有自己独立的截图（图先到、模板后到的情况也走这里）
            db.add(
                RawMessage(
                    message_id="om_re_2_shot",
                    chat_id=CHAT,
                    msg_type="image",
                    raw_text="",
                    parse_status="pending",
                    msg_time=base + timedelta(seconds=18),
                    parent_message_id="om_re_2",
                )
            )
            db.commit()

            order2 = ingest_service.save_order(db, second, parse_text(TEMPLATE_TEXT), None)
            db.refresh(order1)
            db.refresh(order2)

            self.assertNotEqual(
                order1.id, order2.id, "另一条消息 + 自己的截图 = 独立订单，绝不静默并单"
            )
            self.assertEqual(db.query(Order).count(), 2)
            self.assertTrue(order2.needs_review, "内容完全一致的拆单要标「待复核」，让人工确认")
            self.assertIn("疑似重复推送", order2.review_reason or "")


# —— 下面几条来自云端 parse_corrections 表（运营人工纠错记录：机器值 -> 人工改后的值）——
# 机器取错的值不能再出现，人工改的值要能直接解析出来。
ZHU_TEXT = """产品名称：施华洛世奇跳动的心      
收货人:朱先生
手机号码:18864609070
地址信息: 河南省 许昌市 禹州市 颍川街道 锦绣东方小区                                                
备注蓝色
售价：69

成本价：45
所属供应链：BH001
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍"""

# 同一单的截图 OCR：闲鱼页面的营销文案、以及跟这单无关的金额都混在里面
ZHU_OCR = """施华洛世奇跳动的心
成区价 (在支付宝担保账户中)
￥3863.2
朱先生 188****9070
河南省 许昌市 禹州市 颍川街道 锦绣东方小区
实付款 ￥168.00"""

ZHONG_TEXT = """产品名称:N系列网球拍紫色
收货人:钟蕾
手机号码:18616658255
地址信息: 上海 上海市 徐汇区 田林街道 古井路38弄7号1102室
备注:紫色，单球拍
售价：129

成本价：95
所属供应链：BH001
出货闲鱼号：哔哩吧啦
回款支付宝归属人：周玉萍"""

JD_TEXT = """产品名称：360可视门铃6pro
收货人:闲的没事干的鱼帅哥
手机号码:17376596528
地址信息: 浙江省 杭州市 余杭区 五常街道 汇银中心8幢
售价：230

成本价：220
所属供应链：JD0001
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍"""


class HumanCorrectionTest(unittest.TestCase):
    """云端 parse_corrections 里记下的 8 次人工纠错：同一条错不再犯第二次。"""

    def test_address_not_polluted_by_note_without_colon(self) -> None:
        """「备注蓝色」漏了冒号：以前整行被并进地址（……锦绣东方小区 备注蓝色）。"""
        r = parse_text(ZHU_TEXT)
        self.assertEqual(
            r.fields.get("receiver_address"), "河南省 许昌市 禹州市 颍川街道 锦绣东方小区"
        )
        self.assertEqual(r.fields.get("note"), "蓝色")

    def test_ocr_noise_does_not_override_address(self) -> None:
        """截图里的「成区价 (在支付宝担保账户中)」不能顶掉文本里的地址（人工纠过错）。"""
        r = parse_text(ZHU_TEXT, [ZHU_OCR])
        addr = str(r.fields.get("receiver_address") or "")
        self.assertNotIn("成区价", addr)
        self.assertIn("锦绣东方小区", addr)

    def test_money_takes_labeled_value_not_ocr(self) -> None:
        """售价 / 成本价认标签：截图里的 3863.2、168、133 是别的数，不能顶掉 69 / 45。"""
        r = parse_text(ZHU_TEXT, [ZHU_OCR])
        self.assertEqual(r.fields.get("sale_amount"), 69.0)
        self.assertEqual(r.fields.get("cost_amount"), 45.0)

    def test_shipping_fee_absent_stays_empty(self) -> None:
        """模板里没写运费就是没有运费：以前取出了 4，运营还得手工改成 0。"""
        r = parse_text(ZHONG_TEXT)
        self.assertIn(r.fields.get("shipping_fee"), (None, 0, 0.0))

    def test_supplier_code_kept_as_written(self) -> None:
        r = parse_text(JD_TEXT)
        self.assertEqual(r.fields.get("supplier_name"), "JD0001")

    def test_address_keep_full_without_buyer_code(self) -> None:
        """地址不能截断（曾截成「……仓兴街1」），也不能带闲鱼买家编号 [4673]。"""
        r = parse_text(UNLABELED_TEXT)
        addr = str(r.fields.get("receiver_address") or "")
        self.assertTrue(addr.endswith("1单元403"))
        self.assertNotIn("[4673]", addr)

    def test_ocr_sentence_is_not_buyer_nick(self) -> None:
        """「买家已付款，请尽快发货」是句子：行首的「买家」不是「买家昵称」标签。"""
        r = parse_text("", ["买家已付款，请尽快发货"])
        self.assertIsNone(r.fields.get("buyer_nick"))


# 线上两单的真实原文（2026-10-03），运营都是人工改的
PILLOW_TEXT = """产品名称:亚朵四代枕头+枕套
收货人:王慧娟
手机号码:18244792173
地址信息: 广东省 深圳市 宝安区 沙井街道 和一二路15号(满天下网咖这栋)704
备注:枕头加枕套全新未拆封
售价:165

成本价：130

所属供应链：BH001
出货闲鱼号：哔哩吧啦
回款支付宝归属人：周玉萍"""
# 截图里地址那行下面的噪声行（以竖线开头），OCR 把它和上一行地址拼在了一起
PILLOW_OCR = """广东省深圳市宝安区沙井街道和一二路15 BRR
| KRABET, Ata? ¥165.00
订单编号 3317080980081005483 | 复制
"""

TWO_PILLOW_TEXT = """产品名称：亚朵四代枕头➕枕套➕亚朵一代枕头➕枕套
收货人:蒙牛
手机号码:15395408905
地址信息: 安徽省 淮南市 田家庵区 三和镇 一品麓苑11栋504
售价：249
备注不拆开
成本价：130➕80
所属供应链：BH001
出货闲鱼号：卖货的小姑娘
回款支付宝归属人：卫玉珍"""


class OnlineOrderParseTest(unittest.TestCase):
    """2026-10-03 线上两单的解析缺陷：地址带括号丢了、OCR 串行、成本价加法。"""

    def test_10_bracketed_address_kept(self) -> None:
        """地址里的 (满天下网咖这栋) 不能把整条地址搞丢（人工补过一次）。"""
        r = parse_text(PILLOW_TEXT)
        self.assertEqual(
            r.fields.get("receiver_address"),
            "广东省 深圳市 宝安区 沙井街道 和一二路15号(满天下网咖这栋)704",
        )

    def test_20_ocr_does_not_borrow_previous_line(self) -> None:
        """截图里「| 」开头的噪声行不能被跨行拼到上一行：商品名 / 规格都别被污染。"""
        r = parse_text(PILLOW_TEXT, [PILLOW_OCR])
        self.assertEqual(r.fields.get("product_name"), "亚朵四代枕头+枕套")
        spec = r.fields.get("spec")
        self.assertNotIn("KRABET", str(spec or ""), "跨行拼出来的噪声不能当规格")
        self.assertNotIn("BRR", str(r.fields.get("product_name") or ""))

    def test_30_cost_with_plus_is_summed(self) -> None:
        """「成本价：130➕80」是两件货的成本，得加出 210（人工改成 210 过一次）。"""
        r = parse_text(TWO_PILLOW_TEXT)
        self.assertEqual(r.fields.get("cost_amount"), 210.0)

    def test_40_cost_with_ascii_plus_is_summed(self) -> None:
        r = parse_text(TWO_PILLOW_TEXT.replace("130➕80", "130+80"))
        self.assertEqual(r.fields.get("cost_amount"), 210.0)

    def test_50_single_cost_untouched(self) -> None:
        """没有加法时照旧只取那个数（别把 130 变成别的）。"""
        r = parse_text(TWO_PILLOW_TEXT.replace("130➕80", "130"))
        self.assertEqual(r.fields.get("cost_amount"), 130.0)

    def test_60_sale_amount_is_not_summed(self) -> None:
        """只有配了 sum: true 的成本才相加，售价不受影响。"""
        r = parse_text(TWO_PILLOW_TEXT.replace("售价：249", "售价：249+10"))
        self.assertEqual(r.fields.get("sale_amount"), 249.0)


if __name__ == "__main__":
    unittest.main()
