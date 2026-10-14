"""合肥梦想电商商品导入（scripts/import_dreamshop.py）的匹配与整理口径。

这批货的坑都在「配对」上：价格表里的行名常常不带品牌（品牌在上一行分组标题里），
素材那边又爱写「1万」「1w毫安」，而且「1+100w 充电宝」和「1+100w 充电器」只差品类词。
配错的价导入进去比没导更麻烦，所以匹配上有三道硬门槛（大品类 / 品类锚点 / 品牌），
这里逐条守住 —— 宁可漏，不能乱配。

用例只用内存里的假数据，不依赖 /Volumes 上的素材盘（那块盘不在也能跑）。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效


def _load():
    spec = importlib.util.spec_from_file_location(
        "dreamshop_import", ROOT / "scripts" / "import_dreamshop.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMPORT = _load()


def _material(name: str, series: str = "", section: str = "电子", images=None, key: str = ""):
    return {
        "key": key or name,
        "name": name,
        "series": series,
        "section": section,
        "images": images or [f"{name}/01.jpg"],
    }


def _row(name: str, cost: float = 40.0, price: float = 59.0, group: str = "", section: str = "电子", row: int = 1):
    return {
        "row": row,
        "name": name,
        "group": group,
        "cost": cost,
        "cost_text": str(cost),
        "price": price,
        "price_text": str(price),
        "ship_from": "合肥",
        "remark": "",
        "section": section,
    }


class TextNormalizeTest(unittest.TestCase):
    def test_10_normalize_capacity(self):
        """容量写法归一：1万 / 1w毫安 / 10000毫安 都变成 10000。"""
        self.assertIn("10000", IMPORT.normalize("1万毫安"))
        self.assertIn("10000", IMPORT.normalize("1w毫安"))
        self.assertIn("10000", IMPORT.normalize("10000毫安"))
        self.assertIn("20000", IMPORT.normalize("2万毫安"))
        # 全角数字也能认
        self.assertIn("10000", IMPORT.normalize("１００００毫安"))

    def test_15_money_of(self):
        """价格格子里常常夹着颜色、缺货说明。"""
        self.assertEqual(IMPORT.money_of("40（紫色 蓝色 白色）"), 40.0)
        self.assertEqual(IMPORT.money_of("55 左右"), 55.0)
        self.assertEqual(IMPORT.money_of("149 （缺货）"), 149.0)
        self.assertEqual(IMPORT.money_of(""), 0.0)

    def test_20_condition_of(self):
        """成色按闲鱼四档从备注里认。"""
        self.assertEqual(IMPORT.condition_of("全新未拆封"), "new")
        self.assertEqual(IMPORT.condition_of("95-98新"), "like_new")
        self.assertEqual(IMPORT.condition_of("8-9成新"), "light_used")
        self.assertEqual(IMPORT.condition_of("明显使用痕迹"), "used")
        self.assertEqual(IMPORT.condition_of(""), "")

    def test_25_brand_of(self):
        """品牌认原写法（OPPO / 1+ 不该被改成小写）。"""
        self.assertEqual(IMPORT.brand_of("（OPPO 160w）充电器"), "OPPO")
        self.assertEqual(IMPORT.brand_of("小米移动电源3 2万（18w）"), "小米")
        self.assertEqual(IMPORT.brand_of("1+100w充电宝"), "1+")
        self.assertEqual(IMPORT.brand_of("没有品牌的货"), "")

    def test_30_clean_title_only_adds_brand(self):
        """行名缺品牌时只补品牌词，不能把整段分组标题拼上（拼了就是一句废话）。"""
        self.assertEqual(IMPORT.clean_title("1+160w（15-20分钟）", "华强北 充电器 / 安卓 充电器"), "1+160w（15-20分钟）")
        # 行名没品牌时才补（且只补品牌词）
        self.assertEqual(IMPORT.clean_title("160w 充电器", "OPPO 充电器"), "OPPO 160w 充电器")
        # 行名里本来就有品牌 → 原样
        self.assertEqual(IMPORT.clean_title("小米移动电源3 1万", "小米 带线充电宝"), "小米移动电源3 1万")

    def test_35_style_word(self):
        """潮玩款式词：去掉品牌 / 括号 / 尺寸，留下「钻戒」「玫瑰手捧花」这种特异词。"""
        self.assertIn("钻戒", IMPORT.style_word("Jellycat（钻戒和玫瑰手捧花）（合拍）"))
        self.assertIn("吉利猫", IMPORT.style_word("Jellycat（吉利猫 32CM、48CM）（合拍）"))
        self.assertEqual(IMPORT.style_word("Jellycat"), "")


class MatchRuleTest(unittest.TestCase):
    def test_40_category_anchor_blocks(self):
        """「1+100w 充电宝」不能拿「1+100w 充电器」的价 —— 只差品类词，必须否掉。"""
        mat = _material("1+100w充电宝（银色）")
        charger = _row("1+100w（30-40分钟）", group="华强北 充电器")
        power = _row("1+100w12000毫安 充电宝", group="1+100w充电宝")
        self.assertEqual(IMPORT.match_score(mat, charger), 0.0)
        self.assertGreater(IMPORT.match_score(mat, power), 0.45)

    def test_45_brand_must_match(self):
        """华为的移动电源不能拿小米的价。"""
        mat = _material("小米移动电源3 2万（18w）")
        self.assertEqual(IMPORT.match_score(mat, _row("华为移动电源 10000毫安")), 0.0)
        self.assertGreater(IMPORT.match_score(mat, _row("小米移动电源3 20000毫安（18w）")), 0.45)

    def test_50_capacity_and_watt_must_match(self):
        """容量 / 功率对不上就是另一款。"""
        mat = _material("三星 20000毫安 45w 充电宝")
        self.assertEqual(IMPORT.match_score(mat, _row("三星 10000毫安 25w")), 0.0)
        # 功率不同（67w vs 22.5w）而容量相同，也是两款
        mat2 = _material("小米口袋版充电宝10000 67w")
        self.assertEqual(IMPORT.match_score(mat2, _row("小米移动电源3 1万 22.5w")), 0.0)

    def test_55_section_must_match(self):
        """跨大品类不配（户外的睡袋不拿电子的价）。"""
        mat = _material("黑冰睡袋E400", section="户外")
        self.assertEqual(IMPORT.match_score(mat, _row("苹果充电宝", section="电子")), 0.0)

    def test_60_toy_style_word_required(self):
        """潮玩款式名必须对得上（「熊崽」不能配到「钻戒」那行）。"""
        mat = _material("Jellycat（钻戒和玫瑰手捧花）（合拍）", section="潮玩")
        self.assertEqual(IMPORT.match_score(mat, _row("Jellycat 熊崽", section="潮玩")), 0.0)
        ok = _row("Jellycat 钻戒和玫瑰手捧花", section="潮玩")
        ok["name"] = "Jellycat（钻戒和玫瑰手捧花）"
        self.assertGreater(IMPORT.match_score(mat, ok), 0.45)


class BuildEntriesTest(unittest.TestCase):
    def test_70_one_to_one(self):
        """一个素材 / 一条价格行各只用一次：同一款的三组图不能建出三个商品。"""
        materials = [
            _material("oppo 20w 移动电源（充电效果图） 1", key="a"),
            _material("oppo 20w 移动电源（充电效果图） 2", key="b"),
            _material("oppo 20w 移动电源（充电效果图） 3", key="c"),
        ]
        rows = [_row("Oppo20w 10000毫安移动电源", row=1), _row("Oppo20w 10000毫安移动电源 白色", row=2)]
        entries = IMPORT.build_entries(materials, rows, min_score=0.45)
        used_rows = {e["price_row"] for e in entries}
        used_mats = {e["material"] for e in entries}
        self.assertEqual(len(used_rows), len(entries))
        self.assertEqual(len(used_mats), len(entries))
        self.assertLessEqual(len(entries), min(len(materials), len(rows)))

    def test_75_zero_price_rows_skipped(self):
        """没有价格的行不参与匹配（导进去就是 0 元商品）。"""
        materials = [_material("华为100w充电器（银色）", key="a")]
        rows = [_row("华为100w（20-30分钟）", cost=0.0, price=0.0, row=1)]
        self.assertEqual(IMPORT.build_entries(materials, rows, min_score=0.45), [])

    def test_80_bad_names_filtered(self):
        """「全套（图片上的都有）」和长得像说明文的名字不当商品。"""
        materials = [_material("戴森 全套（图片上的都有）", key="a", section="家用电器")]
        rows = [_row("戴森 全套（图片上的都有）", section="家用电器", group="戴森（全套）")]
        self.assertEqual(IMPORT.build_entries(materials, rows, min_score=0.45), [])

        materials2 = [_material("黑冰睡袋", key="b", section="户外")]
        rows2 = [_row("黑冰 E系列：信封式为平底角，木乃伊式包裹性更强，尺寸偏大", section="户外")]
        self.assertEqual(IMPORT.build_entries(materials2, rows2, min_score=0.45), [])

    def test_85_category_of(self):
        """品类 → 系统类目：充电宝落数码，睡袋落运动户外。"""
        self.assertEqual(IMPORT.category_of("电子", "小米移动电源3 20000毫安")[0], "数码、电脑")
        self.assertEqual(IMPORT.category_of("户外", "黑冰睡袋E400")[0], "运动户外")
        self.assertEqual(IMPORT.category_of("生活", "随便一个没规则的商品")[0], "家居日用品")


class CsvRoundTripTest(unittest.TestCase):
    def test_90_csv_can_disable_rows(self):
        """整理表里「是否导入」改成 N 就跳过（人工核对的出口）。"""
        import csv
        import tempfile

        materials = [_material("华为100w充电器", key="dir/华为100w充电器")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as fp:
                writer = csv.writer(fp)
                writer.writerow(["是否导入", "素材目录", "商品名", "系统类目", "拿货价", "建议售价", "成色", "发货地", "产品备注", "状态"])
                writer.writerow(["N", "dir/华为100w充电器", "华为 100w 充电器", "数码、电脑 / 充电器", "35", "49", "", "合肥", "", ""])
                writer.writerow(["Y", "dir/华为100w充电器", "华为 100w 充电器（银色）", "数码、电脑 / 充电器", "38", "55", "new", "合肥", "带包装", ""])
            entries = IMPORT.load_csv_entries(path, materials)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["cost"], 38.0)
        self.assertEqual(entries[0]["price"], 55.0)
        self.assertEqual(entries[0]["category2"], "充电器")
        self.assertEqual(entries[0]["condition"], "new")


if __name__ == "__main__":
    unittest.main()
