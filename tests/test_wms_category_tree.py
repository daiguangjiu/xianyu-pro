"""检测仓三级品类树（对标表三级 ↔ 1688 三级）回归。

守的是这几条：
 1. 建档选的是**三级品类**，存储值是「一级.二级.三级」；
 2. 条码只取一级码 —— 品类拆细了，条码规则与历史条码都不变；
 3. 对应关系三级对三级，且没对标的不编数据（标「未对标」）；
 4. 分级规则可挂在任意一层，同优先级下更具体的先匹配（三级 > 一级 > 通用）；
 5. 检测项模板按一级品类回落（电池健康度 2026-10-11 起已下线，不再参与判级）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import grading, inspection, intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    CATEGORY_TREE,
    WmsBatch,
    WmsGradeRule,
    WmsItem,
    category_leaf,
    category_options,
    category_path,
    l1_code_of,
    leaf_codes,
)

ADMIN = {"username": "admin", "password": "xianyu2026"}
# 对标看板里的一条：家用电器 › 厨房小电 › 电饭煲 → 家用电器 › 厨房电器 › 迷你电饭煲（25 单，高置信）
LEAF = "HA.KC.DFB"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.query(WmsGradeRule).delete(synchronize_session=False)
        s.commit()


class WmsCategoryTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)

    # ------------------------------------------------------------ 树
    def test_10_tree_is_three_level(self):
        """品类树是三级的：每个一级底下有二级，二级底下有三级叶子。"""
        self.assertGreaterEqual(len(leaf_codes()), 50, "三级叶子太少，对标数据没进来")
        for code in (LEAF, "PH.DJ.MN", "ZJ.SZ.GZ", "CB.JR.DCL"):
            self.assertIn(code, leaf_codes())
            self.assertEqual(len(code.split(".")), 3, code)
        for l1, node in CATEGORY_TREE.items():
            kids = node.get("children") or {}
            self.assertTrue(kids, f"{l1} 没有二级")
            for l2, sub in kids.items():
                self.assertTrue(sub.get("children"), f"{l1}.{l2} 没有三级")

    def test_20_path_and_mapping(self):
        """三级对三级：路径是收货侧，map 是 1688 侧；没对标的不编数据。"""
        self.assertEqual(category_path(LEAF), "家用电器 › 厨房小电 › 电饭煲")
        leaf = category_leaf(LEAF) or {}
        self.assertEqual(leaf.get("map"), "家用电器 › 厨房电器 › 迷你电饭煲")
        self.assertEqual(leaf.get("tickets"), 25)
        self.assertEqual(leaf.get("conf"), "高")
        # 没拿到工单数据的品类：只有中文名，没有 map
        other = category_leaf("WJ.QT.QT") or {}
        self.assertEqual(other.get("label"), "其它五金/工具")
        self.assertFalse(other.get("map"))
        # 选项下拉给的是完整路径
        self.assertIn([LEAF, "家用电器 › 厨房小电 › 电饭煲"], category_options())

    # ------------------------------------------------------------ 条码
    def test_30_barcode_only_uses_l1(self):
        """条码只取一级码：三级码 HA.KC.DFB 与老码 HA 出来的前缀一样。"""
        self.assertEqual(l1_code_of(LEAF), "HA")
        self.assertEqual(intake.barcode_of(LEAF, "2610", 7)[:4], "QCHA")
        self.assertEqual(intake.barcode_of("HA", "2610", 7)[:4], "QCHA")
        self.assertEqual(len(intake.barcode_of(LEAF, "2610", 7)), 12)
        # 老的两位品类码（手机）规则不变
        self.assertEqual(intake.barcode_of("PH", "2610", 12), "QCPH26100012")

    # ------------------------------------------------------------ 落库
    def test_40_batch_keeps_three_level_category(self):
        """批次建档按三级存：列表显示完整路径，货品继承批次品类。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": LEAF, "source_type": "recycle",
                    "qty_expected": 2, "supplier_name": "测试回收"},
                operator="tester",
            )
            intake.register(s, batch.id, [
                {"brand": "Midea", "model": "FB-30", "spec": "3L",
                 "serial_no": "SN0001", "purchase_cost": 100.0, "base_price": 200.0}
            ], operator="tester")
            row = s.get(WmsBatch, batch.id)
            self.assertEqual(row.category, LEAF)
            item = s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).first()
            self.assertEqual(item.category, LEAF)
            self.assertTrue(item.barcode.startswith("QCHA"), item.barcode)

        page = self.client.get("/wms/batches").text
        # 2026-10-08：建批次不再选品类（品类按导入清单每行定）→ 列表打「按清单行定」标，
        # 品类本身仍按三级存在货品上（脚本 / 老数据建批次时仍可显式传 category）
        self.assertIn("按清单行定", page)

    # ------------------------------------------------------------ 规则层级
    def test_50_rule_matches_by_level(self):
        """规则可挂在任意一层；同优先级下更具体的先匹配。"""
        with SessionLocal() as s:
            s.add(WmsGradeRule(category="HA", priority=50, grade="B", title="家电一级规则",
                               conditions={"all_of": [{"field": "must_official", "op": "==", "value": True}]}))
            s.add(WmsGradeRule(category=LEAF, priority=50, grade="A", title="电饭煲专属",
                               conditions={"all_of": [{"field": "must_official", "op": "==", "value": True}]}))
            s.commit()
            rules = grading.load_rules(s, LEAF)
            mine = [r for r in rules if r.title in ("家电一级规则", "电饭煲专属")]
            self.assertEqual([r.title for r in mine], ["电饭煲专属", "家电一级规则"])
            # 同是家电、但不是电饭煲 → 只吃一级规则
            others = [r.title for r in grading.load_rules(s, "HA.JD.XCQ")
                      if r.title in ("家电一级规则", "电饭煲专属")]
            self.assertEqual(others, ["家电一级规则"])
            # 通用规则对任何品类都生效
            self.assertTrue(any(r.category == "*" for r in grading.load_rules(s, LEAF)))
            # 别的一级品类不吃家电的规则
            self.assertNotIn("家电一级规则",
                             [r.title for r in grading.load_rules(s, "PH.DJ.MN")])

    # ------------------------------------------------------------ 检测项
    def test_60_template_follows_l1(self):
        """三级品类回落到一级的检测模板（厨房小电 → 家用电器那套）。"""
        self.assertEqual(inspection.template_of(LEAF), inspection.template_of("HA"))
        # 电池健康度已整项下线（2026-10-11）：不再有「有没有电池」这套判定
        self.assertFalse(hasattr(inspection, "has_battery"))

    # ------------------------------------------------------------ 页面
    def test_70_pages_render_picker(self):
        """规则页有三级选择器；批次页按新口径不再选品类（品类按导入清单每行定）。"""
        batches = self.client.get("/wms/batches").text
        self.assertNotIn('id="bcat-l1"', batches, "建批次不问品类，页面不该再有三级选择器")
        rules = self.client.get("/wms/rules").text
        self.assertIn('id="rcat-l1"', rules)
        self.assertIn("通用（全部品类）", rules)
        # 对标卡是三级对三级：收货侧与 1688 侧都在
        self.assertIn("家用电器 › 厨房小电 › 电饭煲", rules)
        self.assertIn("家用电器 › 厨房电器 › 迷你电饭煲", rules)
        self.assertIn("未对标", rules)
