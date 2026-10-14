"""渔集（检测仓）「批次 → Excel 导入建档 → 扫货号检测」回归（2026-10-08）。

用户拍板的三条口径：

  1. **建批次的时候不知道这批货是什么** —— 吃进来的货是「某一家的某一批」，什么类目都有、
     品牌型号规格这时候定不了。所以批次可以勾「类目混杂」，品类按导入表格的每行定；
  2. **Excel 导入建档，表格里的字段沉淀成商品字段** —— 认得出的是货品真列
     （货号 / 品牌 / 型号 / 规格 …），认不出的列登记进 wms_item_fields，
     值存 wms_items.extra，之后在「商品字段」页改名 / 停用 / 排序；
  3. **货号是命根子** —— 扫码枪扫供应商的货号就能把这台货的档案带出来，
     检测完顺手填货架位置（检完上架是一个动作，不该让人再跑一趟台账页）。
"""
from __future__ import annotations

import io
import re
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
from wms import importer, inspection, intake  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms import warehouses as wms_warehouses  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    TEST_PASS,
    WmsBatch,
    WmsImportLog,
    WmsItem,
    WmsItemField,
    WmsStockLedger,
    WmsTestItem,
    WmsTestRecord,
    WmsWarehouse,
)

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsTestItem).delete(synchronize_session=False)
        s.query(WmsTestRecord).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsImportLog).delete(synchronize_session=False)
        s.query(WmsItemField).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


def _make_location(code: str = "A-Q03-L2", name: str = "A 区-03 架-2 层") -> str:
    """备一个库位：2026-10-08 起库位只能从「仓库与货架」维护表里选。"""
    with SessionLocal() as s:
        s.query(WmsWarehouse).filter(WmsWarehouse.level > 1).delete(synchronize_session=False)
        s.commit()
        wms_warehouses.save(s, level=2, parent_code="SALE", code="A-Q03", name="A 区-03 架")
        wms_warehouses.save(s, level=3, parent_code="A-Q03", code=code, name=name)
    return code


def _xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 造一张表（第一行是表头）。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for line in rows:
        ws.append(line)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 供应商清单：货号 / 品牌 / 型号 / 规格 / 品类 / 成本 / 基准价 / 数量 + 一列多余的「颜色」
_SHEET = [
    ["货号", "品牌", "型号", "规格", "品类", "采购价", "基准价", "数量", "颜色"],
    ["A-1001", "美的", "MB-FB30", "3L", "电饭煲", 88, 199, 2, "白色"],
    ["A-1002", "海能达", "PD980", "黑色", "对讲机", 260, 520, 1, "黑色"],
]


class WmsImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self) -> None:
        _wipe()

    def _mixed_batch(self, qty: int = 10) -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"category": "HA", "mixed": "1", "source_type": "purchase",
                 "qty_expected": qty, "supplier_name": "测试供应商"},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    # ------------------------------------------------------------ 批次
    def test_10_mixed_batch(self):
        """建批次不问品类（2026-10-08 拍板）：品类一律按导入清单每行定。"""
        batch = self._mixed_batch()
        self.assertTrue(batch.mixed, "品类按清单每行定要记在批次上")
        self.assertEqual(batch.batch_no[:2], "26"[:2] or batch.batch_no[:2])
        page = self.client.get("/wms/batches")
        self.assertEqual(page.status_code, 200)
        self.assertIn("按清单行定", page.text, "列表里要看得出来品类按清单行定")
        self.assertNotIn("品类（三级）", page.text, "新建批次表单不再有品类选择器")

    def test_15_import_pages(self):
        """导入页与模板下载都在。"""
        batch = self._mixed_batch()
        page = self.client.get(f"/wms/batches/{batch.id}/import")
        self.assertEqual(page.status_code, 200)
        self.assertIn("上传货品清单", page.text)
        tpl = self.client.get(f"/wms/batches/{batch.id}/import/template")
        self.assertEqual(tpl.status_code, 200)
        self.assertTrue(tpl.content[:2] == b"PK", "模板应是 xlsx")

    # ------------------------------------------------------------ 认表头
    def test_20_guess_mapping(self):
        """表头自动识别：货号 / 品牌 / 型号 / 品类 / 数量都认出来，多余的列留空。"""
        headers = _SHEET[0]
        mapping = importer.guess_mapping(headers)
        got = {headers[i]: v for i, v in mapping.items()}
        self.assertEqual(got.get("货号"), "sku_code")
        self.assertEqual(got.get("品牌"), "brand")
        self.assertEqual(got.get("型号"), "model")
        self.assertEqual(got.get("规格"), "spec")
        self.assertEqual(got.get("品类"), "category")
        self.assertEqual(got.get("采购价"), "purchase_cost")
        self.assertEqual(got.get("基准价"), "base_price")
        self.assertEqual(got.get("数量"), "qty")
        self.assertIsNone(got.get("颜色"), "认不出的列不该硬塞")

    def test_25_match_category(self):
        """品类认中文名（电饭煲 / 对讲机），也认三级码。"""
        self.assertEqual(importer.match_category("电饭煲"), "HA.KC.DFB")
        self.assertEqual(importer.match_category("HA.KC.DFB"), "HA.KC.DFB")
        self.assertEqual(importer.match_category("不存在的品类"), "")

    def test_30_preview(self):
        """上传后给的是「确认映射」页：列 + 示例值 + 沉淀成字段的选项。"""
        batch = self._mixed_batch()
        resp = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(_SHEET), "application/octet-stream")},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("确认映射", resp.text)
        self.assertIn("沉淀为商品字段", resp.text)
        self.assertIn("A-1001", resp.text, "预览要能看到前几行的真实内容")
        self.assertIn("token", resp.text)

    # ------------------------------------------------------------ 执行导入
    def _commit(self, batch_id: int, sheet=None, extra_cols=(8,), strict: bool = False):
        """走一遍完整流程：上传 → 预览 → 按映射提交。

        ``strict=True`` 勾「严格模式」：任一行不合法就整批不导（默认是跳过坏行、导其余）。
        """
        sheet = sheet or _SHEET
        headers = sheet[0]
        up = self.client.post(
            f"/wms/batches/{batch_id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        )
        token = up.text.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(headers)
        form = {"token": token, "file_name": "清单.xlsx"}
        if strict:
            form["strict_mode"] = "1"
        for idx, key in mapping.items():
            form[f"map_{idx}"] = key
        for idx in extra_cols:
            form[f"map_{idx}"] = "__extra__"
            form[f"key_{idx}"] = importer.slugify(headers[idx], set())
            form[f"type_{idx}"] = "text"
        return self.client.post(f"/wms/batches/{batch_id}/import/commit", data=form,
                                follow_redirects=False)

    def test_40_import_commit(self):
        """导入即建档：货号入库、每行的品类各写各的、**数量记在这一条货上**（不再拆成多条）。"""
        batch = self._mixed_batch()
        resp = self._commit(batch.id)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            # 数量 2 = 一条货 qty=2、同一个条码（2026-10-10 口径），不是两台两个码
            self.assertEqual(len(items), 2, "A-1001 一条（数量 2）+ A-1002 一条")
            by_sku: dict[str, list] = {}
            for it in items:
                by_sku.setdefault(it.sku_code, []).append(it)
            self.assertEqual(sorted(by_sku), ["A-1001", "A-1002"])
            self.assertEqual(len(by_sku["A-1001"]), 1, "数量不再拆成多条")
            self.assertEqual(int(by_sku["A-1001"][0].qty or 0), 2, "数量 2 记在这条货上")
            self.assertEqual(int(by_sku["A-1002"][0].qty or 0), 1)
            # 批次台数按数量累加（不是按条数）
            self.assertEqual(int(s.get(WmsBatch, batch.id).qty_actual or 0), 3)
            # 混合批次：品类按行走（电饭煲 HA.KC.DFB / 对讲机 → 没细分，落 PH.DJ.QT）
            self.assertEqual(by_sku["A-1001"][0].category, "HA.KC.DFB")
            self.assertEqual(by_sku["A-1002"][0].category, "PH.DJ.QT")
            self.assertTrue(by_sku["A-1001"][0].barcode.startswith("QCHA"))
            self.assertTrue(by_sku["A-1002"][0].barcode.startswith("QCPH"))
            # 沉淀的字段：值跟着货品存
            self.assertEqual(by_sku["A-1001"][0].extra.get("颜色"), "白色")
            self.assertEqual(by_sku["A-1002"][0].extra.get("颜色"), "黑色")
            self.assertEqual(by_sku["A-1001"][0].purchase_cost, 88.0)
            self.assertEqual(by_sku["A-1002"][0].base_price, 520.0)
            # 入库流水：每台一条（账从导入这一刻开始）
            ids = [it.id for it in items]
            ledgers = s.scalars(
                select(WmsStockLedger).where(WmsStockLedger.item_id.in_(ids))
            ).all()
            self.assertEqual(len(ledgers), 2, "一条货一条入库流水（数量写在流水上）")
            # 数量 2 的那条：入库流水 qty_delta = 2（余额按台数走）
            qty_by_item = {row.item_id: int(row.qty_delta or 0) for row in ledgers}
            self.assertEqual(qty_by_item[by_sku["A-1001"][0].id], 2)
            self.assertEqual(qty_by_item[by_sku["A-1002"][0].id], 1)
            # 导入记录
            log = s.scalars(select(WmsImportLog).where(
                WmsImportLog.batch_id == batch.id)).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.row_count, 2)
            self.assertEqual(log.ok_count, 2)
            self.assertIn("颜色", log.fields)

    def test_45_fields_settled(self):
        """认不出的列沉淀成商品字段，之后能在「商品字段」页改名。"""
        batch = self._mixed_batch()
        self._commit(batch.id)
        with SessionLocal() as s:
            row = s.scalar(select(WmsItemField).where(WmsItemField.key == "颜色"))
            self.assertIsNotNone(row, "沉淀的列要登记进 wms_item_fields")
            self.assertEqual(row.label, "颜色")
        page = self.client.get("/wms/fields")
        self.assertEqual(page.status_code, 200)
        self.assertIn("颜色", page.text)
        # 改名：改的是显示名，值照旧（按标识存）
        resp = self.client.post("/wms/fields", data={
            "field_id": str(row.id) if row else "0", "label": "机身颜色",
            "field_type": "text", "sort": "100", "enabled": "1",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItemField, row.id).label, "机身颜色")
            item = s.scalars(select(WmsItem).where(WmsItem.sku_code == "A-1001")).first()
            self.assertEqual(item.extra.get("颜色"), "白色", "改名不动已存的值")

    def test_50_import_without_sku(self):
        """没有货号列不许导 —— 货号是扫码认货的凭据。"""
        batch = self._mixed_batch()
        sheet = [["品牌", "型号"], ["美的", "MB-FB30"]]
        up = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        )
        token = up.text.split('name="token" value="')[1].split('"')[0]
        resp = self.client.post(
            f"/wms/batches/{batch.id}/import/commit",
            data={"token": token, "file_name": "x.xlsx"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 0)

    def test_55_duplicate_sku(self):
        """同一张表里货号重复：**严格模式下**整批不导（导一半的库存更难收拾）。"""
        batch = self._mixed_batch()
        sheet = [
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["A-1", "美的", "电饭煲"],
        ]
        resp = self._commit(batch.id, sheet=sheet, extra_cols=(), strict=True)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 0, "整批回滚")

    # ------------------------------------------------------------ 扫货号
    def test_60_scan_by_sku(self):
        """扫码枪扫货号：把这台货的档案带出来（含沉淀字段）。"""
        batch = self._mixed_batch()
        self._commit(batch.id)
        data = self.client.get("/wms/api/item.json?code=A-1002").json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["item"]["sku_code"], "A-1002")
        self.assertEqual(data["item"]["model"], "PD980")
        # 「对讲机」没说模拟 / 公网，归到这个二级的兜底叶
        self.assertEqual(data["item"]["category_label"].split(" › ")[1], "对讲机")
        self.assertEqual(data["item"]["extra"].get("颜色"), "黑色")
        # 同货号：数量 2 记在这一条货上，不再拆成两台（扫货号直接就是这一条）
        first = self.client.get("/wms/api/item.json?code=A-1001").json()
        self.assertEqual(first["item"]["qty"], 2, "数量带进检测台")
        self.assertEqual(len(first["item"]["twins"]), 0, "不再生成同货号的多条")

    def test_65_inspect_by_sku(self):
        """检测台扫货号进来能载入，提交时把货架位置一起写上。"""
        batch = self._mixed_batch()
        self._commit(batch.id)
        page = self.client.get("/wms/inspect?code=A-1001")
        self.assertEqual(page.status_code, 200)
        self.assertIn("A-1001", page.text)
        with SessionLocal() as s:
            item = s.scalars(select(WmsItem).where(WmsItem.sku_code == "A-1001")).first()
            template = [k for k, _label in inspection.full_template(item.category)]
            results = {k: TEST_PASS for k in template}
        payload = {
            "item_id": item.id, "results": results, "fail_descs": {},
            "battery_health": 90, "flaw_desc": "", "photos": [],
            "final_grade": "", "override_reason": "", "uncertain": False,
            "inhouse_fixable": False, "must_official": False, "missing_parts": False,
            "note": "", "location_code": _make_location(),
        }
        resp = self.client.post("/wms/api/inspect/submit", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["location"], "A-Q03-L2")
        with SessionLocal() as s:
            saved = s.get(WmsItem, item.id)
            self.assertEqual(saved.location_code, "A-Q03-L2")
            self.assertNotEqual(saved.grade, "W", "检测完要定级")


# ---------------------------------------------------------------- 供应商清单口径
# 《河南仓库第一批清单》（京东退货仓）的字段口径 —— 建档字段以它为准（2026-10-08 用户拍板）
_MANIFEST = [
    ["货号", "下单时间", "京东订单号", "品牌", "产品", "一级分类", "二级分类", "三级分类",
     "下单价格", "故障原因", "产品状态", "售后状态", "测试说明", "建议折扣", "当前状态"],
    ["8617665947744253", "2025-08-01 00:19:15", "319630835159", "乖巧妻 GQQI",
     "乖巧妻 GQQI 德国品牌无渣破壁机加热免过滤", "家用电器", "厨房小电", "破壁机",
     "389", "不运行，不加热", "完好", "", "开机先转两秒属正常", "", ""],
    ["8717721965357864", "2026-02-05 15:26:39", "3393251007651646", "小贝熊",
     "小贝熊宝宝恒温辅食碗 300ml", "母婴", "喂养用品", "儿童餐具",
     "159", "按键不稳定", "完好", "", "", "", ""],
]


class _WmsCase(unittest.TestCase):
    """公共基座：登录 + 建批次（建批次不问品类，品类按清单每行定）。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self) -> None:
        _wipe()

    def _batch(self, qty: int = 10) -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"source_type": "purchase", "qty_expected": qty, "supplier_name": "京东退货仓"},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def _register(self, batch_id: int, **kw) -> WmsItem:
        with SessionLocal() as s:
            rows = [{"sku_code": "T-1", "product_name": "测试货", **kw}]
            item = intake.register(s, batch_id, rows, operator="tester")[0]
            return s.get(WmsItem, item.id)

    def _commit(self, batch_id: int, sheet=None, strict: bool = False):
        """上传 → 预览 → 提交：认不出的列一律沉淀成商品字段。

        ``strict=True`` 勾「严格模式」：任一行不合法就整批不导（默认跳过坏行、导其余）。
        """
        sheet = sheet or _MANIFEST
        headers = sheet[0]
        up = self.client.post(
            f"/wms/batches/{batch_id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        )
        token = up.text.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(headers)
        form: dict = {"token": token, "file_name": "清单.xlsx"}
        if strict:
            form["strict_mode"] = "1"
        for idx, head in enumerate(headers):
            if not str(head or "").strip():
                continue
            key = mapping.get(idx)
            if key:
                form[f"map_{idx}"] = key
            else:
                form[f"map_{idx}"] = "__extra__"
                form[f"key_{idx}"] = importer.slugify(head, set())
                form[f"type_{idx}"] = "text"
        return self.client.post(f"/wms/batches/{batch_id}/import/commit", data=form,
                                follow_redirects=False)


class ManifestFieldsTest(_WmsCase):
    """建档字段按供应商清单口径整理 + 建批次不再选品类。"""

    def test_10_batch_asks_no_category(self):
        """建批次不问品类：表单里没有品类选择器，批次落库也不带品类。"""
        batch = self._batch()
        self.assertTrue(batch.mixed)
        self.assertFalse(batch.category, "建批次不再定品类")
        page = self.client.get("/wms/batches")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("品类（三级）", page.text, "新建批次表单不再有品类三级选择器")
        self.assertNotIn('name="mixed"', page.text, "不该再问「这一批的类目」")
        self.assertIn("不问品类", page.text)
        self.assertIn("按清单行定", page.text, "批次列表要看得出来品类按清单行定")

    def test_20_manifest_headers(self):
        """清单表头认列：货号 / 产品 / 品牌 / 三级分类三列 / 下单价格；过程列留给沉淀。"""
        headers = _MANIFEST[0]
        got = {headers[i]: v for i, v in importer.guess_mapping(headers).items()}
        self.assertEqual(got.get("货号"), "sku_code")
        self.assertEqual(got.get("产品"), "product_name")
        self.assertEqual(got.get("品牌"), "brand")
        self.assertEqual(got.get("一级分类"), "category_l1")
        self.assertEqual(got.get("二级分类"), "category_l2")
        self.assertEqual(got.get("三级分类"), "category_l3")
        self.assertEqual(got.get("下单价格"), "purchase_cost")
        for col in ("下单时间", "京东订单号", "故障原因", "产品状态", "售后状态",
                    "测试说明", "建议折扣", "当前状态"):
            self.assertIsNone(got.get(col), f"{col} 该沉淀成商品字段，不能认成系统字段")

    def test_25_category_from_manifest_columns(self):
        """一级 + 二级 + 三级拼起来认品类（三级最可信）。"""
        self.assertEqual(importer.match_category("家用电器 厨房小电 破壁机"), "HA.KC.PBJ")
        self.assertEqual(importer.match_category("家用电器/厨房小电/破壁机"), "HA.KC.PBJ")
        # 三级名在这套树里没有（「儿童餐具」）→ 退到二级「喂养用品」的兜底叶
        self.assertEqual(importer.match_category("母婴 喂养用品 儿童餐具"), "BB.WY.QT")
        self.assertEqual(importer.match_category("喂养用品"), "BB.WY.QT")

    def test_30_import_manifest(self):
        """清单导进来：产品名 / 下单价格 / 品类落库，过程列沉淀成商品字段。"""
        batch = self._batch()
        resp = self._commit(batch.id)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            self.assertEqual(len(items), 2)
            item = [x for x in items if x.sku_code == "8617665947744253"][0]
            self.assertEqual(item.brand, "乖巧妻 GQQI")
            self.assertEqual(item.product_name, "乖巧妻 GQQI 德国品牌无渣破壁机加热免过滤")
            self.assertEqual(item.title, item.product_name, "展示名优先用清单里的产品名")
            self.assertEqual(item.category, "HA.KC.PBJ", "三列分类要拼起来认品类")
            self.assertEqual(float(item.purchase_cost), 389.0, "下单价格就是采购成本")
            self.assertEqual(item.extra.get("故障原因"), "不运行，不加热")
            self.assertEqual(item.extra.get("产品状态"), "完好")
            self.assertTrue(item.barcode.startswith("QCHA"))
            labels = {f.label for f in importer.list_fields(s)}
            self.assertIn("售后状态", labels, "过程列要登记成商品字段")
        # 建档表单 2026-10-08 搬到了独立页（/register），批次详情页只留入口
        detail = self.client.get(f"/wms/batches/{batch.id}")
        for frag in (f"/wms/batches/{batch.id}/register", "Excel 导入建档"):
            self.assertIn(frag, detail.text, "批次详情页要留建档入口")
        register_page = self.client.get(f"/wms/batches/{batch.id}/register").text
        # 检修后条码的输入框 2026-10-12 晚从建档页删了（改在货品详情页贴），这里换成数量
        for frag in ('name="sku_code"', 'name="product_name"', 'name="category"',
                     'name="qty"'):
            self.assertIn(frag, register_page, "建档表单要按清单口径给字段")


class OwnBarcodeTest(_WmsCase):
    """检修后条码：检修规整完我们自己重新贴的码（一码一台）。"""

    def test_10_register_and_scan(self):
        """登记自有码 → 落库带时间 → 扫码 / 搜索都认它。"""
        batch = self._batch()
        item = self._register(batch.id)
        resp = self.client.post(f"/wms/items/{item.id}/own-barcode",
                                data={"own_barcode": "WMS-TEST-0001"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            saved = s.get(WmsItem, item.id)
            self.assertEqual(saved.own_barcode, "WMS-TEST-0001")
            self.assertIsNotNone(saved.own_barcode_at, "贴码时间要记上")
            found = intake.find_item(s, "WMS-TEST-0001")
            self.assertIsNotNone(found, "扫自有码要认这台")
            self.assertEqual(found.id, item.id)
        data = self.client.get("/wms/api/item.json?code=WMS-TEST-0001").json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["item"]["own_barcode"], "WMS-TEST-0001")
        page = self.client.get(f"/wms/items/{item.id}")
        self.assertIn("WMS-TEST-0001", page.text)
        self.assertIn("检修后条码", page.text)

    def test_20_one_code_one_item(self):
        """一码一台：同一个码贴到第二台要拦住；留空 = 清除（贴错了）。"""
        batch = self._batch()
        first = self._register(batch.id, sku_code="T-1")
        second = self._register(batch.id, sku_code="T-2")
        self.client.post(f"/wms/items/{first.id}/own-barcode",
                         data={"own_barcode": "WMS-DUP-1"}, follow_redirects=False)
        resp = self.client.post(f"/wms/items/{second.id}/own-barcode",
                                data={"own_barcode": "WMS-DUP-1"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertFalse(s.get(WmsItem, second.id).own_barcode, "撞码不许写上")
            self.assertEqual(s.get(WmsItem, first.id).own_barcode, "WMS-DUP-1")
        self.client.post(f"/wms/items/{first.id}/own-barcode", data={"own_barcode": ""},
                         follow_redirects=False)
        with SessionLocal() as s:
            self.assertFalse(s.get(WmsItem, first.id).own_barcode)
            self.assertIsNone(s.get(WmsItem, first.id).own_barcode_at)

    def test_30_generate(self):
        """没有现成的码可贴：按规则生成一条（WMS + 年月 + 流水），两台不重号。"""
        batch = self._batch()
        first = self._register(batch.id, sku_code="T-1")
        second = self._register(batch.id, sku_code="T-2")
        for item in (first, second):
            self.client.post(f"/wms/items/{item.id}/own-barcode", data={"action": "gen"},
                             follow_redirects=False)
        with SessionLocal() as s:
            codes = [str(s.get(WmsItem, i.id).own_barcode or "") for i in (first, second)]
            self.assertTrue(all(c.startswith("WMS") for c in codes), codes)
            self.assertEqual(len(set(codes)), 2, "生成的两条要不重号")

    def test_40_inspect_submit_records_own_barcode(self):
        """检测台提交时把刚贴的自有码一起登记。"""
        batch = self._batch()
        item = self._register(batch.id, sku_code="T-1", category="HA.KC.DFB")
        with SessionLocal() as s:
            template = [k for k, _label in inspection.full_template("HA.KC.DFB")]
            results = {k: TEST_PASS for k in template}
        payload = {
            "item_id": item.id, "results": results, "fail_descs": {},
            "battery_health": 88, "flaw_desc": "", "photos": [],
            "final_grade": "", "override_reason": "", "uncertain": False,
            "inhouse_fixable": False, "must_official": False, "missing_parts": False,
            "note": "", "location_code": _make_location("A-Q01-L1", "A 区-01 架-1 层"),
            "own_barcode": "WMS-INSPECT-9",
        }
        resp = self.client.post("/wms/api/inspect/submit", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["own_barcode"], "WMS-INSPECT-9")
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItem, item.id).own_barcode, "WMS-INSPECT-9")
        page = self.client.get("/wms/inspect")
        self.assertIn('id="ownBarcode"', page.text, "检测台要有检修后条码输入框")


class ItemFieldsTest(_WmsCase):
    """商品字段以导入的 Excel 表头为准（2026-10-08 用户拍板）+ 手工补 / 调顺序。"""

    def test_10_fields_from_manifest_headers(self):
        """导表沉淀下来的字段：按表头顺序登记，来源记着是哪张表带来的。"""
        batch = self._batch()
        self._commit(batch.id)
        with SessionLocal() as s:
            rows = importer.list_fields(s)
            self.assertEqual(
                [f.label for f in rows],
                ["下单时间", "京东订单号", "故障原因", "产品状态", "售后状态",
                 "测试说明", "建议折扣", "当前状态"],
                "顺序要跟着表头的列顺序走",
            )
            sorts = [f.sort for f in rows]
            self.assertEqual(sorts, sorted(sorts), "排序要递增")
            self.assertTrue(all("清单.xlsx" in str(f.source or "") for f in rows),
                            "来源要写着是哪张表带来的")
            self.assertTrue(all(b in "".join(str(f.source or "") for f in rows)
                                for b in (batch.batch_no,)))

    def test_20_manual_field_and_reorder(self):
        """手工补的字段排最后并写「手工添加」；上移 / 下移能调顺序。"""
        batch = self._batch()
        self._commit(batch.id)
        resp = self.client.post("/wms/fields", data={"label": "机身颜色",
                                                     "field_type": "text"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            rows = importer.list_fields(s)
            self.assertEqual(rows[-1].label, "机身颜色", "手工加的排最后")
            self.assertEqual(rows[-1].source, "手工添加")
            self.assertTrue(rows[-1].enabled, "手工加的字段默认启用")
            field_id = rows[-1].id
        self.client.post(f"/wms/fields/{field_id}/move?dir=up", follow_redirects=False)
        with SessionLocal() as s:
            rows = importer.list_fields(s)
            self.assertEqual(rows[-2].label, "机身颜色", "上移一位")
        self.client.post(f"/wms/fields/{field_id}/move?dir=down", follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(importer.list_fields(s)[-1].label, "机身颜色", "再下移回来")


class RulesBoardTest(_WmsCase):
    """分级规则页的「品类与对标」：对标的是渔枢系统三级类目，在库数字写清口径。"""

    def test_10_wording(self):
        page = self.client.get("/wms/rules").text
        self.assertEqual(self.client.get("/wms/rules").status_code, 200)
        self.assertIn("渔枢系统三级类目", page, "对标的是渔枢系统的三级类目")
        self.assertNotIn("1688 三级类目", page)
        self.assertIn("在库货品（台）", page)
        self.assertIn("现在库里属于这个三级品类的货品台数", page, "数字要说清是什么")

    def test_20_in_stock_counts(self):
        """在库货品只算还在库的（已售这类终态不算），并把待检的分出来。"""
        batch = self._batch()
        first = self._register(batch.id, sku_code="T-1", category="HA.KC.PBJ")
        second = self._register(batch.id, sku_code="T-2", category="HA.KC.PBJ")
        with SessionLocal() as s:
            s.get(WmsItem, second.id).status = "SOLD"
            s.commit()
        page = self.client.get("/wms/rules").text
        row = page.split("HA.KC.PBJ")[1].split("</tr>")[0]
        m = re.search(r">(\d+)<br><span class=\"muted\">待检 (\d+)</span>", row)
        self.assertIsNotNone(m, row)
        self.assertEqual((m.group(1), m.group(2)), ("1", "1"), "已售那台不算在库，剩 1 台待检")


class ImportFailureTest(_WmsCase):
    """导入没成功要看得见为什么（2026-10-08）：失败留进导入记录 + 预览页先预检。"""

    def test_10_failed_import_leaves_a_log(self):
        """**严格模式**下整批没导进来：也要在「导入记录」里写清毛病（以前只在提示条里闪一次）。"""
        batch = self._batch()
        sheet = [
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["A-2", "九阳", "不存在的品类"],
        ]
        resp = self._commit(batch.id, sheet, strict=True)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            log = s.scalars(
                select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)
            ).first()
            self.assertIsNotNone(log, "失败也要留一条记录，不然回头查不到")
            self.assertEqual(log.ok_count, 0)
            self.assertEqual(log.fail_count, 1)
            self.assertTrue(
                any("认不出来" in str(e) for e in (log.errors or [])), log.errors
            )
            self.assertEqual(
                len(s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()),
                0,
                "整批一个事务：有一行不合法就一台都不导",
            )
        page = self.client.get(f"/wms/batches/{batch.id}/import").text
        self.assertIn("没导成", page, "页面要看得见这次是没导成")
        self.assertIn("认不出来", page, "还要看得见为什么没导成")

    def test_20_preview_prechecks(self):
        """预览页先预检一遍：毛病在导入**之前**就摆出来，行号跟表格对得上。"""
        batch = self._batch()
        sheet = [["货号", "品牌", "品类"], ["A-1", "美的", "电饭煲"], ["", "九阳", "电饭煲"]]
        page = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        ).text
        self.assertIn("预检", page)
        self.assertIn("第 3 行没有货号", page, "表头在第 1 行 → 第一行数据是第 2 行")
        ok = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx([["货号", "品类"], ["A-9", "电饭煲"]]),
                            "application/octet-stream")},
        ).text
        self.assertIn("预检通过", ok, "没毛病的表要给一句安心话")

    def test_25_bad_rows_skipped_by_default(self):
        """**默认**就跳过有毛病的行：几十行脏数据不挡几百行好货（坏行照样留记录）。"""
        batch = self._batch()
        sheet = [
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["A-2", "九阳", "不存在的品类"],
            ["A-3", "苏泊尔", "电饭煲"],
        ]
        page = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        ).text
        self.assertIn("默认做法", page, "预览页要写明默认就跳过坏行")
        self.assertIn("strict_mode", page, "想整批不导的人可以勾严格模式")
        token = page.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(sheet[0])
        form = {"token": token, "file_name": "清单.xlsx"}  # 一个勾都不打：跳过坏行、导其余
        for idx, key in mapping.items():
            form[f"map_{idx}"] = key
        resp = self.client.post(f"/wms/batches/{batch.id}/import/commit", data=form,
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/wms/batches/{batch.id}", resp.headers["location"])
        self.assertNotIn("/import", resp.headers["location"], "导入成功要回批次页")
        with SessionLocal() as s:
            items = s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()
            self.assertEqual(sorted(i.sku_code for i in items), ["A-1", "A-3"])
            log = s.scalars(
                select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)
            ).first()
            self.assertEqual(log.ok_count, 2)
            self.assertEqual(log.fail_count, 1, "跳过的行也要记着")
            self.assertTrue(any("认不出来" in str(e) for e in (log.errors or [])))

    def test_26_strict_mode_stops_the_whole_batch(self):
        """勾了「严格模式」：任一行不合法就整批不导（导一半的库存比不导更难收拾）。"""
        batch = self._batch()
        sheet = [
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["A-2", "九阳", "不存在的品类"],
        ]
        page = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        ).text
        token = page.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(sheet[0])
        form = {"token": token, "file_name": "清单.xlsx", "strict_mode": "1"}
        for idx, key in mapping.items():
            form[f"map_{idx}"] = key
        self.client.post(f"/wms/batches/{batch.id}/import/commit", data=form,
                         follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(
                len(s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()), 0
            )
            log = s.scalars(
                select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)
            ).first()
            self.assertEqual(log.ok_count, 0)
            self.assertEqual(log.fail_count, 1)

    def test_27_cat_fallback_imports_every_row(self):
        """分类认不出来的行：预览页当场指定类目，整批照导（不用回去改几百行的表）。"""
        batch = self._batch()
        sheet = [
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["A-2", "九阳", "不存在的品类"],
        ]
        page = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        ).text
        self.assertIn("cat_fallback", page, "预览页要给「分类认不出来」一个兜底出口")
        token = page.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(sheet[0])
        form = {"token": token, "file_name": "清单.xlsx", "cat_fallback": "HA.KC.DFB"}
        for idx, key in mapping.items():
            form[f"map_{idx}"] = key
        resp = self.client.post(f"/wms/batches/{batch.id}/import/commit", data=form,
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = s.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()
            self.assertEqual(len(items), 2, "整批都建了档")
            got = {i.sku_code: i.category for i in items}
            self.assertEqual(got["A-2"], "HA.KC.DFB", "认不出的那行按兜底类目建档")
            self.assertTrue(got["A-1"].startswith("HA"), got)
            log = s.scalars(
                select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)
            ).first()
            self.assertEqual(log.fail_count, 0, "有兜底就不算毛病")

    def test_30_title_row_sheet(self):
        """第一行是标题（不是表头）的表：自动拿第 2 行当表头，行号跟着往下顺一位。"""
        batch = self._batch()
        sheet = [
            ["河南仓库第一批清单"],
            ["货号", "品牌", "品类"],
            ["A-1", "美的", "电饭煲"],
            ["", "九阳", "电饭煲"],
        ]
        page = self.client.post(
            f"/wms/batches/{batch.id}/import/preview",
            files={"file": ("清单.xlsx", _xlsx(sheet), "application/octet-stream")},
        ).text
        self.assertIn("第 2 行", page, "要告诉人表头用的是第 2 行")
        self.assertIn("第 4 行没有货号", page, "表头在第 2 行 → 数据从第 3 行起算")
        token = page.split('name="token" value="')[1].split('"')[0]
        mapping = importer.guess_mapping(sheet[1])
        form = {"token": token, "file_name": "清单.xlsx"}
        for idx, key in mapping.items():
            form[f"map_{idx}"] = key
        self.client.post(f"/wms/batches/{batch.id}/import/commit", data=form,
                         follow_redirects=False)
        with SessionLocal() as s:
            log = s.scalars(
                select(WmsImportLog).where(WmsImportLog.batch_id == batch.id)
            ).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.fail_count, 1)
            self.assertIn("第 4 行", " ".join(str(e) for e in (log.errors or [])))


if __name__ == "__main__":
    unittest.main()
