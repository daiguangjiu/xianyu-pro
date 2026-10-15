"""货品检测入库出库管理子系统（WMS，页面 /wms）回归。

覆盖的是「钱 / 货会出错」的那几条红线，不是页面长什么样：
 1. 一码到底：条码服务端按号段发，不重号；等级是字段，改档不换码；
 2. 分级引擎：安全红线第一匹配；不合格项没填描述不允许提交；覆盖引擎必须填理由；
 3. 库存账本：只认流水（没有改数量的入口）、幂等、负库存能查出来；
 4. 出库：PENDING / REWORK / 危废禁止出库，A 档不能当 S 档发；
 5. 贴标复核：等级不符阻断入库；
 6. 报修：必填校验、超时标记、修回必须重新检测定级；
 7. 拆机件：D 档无残值明细不允许关工单；
 8. 盘点：未扫到不算盘亏，差异超阈值升级主管，审批后才生成调整流水；
 9. 页面：各页 200、左上角主名「渔集」、检测工作台有扫码枪检测器。
"""
from __future__ import annotations

import pathlib

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import grading, importer, inspection, intake, labeling, ledger, outbound, parts, repairs  # noqa: E402
from wms import nav as wms_nav  # noqa: E402
from wms import audit as wms_audit  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms import state  # noqa: E402
from wms import stocktake  # noqa: E402
from wms import warehouses as wms_warehouses  # noqa: E402
from wms.config import config as wms_config  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    BUILTIN_WAREHOUSE_CODES,
    GRADE_A,
    GRADE_C,
    GRADE_CHOICES,
    GRADE_D,
    GRADE_N,
    GRADE_ROUTE,
    GRADE_S,
    SAFETY_ITEM_KEYS,
    SELLABLE_GRADES,
    STATUS_GRADED,
    STATUS_PENDING,
    STATUS_SELLABLE,
    TEST_FAIL,
    TEST_PASS,
    WmsBatch,
    WmsGradeRule,
    WmsItem,
    WmsLabelPrint,
    WmsOutboundItem,
    WmsOutboundOrder,
    WmsPriceRule,
    WmsPartsItem,
    WmsRepairOrder,
    WmsStockBalance,
    WmsStockLedger,
    WmsStocktake,
    WmsStocktakeLine,
    WmsTestItem,
    WmsTestRecord,
    WmsWarehouse,
    warehouse_label,
)

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 统一检测项（2026-10-13 起不分品类，一套 10 项）全过；安全红线 3 项另判。
# 名字还是 _PHONE_RESULTS（老用例都在用它），内容已经换成统一模板那套 key。
_PHONE_RESULTS = {
    "appear": TEST_PASS, "inner": TEST_PASS, "power": TEST_PASS, "panel": TEST_PASS,
    "touch": TEST_PASS, "charge": TEST_PASS, "camera": TEST_PASS, "wifi": TEST_PASS,
    "function": TEST_PASS, "accessory": TEST_PASS,
}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsStocktakeLine).delete(synchronize_session=False)
        s.query(WmsStocktake).delete(synchronize_session=False)
        s.query(WmsPartsItem).delete(synchronize_session=False)
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsOutboundItem).delete(synchronize_session=False)
        s.query(WmsOutboundOrder).delete(synchronize_session=False)
        s.query(WmsRepairOrder).delete(synchronize_session=False)
        s.query(WmsTestItem).delete(synchronize_session=False)
        s.query(WmsTestRecord).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        # 仓库 / 货架：内置 7 仓留着（分级路由按它入仓），测试自己加的行清掉
        s.query(WmsWarehouse).filter(
            (WmsWarehouse.level > 1)
            | WmsWarehouse.code.notin_(sorted(BUILTIN_WAREHOUSE_CODES))
        ).delete(synchronize_session=False)
        s.commit()


class WmsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        # 检测仓的表由它自己建（自己的 Base），平台的 init_db 不再代劳
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()

    # ------------------------------------------------------------ 建档
    def _batch(self, qty: int = 5, base_price: float = 3000.0) -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"category": "PH", "source_type": "recycle", "qty_expected": qty,
                 "supplier_name": "测试回收"},
                operator="tester",
            )
            rows = [
                {"brand": "Apple", "model": "iPhone 13", "spec": "128G",
                 "serial_no": f"SN{i:04d}", "purchase_cost": 2000.0,
                 "base_price": base_price}
                for i in range(qty)
            ]
            intake.register(s, batch.id, rows, operator="tester")
            return s.get(WmsBatch, batch.id)

    def test_10_batch_and_barcode(self):
        """批次领号段发码：一机一码、条码唯一、等级初始 W、状态初始待检。"""
        batch = self._batch(3)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            self.assertEqual(len(items), 3)
            codes = [it.barcode for it in items]
            self.assertEqual(len(set(codes)), 3, "条码不能重复")
            for it in items:
                self.assertTrue(it.barcode.startswith("QCPH"), it.barcode)
                self.assertEqual(it.grade, "W")
                self.assertEqual(it.status, STATUS_PENDING)
                self.assertTrue(it.display_code.startswith("QC-PH-"))
            # 号段被领用：下一次从 4 开始
            self.assertEqual(s.get(WmsBatch, batch.id).serial_next, 4)

    def test_15_bulk_import_rejects_dup(self):
        """批量导入：空号 / 重复号必须报出行号，不能静默导入。"""
        with SessionLocal() as s:
            batch = intake.create_batch(s, {"category": "PH", "qty_expected": 3},
                                        operator="t")
            with self.assertRaises(ValueError) as ctx:
                intake.register_bulk(s, batch.id, brand="Apple", model="iPhone",
                                     serials=["A1", "A1"], operator="t")
            self.assertIn("第 2 行", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                intake.register_bulk(s, batch.id, brand="Apple", model="iPhone",
                                     serials=["A1", "  "], operator="t")
            self.assertIn("第 2 行", str(ctx.exception))

    def test_16_barcode_seq_is_per_category(self):
        """条码流水按「一级品类 + 年月」全局排，**不按批次各从 0001 起**。

        2026-10-08 的坑：批次不再定品类（品类按导入表格每行定），流水若还按批次算，
        两个批次的同一品类在同一个月必然撞码 —— 导入时报「条码重复」整批退回去。
        """
        with SessionLocal() as s:
            first = intake.create_batch(s, {"qty_expected": 2}, operator="t")
            intake.register(s, first.id, [{"category": "HA.KC.DFB", "sku_code": "X-1"}],
                            operator="t")
            second = intake.create_batch(s, {"qty_expected": 2}, operator="t")
            intake.register(s, second.id, [{"category": "HA.KC.DFB", "sku_code": "X-2"}],
                            operator="t")
            codes = sorted(i.barcode for i in intake.list_items(s))
            self.assertEqual(len(set(codes)), 2, "两个批次的同一品类不能撞码")
            self.assertTrue(all(c.startswith("QCHA") for c in codes), codes)
            self.assertEqual(int(codes[0][-4:]) + 1, int(codes[1][-4:]),
                             "同一品类的流水要接着往下排")

    def test_17_serial_span_grows(self):
        """号段不再是拦路虎：清单一来几百行，号段按需撑开（只记「这批收了多少台」）。"""
        with SessionLocal() as s:
            batch = intake.create_batch(s, {"qty_expected": 1}, operator="t")
            batch.serial_end = 1
            s.commit()
            rows = [{"category": "PH.DJ.QT", "sku_code": f"Y-{i}"} for i in range(3)]
            intake.register(s, batch.id, rows, operator="t")
            got = s.get(WmsBatch, batch.id)
            self.assertEqual(got.serial_next, 4)
            self.assertGreaterEqual(got.serial_end, 3, "号段要撑到够用")
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 3)

    # ------------------------------------------------------------ 分级引擎
    def test_20_grading_s_and_a(self):
        """引擎按库里的规则判档，并返回可读依据。"""
        batch = self._batch(2)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            facts = {"battery_health": 92, "all_items_pass": True, "fail_count": 0,
                     "flaw_count": 0, "safety_hit": False}
            out = grading.evaluate(s, "PH", facts)
            self.assertEqual(out["suggest_grade"], GRADE_S)
            self.assertTrue(out["reasons"], "必须返回依据")
            facts["battery_health"] = 82
            facts["flaw_count"] = 2
            out = grading.evaluate(s, "PH", facts)
            self.assertEqual(out["suggest_grade"], GRADE_A)

    def test_25_safety_first(self):
        """安全红线永远第一匹配：命中即 C 报修 + safety=True。

        2026-10-13 用户拍板：红线先修不先废 —— 只有勾了「缺件严重 / 无维修价值」
        才判 D 报废（红线规则里排除了它，让 D 规则接得住）。
        """
        with SessionLocal() as s:
            out = grading.evaluate(s, "PH", {"safety_hit": True, "all_items_pass": True,
                                             "battery_health": 99, "flaw_count": 0})
            self.assertEqual(out["suggest_grade"], GRADE_C)
            self.assertTrue(out["safety"])
            # 勾了「缺件严重」→ 该报废的照旧报废，不被红线抢走
            out = grading.evaluate(s, "PH", {"safety_hit": True, "all_items_pass": True,
                                             "missing_parts": True, "flaw_count": 0})
            self.assertEqual(out["suggest_grade"], GRADE_D)

    def test_30_price_rule(self):
        """定价：系数 + 毛利告警 + 低于底价需审批。"""
        with SessionLocal() as s:
            out = grading.price_of(s, GRADE_S, 3000.0, 2000.0)
            self.assertEqual(out["price"], 3000.0)
            self.assertFalse(out["need_approval"])
            out = grading.price_of(s, GRADE_S, 3000.0, 2000.0, manual_price=2100.0)
            self.assertTrue(out["need_approval"], "低于底价 ¥2550 要审批")
            self.assertAlmostEqual(out["floor"], 2550.0, places=2)
            self.assertTrue(out["low_margin"], "毛利 4.8% 低于 15% 下限要告警")

    # ------------------------------------------------------------ 检测提交
    def test_35_submit_requires_fail_desc(self):
        """不合格项没填描述不允许提交。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            results = dict(_PHONE_RESULTS)
            results["panel"] = TEST_FAIL
            with self.assertRaises(ValueError) as ctx:
                inspection.submit(s, item, tester="t", results=results)
            self.assertIn("不合格项", str(ctx.exception))

    def test_40_submit_grades_and_writes_ledger(self):
        """提交检测：定级 + 出价 + 写流水；改档不换码。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            barcode = item.barcode
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, flaw_desc="")
            self.assertEqual(item.grade, GRADE_S)
            # 2026-10-13 起定级即按等级路由入仓：S 档直接进可售仓「可售」
            self.assertEqual(item.status, STATUS_SELLABLE)
            self.assertEqual(item.warehouse_code, "SALE")
            self.assertEqual(item.barcode, barcode, "改档不换码")
            self.assertEqual(item.selling_price, 3000.0)
            rows = ledger.item_ledgers(s, item.id)
            self.assertTrue(any(r.biz_type == "INBOUND" for r in rows))
            self.assertTrue(any(r.biz_type == "GRADE" for r in rows))
            # 检测记录留痕（只追加）
            records = inspection.records_of(s, item.id)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].final_grade, GRADE_S)

    def test_45_override_needs_reason(self):
        """覆盖引擎建议必须填理由（这是规则调优的唯一依据）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            with self.assertRaises(ValueError) as ctx:
                inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                                  battery_health=90, final_grade="B")
            self.assertIn("覆盖引擎建议", str(ctx.exception))
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, final_grade="B",
                              override_reason="外观太差")
            self.assertEqual(item.grade, "B")
            self.assertEqual(inspection.records_of(s, item.id)[0].override_reason,
                             "外观太差")

    def test_50_safety_goes_repair(self):
        """命中安全红线：判 C 报修 + 进报修仓（不再直接转危废）。

        2026-10-13 用户拍板：先走报修，除非完全不能用 / 缺件严重才报废。
        """
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            results = dict(_PHONE_RESULTS)
            results["swell"] = TEST_FAIL
            inspection.submit(s, item, tester="t", results=results,
                              fail_descs={"swell": "电池鼓包 2mm"},
                              battery_health=70)
            self.assertEqual(item.grade, GRADE_C)
            self.assertEqual(item.status, "OUT_REPAIR")
            self.assertEqual(item.warehouse_code, "OUTR", "进报修仓，不是危废区")
            self.assertTrue(item.safety_hit, "红线标记照旧留着（禁止充电）")
            ok, _why = state.can_sell(item.grade, item.status)
            self.assertFalse(ok, "报修中的货不能卖")
            # 也不许被手工搬进可售仓
            ok2, why2 = state.can_enter_sale(item.grade)
            self.assertFalse(ok2)
            self.assertIn("不能进可售仓", why2)

    def test_55_uncertain_goes_recheck_pool(self):
        """判「?」存疑：进复检池，状态不动（不隔夜）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              uncertain=True, note="屏幕偶发闪屏")
            self.assertTrue(item.need_recheck)
            self.assertEqual(item.status, STATUS_PENDING)
            self.assertTrue(inspection.recheck_pool(s))

    # ------------------------------------------------------------ 状态机
    def test_60_transition_whitelist(self):
        """跳步被挡：PENDING 不能直接出库 / 直接已售。"""
        self.assertTrue(state.can_transition("PENDING", "GRADED"))
        self.assertFalse(state.can_transition("PENDING", "SELLABLE"))
        self.assertFalse(state.can_transition("PENDING", "SOLD"))
        self.assertFalse(state.can_transition("SOLD", "SELLABLE"), "终态不可逆")
        self.assertIn("不允许", state.transition_error("PENDING", "SOLD"))

    def test_65_outbound_status_guard(self):
        """待检 / 返工 / 危废 / 召回 / 已售禁止出库。"""
        for status in ("PENDING", "REWORK", "HAZARD", "RECALL", "SOLD"):
            ok, _why = state.can_outbound(status)
            self.assertFalse(ok, status)
        ok, _why = state.can_outbound("SELLABLE")
        self.assertTrue(ok)

    # ------------------------------------------------------------ 标签
    def test_70_label_and_verify(self):
        """贴标复核三项校验：等级不符阻断入库，对了才进可售仓。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90)
            out = labeling.verify(s, item.barcode, "A", operator="t")
            self.assertFalse(out["ok"])
            self.assertIn("等级不符", out["message"])
            # 2026-10-13 起定级即入可售仓；复核阻断拦的是「贴错标签的货过闸」，
            # 状态与仓保持定级时落定的样子
            self.assertEqual(item.status, STATUS_SELLABLE)
            self.assertEqual(item.warehouse_code, "SALE")
            out = labeling.verify(s, item.barcode, "S", operator="t")
            self.assertTrue(out["ok"])
            self.assertEqual(item.status, STATUS_SELLABLE)
            self.assertEqual(item.warehouse_code, "SALE")
            out = labeling.verify(s, "QCXX99000001", "S")
            self.assertFalse(out["ok"])
            self.assertIn("未知条码", out["message"])

    def test_75_reprint_needs_reason(self):
        """补打必须填原因；打印走队列且幂等。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            with self.assertRaises(ValueError):
                labeling.enqueue(s, [item], operator="t", label_type="reprint")
            first = labeling.enqueue(s, [item], operator="t", label_type="formal")
            again = labeling.enqueue(s, [item], operator="t", label_type="formal")
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].id, again[0].id, "重复点不会多打")
            self.assertIn("BARCODE", first[0].command)
            self.assertTrue(labeling.build_zpl(labeling.label_data(item)).startswith("^XA"))

    # ------------------------------------------------------------ 账本
    def test_80_ledger_idempotent(self):
        """库存只认流水，重复提交只记一条（幂等键）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90)
            labeling.verify(s, item.barcode, "S", operator="t")
            before = len(ledger.item_ledgers(s, item.id))
            ledger.apply(s, item, biz_type="LOCK", biz_id="TEST-1",
                         to_status="LOCKED", operator="t",
                         idempotent_key="LOCK:TEST-1:%s" % item.id)
            ledger.apply(s, item, biz_type="LOCK", biz_id="TEST-1",
                         to_status="LOCKED", operator="t",
                         idempotent_key="LOCK:TEST-1:%s" % item.id)
            s.commit()
            self.assertEqual(len(ledger.item_ledgers(s, item.id)), before + 1)
            self.assertEqual(len(ledger.negative_balances(s)), 0)

    # ------------------------------------------------------------ 出库
    def test_85_sale_outbound_grade_guard(self):
        """销售出库：B 档不能卖；A 档不能当 S 档发。"""
        batch = self._batch(2)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            # 第一台判 S
            inspection.submit(s, items[0], tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90)
            labeling.verify(s, items[0].barcode, "S", operator="t")
            # 第二台判 A
            results = dict(_PHONE_RESULTS)
            inspection.submit(s, items[1], tester="t", results=results,
                              battery_health=82, flaw_desc="边框磕碰 1 处")
            labeling.verify(s, items[1].barcode, "A", operator="t")

            order = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                        "target_no": "SO-1"}, operator="t")
            # 没锁货不能直接拣
            with self.assertRaises(ValueError) as ctx:
                outbound.pick(s, order, items[0].barcode, require_grade="S", operator="t")
            self.assertIn("锁定", str(ctx.exception))
            outbound.lock(s, order, items[0], operator="t")
            # 等级不符：要 S 实扫 A（先把 A 那台也锁上，否则先撞上「没锁定」）
            outbound.lock(s, order, items[1], operator="t")
            with self.assertRaises(ValueError) as ctx:
                outbound.pick(s, order, items[1].barcode, require_grade="S", operator="t")
            self.assertIn("等级不符", str(ctx.exception))
            # 撤销第二台的占用，别影响后面核销
            outbound.release(s, items[1], operator="t")
            outbound.pick(s, order, items[0].barcode, require_grade="S", operator="t")
            # 销售出库必须填运单号
            with self.assertRaises(ValueError):
                outbound.ship(s, order.id, operator="t")
            outbound.ship(s, order.id, operator="t", waybill_no="SF123456")
            self.assertEqual(items[0].status, "SOLD")
            ok, _ = state.can_outbound("SOLD")
            self.assertFalse(ok)

    def test_90_pending_cannot_out(self):
        """待检货禁止出库（最常见的发错货）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            order = outbound.create(s, {"type": "SALE", "warehouse_code": "PEND"},
                                    operator="t")
            with self.assertRaises(ValueError) as ctx:
                outbound.pick(s, order, item.barcode, operator="t")
            self.assertTrue("待检" in str(ctx.exception) or "锁定" in str(ctx.exception))

    # ------------------------------------------------------------ 报修
    def test_95_repair_flow(self):
        """报修：必填校验 → 送出 → 超时标记 → 寄回 → 确认再入库强制重新检测定级。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            item.serial_no = "IMEI-123"
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=60, final_grade="C",
                              override_reason="主板故障需官方修")
            # 通道固定三选（2026-10-15）：乱通道直接拒
            with self.assertRaises(ValueError) as ctx:
                repairs.create(s, item, {"channel": "XX", "vendor": "京东服务",
                                         "fault_desc": "不开机"})
            self.assertIn("通道", str(ctx.exception))
            row = repairs.create(s, item, {"channel": "BS",
                                           "vendor": "京东服务",
                                           "ticket_no": "JD-1", "fault_desc": "不开机",
                                           "promise_days": 15,
                                           "repair_fee": 600, "express_fee": 200},
                                 operator="t")
            self.assertEqual(row.paid_amount, 800, "垫付合计 = 维修费 + 快递费")
            repairs.send(s, row.id, waybill_no="SF-1", operator="t")
            self.assertEqual(item.status, "OUT_REPAIR")
            # 超时：把承诺日期改到过去再扫
            s.refresh(row)
            row.promise_date = row.promise_date.replace(year=row.promise_date.year - 1)
            s.commit()
            self.assertGreaterEqual(repairs.mark_timeouts(s), 1)
            self.assertTrue(repairs.timeouts(s))
            # 寄回：只记单号，不搬账（货还在在途回程）
            repairs.return_back(s, row.id, operator="t", waybill_no="SF-2")
            self.assertEqual(row.status, "returned")
            self.assertEqual(item.status, "OUT_REPAIR", "寄回在途不搬账")
            # 确认再入库：货真回仓才搬账 + 强制重检
            repairs.restock(s, row.id, operator="t")
            self.assertEqual(item.status, "REWORK", "确认再入库必须重新检测定级")
            self.assertTrue(item.need_recheck)

    def test_100_repair_cancel_needs_reason(self):
        """撤单必须填原因（主管审批）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=60, final_grade="C",
                              override_reason="主板故障")
            row = repairs.create(s, item, {"repair_type": "inhouse", "vendor": "授权网点",
                                           "contact_phone": "13800000000",
                                           "fault_desc": "不开机"}, operator="t")
            with self.assertRaises(ValueError) as ctx:
                repairs.cancel(s, row.id, reason="", operator="t")
            self.assertIn("原因", str(ctx.exception))
            repairs.cancel(s, row.id, reason="官方拒修", operator="t")
            self.assertEqual(row.status, "cancelled")

    # ------------------------------------------------------------ 拆机件
    def test_105_parts_require_residual(self):
        """D 档无残值明细不允许关工单。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=60, final_grade="D",
                              override_reason="整机报废")
            with self.assertRaises(ValueError) as ctx:
                parts.close(s, item, operator="t")
            self.assertIn("残值明细", str(ctx.exception))
            parts.add(s, item, part_type="mainboard", est_value=600, operator="t")
            parts.add(s, item, part_type="battery", est_value=80, operator="t")
            value = parts.close(s, item, operator="t")
            self.assertAlmostEqual(value, (600 + 80) * 0.8, places=2)
            self.assertGreater(parts.recovery_rate(s)["est"], 0)

    # ------------------------------------------------------------ 盘点
    def test_110_stocktake(self):
        """盘点：未扫到不算盘亏（只列出来），审批后才生成调整流水。"""
        batch = self._batch(2)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            take = stocktake.create(s, {"scope": "batch", "batch_id": batch.id},
                                    operator="t")
            self.assertEqual(take.plan_qty, 2)
            stocktake.scan(s, take.id, items[0].barcode, operator="t")
            with self.assertRaises(ValueError):
                stocktake.scan(s, take.id, items[0].barcode, operator="t")
            take = stocktake.finish(s, take.id, operator="t")
            self.assertEqual(take.actual_qty, 1)
            self.assertEqual(take.diff_qty, -1)
            # 差异率 50% > 0.5%：升级主管，审批必须填说明
            self.assertTrue(take.need_upgrade)
            with self.assertRaises(ValueError) as ctx:
                stocktake.approve(s, take.id, approver="boss", note="")
            self.assertIn("核查说明", str(ctx.exception))
            before = len(ledger.item_ledgers(s, items[1].id))
            stocktake.approve(s, take.id, approver="boss", note="已确认丢失")
            self.assertGreater(len(ledger.item_ledgers(s, items[1].id)), before)
            self.assertEqual(take.status, "approved")

    # ------------------------------------------------------------ 页面
    def test_115_pages_ok(self):
        """页面都能打开，而且**用的是检测仓自己的壳**（不是平台侧边栏）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90)
            item_id, batch_id = item.id, batch.id
        for url in ("/wms", "/wms/batches", f"/wms/batches/{batch_id}", "/wms/items",
                    f"/wms/items/{item_id}", "/wms/inspect", "/wms/labels",
                    f"/wms/batches/{batch_id}/register", f"/wms/items/{item_id}/edit",
                    "/wms/outbound", "/wms/repairs", "/wms/parts", "/wms/stocktake",
                    "/wms/rules", "/wms/reports", "/wms/ledger", "/wms/members",
                    "/wms/warehouses", "/wms/changelog", "/wms/fields"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertIn(wms_config.system_name, resp.text, url)
            # 自己的壳（自有侧栏 + 「切换子系统」面板），不是平台那套布局
            self.assertIn('class="side"', resp.text, url)
            self.assertIn("切换子系统", resp.text, url)
            self.assertIn("渔枢系统", resp.text, url)
            self.assertIn("渔货栈", resp.text, url)
            self.assertNotIn('class="app-sidebar"', resp.text, url)

    def test_116_hub_menu_has_no_wms(self):
        """检测仓不再占平台侧边栏：2026-10-04 起它是并列的平台子系统，走左上角子系统切换。"""
        html = self.client.get("/").text
        for keyword in ("扫码检测", "标签打印复核", "拆机件残值", "盘点与差异", "库存账本"):
            self.assertNotIn(keyword, html, f"平台侧边栏不该出现「{keyword}」")
        # 检测仓自己的侧边栏里才有这些入口
        wms_html = self.client.get("/wms").text
        self.assertIn("扫码检测", wms_html)
        self.assertIn("库存账本", wms_html)

        from app.web.nav import NAV

        def _urls(items):
            for it in items:
                if it.url:
                    yield it.url
                yield from _urls(it.children)

        nav_urls = list(_urls([it for sec in NAV for it in sec.items]))
        self.assertEqual([u for u in nav_urls if u.startswith("/wms")], [],
                         "平台菜单里一个 /wms 入口都不留（入口在子系统切换面板里）")

        # 入口改到子系统切换面板：/wms 是三个平级子系统之一
        from app.portals import PORTAL_WMS, switch_options
        from app.models import User

        with SessionLocal() as s:
            admin = s.scalar(select(User).where(User.username == "admin"))
        options = switch_options(admin, "/wms")
        self.assertIn(PORTAL_WMS, [o["key"] for o in options])
        self.assertTrue(next(o for o in options if o["key"] == PORTAL_WMS)["active"])

    def test_117_permission_is_own(self):
        """平台的子账号不等于检测仓权限：没分配角色 → 页面被守卫拦下。"""
        from app.models import User
        from app.web.auth import hash_password

        with SessionLocal() as s:
            user = User(username="tom", password_hash=hash_password("pwd12345"),
                        display_name="汤姆", role="user", status="active")
            s.add(user)
            s.commit()
            uid = int(user.id)
        client = TestClient(app)
        resp = client.post("/login", data={"username": "tom", "password": "pwd12345"},
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

        # 没角色 → 一律打到 /wms/denied（403 白页是不给的）
        resp = client.get("/wms/inspect", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/wms/denied", resp.headers.get("location", ""))
        self.assertIn("没有给你这个权限", client.get("/wms/denied").text)

        # 给了「检测员」角色就能进检测，但仍然动不了批次的建档（POST 被拦）
        with SessionLocal() as s:
            inspector = [r for r in wms_permissions.roles(s) if r.code == "inspector"][0]
            inspector_id = int(inspector.id)
        with SessionLocal() as s:
            wms_permissions.grant(s, user_id=uid, username="tom", display_name="汤姆",
                                  role_ids=[inspector_id], actor="admin")
        self.assertEqual(client.get("/wms/inspect").status_code, 200)
        resp = client.post("/wms/batches", data={"category": "PH", "source_type": "recycle",
                                                 "qty_expected": 1}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/wms/denied", resp.headers.get("location", ""))

        # 换成「只读」角色：写接口（扫码提交检测结果）走 JSON 403，不跳页面
        with SessionLocal() as s:
            viewer = [r for r in wms_permissions.roles(s) if r.code == "viewer"][0]
            viewer_id = int(viewer.id)
            wms_permissions.grant(s, user_id=uid, username="tom", display_name="汤姆",
                                  role_ids=[viewer_id], actor="admin")
        resp = client.post("/wms/api/inspect/preview", json={}, follow_redirects=False)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("没有", resp.json().get("detail", ""))

        # 平台账号本身完好无损（移出检测仓只是收权，不动账号）
        with SessionLocal() as s:
            wms_permissions.revoke(s, uid)
            self.assertFalse(
                list(s.scalars(select(User).where(User.username == "tom")).all()) == []
            )
            s.query(User).filter(User.username == "tom").delete(synchronize_session=False)
            s.commit()

    def test_120_inspect_api(self):
        """扫码接口：载入档案 + 检测项按品类渲染 + 预览 + 提交。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            barcode = intake.list_items(s, batch_id=batch.id)[0].barcode
        resp = self.client.get("/wms/api/item.json", params={"code": barcode})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["template"]), 11)
        self.assertIn("swell", data["safety_keys"])

        resp = self.client.get("/wms/api/item.json", params={"code": "NOT-EXIST"})
        self.assertEqual(resp.status_code, 404)

        page = self.client.get("/wms/inspect").text
        self.assertIn("ScannerDetector", page, "检测工作台要有扫码枪检测器")
        self.assertIn("minLength", page)

        item_id = self.client.get("/wms/api/item.json", params={"code": barcode}).json()["item"]["id"]
        resp = self.client.post("/wms/api/inspect/preview", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "battery_health": 90, "flaw_desc": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["suggest_grade"], "S")

        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "battery_health": 90, "flaw_desc": ""})
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["final"], "S")

    def test_121_safety_items_converged(self):
        """安全红线 6 项 → 3 项（2026-10-13 用户拍板）。

        鼓包 / 漏液 / 异味 / 漏电 / 焦糊 / 火烧 六格，现场看到任一都是「直接
        危废」，拆开只是让检测员多点三下 —— 收敛成：
        电池鼓包漏液 / 异味焦糊火烧 / 漏电触电。
        key 沿用 swell / burn / shock 三个老名，历史检测记录照样对得上。
        """
        keys = [k for k, _label in inspection.SAFETY_ITEMS]
        self.assertEqual(keys, ["swell", "burn", "shock"])
        self.assertEqual(tuple(keys), SAFETY_ITEM_KEYS,
                         "models.SAFETY_ITEM_KEYS 与 inspection.SAFETY_ITEMS 必须一致")

        labels = dict(inspection.SAFETY_ITEMS)
        self.assertEqual(labels["swell"], "电池鼓包 / 漏液")
        self.assertEqual(labels["burn"], "异味 / 焦糊 / 火烧痕迹")
        self.assertEqual(labels["shock"], "漏电 / 触电")

        # 下线的三项不再出现
        for old in ("leak", "smell", "fire"):
            self.assertNotIn(old, keys)
            self.assertNotIn(old, SAFETY_ITEM_KEYS)

        # 检测员看到的全套检项 = 品类检项 + 红线 3 项
        for cat in ("HA.KC.DFB", "PH", "OT"):
            self.assertEqual([k for k, _l in inspection.full_template(cat)][-3:], keys)

        # 收敛后的三项，判 × 照样直接危废
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            facts = inspection.facts_of(
                item, dict(_PHONE_RESULTS, burn=TEST_FAIL), None, "")
            self.assertTrue(facts["safety_hit"])
            facts = inspection.facts_of(
                item, dict(_PHONE_RESULTS, shock=TEST_FAIL), None, "")
            self.assertTrue(facts["safety_hit"])
            # 全是合格就不该命中
            facts = inspection.facts_of(
                item, dict(_PHONE_RESULTS, swell=TEST_PASS, burn=TEST_PASS,
                           shock=TEST_PASS), None, "")
            self.assertFalse(facts["safety_hit"])

            item = intake.list_items(s, batch_id=batch.id)[0]
            results = dict(_PHONE_RESULTS)
            results["burn"] = TEST_FAIL
            inspection.submit(s, item, tester="t", results=results,
                              fail_descs={"burn": "有焦糊味"})
            self.assertTrue(item.safety_hit)
            # 2026-10-13 口径：红线先走报修（C + 报修仓），不再直接判 D 危废
            self.assertEqual(item.grade, GRADE_C)
            self.assertEqual(item.status, "OUT_REPAIR")
            self.assertEqual(item.warehouse_code, "OUTR")


    # ------------------------------------------------------------ 批次停用 / 删除
    def test_70_disable_batch_keeps_goods(self):
        """停用批次：不再出现在选择里，但货品与流水一条不少；随时能恢复。"""
        batch = self._batch(2)
        resp = self.client.post(f"/wms/batches/{batch.id}/disable", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn(f'href="/wms/batches/{batch.id}"', self.client.get("/wms/batches").text,
                         "停用后默认列表里不再出现")
        self.assertIn("已停用", self.client.get("/wms/batches?status=disabled").text)
        with SessionLocal() as s:
            self.assertEqual(
                len(s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()), 2,
                "停用只挡新货，历史货品一条不少",
            )
        # 停用的批次不能再导入 / 建档
        self.assertEqual(
            self.client.get(f"/wms/batches/{batch.id}/import", follow_redirects=False).status_code,
            303, "停用的批次不该再进导入页",
        )
        self.client.post(f"/wms/batches/{batch.id}/disable", follow_redirects=False)  # 恢复
        self.assertIn(f'href="/wms/batches/{batch.id}"', self.client.get("/wms/batches").text,
                      "恢复了就该回到列表里")

    def test_71_delete_only_empty_batch(self):
        """有货的批次删不掉（提示改用停用），空批次才删。"""
        full = self._batch(1)
        self.assertEqual(
            self.client.post(f"/wms/batches/{full.id}/delete", follow_redirects=False).status_code,
            303,
        )
        self.assertIn("停用", self.client.get(f"/wms/batches/{full.id}").text,
                      "有货删不掉时，要给人指到「停用」这条路")
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(WmsBatch, full.id), "有货的批次不能真删")

        with SessionLocal() as s:
            empty = intake.create_batch(
                s, {"category": "PH", "source_type": "recycle", "qty_expected": 3,
                    "supplier_name": "空批次"}, operator="tester",
            )
            empty_id = empty.id
        self.assertEqual(
            self.client.post(f"/wms/batches/{empty_id}/delete", follow_redirects=False).status_code,
            303,
        )
        with SessionLocal() as s:
            self.assertIsNone(s.get(WmsBatch, empty_id), "空批次要删得掉")


    # ------------------------------------------------------------ 建档页 / 编辑 / 删除
    def _empty_batch(self, qty: int = 5) -> WmsBatch:
        """建一个空批次（不带货品）—— 建档页 / 编辑 / 删除的用例从这里起步。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "PH", "source_type": "recycle", "qty_expected": qty,
                    "supplier_name": "测试"}, operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def test_72_register_page_matches_manifest_fields(self):
        """手动建档独立页：字段与 Excel 清单一致，商品字段也能填（单条 / 多台）。"""
        batch = self._empty_batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register")
        self.assertEqual(page.status_code, 200)
        for frag in ('name="sku_code"', 'name="product_name"', 'name="brand"',
                     # 分类合成一个控件（2026-10-12）：三级下拉一份候选，不再分一级/二级/三级三格
                     'name="category"',
                     'name="purchase_cost"', 'name="base_price"',
                     'name="serial_no"', 'name="mode"',
                     # 数量（2026-10-10）：一条货 N 台、同一个条码，不再按数量拆成多条
                     'name="qty"'):
            self.assertIn(frag, page.text, frag)
        # 型号 / 规格 / 检修后条码 这一整块（2026-10-12 晚 用户要求）建档页删了：
        # 型号 / 规格归到规格维度；检修后条码在货品详情页贴（那边有 /own-barcode 表单）。
        for gone in ('name="model"', 'name="spec"', 'name="own_barcode"'):
            self.assertNotIn(gone, page.text, gone)
        with SessionLocal() as s:
            importer.ensure_field(s, "故障原因", "故障原因", "text", "手工")
            s.commit()
        self.assertIn('name="extra__故障原因"',
                      self.client.get(f"/wms/batches/{batch.id}/register").text,
                      "商品字段要能在建档页填")

        resp = self.client.post(f"/wms/batches/{batch.id}/items", data={
            "sku_code": "T-9001", "product_name": "测试破壁机", "brand": "美的",
            "category_l3": "破壁机", "model": "MB-1", "spec": "128G 黑",
            "purchase_cost": "389", "base_price": "599", "qty": "2",
            "extra__故障原因": "不加热",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            # 数量 2 = 一条货 2 台、同一个条码（2026-10-10 口径），不再拆成两台两个码
            self.assertEqual(len(items), 1, "数量 2 → 一条货")
            for it in items:
                self.assertEqual(int(it.qty or 0), 2, "数量记在这条货上")
                self.assertEqual(it.sku_code, "T-9001")
                self.assertEqual(it.category, "HA.KC.PBJ", "三级分类要认成品类")
                self.assertEqual(float(it.purchase_cost), 389.0)
                self.assertEqual(it.extra.get("故障原因"), "不加热")
            # 批次台数与入库流水都按数量走
            self.assertEqual(int(s.get(WmsBatch, batch.id).qty_actual or 0), 2)
            row = s.scalars(select(WmsStockLedger).where(
                WmsStockLedger.item_id == items[0].id)).first()
            self.assertEqual(int(row.qty_delta or 0), 2)

    def test_73_edit_item_keeps_barcode(self):
        """编辑货品：改货号 / 品牌 / 成本 / 商品字段都行，**条码终身不变**，改动留审计。"""
        batch = self._empty_batch()
        with SessionLocal() as s:
            importer.ensure_field(s, "故障原因", "故障原因", "text", "手工")
            created = intake.register(s, batch.id, [{
                "sku_code": "T-1", "brand": "美的", "category": "HA.KC.DFB",
                "product_name": "电饭煲", "purchase_cost": 100.0, "base_price": 200.0,
                "extra": {"故障原因": "不通电"},
            }], operator="tester")
            item_id, barcode = created[0].id, created[0].barcode
        page = self.client.get(f"/wms/items/{item_id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="T-1"', page.text, "编辑页要回填原值")
        self.assertIn('value="不通电"', page.text, "商品字段也要回填")

        resp = self.client.post(f"/wms/items/{item_id}/edit", data={
            "sku_code": "T-1B", "product_name": "电饭煲（换过主板）", "brand": "九阳",
            "category": "电饭煲", "model": "JY-2", "spec": "3L",
            "purchase_cost": "150", "base_price": "260", "serial_no": "SN-9",
            "extra__故障原因": "换过主板",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            it = s.get(WmsItem, item_id)
            self.assertEqual(it.sku_code, "T-1B")
            self.assertEqual(it.brand, "九阳")
            self.assertEqual(float(it.purchase_cost), 150.0)
            self.assertEqual(it.serial_no, "SN-9")
            self.assertEqual(it.category, "HA.KC.DFB", "中文品类认成三级码")
            self.assertEqual(it.extra.get("故障原因"), "换过主板")
            self.assertEqual(it.barcode, barcode, "条码是终身凭据，编辑不动它")
            logs = wms_audit.of_entity(s, "item", item_id)
            self.assertTrue(any(lg.action == "edit" for lg in logs),
                            [lg.action for lg in logs])

    def test_74_delete_item_cleans_ledger_and_balance(self):
        """删货品：没流动过的才删得掉（流水一并清、余额重算）；检测过的不许删。"""
        batch = self._empty_batch()
        with SessionLocal() as s:
            created = intake.register(s, batch.id, [
                {"sku_code": "D-1", "brand": "美的"},
                {"sku_code": "D-2", "brand": "九阳"},
            ], operator="tester")
            first_id, second_id = created[0].id, created[1].id
        with SessionLocal() as s:
            before = sum(b.qty for b in s.scalars(select(WmsStockBalance)).all())
            self.assertEqual(
                len(s.scalars(select(WmsStockLedger)
                              .where(WmsStockLedger.item_id.in_([first_id, second_id]))).all()),
                2, "两台货各一条入库流水",
            )
        resp = self.client.post(f"/wms/items/{first_id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.get(WmsItem, first_id))
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 1)
            self.assertEqual(
                len(s.scalars(select(WmsStockLedger)
                              .where(WmsStockLedger.item_id == first_id)).all()),
                0, "这台自己的流水一并清掉",
            )
            # rebuild 会把全库余额按**现存的流水**重算：脏桶（流水已删的）一并被清掉，
            # 所以这里不是「before - 1」，而是「只剩这一台在库」
            self.assertEqual(
                sum(b.qty for b in s.scalars(select(WmsStockBalance)).all()), 1,
                "余额按剩下的流水重算：只剩 1 台在库",
            )
            self.assertEqual(s.get(WmsBatch, batch.id).qty_actual, 1, "批次计数跟着退一台")

        with SessionLocal() as s:
            inspection.submit(s, s.get(WmsItem, second_id), tester="t",
                              results=dict(_PHONE_RESULTS), battery_health=90)
        resp2 = self.client.post(f"/wms/items/{second_id}/delete", follow_redirects=False)
        self.assertEqual(resp2.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(WmsItem, second_id), "检测过的货不许删（账对不上）")


    # ------------------------------------------------------------ 检测台分块 / 字段页 / 菜单序号
    def test_75_inspect_splits_inbound_and_inspection(self):
        """② 商品入库信息 与 ③ 检测信息 分成两块；沉淀字段按「商品字段」页的顺序与显示名给出来。"""
        page = self.client.get("/wms/inspect").text
        for frag in ("② 商品入库信息", "③ 检测信息", "④ 引擎建议"):
            self.assertIn(frag, page, frag)
        self.assertNotIn("② 检测结果", page, "原来那块混在一起的「检测结果」要拆掉")

        batch = self._empty_batch()
        with SessionLocal() as s:
            late = importer.ensure_field(s, "售后状态", "售后状态", "text", "河南清单")
            late.sort = 20
            early = importer.ensure_field(s, "下单时间", "下单时间", "text", "河南清单")
            early.sort = 10
            s.commit()
            created = intake.register(s, batch.id, [{
                "sku_code": "UX-1", "brand": "美的", "category": "HA.KC.DFB",
                "product_name": "电饭煲", "purchase_cost": 100.0, "base_price": 200.0,
                "extra": {"下单时间": "2026-05-15 11:38", "售后状态": "已换新"},
            }], operator="tester")
            barcode = created[0].barcode
        data = self.client.get(f"/wms/api/item.json?code={barcode}").json()
        rows = data["item"]["extra_rows"]
        self.assertEqual([r["label"] for r in rows], ["下单时间", "售后状态"],
                         "沉淀字段按排序给，显示名跟着「商品字段」页走")
        self.assertEqual(rows[0]["value"], "2026-05-15 11:38")
        self.assertTrue(data["item"]["batch_no"], "商品入库信息要带批次号")
        self.assertTrue(data["item"]["inbound_at"], "入库时间要有（页面时区口径）")

    def test_76_fields_page_rows_are_aligned(self):
        """商品字段页：一个字段一行（左栏元信息 + 右栏控件 + 靠右动作按钮组），不再七零八落。"""
        with SessionLocal() as s:
            f = importer.ensure_field(s, "故障原因", "故障原因", "text", "河南清单")
            s.commit()
            fid = f.id
        html = self.client.get("/wms/fields").text
        for frag in ('class="frow"', 'class="fmeta"', 'class="fedit"', 'class="facts"'):
            self.assertIn(frag, html, frag)
        self.assertIn(f'formaction="/wms/fields/{fid}/move?dir=up"', html, "上下移按钮并进同一行")
        self.assertIn(f'formaction="/wms/fields/{fid}/delete"', html)
        self.assertNotIn('class="keyname"', html, "老的一坨布局不该再出现")

    def test_77_nav_has_step_numbers(self):
        """入库分拣主线在菜单上带序号：1 批次与建档 → 2 扫码检测 → 3 标签打印与贴标复核。"""
        html = self.client.get("/wms").text
        self.assertIn('class="step"', html)
        for n, label in ((1, "批次与建档"), (2, "扫码检测"), (3, "标签打印与贴标复核")):
            self.assertIn(f'title="入库分拣第 {n} 步">{n}</span>', html, label)
            self.assertIn(label, html, label)
        steps = [(it.step, it.path) for it in wms_nav.items() if it.step]
        self.assertEqual(steps, [(1, "/wms/batches"), (2, "/wms/inspect"), (3, "/wms/labels")])
        self.assertEqual(wms_nav.crumbs("/wms/batches")[0].label, "批次与建档",
                         "面包屑 / 页面标题不能被序号污染")
        self.assertEqual(wms_nav.needed_perm("/wms/labels", "GET"), "wms.label.view")


    def test_78_item_api_serializes_records(self):
        """扫一台**已经检测过**的货（复检）：接口必须能出 JSON —— 检测记录里的时间是 datetime，
        以前直接塞进 JSONResponse 会 500（新货没记录，所以只在复检时暴露）。"""
        batch = self._empty_batch()
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [{"sku_code": "RC-1", "brand": "美的"}],
                                   operator="tester")[0]
            barcode = item.barcode
            inspection.submit(s, item, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90)
        resp = self.client.get(f"/wms/api/item.json?code={barcode}")
        self.assertEqual(resp.status_code, 200)
        records = resp.json()["records"]
        self.assertTrue(records, "检测过的货要有检测记录")
        self.assertIsInstance(records[0]["at"], str, "记录时间要转成文本再出 JSON")

    def test_78b_item_api_records_carry_details(self):
        """回看历史要把上一轮判定带进来（2026-10-14）：records 里带逐项判定与不合格原因。"""
        batch = self._empty_batch()
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [{"sku_code": "RC-2", "brand": "美的"}],
                                   operator="tester")[0]
            barcode = item.barcode
            results = dict(_PHONE_RESULTS)
            results["camera"] = TEST_FAIL
            inspection.submit(s, item, tester="t", results=results,
                              fail_descs={"camera": "后摄无法对焦"})
        resp = self.client.get(f"/wms/api/item.json?code={barcode}")
        self.assertEqual(resp.status_code, 200)
        rec = resp.json()["records"][-1]
        self.assertEqual(rec["results"].get("camera"), TEST_FAIL, "判定要逐项带出来")
        self.assertEqual(rec["fail_descs"].get("camera"), "后摄无法对焦", "不合格原因要带出来")

    def test_78c_inspect_page_history_prefill(self):
        """检测台：回看历史把上一轮判定带回检查项；提示条挪到「引擎建议」上方（2026-10-14）。"""
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        self.assertIn("applyHistory", page.text)
        self.assertIn("已经带到下面的检查项里", page.text)
        self.assertLess(page.text.find('id="recheckBar"'), page.text.find("④ 引擎建议"),
                        "提示条要在引擎建议上面")


    # ------------------------------------------------------------ 仓库与货架维护
    def test_79_warehouses_are_maintainable(self):
        """仓库 / 区 / 货架可在页面上增改删：内置 7 仓只能改名与停用，
        业务页面的库位只能从这里选（接口直连塞脏值也要被后端拦下）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
            sale_id = s.scalar(select(WmsWarehouse.id).where(WmsWarehouse.code == "SALE"))

        page = self.client.get("/wms/warehouses")
        self.assertEqual(page.status_code, 200)
        for name in ("待检仓", "可售仓", "维修仓", "报修仓", "拆机件仓", "危废区", "退货暂存区"):
            self.assertIn(name, page.text, name)
        self.assertIn("内置", page.text, "内置仓要有标记（不能删）")

        # 新增「区」，再在它下面挂一个货架（编码留空按上级自动生成）
        self.assertEqual(
            self.client.post("/wms/warehouses", data={
                "level": "2", "parent_code": "SALE", "code": "SALE-QA", "name": "A 区",
                "grade_zone": "S", "can_out": "1", "enabled": "1",
            }, follow_redirects=False).status_code, 303)
        self.assertEqual(
            self.client.post("/wms/warehouses", data={
                "level": "3", "parent_code": "SALE-QA", "name": "03 号货架", "enabled": "1",
            }, follow_redirects=False).status_code, 303)
        with SessionLocal() as s:
            zone = s.scalar(select(WmsWarehouse).where(WmsWarehouse.code == "SALE-QA"))
            shelf = s.scalar(select(WmsWarehouse).where(WmsWarehouse.parent_code == "SALE-QA"))
            zone_id, shelf_id, shelf_code = zone.id, shelf.id, shelf.code
            self.assertEqual(zone.full_path, "可售仓 › A 区", "路径要把上级串起来")
            self.assertEqual(shelf_code, "SALE-QA-01", "编码留空按上级自动生成")
            self.assertEqual(shelf.full_path, "可售仓 › A 区 › 03 号货架")

        # 改名：全站展示名跟着走（货品上存的是编码，展示靠这份缓存）
        self.assertEqual(
            self.client.post("/wms/warehouses", data={
                "wh_id": str(zone_id), "name": "A 区（改过名）", "parent_code": "SALE",
                "enabled": "1",
            }, follow_redirects=False).status_code, 303)
        self.assertEqual(warehouse_label("SALE-QA"), "可售仓 › A 区（改过名）")

        # 业务页面：库位是下拉，且能选到刚建的货架
        detail = self.client.get(f"/wms/items/{item_id}").text
        self.assertIn('select id="mv-loc"', detail, "移库的库位是下拉，不是手打输入框")
        self.assertIn(shelf_code, detail)
        inspect_page = self.client.get("/wms/inspect").text
        self.assertIn('select id="location"', inspect_page, "检测台的货架位置也是下拉")
        self.assertIn(shelf_code, inspect_page)

        # 接口直连塞一个不存在的库位：后端要拦住（页面下拉只是第一道）
        resp = self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "move", "warehouse_code": "SALE", "location_code": "NOT-EXIST",
        })
        self.assertIn("不在「仓库与货架」维护表里", resp.text)
        with SessionLocal() as s:
            self.assertNotEqual(s.get(WmsItem, item_id).location_code, "NOT-EXIST",
                                "脏库位不能落库")

        # 内置仓删不掉（分级路由按它入仓），自建的货架能删
        resp = self.client.post(f"/wms/warehouses/{sale_id}/delete")
        self.assertIn("内置仓", resp.text)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(WmsWarehouse, sale_id), "拦下之后内置仓还在")
        self.assertEqual(
            self.client.post(f"/wms/warehouses/{shelf_id}/delete",
                             follow_redirects=False).status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.get(WmsWarehouse, shelf_id), "没人用的货架可以删")
            self.assertIsNotNone(
                s.scalar(select(WmsWarehouse).where(WmsWarehouse.code == "SALE-QA")),
                "删子级不影响上级")

        # 停用：业务下拉里不再出现（历史货品与流水照旧）
        self.assertEqual(
            self.client.post(f"/wms/warehouses/{zone_id}/toggle",
                             follow_redirects=False).status_code, 303)
        with SessionLocal() as s:
            self.assertFalse(s.get(WmsWarehouse, zone_id).enabled)
            codes = [o["code"] for o in wms_warehouses.location_options(s)]
        self.assertNotIn("SALE-QA", codes, "停用的库位不再进业务下拉")
        self.assertIn("已停用", self.client.get("/wms/warehouses").text)

    def test_80_warehouse_dropdown_is_level1_only(self):
        """仓库下拉只列第 1 级：区 / 货架是库位，不能混进出库单的「目标仓」。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        self.client.post("/wms/warehouses", data={
            "level": "2", "parent_code": "SALE", "code": "SALE-QB", "name": "B 区", "enabled": "1"})
        self.client.post("/wms/warehouses", data={
            "level": "3", "parent_code": "SALE-QB", "code": "SALE-QB-01", "name": "01 号货架",
            "enabled": "1"})

        # 建单表单（含仓库下拉）在 /wms/outbound/new 子页面（2026-10-16 拆出去）
        outbound = self.client.get("/wms/outbound/new").text
        self.assertIn("目标仓", outbound)
        self.assertNotIn("SALE-QB", outbound, "区 / 货架不该混进出库单的仓库下拉")
        self.assertIn("SALE-QB-01", self.client.get(f"/wms/items/{item_id}").text,
                      "库位下拉里要有货架")

        # 检测提交时塞一个脏库位：400，而且不该把检测记录写进去
        with SessionLocal() as s:
            before = len(inspection.records_of(s, item_id))
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "battery_health": 90, "location_code": "NOT-EXIST",
        })
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("维护表", resp.json()["message"])
        with SessionLocal() as s:
            self.assertEqual(len(inspection.records_of(s, item_id)), before,
                             "脏库位不该把检测记录也写进去")

        # 从下拉里选真库位 → 检测通过并顺手登记位置
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "battery_health": 90, "location_code": "SALE-QB-01",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItem, item_id).location_code, "SALE-QB-01")

    def test_82_legacy_free_text_location_still_works(self):
        """下拉时代之前手打的库位：页面照旧显示（标「历史手填」），原样提交不被拦。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            item.location_code = "旧手填-A3"
            s.commit()
            item_id = item.id

        page = self.client.get(f"/wms/items/{item_id}").text
        self.assertIn("旧手填-A3", page)
        self.assertIn("历史手填", page, "老值要能看出来不是从维护表里选的")

        resp = self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "move", "warehouse_code": "SALE", "location_code": "旧手填-A3"})
        # 注意别断言整页不含「维护表」三个字：下拉里那个历史值选项本身就写着「已不在维护表里」
        self.assertNotIn("不在「仓库与货架」维护表里", resp.text,
                         "没改动就不该拦（复检老货不能卡流程）")

        resp = self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "move", "warehouse_code": "SALE", "location_code": "乱填的库位"})
        self.assertIn("不在「仓库与货架」维护表里", resp.text, "换新值必须有据可依")
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItem, item_id).location_code, "旧手填-A3")

    # ------------------------------------------------------------ 库位搜索联想
    def test_84_location_picker_short_code_and_keys(self):
        """库位选择器（2026-10-11 用户反馈「选项太长挑起来太麻烦」）：

        每个库位给一份联想料 —— 层级序号短码（第几个仓 › 第几个区 › 第几个货架）、
        搜索键（短码 + 全路径 + 名称 + 拼音首字母）、在用台数、上级路径；
        页面渲染成搜索联想组件，值仍存在隐藏的 <select> 里（老代码 / 表单提交不用改）。
        """
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        self.client.post("/wms/warehouses", data={
            "level": "2", "parent_code": "SALE", "code": "SALE-QC", "name": "C 区", "enabled": "1"})
        self.client.post("/wms/warehouses", data={
            "level": "3", "parent_code": "SALE-QC", "name": "07 号货架", "enabled": "1"})

        with SessionLocal() as s:
            opts = [o for o in wms_warehouses.location_options(s) if o["code"] == "SALE-QC-01"]
            roots = [r.code for r in wms_warehouses.all_rows(s) if int(r.level or 1) == 1]
        self.assertEqual(len(opts), 1, "新建的货架要进业务选择器")
        shelf = opts[0]
        parts = shelf["short"].split("-")
        self.assertEqual(len(parts), 3, "仓库 › 区 › 货架 三段短码")
        self.assertTrue(all(p.isdigit() for p in parts), shelf["short"])
        self.assertEqual(parts[0], str(roots.index("SALE") + 1),
                         "短码按维护表的顺序编号（↑↓ 改顺序，短码跟着变）")
        self.assertEqual(parts[1:], ["1", "1"], "可售仓下第一个区里的第一个货架")
        self.assertEqual(shelf["root_name"], "可售仓")
        self.assertEqual(shelf["parent_path"], "可售仓 › C 区", "列表里省掉叶子名，行短一半")
        self.assertIn("07 号货架", shelf["search"], "搜索键要含名称")
        self.assertIn(shelf["full_path"], shelf["search"], "也要含全路径")
        self.assertIn("KS", shelf["search"], "拼音首字母：ks → 可售仓（hj → 货架）")
        self.assertEqual(shelf["use"], 0, "还没挂货 → 在用 0")

        page = self.client.get("/wms/inspect").text
        self.assertIn('class="lp-input"', page, "检测台的货架位置是搜索联想框")
        self.assertIn("wmsLocPicker", page, "选择器的脚本要装进页面")
        self.assertIn(f'data-short="{shelf["short"]}"', page, "短码要落到选项上")
        self.assertIn(f'data-keys="{shelf["search"]}"', page, "搜索键要真的渲染进选项")
        self.assertNotIn("built-in method", page,
                         "字典那个「联想串」不能叫 keys：Jinja 的 loc.keys 取到的是 dict 的方法")

        detail = self.client.get(f"/wms/items/{item_id}").text
        self.assertIn('class="loc-pick"', detail, "移库页也用同一个选择器")
        self.assertIn("location_code", detail, "值仍由隐藏 select 提交")

        # 挂一台货上去：在用台数进选项（前端按它把常用的排前面）
        self.assertEqual(self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "move", "warehouse_code": "SALE", "location_code": "SALE-QC-01",
        }, follow_redirects=False).status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItem, item_id).location_code, "SALE-QC-01")
            opts = [o for o in wms_warehouses.location_options(s) if o["code"] == "SALE-QC-01"]
        self.assertEqual(opts[0]["use"], 1, "挂在库位上的台数就是「在用」")
        self.assertIn('data-use="1"', self.client.get("/wms/inspect").text)

    # ------------------------------------------------------------ 更新日志分系统
    def test_81_wms_changelog_lists_only_wms(self):
        """渔集有自己的更新日志页，只列渔集的更新（渔枢系统 / 渔货栈的不在这儿露面）。"""
        page = self.client.get("/wms/changelog")
        self.assertEqual(page.status_code, 200)
        self.assertIn("更新日志", page.text)
        self.assertIn("渔集", page.text)
        self.assertIn("v1.0.8", page.text, "渔集的某条版本要能看到（日志从 v1.0.1 起独立编号）")
        self.assertIn("config/changelog.json", page.text, "要说清记录记在哪")
        # 菜单里有入口
        self.assertIn("/wms/changelog", self.client.get("/wms").text)

    # ------------------------------------------------------------ 分级条件说人话
    def test_83_grading_conditions_in_plain_chinese(self):
        """conditions 那串 JSON 翻成中文句子：看规则的人不用懂字段的英文名。"""
        from wms import grading

        text = grading.conditions_text({"all_of": [
            {"field": "battery_health", "op": ">=", "value": 85},
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": "==", "value": 0},
        ]})
        self.assertIn("全部满足", text)
        self.assertIn("电池健康度 ≥ 85%", text)
        self.assertIn("必检项是否全过 = 是", text)
        self.assertNotIn("battery_health", text, "别再印英文字段名")

        any_text = grading.conditions_text(
            {"any_of": [{"field": "safety_hit", "op": "==", "value": True}]})
        self.assertIn("满足任一", any_text)
        self.assertIn("是否命中安全红线 = 是", any_text)
        self.assertIn("没写条件", grading.conditions_text({}), "空条件是兜底，要讲清楚")

    def test_84_rules_page_has_condition_builder(self):
        """规则页：条件列是中文，新增时能靠下拉拼条件（不必手写 JSON）。"""
        page = self.client.get("/wms/rules").text
        self.assertIn("必检项是否全过", page, "字段要有中文名")
        self.assertIn('id="cb-rows"', page, "要有那个挑条件的格子")
        self.assertIn("加一条条件", page)
        self.assertIn("必检项是否全过 = 是", page, "列表里的条件要已经是中文")
        self.assertNotIn('"field": "battery_health"', page, "电池已下线，条件下拉里不该再有")

    def test_84b_rules_page_layout_and_price_order(self):
        """规则页版面三条（2026-10-11）：

        ① 「品类与对标」挪到页面**最下面** —— 它是查对用的参照表，摆最上面会把
           真正要动的两张表（分级规则 / 价格系数）顶出屏幕；
        ② 「新增分级规则」**默认收起**，点标题条展开；点表格里的「编辑」过来时自动展开；
        ③ 价格系数的等级按**分级规则的优先级升序**排 —— 字母序是 A/B/C/D/S，
           S 反而排最后，而 S 恰恰是最先匹配的那一档。
        """
        import re

        page = self.client.get("/wms/rules").text
        i_rule = page.index("分级规则（安全红线永远第一匹配）")
        i_price = page.index("价格系数（渔货价")
        i_cat = page.index("品类与对标")
        self.assertLess(i_rule, i_price, "先看到规则，再看到价格系数")
        self.assertLess(i_price, i_cat, "品类与对标要在最下面")

        # ② 默认收起：<details id="rule-form"> 上不带 open；?edit= 过来要自动展开
        fold = re.search(r'<details class="rule-fold" id="rule-form"[^>]*>', page)
        self.assertTrue(fold, "新增表单要包在可折叠的 details 里")
        self.assertNotIn("open", fold.group(0), "默认收起")
        self.assertIn("默认收起 · 点这里展开填表", page)
        with SessionLocal() as s:
            rid = s.scalars(select(WmsGradeRule)).first().id
        edit_page = self.client.get(f"/wms/rules?edit={rid}").text
        fold_edit = re.search(r'<details class="rule-fold" id="rule-form"[^>]*>', edit_page)
        self.assertIn("open", fold_edit.group(0), "点「编辑」跳过来必须展开，不然填不了")
        self.assertIn("编辑分级规则", edit_page)

        # ③ 表格里的等级顺序 = 分级规则表里第一次出现的顺序（规则已按优先级升序）
        def shown_grades(html):
            block = html.split("价格系数（渔货价")[1].split("品类与对标")[0]
            return re.findall(r'title="等级（只由检测决定）">(\w)', block)

        with SessionLocal() as s:
            rules = list(
                s.scalars(
                    select(WmsGradeRule).order_by(WmsGradeRule.priority.asc(), WmsGradeRule.id.asc())
                ).all()
            )
            prices = [str(p.grade or "") for p in s.scalars(select(WmsPriceRule)).all()]
        seen, expect = set(), []
        for r in rules:
            if r.grade not in seen:
                seen.add(r.grade)
                expect.append(r.grade)
        expect = [g for g in expect if g in prices] + [g for g in sorted(prices) if g not in expect]
        self.assertEqual(shown_grades(page), expect, "价格系数跟着规则优先级排，不是字母序")

        # 把某一档调到优先级最前，表也得跟着变（字母序下 D 排第 4 位）
        with SessionLocal() as s:
            backup = {r.id: r.priority for r in s.scalars(select(WmsGradeRule)).all()}
            for r in s.scalars(select(WmsGradeRule).where(WmsGradeRule.grade == GRADE_D)).all():
                r.priority = -1
            s.commit()
        try:
            self.assertEqual(shown_grades(self.client.get("/wms/rules").text)[0], GRADE_D,
                             "优先级最高的那一档要排最前（不是 A/B/C/D/S 的字母序）")
        finally:
            with SessionLocal() as s:
                for row_id, pri in backup.items():
                    row = s.get(WmsGradeRule, row_id)
                    if row is not None:
                        row.priority = pri
                s.commit()

    def test_84c_condition_builder_two_groups_and_copy(self):
        """条件区要能同时表达「全部满足」与「满足任一」（2026-10-11 现场反馈）。

        引擎判级口径是「all_of 全部命中 **且** any_of 至少命中一条」，而界面上以前只有一组
        条件 + 一个「全部 / 任一」单选 —— N 全新 那种「（原封未拆 或 拆封未使用）且 全项通过
        且 零瑕疵」根本表达不出来：点「照抄」只插进「或」的半截，「全项通过 + 零瑕疵」被悄悄
        丢掉；打开 N 全新 那条规则编辑更糟，一进来就少一半，点保存真就删了。
        """
        import re

        from wms import grading

        page = self.client.get("/wms/rules").text
        self.assertIn('id="cb-rows"', page, "「全部满足」那组要有自己的格子")
        self.assertIn('id="cb-rows-any"', page, "「满足任一」那组也要有")
        self.assertIn('id="cb-add-any"', page, "两组各自能加条件")
        self.assertNotIn('name="cbmode"', page, "不再用「全部 / 任一」二选一（选了一组就丢另一组）")
        # 照抄 = 连档位 / 优先级一起填：只填条件会让人以为「条件满足就自动判那一档」
        n_ex = grading.CONDITION_EXAMPLES[0]
        self.assertEqual(n_ex["grade"], GRADE_N)
        self.assertEqual(n_ex["priority"], 5)
        self.assertTrue(n_ex["json"].get("all_of"), "N 全新 的「且」那半不能丢")
        self.assertTrue(n_ex["json"].get("any_of"), "N 全新 的「或」那半也不能丢")
        self.assertRegex(page, r'"grade":\s*"N"', "示例要带档位，照抄时才能一起填")
        self.assertRegex(page, r'"safety":\s*true', "安全红线示例要连「安全红线 = 是」一起照抄")
        self.assertIn("别抄错地方", page, "要写明：同一批条件填在 D 报废 里 = 满足这些条件的判 D")

        # 编辑已有规则：条件框里「且」「或」两半都要带着（JS 负责回填两组）
        with SessionLocal() as s:
            n_rule = s.scalar(select(WmsGradeRule).where(WmsGradeRule.grade == GRADE_N))
            n_id, n_title = (n_rule.id, n_rule.title) if n_rule else (None, "")
        if n_id:
            edit_page = self.client.get(f"/wms/rules?edit={n_id}").text
            box = re.search(r'id="r-cond"[^>]*>(.*?)</textarea>', edit_page, re.S)
            self.assertIn("all_of", box.group(1), f"「{n_title}」的「且」条件要带进表单")
            self.assertIn("any_of", box.group(1), "「或」条件也要带进表单")

    def test_84d_condition_items_unified_and_rule_check(self):
        """各档条件项统一 + 规则体检（2026-10-11 现场要求：循环检查所有档的条件项）。

        现场原话：「照抄的完美条件里还有一个电池的单独条件，是不是也循环检查下，
        把所有的等级条件判断都统一一下条件项」。结论是**电池健康度整项下线**
        （主营小家电，电池饱和度是手机品类专属）：各档统一成「必检项 + 外观瑕疵」，
        检测台不再采集、条件下拉里也不再有这一项。
        """
        fields_of = lambda ex: {c["field"] for k in ("all_of", "any_of") for c in ex["json"].get(k, [])}
        by_grade = {ex["grade"]: ex for ex in grading.CONDITION_EXAMPLES}
        for g in (GRADE_N, GRADE_S, GRADE_A):
            self.assertIn(g, by_grade, f"{g} 档要有可照抄的示例")
        # 体检档统一成同一套条件项（必检项 + 外观瑕疵），**都不许再出现电池**
        for g in (GRADE_S, GRADE_A):
            self.assertTrue(
                {"all_items_pass", "flaw_count"} <= fields_of(by_grade[g]),
                f"{g} 档的体检项要写齐（必检项 + 外观瑕疵）",
            )
            self.assertNotIn("battery_health", fields_of(by_grade[g]),
                             f"{g} 档不该再有电池条件（已下线）")
        # N 档与 S 一致（必检项 + 零瑕疵），多的只有「原封未拆 / 拆封未使用」
        self.assertTrue({"all_items_pass", "flaw_count", "sealed", "opened_unused"}
                        <= fields_of(by_grade[GRADE_N]))
        self.assertNotIn("battery_health", fields_of(by_grade[GRADE_N]))
        # 播种口径同步（新库就是这套）
        for row in wms_seed.GRADE_RULE_SEED:
            self.assertNotIn("battery_health",
                             {c["field"] for k in ("all_of", "any_of")
                              for c in (row["conditions"].get(k) or [])},
                             f"播种的 {row.get('grade')} 档不该再有电池条件")
        # 条件项下拉里也不许再给电池（不能再新建这种条件）
        self.assertNotIn("battery_health", [f["field"] for f in grading.CONDITION_FIELDS])
        # 但老规则里已有的电池条件仍要能翻成中文（看规则的人看得懂）
        self.assertIn("电池健康度", grading.FIELD_LABELS.get("battery_health", ""))

        def rule(grade, conditions, priority=10, title="", enabled=True):
            return WmsGradeRule(category="ZZ", priority=priority, grade=grade, title=title,
                                conditions=conditions, safety_flag=False, enabled=enabled)

        batt = {"all_of": [{"field": "battery_health", "op": ">=", "value": 85}]}
        nobatt = {"all_of": [{"field": "all_items_pass", "op": "==", "value": True}]}
        # ① 用了已下线的条件项 → 点出来让人删（这一条永远不满足，会挡着这一档）
        check = grading.rule_checklist([rule(GRADE_S, batt, 10, "S 还有电池")])
        self.assertIn("已经下线", check["rows"][0]["note"], check["rows"][0]["note"])
        self.assertIn("电池健康度", check["rows"][0]["note"])
        self.assertEqual(check["row_notes"][0]["rule"].grade, GRADE_S)
        # 没用到下线项、说明与条件也对得上 → 不该报
        clean = grading.rule_checklist([rule(GRADE_S, {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": "==", "value": 0}]}, 10, "S 完美：全项通过 + 零瑕疵")])
        self.assertEqual(clean["rows"][0]["note"], "")
        self.assertEqual(clean["row_notes"], [])
        # 「零瑕疵」与「全项通过」是一个意思（全项通过就蕴含零瑕疵）——
        # 条件里只有 all_items_pass 不算说明与条件各说各话，别再误报黄条
        # （2026-10-13 用户截图：线上 S 规则被这条误报顶出「体检发现 1 处处理」）
        implied = grading.rule_checklist([rule(GRADE_S, nobatt, 10, "S 完美：全项通过 + 零瑕疵")])
        self.assertEqual(implied["rows"][0]["note"], "", implied["rows"][0]["note"])
        self.assertEqual(implied["row_notes"], [])
        # B 档说明里的「瑕疵」是泛指（缺/瑕疵/可卖 —— 有瑕疵但照常卖），条件用
        # minor_defect 表达 —— 也不算各说各话（2026-10-14 用户截图黄条的另一半）
        b_implied = grading.rule_checklist([rule("B", {"all_of": [
            {"field": "minor_defect", "op": "==", "value": True}]},
            25, "B 缺/瑕疵/可卖：只有不影响使用的缺陷")])
        self.assertEqual(b_implied["rows"][0]["note"], "", b_implied["rows"][0]["note"])
        # ② 条件一模一样、档位不同 → 另一条永远轮不到
        same = grading.rule_checklist([rule(GRADE_N, nobatt, 5), rule(GRADE_D, nobatt, 50)])
        self.assertTrue(any("条件一模一样" in n for n in same["notes"]), same["notes"])
        # ③ 空条件（兜底档）排在最前面 → 后面的规则永远轮不到
        empty_first = grading.rule_checklist([rule(GRADE_N, {}, 1, "空条件"), rule(GRADE_S, nobatt, 10)])
        self.assertTrue(any("兜底档" in n and "永远轮不到" in n for n in empty_first["notes"]),
                        empty_first["notes"])
        # ④ 说明里写了某一项、条件里没写 → 说明与条件对不上
        mismatch = grading.rule_checklist([
            rule(GRADE_D, nobatt, 50, "D 报废：缺件严重 / 无维修价值")])
        row = mismatch["rows"][0]
        self.assertIn("缺件", row["note"])
        self.assertIn("说明与条件对不上", row["note"])
        # 说明与条件对得上就不该报
        ok = grading.rule_checklist([
            rule(GRADE_D, {"all_of": [{"field": "missing_parts", "op": "==", "value": True}]},
                 50, "D 报废：缺件严重")])
        self.assertEqual(ok["rows"][0]["note"], "")
        self.assertEqual(ok["row_notes"], [])
        # ⑤ 「条件抄了别档、档位没改」：内置口径比对（这是「判断条件是否对」的核心）
        n_json = grading.CONDITION_EXAMPLES[0]["json"]
        self.assertIn("抄错了地方", grading.mismatch_note(n_json, GRADE_D))
        self.assertEqual(grading.mismatch_note(n_json, GRADE_N), "", "档位和示例一致就不该提醒")
        d_cond = [r for r in wms_seed.GRADE_RULE_SEED if r["grade"] == GRADE_D and not r.get("safety_flag")][0]
        self.assertIn("抄错了地方", grading.mismatch_note(d_cond["conditions"], GRADE_S))
        self.assertEqual(grading.mismatch_note(d_cond["conditions"], GRADE_D), "")
        self.assertEqual(grading.mismatch_note({"all_of": []}, GRADE_D), "", "没条件不算抄错")

    def test_84e_rule_check_card_and_copy_guard_on_page(self):
        """规则页要把「且 / 或」讲清、要有体检卡、保存前问一句条件对不对。"""
        page = self.client.get("/wms/rules").text
        self.assertIn("规则体检", page, "要有体检卡（循环所有规则比对条件项）")
        self.assertIn("点开看对照表", page)
        for label in ("必检项全过", "外观瑕疵", "原封未拆", "缺件严重"):
            self.assertIn(label, page, "对照表要列出每个条件项")
        self.assertNotIn("电池健康度</th>", page, "电池已下线，对照表里不该再有这一列")
        # 措辞：两组是「且」的关系（现场把「满足任一即可」读成了自成一条规则）
        self.assertIn("两组是「且」的关系", page)
        self.assertIn("还要满足任一", page)
        self.assertNotIn("满足任一</strong>即可", page, "旧措辞会让人以为「满足任一」本身就是判级标准")
        self.assertIn("条件只是「什么时候套这一档」", page)
        # 保存前确认（前端）＋后端保存后的提醒（test_84f）
        self.assertIn("先确认条件对不对", page, "抄了别档的条件时保存前要问一句")
        self.assertIn('id="rule-form-el"', page)

    def test_84f_save_warns_when_conditions_copied_to_wrong_grade(self):
        """把 N 的条件存进 D 档 → 保存成功但提醒「是不是抄错了地方」。"""
        made: list[int] = []
        try:
            n_cond = json.dumps(grading.CONDITION_EXAMPLES[0]["json"], ensure_ascii=False)
            resp = self.client.post("/wms/rules/grade", data={
                "category": "ZZ", "priority": "50", "grade": GRADE_D, "safety_flag": "0",
                "title": "D 报废（抄错演练）", "reason": "", "enabled": "1",
                "conditions": n_cond})
            self.assertEqual(resp.status_code, 200)
            # 提示条里的这句是服务端拼的（页面 JS 里也有「抄错了地方」几个字，所以只认这句）
            self.assertIn("但有件事要你确认", resp.text, "条件与别档一模一样时要提醒")
            self.assertIn("抄错了地方", resp.text)
            with SessionLocal() as s:
                row = s.scalar(select(WmsGradeRule).where(WmsGradeRule.category == "ZZ"))
                self.assertIsNotNone(row, "提醒归提醒，规则要照存")
                made.append(row.id)
            # 反例：同样的条件挂在 N 档（与示例一致）→ 不该提醒
            resp = self.client.post("/wms/rules/grade", data={
                "category": "ZY", "priority": "5", "grade": GRADE_N, "safety_flag": "0",
                "title": "N 全新（正常）", "reason": "", "enabled": "1",
                "conditions": n_cond})
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("但有件事要你确认", resp.text)
            with SessionLocal() as s:
                made.append(s.scalar(select(WmsGradeRule).where(WmsGradeRule.category == "ZY")).id)
        finally:
            with SessionLocal() as s:
                for rid in made:
                    row = s.get(WmsGradeRule, rid)
                    if row:
                        s.delete(row)
                s.commit()

    def test_85_inspect_highlights_base_price_and_compares(self):
        """检测台：基准价要红字放大；引擎建议里要有「基准价 → 渔货价」的对比条。"""
        page = self.client.get("/wms/inspect").text
        self.assertIn("price-hot", page, "基准价要有单独的样式钩子（红字 + 大一号）")
        self.assertIn(".v.price-hot", page)
        self.assertIn('id="priceVs"', page, "渔货价卡片里要有对比条")
        self.assertIn('id="vsBase"', page)
        self.assertIn('id="vsFinal"', page)
        self.assertIn("vs-diff", page, "要标出差了多少")
        self.assertIn("fillVs", page)

    # ------------------------------------------------------------ 检测台即时打标签
    def test_86_inspect_label_preview_api(self):
        """检测台的标签预览：不落库、不打印，直接给出「纸上长什么样」。"""
        from wms.models import WmsLabelPrint

        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        d = self.client.get(f"/wms/api/label/preview?item_id={item_id}").json()
        self.assertTrue(d.get("ok"), d)
        self.assertIn("<div", d.get("html", ""), "要给得出能塞进页面的片段")
        self.assertIn("60", (d.get("footer") or ""))
        with SessionLocal() as s:
            self.assertEqual(
                s.query(WmsLabelPrint).filter(WmsLabelPrint.item_id == item_id).count(), 0,
                "预览不该在打印队列里留痕")

    def test_87_inspect_print_label_enqueues(self):
        """检测台点「打印标签」：先入队再下发；云打印没配也不崩，队列里照样留痕。"""
        from wms.models import WmsLabelPrint

        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        d = self.client.post("/wms/api/label/print",
                             json={"item_id": item_id, "grade": "S"}).json()
        self.assertIn("message", d)
        with SessionLocal() as s:
            row = s.query(WmsLabelPrint).filter(WmsLabelPrint.item_id == item_id).first()
            self.assertIsNotNone(row, "要排进打印队列（打没打成都得留痕）")
            self.assertEqual(row.grade, "S", "印在纸上的等级按传进来的建议档")
            self.assertEqual(row.transport, "CLOUD")

    def test_88_labels_page_shows_printed_and_labeled(self):
        """标签打印页：打过的显示已打印，贴标复核过的显示已贴标。"""
        from wms import labeling
        from wms.timeutil import now as _now

        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            item.grade, item.status = "S", "SELLABLE"
            s.commit()
            rows = labeling.enqueue(s, [item], operator="tester")
            rows[0].status, rows[0].printed_at, rows[0].grade = "printed", _now(), "S"
            s.commit()
        page = self.client.get("/wms/labels").text
        self.assertIn("已打印", page)
        self.assertIn("已贴标", page, "复核通过的货要在这一页显示已贴标")

    def test_89_inspect_check_own_code(self):
        """扫「检修后条码」的防错：跟刚打出来那张比对 —— 一致放行 / 别台货拦下 / 陌生码要确认。"""
        batch = self._batch(2)
        with SessionLocal() as s:
            a, b = intake.list_items(s, batch_id=batch.id)[:2]
            a_id, a_code, b_id, b_code = a.id, a.barcode, b.id, b.barcode
        # ① 扫的就是自己那张标签上印的码 → 直接填
        d = self.client.get(f"/wms/api/label/check-code?item_id={a_id}&code={a_code}").json()
        self.assertTrue(d.get("ok"), d)
        self.assertTrue(d.get("same"), d)
        self.assertEqual(d.get("label_barcode"), a_code)
        # ② 扫到库里另一台货的码 → 贴错标，拦下（页面只给「重新扫」）
        d = self.client.get(f"/wms/api/label/check-code?item_id={a_id}&code={b_code}").json()
        self.assertTrue(d.get("ok"), d)
        self.assertFalse(d.get("same"))
        self.assertEqual((d.get("other_item") or {}).get("id"), b_id)
        self.assertIn("贴错标", d.get("message") or "")
        # ③ 库里没有的陌生码 → 可能是自己的内部码，弹出来确认才填
        d = self.client.get(
            f"/wms/api/label/check-code?item_id={a_id}&code=OWN-XYZ-9999").json()
        self.assertTrue(d.get("ok"), d)
        self.assertFalse(d.get("same"))
        self.assertIsNone(d.get("other_item"))
        self.assertIn("内部码", d.get("message") or "")

    def test_90_inspect_recheck_prompt(self):
        """扫到检测过的货：接口给出「已检测」标记 + 上一轮摘要，页面要先问一句是不是重新提交。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            code = intake.list_items(s, batch_id=batch.id)[0].barcode
        # ① 新货：没检测过
        d = self.client.get("/wms/api/item.json", params={"code": code}).json()
        self.assertTrue(d.get("ok"), d)
        self.assertFalse(d.get("tested"), "没检测过的货不该被当成复检")
        self.assertIsNone(d.get("last_record"))
        # ② 提交一轮检测
        item_id = d["item"]["id"]
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS), "battery_health": 90})
        self.assertTrue(resp.json().get("ok"), resp.text)
        # ③ 再扫进来：给出「已检测」与上一轮摘要
        d = self.client.get("/wms/api/item.json", params={"code": code}).json()
        self.assertTrue(d.get("tested"), "检测过的货要告诉页面：这台提过了")
        self.assertEqual(d["last_record"]["round"], 1)
        self.assertEqual(d["last_record"]["final"], "S")
        self.assertTrue(d["last_record"]["at"])
        # ④ 页面要有「是不是重新检测（覆盖上一轮）」这一问
        page = self.client.get("/wms/inspect").text
        self.assertIn('id="recheckMask"', page)
        self.assertIn("重新检测（覆盖上一轮）", page)
        self.assertIn("算了，先看历史", page)

    def test_91_items_ledger_location_and_scan_search(self):
        """台账「所在仓」要把货架带上；搜索框支持扫码枪扫一下直接搜（2026-10-10）。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        self.client.post("/wms/warehouses", data={
            "level": "2", "parent_code": "SALE", "code": "SALE-QB", "name": "B 区", "enabled": "1"})
        self.client.post("/wms/warehouses", data={
            "level": "3", "parent_code": "SALE-QB", "code": "SALE-QB-01", "name": "01 号货架",
            "enabled": "1"})
        resp = self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "move", "warehouse_code": "SALE", "location_code": "SALE-QB-01"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])

        page = self.client.get("/wms/items").text
        self.assertIn("可售仓", page)
        self.assertIn("B 区 › 01 号货架", page,
                      "所在仓只写仓库名找不着货 —— 货架全路径要一起带上")
        # 扫码枪：连续击键 + 回车结尾 = 扫码，直接拿这个码提交搜索
        self.assertIn("getElementById('f-q')", page)
        self.assertIn("form.submit()", page)

    def test_92_item_detail_brand_and_price_cells(self):
        """详情页：品牌不再跟着产品名重复；三个价格带底色放大，渔货价按系数自动带出。"""
        batch = self._batch(1)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            item.brand = "小米"
            item.base_price = 100.0
            item.purchase_cost = 60.0
            quote = grading.price_of(s, item.grade, 100.0, 60.0)
            item_id = item.id
            s.commit()

        page = self.client.get(f"/wms/items/{item_id}").text
        self.assertNotIn("品牌型号", page, "这一格以前放的是展示名，跟产品名重复")
        self.assertIn("<b>品牌</b>", page)
        self.assertIn("小米", page)
        self.assertIn('class="price-grid"', page, "三个价格要各占一格（带底色 + 放大数字）")
        self.assertIn('id="p-base"', page)
        self.assertIn('id="p-cost"', page)
        self.assertIn('name="selling_price" value="%.2f"' % float(quote["price"]), page,
                      "渔货价要按「市场价 × 该档系数」自动带出来，别让人自己乘")

        # 原样保存：跟算出来的一样 → 不算人工定价（审计里能区分）
        self.client.post(f"/wms/items/{item_id}/price", data={
            "base_price": "100", "purchase_cost": "60",
            "selling_price": "%.2f" % float(quote["price"])}, follow_redirects=False)
        with SessionLocal() as s:
            self.assertAlmostEqual(float(s.get(WmsItem, item_id).selling_price or 0),
                                   float(quote["price"]), 2)
            self.assertTrue(
                any("按等级系数" in (l.note or "") for l in wms_audit.of_entity(s, "item", item_id))
            )
        # 改成别的数 → 记人工定价
        self.client.post(f"/wms/items/{item_id}/price", data={
            "base_price": "100", "purchase_cost": "60", "selling_price": "88.88"},
            follow_redirects=False)
        with SessionLocal() as s:
            # 人工填价也按整数口径向下取整（价格一律不带小数）
            self.assertAlmostEqual(float(s.get(WmsItem, item_id).selling_price or 0), 88.0, 2)
            self.assertTrue(
                any("人工定价" in (l.note or "") for l in wms_audit.of_entity(s, "item", item_id))
            )

    def test_93_grade_rule_edit_and_no_twin(self):
        """分级规则：能原地编辑（不多出一条、条件真的删得掉）、同档位不许重复、判过货的只能停用。

        现场反馈「电池检查删除没用，保存后条件还在」——根因是以前只有「新增」：
        在新增表单里删条件 = 又建了一条同档规则，老规则（带电池）纹丝不动。
        """
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
            snap = [(r.id, r.category, r.priority, r.grade, r.safety_flag, r.title, r.reason,
                     dict(r.conditions or {}), r.enabled)
                    for r in s.scalars(select(WmsGradeRule).order_by(WmsGradeRule.id)).all()]
            total = len(snap)
            s_rule = next(r for r in s.scalars(select(WmsGradeRule)).all()
                          if r.grade == GRADE_S and not r.safety_flag)
            s_id, s_title, s_pri = s_rule.id, s_rule.title, s_rule.priority
        try:
            # ① 「编辑」把这条规则原样填进表单（优先级 / 条件都是现值，不是空白）
            page = self.client.get(f"/wms/rules?edit={s_id}").text
            self.assertIn(f'name="rule_id" value="{s_id}"', page)
            self.assertIn("编辑分级规则", page)
            self.assertIn(f'value="{s_pri}"', page)
            self.assertIn("all_items_pass", page, "表单要预填这条规则现有的条件")

            # ② 原地改：把「电池≥85」那条删掉，保存后不多出规则、条件真没了
            resp = self.client.post("/wms/rules/grade", data={
                "rule_id": str(s_id), "category": "*", "priority": str(s_pri),
                "grade": GRADE_S, "title": s_title, "enabled": "1",
                "conditions": '{"all_of":[{"field":"all_items_pass","op":"==","value":true},'
                              '{"field":"flaw_count","op":"==","value":0}]}'})
            self.assertIn("已更新", resp.text)
            with SessionLocal() as s:
                self.assertEqual(len(s.scalars(select(WmsGradeRule)).all()), total,
                                 "编辑是原地改，不该多出一条规则")
                row = s.get(WmsGradeRule, s_id)
                self.assertFalse(
                    [c for c in row.conditions["all_of"] if c["field"] == "battery_health"],
                    "电池那条删掉了就该真没了")
            # 引擎立刻按新条件判级：电池只有 70 也判 S（旧条件要 ≥85，判不到）
            resp = self.client.post("/wms/api/inspect/submit", json={
                "item_id": item_id, "results": dict(_PHONE_RESULTS), "battery_health": 70})
            self.assertTrue(resp.json().get("ok"), resp.text)
            self.assertEqual(resp.json().get("final"), GRADE_S, "改了条件就该按新条件判")

            # ③ 同品类 + 同档位再来一条 → 拦下（页面提示去编辑那一条）
            resp = self.client.post("/wms/rules/grade", data={
                "category": "*", "priority": "99", "grade": GRADE_S, "safety_flag": "0",
                "title": "S 想再来一条", "enabled": "1", "conditions": '{"all_of": []}'})
            self.assertIn("已经有一条了", resp.text)
            with SessionLocal() as s:
                self.assertEqual(len(s.scalars(select(WmsGradeRule)).all()), total,
                                 "重复档位不该被建出来")

            # ③b 同档位另有一条「停用」的旧规则：不挡编辑（停用的本来就不判级），但新增照样挡
            with SessionLocal() as s:
                stale = WmsGradeRule(category="*", priority=100, grade=GRADE_S,
                                     safety_flag=False, title="S 旧版（停用）",
                                     conditions={"all_of": []}, enabled=False)
                s.add(stale)
                s.commit()
                stale_id = stale.id
            resp = self.client.post("/wms/rules/grade", data={
                "rule_id": str(s_id), "category": "*", "priority": str(s_pri),
                "grade": GRADE_S, "title": s_title, "enabled": "1",
                "conditions": '{"all_of":[{"field":"all_items_pass","op":"==","value":true},'
                              '{"field":"flaw_count","op":"==","value":0}]}'})
            self.assertIn("已更新", resp.text, "停用的同档规则不该挡着改这条")
            self.assertIn("已停用的", resp.text, "但要提醒一句：同档还留着一条停用的")
            with SessionLocal() as s:      # 把它启用 → 这时候再改就会被挡
                s.get(WmsGradeRule, stale_id).enabled = True
                s.commit()
            resp = self.client.post("/wms/rules/grade", data={
                "rule_id": str(s_id), "category": "*", "priority": str(s_pri),
                "grade": GRADE_S, "title": s_title, "enabled": "1",
                "conditions": '{"all_of": []}'})
            self.assertIn("已经有一条了", resp.text, "两条同档同时启用才是真重复，必须挡")
            with SessionLocal() as s:
                self.assertNotEqual(s.get(WmsGradeRule, s_id).conditions, {"all_of": []})
                s.get(WmsGradeRule, stale_id).enabled = False
                s.delete(s.get(WmsGradeRule, stale_id))
                s.commit()

            # ④ 安全红线现在判 C 报修（2026-10-13），D 只剩「缺件报废 / 严重故障」那路；
            #    两路不同档，不该被当成重复、也不该标「同档还有」
            with SessionLocal() as s:
                safety_grades = {r.grade for r in s.scalars(
                    select(WmsGradeRule).where(WmsGradeRule.safety_flag.is_(True))).all()}
                flags = {r.safety_flag for r in s.scalars(
                    select(WmsGradeRule).where(WmsGradeRule.grade == GRADE_D)).all()}
            self.assertEqual(safety_grades, {GRADE_C}, "安全红线档位应该是 C 报修")
            self.assertEqual(flags, {False}, "D 只剩「缺件报废 / 严重故障」这类普通规则")
            self.assertNotIn("同档还有", self.client.get("/wms/rules").text)

            # ⑤ 删光所有条件 = 兜底档（不是被偷偷补回一条）
            self.client.post("/wms/rules/grade", data={
                "rule_id": str(s_id), "category": "*", "priority": str(s_pri),
                "grade": GRADE_S, "title": s_title, "enabled": "1", "conditions": ""})
            with SessionLocal() as s:
                self.assertEqual(s.get(WmsGradeRule, s_id).conditions, {"all_of": []},
                                 "条件是空的就该是空的 —— 这就是兜底档")
            self.assertIn("还没填条件", self.client.get(f"/wms/rules?edit={s_id}").text)

            # ⑥ 判过货的规则不能删（检测记录里存着 rule_id，删了查不到依据）→ 只能停用
            with SessionLocal() as s:
                used = s.scalars(select(WmsTestRecord)
                                 .where(WmsTestRecord.item_id == item_id)).first().rule_id
            self.assertTrue(used, "这轮检测要记下命中的规则")
            resp = self.client.post(f"/wms/rules/grade/{used}/delete")
            self.assertIn("不能删", resp.text)
            with SessionLocal() as s:
                self.assertIsNotNone(s.get(WmsGradeRule, used))

            # ⑦ 没判过货的能删；删掉的内置规则不会被播种补回来（「删了又回来」也是这次的病根）
            with SessionLocal() as s:
                a_id = s.scalars(select(WmsGradeRule)
                                 .where(WmsGradeRule.grade == GRADE_A)).first().id
            resp = self.client.post(f"/wms/rules/grade/{a_id}/delete")
            self.assertIn("已删除", resp.text)
            with SessionLocal() as s:
                self.assertIsNone(s.get(WmsGradeRule, a_id))
                wms_seed.ensure_defaults(s)      # 保存规则 / 重启服务都会跑它
                s.commit()
                self.assertIsNone(s.get(WmsGradeRule, a_id),
                                  "删掉的规则不该被播种补回来")
                self.assertEqual(len(s.scalars(select(WmsGradeRule)).all()), total - 1)
        finally:
            # 规则表是测试之间共享的（_wipe 不动它）—— 实验完按快照恢复原样
            with SessionLocal() as s:
                for r in s.scalars(select(WmsGradeRule)).all():
                    s.delete(r)
                s.flush()
                for rid, cat, pri, grade, safety, title, reason, cond, enabled in snap:
                    s.add(WmsGradeRule(id=rid, category=cat, priority=pri, grade=grade,
                                       safety_flag=safety, title=title, reason=reason,
                                       conditions=cond, enabled=enabled))
                s.commit()

    def test_94_items_list_seq_and_product_name(self):
        """库存台账：行首加自然数序号；「品牌型号」那一列换成「产品名称」。"""
        batch = self._batch(2)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            items[0].product_name = "测试产品名 甲"
            items[1].product_name = ""      # 没产品名的退回品牌 + 型号，别留空格子
            s.commit()
        page = self.client.get("/wms/items").text
        self.assertIn("产品名称", page)
        self.assertNotIn("品牌型号", page, "表头不该再叫品牌型号")
        self.assertIn("测试产品名 甲", page)
        self.assertIn("Apple iPhone 13", page, "没产品名的那台退回「品牌 + 型号 + 规格」")
        # 序号是这一页从上往下数的自然数：1、2 …
        self.assertIn('<td class="num muted">1</td>', page)
        self.assertIn('<td class="num muted">2</td>', page)

    def test_95_manual_price_only_upwards(self):
        """引擎建议处的改价：只许往上调，低于「低于基准价此比例需审批」那条线直接拦下。

        以前人工填多低都给存，只在提示里加一句「需审批」—— 等于没拦（2026-10-10）。
        """
        batch = self._batch(1, base_price=100.0)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
            rule = grading.price_rule_of(s, GRADE_A)
            self.assertIsNotNone(rule, "A 档要有一条启用中的价格系数")
            floor = round(100.0 * float(rule.need_approval_below), 2)
            auto = round(100.0 * float(rule.factor), 2)
        self.assertGreater(floor, 0, "这条系数得配了审批比例才谈得上拦")

        # ① 低于审批线：提交被拦下，价一个字都不写
        # （2026-10-13 起「外观瑕疵描述」输入框撤了，A 档的「差在哪」直接取外观两项
        #   判 × 时填的不合格原因 —— 这里给外观项带一条原因就够）
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "fail_descs": {"appear": "外壳轻微划痕（测试）"},
            "final_grade": "A", "override_reason": "测试定档",
            "manual_price": round(floor - 10, 2)})
        self.assertFalse(resp.json().get("ok"))
        self.assertIn("只能往上调", resp.json().get("message") or "")
        self.assertIn("审批线", resp.json().get("message") or "")
        with SessionLocal() as s:
            self.assertEqual(float(s.get(WmsItem, item_id).selling_price or 0), 0.0,
                             "被拦下的价不许写进货品")

        # ② 往上调（高于系统默认价）：照人填的写
        want = round(auto + 5, 2)
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": dict(_PHONE_RESULTS),
            "final_grade": "A", "override_reason": "测试定档",
            "flaw_desc": "外壳轻微划痕（测试）", "manual_price": want})
        self.assertTrue(resp.json().get("ok"), resp.text)
        with SessionLocal() as s:
            self.assertAlmostEqual(float(s.get(WmsItem, item_id).selling_price or 0), want,
                                   places=2)

        # ③ 货品详情页改价同一把尺子：低于审批线也不让存
        resp = self.client.post(f"/wms/items/{item_id}/price", data={
            "base_price": "100", "purchase_cost": "50",
            "selling_price": str(round(floor - 1, 2))})
        self.assertIn("只能往上调", resp.text)
        with SessionLocal() as s:
            self.assertAlmostEqual(float(s.get(WmsItem, item_id).selling_price or 0), want,
                                   places=2, msg="被拦下的改价不该落库")

    def test_96_grade_n_new_unused(self):
        """N 全新（2026-10-10）：原封未拆 / 拆封未使用 → 判 N，N 与 S/A 一样直接上架出价。"""
        self.assertIn(GRADE_N, GRADE_CHOICES)
        self.assertIn(GRADE_N, SELLABLE_GRADES)
        self.assertEqual(GRADE_ROUTE[GRADE_N][1], STATUS_SELLABLE)
        # 新库首次初始化就带这一档：播种里得有 N 的规则与系数
        self.assertTrue(any(r["grade"] == GRADE_N for r in wms_seed.GRADE_RULE_SEED))
        self.assertTrue(any(r["grade"] == GRADE_N for r in wms_seed.PRICE_RULE_SEED))

        # 老库（seed 不补规则）里自己建一条，跑一遍端到端
        made: list = []
        try:
            with SessionLocal() as s:
                if not s.scalar(select(WmsGradeRule).where(WmsGradeRule.grade == GRADE_N)):
                    row = WmsGradeRule(
                        category="*", priority=5, grade=GRADE_N, title="N 全新（测试）",
                        reason="原封未拆或拆封未使用",
                        conditions={
                            "all_of": [{"field": "all_items_pass", "op": "==", "value": True}],
                            "any_of": [{"field": "sealed", "op": "==", "value": True},
                                       {"field": "opened_unused", "op": "==", "value": True}],
                        })
                    s.add(row)
                    s.flush()
                    made.append(("rule", row.id))
                if not s.scalar(select(WmsPriceRule).where(WmsPriceRule.grade == GRADE_N)):
                    pr = WmsPriceRule(grade=GRADE_N, factor=1.05, min_margin=0.15,
                                      need_approval_below=0.9)
                    s.add(pr)
                    s.flush()
                    made.append(("price", pr.id))
                s.commit()

            batch = self._batch(1, base_price=200.0)
            with SessionLocal() as s:
                item_id = intake.list_items(s, batch_id=batch.id)[0].id
            page = self.client.get("/wms/inspect").text
            self.assertIn('id="sealed"', page, "检测台要有「原封未拆」这个勾")
            self.assertIn('id="opened"', page, "检测台要有「拆封未使用」这个勾")

            # ① 勾「原封未拆」→ 引擎建议 N
            resp = self.client.post("/wms/api/inspect/preview", json={
                "item_id": item_id, "results": dict(_PHONE_RESULTS), "sealed": True})
            self.assertEqual(resp.json().get("suggest_grade"), "N", resp.text)
            # ② 勾「拆封未使用」也算 N
            resp = self.client.post("/wms/api/inspect/preview", json={
                "item_id": item_id, "results": dict(_PHONE_RESULTS), "opened_unused": True})
            self.assertEqual(resp.json().get("suggest_grade"), "N")
            # ③ 两个都不勾 → 不判 N（别把用过的货判成全新）
            resp = self.client.post("/wms/api/inspect/preview", json={
                "item_id": item_id, "results": dict(_PHONE_RESULTS)})
            self.assertNotEqual(resp.json().get("suggest_grade"), "N")

            # ④ 提交：判 N + 按 N 档系数写渔货价（N 是写价档，跟 S / A 一样）
            resp = self.client.post("/wms/api/inspect/submit", json={
                "item_id": item_id, "results": dict(_PHONE_RESULTS), "sealed": True})
            self.assertTrue(resp.json().get("ok"), resp.text)
            with SessionLocal() as s:
                row = s.get(WmsItem, item_id)
                self.assertEqual(str(row.grade), "N")
                self.assertAlmostEqual(float(row.selling_price or 0), 210.0, places=2)
                self.assertIn("原封未拆", str(row.grade_reason or ""))
        finally:
            with SessionLocal() as s:      # 规则表是测试之间共享的，用完收回
                for kind, rid in made:
                    obj = s.get(WmsGradeRule if kind == "rule" else WmsPriceRule, rid)
                    if obj:
                        s.delete(obj)
                s.commit()


    def test_97_inspect_new_zone_and_manual_price(self):
        """检测台改版（2026-10-10 二次调整）：全新判定区提到检查项上面、检修判定独立成区、
        勾「全新」检查项自动全部通过、不合格原因不再必填、人工改价不再被刷新盖回系统默认。"""
        page = self.client.get("/wms/inspect").text
        # 三个区从上到下：全新判定 → 检查项目 → 检修判定
        i_new = page.index('id="zoneNew"')
        i_items = page.index('<div id="items">')
        i_fix = page.index('class="zone zone-fix"')
        self.assertLess(i_new, i_items, "「全新判定」要在检查项目上面")
        self.assertLess(i_items, i_fix, "「检修判定」留在检查项目下面")
        # 大勾选框 + 整块可点（现场戴手套也点得准）
        self.assertIn('class="big-check"', page)
        self.assertIn('class="bc-title"', page)
        self.assertIn('.big-check input { width:22px', page)
        # 勾「全新」→ 检查项默认全部通过；取消 → 清回未判
        self.assertIn("function setAllPass(", page)
        self.assertIn("setAllPass($('sealed').checked || $('opened').checked);", page)
        self.assertIn('id="autoPassNote"', page)
        # 不合格原因（2026-10-13 用户拍板：改回必填 + 红框提示，整行输入框）
        self.assertIn('不合格原因（必填', page)
        self.assertIn('.fail-desc.err', page)
        self.assertNotIn('不合格原因（选填）', page)
        # 人工改价：refresh 末尾要保住人填的价，且把该档下限价写在框下面
        # 改价填了不能被 refresh 抹掉（2026-10-11：B/C/D 也能人工定价后，不再按档位清空填过的价）
        self.assertIn("if ($('manualPrice').value) { reflectPrice(); }", page)
        self.assertNotIn("$('manualPrice').value = '';        // 换了档位", page)
        self.assertIn('id="floorHint"', page)
        self.assertIn('最低可填', page)
        self.assertIn('低于下限价，提交会被拦下', page)
        # 外观瑕疵描述已撤（2026-10-13 用户拍板：判 A 的「差在哪」直接取外观两项的
        # 不合格原因，不再单独填一遍）；电池健康度已下线（2026-10-11）
        self.assertNotIn('id="flaw"', page)
        self.assertNotIn('外观瑕疵描述', page)
        # 建议 C 报修：检修判定区高亮 + 必须选维修方式（2026-10-13 用户拍板）；
        # 建议 D 报废：自动勾上「缺件严重 / 无维修价值」（同一轮拍板）
        self.assertIn('id="repairHint"', page)
        self.assertIn("suggestGradeNow() === 'C'", page)
        self.assertIn("=== 'D' && !missing.checked && !missing.dataset.user", page)
        self.assertNotIn('id="battery"', page)
        self.assertNotIn("电池健康", page)

    def test_98_new_tick_auto_pass_all_items(self):
        """勾「原封未拆」：提交时 14 项没逐项点也算全过（前端自动全判通过，后端收得下）。"""
        batch = self._batch(1, base_price=1000.0)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        results = {k: "pass" for k in _PHONE_RESULTS}
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": results, "sealed": True,
            "battery_health": 95, "flaw_desc": ""})
        self.assertTrue(resp.json().get("ok"), resp.text)
        with SessionLocal() as s:
            self.assertIn(str(s.get(WmsItem, item_id).grade), ("N", "S"))

    # ================================================ 2026-10-11 现场四条
    def test_99_items_page_pager_reaches_every_row(self):
        """库存台账能翻页（2026-10-11 用户：「只有 200 条记录，我们有 867 个商品」）。

        老写法 `limit=PAGE_SIZE*4` 一刀切 200 条、没有下一页 —— 后面的货根本翻不到，
        而且标题里的「共 N 台」写的是当前页行数（自己骗自己）。现在按 50 条一页真分页。
        """
        batch = self._batch(60)
        with SessionLocal() as s:
            total = len(intake.list_items(s, batch_id=batch.id, limit=500))
        self.assertEqual(total, 60)

        page1 = self.client.get("/wms/items").text
        self.assertIn(f"共 {total} 台", page1, "标题要写全库台数，不是当前页行数")
        self.assertEqual(page1.count('class="num muted"'), 50, "第一页 50 行")
        self.assertIn("下一页", page1)
        self.assertIn("page=2", page1, "翻页链接要指到第 2 页")
        # 统一分页组件（2026-10-16 换 _pager.html）：非空筛选条件要带着走
        graded = self.client.get("/wms/items", params={"grade": "W"}).text
        self.assertIn("grade=W", graded, "翻页要把筛选条件带着走（别把筛掉的货翻回来）")

        page2 = self.client.get("/wms/items?page=2").text
        self.assertEqual(page2.count('class="num muted"'), total - 50, "第二页剩 10 行")
        self.assertIn(f">{50 + 1}<", page2, "序号接着全库往下数（第二页从 51 起）")
        self.assertIn("上一页", page2)

        # 翻过头 / 乱填：钳到最后一页，别给空列表（现场会以为货丢了）
        last = self.client.get("/wms/items?page=99").text
        self.assertEqual(last.count('class="num muted"'), total - 50)

        # 筛选后按筛选结果分页
        one = self.client.get("/wms/items?q=SN0001").text
        self.assertIn("共 1 台", one)

    def test_100_manual_price_on_repair_grades(self):
        """B / C / D 也能人工定价（2026-10-11 用户：「价格支持修改」）。

        系统默认还是不给这几档写价（修完重判 / 逐件估值），但检测员填了就得按人填的落库，
        并记一条审计；低于审批线照样拦（与 N / S / A 同一把尺子）。
        """
        batch = self._batch(1, base_price=1000.0)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        # 「不影响使用的缺陷（面板）+ 厂内可修」= 引擎建议 B
        # （S/A 要求全项通过轮不到；真故障现在判 C，不再是兜底 B）
        results = dict(_PHONE_RESULTS)
        results["panel"] = TEST_FAIL
        fails = {"panel": "面板有划痕"}
        prev = self.client.post("/wms/api/inspect/preview", json={
            "item_id": item_id, "results": results, "fail_descs": fails,
            "inhouse_fixable": True})
        self.assertTrue(prev.json().get("ok"), prev.text)
        self.assertEqual(prev.json().get("suggest_grade"), "B")
        p = prev.json()["price"]
        self.assertTrue(p.get("priced"), "2026-10-13 起 B 档照常写价（缺件/微瑕可卖）")
        floor = float(p.get("floor") or 0)
        ceil = float(p.get("suggest_price") or 0)
        manual = round(ceil * 0.9, 2) if ceil else 500.0
        if floor and manual < floor:
            manual = round(floor, 2)

        # ① 低于审批线：拦下（只许往上调，与写价档一个口径）
        if floor > 0:
            resp = self.client.post("/wms/api/inspect/submit", json={
                "item_id": item_id, "results": results, "fail_descs": fails,
                "inhouse_fixable": True, "manual_price": round(floor - 1, 2)})
            self.assertFalse(resp.json().get("ok"), resp.text)
            self.assertIn("审批线", resp.json().get("message", ""))
            with SessionLocal() as s:
                self.assertAlmostEqual(
                    float(s.get(WmsItem, item_id).selling_price or 0), 0.0, places=2,
                    msg="被拦下的提交不该把价写进去")

        # ② 填了合法价：按人填的落库 + 记审计
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": results, "fail_descs": fails,
            "inhouse_fixable": True, "manual_price": manual})
        self.assertTrue(resp.json().get("ok"), resp.text)
        with SessionLocal() as s:
            row = s.get(WmsItem, item_id)
            self.assertEqual(str(row.grade), "B")
            self.assertAlmostEqual(float(row.selling_price or 0), float(manual), places=2)
            self.assertTrue(
                any("人工改价" in (l.note or "")
                    for l in wms_audit.of_entity(s, "item", item_id)),
                "人工定价要留审计：谁把 B 档定成了多少钱",
            )

        # ③ 不填：按系统默认落价（2026-10-13 起 B 档照常写价：基准价 ×0.30）
        batch2 = self._batch(1, base_price=1000.0)
        with SessionLocal() as s:
            item2 = intake.list_items(s, batch_id=batch2.id)[0].id
        resp = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item2, "results": results, "fail_descs": fails,
            "inhouse_fixable": True})
        self.assertTrue(resp.json().get("ok"), resp.text)
        with SessionLocal() as s:
            self.assertAlmostEqual(
                float(s.get(WmsItem, item2).selling_price or 0), 300.0, places=2,
                msg="B 档系统默认价 = 基准价 × 0.30")

    def test_101_photo_multi_upload_and_delete(self):
        """检测台拍照：能一次选多张、也能删单张（2026-10-11 用户要求）。"""
        page = self.client.get("/wms/inspect").text
        self.assertIn('id="photoInput"', page)
        self.assertIn('multiple', page)
        # 带 capture 的手机只会开相机、退回一张 —— 要选多张就不能带
        self.assertNotIn('capture="environment"', page)
        for bit in ("function renderPhotos(", "ph-del", 'id="photoCount"', "全部清空"):
            self.assertIn(bit, page)
        # 每张图一个删除按钮（绑在图上），点一下只从这一轮里去掉
        self.assertIn("PHOTOS.splice(i, 1)", page)
        self.assertIn("e.target.value = ''", page, "选完清空 → 同一张还能再选一次")

    def test_102_uncertain_highlight_and_picker_fit_viewport(self):
        """判「?」存疑要显眼；库位下拉不能再被裁掉（2026-10-11 用户两条反馈）。"""
        page = self.client.get("/wms/inspect").text
        # ① 存疑：橙色高亮卡片 + 说明「不写等级、不进可售仓」，就贴在提交按钮上面
        self.assertIn("ask-check", page)
        self.assertIn("判「?」存疑 —— 进复检池，不硬判", page)
        self.assertIn("不写等级、不进可售仓", page)
        self.assertLess(page.index('id="uncertainBox"'), page.index('id="submitBtn"'))
        # ② 库位下拉：宽度给足 + 由 JS 量着视口定位（老写法窄屏会被裁掉）
        # 2026-10-15：宽度那条规则随设计系统搬到了外部样式表，这里查样式表 + 确认页面引了它
        css = pathlib.Path(__file__).resolve().parents[1].joinpath(
            "app", "static", "wms_design.css").read_text(encoding="utf-8")
        self.assertIn("/static/wms_design.css", page, "页面要引这张外部样式表")
        self.assertIn("width:max(100%,480px)", css, "下拉宽度由这一条保证")
        self.assertIn("LIST_MIN_W", page)
        self.assertIn("function place()", page)
        self.assertIn('role="option" title="', page, "每行给全路径 title：再长也能看全")

    def test_103_location_short_code_legend(self):
        """库位字段旁边那个「?」要说清短码第 1 位 1~N 各是哪个仓（2026-10-11 用户要求）。

        短码（如 ``2-1-3``）第 1 位是「第几个仓」——不给对照表，现场只能靠猜，
        短码就只有一半好用。序号口径必须和 ``_sibling_index`` 一致（**停用的仓也占号**），
        否则说明里写「3 = 维修仓」而短码里的 3 指的是另一个仓，比不给说明还糟。
        """
        with SessionLocal() as s:
            roots = wms_warehouses.root_index(s)
        self.assertGreaterEqual(len(roots), 7, "内置 7 个仓都要在对照表里")
        self.assertEqual([r["n"] for r in roots], list(range(1, len(roots) + 1)), "序号连续")
        self.assertTrue(all(r["name"] for r in roots))
        self.assertIn("待检仓", [r["name"] for r in roots])

        # 对照表里的号必须就是短码里的号（同一个 _sibling_index 口径）
        self.client.post("/wms/warehouses", data={
            "level": "2", "parent_code": "SALE", "code": "SALE-QD", "name": "D 区", "enabled": "1"})
        self.client.post("/wms/warehouses", data={
            "level": "3", "parent_code": "SALE-QD", "name": "09 号货架", "enabled": "1"})
        with SessionLocal() as s:
            shelf = [o for o in wms_warehouses.location_options(s)
                     if o["code"] == "SALE-QD-01"][0]
            sale_n = [r["n"] for r in wms_warehouses.root_index(s) if r["code"] == "SALE"][0]
        self.assertEqual(shelf["short"].split("-")[0], str(sale_n),
                         "说明里的号 = 短码里的号（别各说各的）")

        page = self.client.get("/wms/inspect").text
        self.assertIn('class="lp-legend"', page, "库位字段旁边要有「?」")
        self.assertIn("短码第 1 位 = 第几个仓", page)
        for r in roots:
            self.assertIn(f'<b>{r["n"]}</b>{r["name"]}', page,
                          f'第 {r["n"]} 个仓是 {r["name"]}（页面要写出来）')
        self.assertIn("区 › 货架 › 层 › 位", page, "后面几位依次是什么也要说清")

        # 移库表单里那个「库位」字段同样要给（不止检测台一处）
        batch = self._batch(1)
        with SessionLocal() as s:
            item_id = intake.list_items(s, batch_id=batch.id)[0].id
        self.assertIn('class="lp-legend"', self.client.get(f"/wms/items/{item_id}").text)


if __name__ == "__main__":
    unittest.main()
