"""渔集按商品信息补一轮「型号 / 规格」并同步渔枢 / 渔货栈（2026-10-16 用户要求）。

三条口径：

  1. **只补空缺** —— 已经有型号 / 规格的一律不动（人填的永远优先于猜的）；
  2. **认不出来就留空** —— 猜错的型号比空着更害人（会贴到闲鱼描述里）；
  3. 补出来的要能在渔枢商品档案与渔货栈货源的「型号 / 规格」两行看到
     （``wms.bridge.push_profile_change`` → ``app.services.wms_bridge.sync_profile_from_wms``）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.wms_bridge import sync_profile_from_wms  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import SupplierItem  # noqa: E402
from wms import intake, profile_guess  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

FISH = "YS202610169001"
SUPPLIER = "型号规格测试"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(
            SupplierItem.item_id.like("hub-prd-%")).filter(
            SupplierItem.title.like("型号规格测试%")).delete(synchronize_session=False)
        s.query(Product).filter(Product.code.like("YS202610169%")).delete(
            synchronize_session=False)
        s.query(WmsItem).filter(WmsItem.fish_code.like("YS202610169%")).delete(
            synchronize_session=False)
        s.query(WmsBatch).filter(WmsBatch.supplier_name == SUPPLIER).delete(
            synchronize_session=False)
        s.commit()


class ProfileGuessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()

    def setUp(self):
        _wipe()

    def tearDown(self):
        _wipe()

    def _mk_item(self, fish: str = FISH, **kw) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 2,
                    "supplier_name": SUPPLIER}, operator="tester")
            batch = s.get(WmsBatch, batch.id)
            rows = intake.register(s, batch.id, [{
                "sku_code": f"PG-{fish[-4:]}", "product_name": "型号规格测试款",
                "fish_code": fish, "qty": 1, **kw,
            }], operator="tester")
            return s.get(WmsItem, rows[0].id)

    # ------------------------------------------------------------ 型号识别
    def test_10_guess_model(self):
        """型号 = 商品名里的「字母 + 数字混合串」；认不出来就空着。"""
        cases = [
            ("欧克森X100proUltra擦窗机器人机械臂喷水全自动高层家用", "欧克森（OUKESEN）",
             "X100proUltra"),
            ("北峰对讲机配件BF-HL302耳机 适配机型：SC500UV/TD516", "北峰", "BF-HL302"),
            # 纯营销词不是型号（Pro4L 是「Pro + 容量」，不当型号）
            ("小米米家电饭煲Pro4L家用不粘黑晶内胆快煮电饭锅", "小米米家", ""),
            # 标题里一个字母数字混合串都没有 → 留空，绝不瞎猜
            ("欧克森（OUKESEN）【开工大吉】全自动双向喷水擦窗机器人家用遥控电动",
             "欧克森（OUKESEN）", ""),
        ]
        for name, brand, want in cases:
            self.assertEqual(profile_guess.guess_model(name, brand), want, name)
        # 清单里专门有「型号」那一列时最可信
        self.assertEqual(
            profile_guess.guess_model("破壁机", "美的", {"型号": "MJ-PB40E254D"}),
            "MJ-PB40E254D")

    def test_20_guess_spec(self):
        """规格认容量 / 套装 / 颜色，最多 3 段。"""
        self.assertIn("橄榄绿", profile_guess.guess_spec(
            "小贝熊宝宝恒温辅食碗充电式不锈钢婴幼儿童智能保温加热碗 【橄榄绿】300ml",
            "小贝熊（xiaobeixiong）"))
        self.assertIn("复古绿", profile_guess.guess_spec(
            "苏泊尔（SUPOR）一人食迷你电压力锅开盖火锅家用智能旋钮面板煲汤煮粥复古绿易清洗",
            "苏泊尔（SUPOR）"))
        self.assertEqual(
            profile_guess.guess_spec("破壁机", "美的", extra={"规格": "1.75L"}), "1.75L")

    def test_30_fill_of_only_fills_empty(self):
        """**只补空缺**：已经有型号的一律不动，人填的优先于猜的。"""
        with SessionLocal() as s:
            item = WmsItem(barcode="PGTEST1", product_name="欧克森X100proUltra擦窗机器人",
                           brand="欧克森（OUKESEN）", model="", spec="")
            got = profile_guess.fill_of(item)
            self.assertEqual(got["model"], "X100proUltra")
            item.model = "人工填的型号"
            item.spec = "人工填的规格"
            got = profile_guess.fill_of(item)
            self.assertEqual(got["model"], "", "人填过的不许动")
            self.assertEqual(got["spec"], "", "人填过的不许动")

    # ------------------------------------------------------------ 建档自动补
    def test_40_register_fills_model_spec(self):
        """新货建档：清单里没写型号规格就按商品名认一次。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 1,
                    "supplier_name": SUPPLIER}, operator="tester")
            batch = s.get(WmsBatch, batch.id)
            rows = intake.register(s, batch.id, [{
                "sku_code": "PG-NEW", "product_name": "北峰对讲机配件BF-HL302耳机",
                "brand": "北峰", "fish_code": FISH,
            }], operator="tester")
            row = s.get(WmsItem, rows[0].id)
            self.assertEqual(str(row.model or ""), "BF-HL302", "建档时自动认出型号")
            # 人填过的原样保留
            rows2 = intake.register(s, batch.id, [{
                "sku_code": "PG-NEW2", "product_name": "北峰对讲机配件BF-HL302耳机",
                "brand": "北峰", "model": "手工型号",
            }], operator="tester")
            row2 = s.get(WmsItem, rows2[0].id)
            self.assertEqual(str(row2.model or ""), "手工型号")

    # ------------------------------------------------------------ 历史数据脚本
    def test_50_script_plan_and_apply(self):
        """scripts/wms_model_spec_guess.py：预演不写库，--apply 补齐 + 幂等。"""
        import wms_model_spec_guess as script

        item = self._mk_item()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            row.product_name = "欧克森X100proUltra擦窗机器人机械臂喷水"
            row.brand = "欧克森（OUKESEN）"
            s.commit()

        with SessionLocal() as s:
            rows = script.plan(s)
            ids = {r["item"].id for r in rows}
            self.assertIn(item.id, ids)
            hit = next(r for r in rows if r["item"].id == item.id)
            self.assertEqual(hit["model"], "X100proUltra")
            # 预演不写库
            self.assertEqual(str(s.get(WmsItem, item.id).model or ""), "")
            stat = script.apply_rows(s, rows)

        self.assertGreaterEqual(stat["items"], 1)
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            self.assertEqual(str(row.model or ""), "X100proUltra")
            # 幂等：补完再跑一次没有要补的了
            self.assertNotIn(item.id, {r["item"].id for r in script.plan(s)})

    # ------------------------------------------------------------ 同步渔枢 / 渔货栈
    def test_60_sync_profile_to_hub_and_self(self):
        """补出来的型号 / 规格要写到渔枢商品档案 + 渔货栈货源（只补不盖）。"""
        with SessionLocal() as s:
            p = Product(name="型号规格测试款", code=FISH, stock=2,
                        source_type="self_operated", model="", spec="")
            s.add(p)
            s.commit()
            pid = int(p.id)
            s.add(SupplierItem(item_id=f"hub-prd-{pid}", title="型号规格测试款",
                               cost_price=0, stock=2))
            s.commit()

        report = sync_profile_from_wms(FISH, brand="欧克森", model="X100proUltra",
                                       spec="橄榄绿")
        self.assertTrue(report["updated"], report)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(str(p.model or ""), "X100proUltra")
            self.assertEqual(str(p.spec or ""), "橄榄绿")
            self.assertEqual(str(p.brand or ""), "欧克森")
            src = s.scalar(select(SupplierItem).where(
                SupplierItem.item_id == f"hub-prd-{pid}"))
            self.assertEqual(str(src.model or ""), "X100proUltra")
            self.assertEqual(str(src.spec or ""), "橄榄绿")

        # 渔集是源头：改了就跟过去；**传空的那一项不许把渔枢已有的值清掉**
        sync_profile_from_wms(FISH, model="系统猜的型号", spec="")
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(str(p.model or ""), "系统猜的型号", "渔集改了就跟着改")
            self.assertEqual(str(p.spec or ""), "橄榄绿", "传空的不许清掉已有值")

    def test_65_sync_profile_unknown_code_is_noop(self):
        """渔枢里没有这款商品 → 不报错，返回没同步。"""
        report = sync_profile_from_wms("YS202610169999", model="X1")
        self.assertFalse(report["updated"])
        self.assertTrue(report["notes"])


if __name__ == "__main__":
    unittest.main()
