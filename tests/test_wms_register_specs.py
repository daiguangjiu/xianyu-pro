"""手动建档「多规格」（2026-10-12）：页面按模块分区 + 重要信息高亮 + 多规格一次建完。

口径（用户拍板，分三轮）：
1. 手动建档要能一次建多规格，交互参照渔枢系统「新建商品」的规格矩阵；
2. 表单信息按模块分区，录入时清楚哪块是哪块；
3. 重要的信息（货号 / 品类 / 规格 · 价格 · 库存）高亮显示，块内再分组（第二轮：
   系统生成货号按钮做醒目、输入框排位调整）；
4. **第三轮（2026-10-12 晚）**：② 那块从「规格 · 数量 · 库存」扩成「规格 · 价格 · 库存」——
   批量设置与规格矩阵的字段**照渔枢系统的**（成本价 / 渔货价 / 建议售价 / 市场价 / 库存），
   「价格与标识」整块建档页不要了（多规格已经替代），只在编辑页保留「价格与库存」；
5. **第四轮（2026-10-12 晚）**：① 里的「型号 / 规格 / 检修后条码」整块（含两句说明）删掉
   （型号 / 规格归规格维度；检修后条码改在货品详情页贴）；「应用类目规格模板」没选分类时
   置灰 + 点了提示「请先选择商品的类目！」；**渔货价必填**（标 * + required）。

业务口径不变：**一个规格 = 一条货 = 一个条码**，台数记在这一条货的 qty 上
（跟 2026-10-10「一条货 N 台不拆码」是同一条规矩）。
"""
from __future__ import annotations

import json
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
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import grading, intake  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem, WmsStockLedger  # noqa: E402


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class RegisterSpecsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self):
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def _batch(self) -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 20,
                    "supplier_name": "多规格建档测试"},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def _post(self, batch: WmsBatch, **kw):
        return self.client.post(
            f"/wms/batches/{batch.id}/items", data=kw, follow_redirects=False)

    # ------------------------------------------------------------------ 页面
    def test_10_page_sections_and_highlight(self):
        """建档页按模块分区，重要块高亮，多规格矩阵与规格模板都在。"""
        page = self.client.get(f"/wms/batches/{self._batch().id}/register")
        self.assertEqual(page.status_code, 200)
        # 分区 + 高亮（建档页三块：① 货号与品类 ② 规格 · 价格 · 库存 ③ 商品字段）
        for frag in ("frm-sec", "frm-sec-head", "hl-box",
                     "①", "②", "③",
                     "货号与品类", "规格 · 价格 · 库存", "商品字段"):
            self.assertIn(frag, page.text, frag)
        self.assertNotIn("价格与标识", page.text,
                         "2026-10-12 第三轮：价格并进 ②，这块建档页不要了")
        # 分组小标题（2026-10-12 用户要求：每块里的信息再分组重点显示）
        for frag in ("frm-sub", "识别 · 货号", "基本资料", "规格与 SKU", "沉淀字段"):
            self.assertIn(frag, page.text, frag)
        # 多规格矩阵（照渔枢：批量设置 + 规格维度 + SKU 表 + 三个按钮）
        for frag in ('id="specEditor"', 'id="specTable"', 'class="sku-batch"',
                     'name="specs_json"', "addSpecRow()", "applyTpl()", "batchFill()",
                     "genSkuMatrix()", "按照规则生成 SKU", "应用类目规格模板",
                     "批量设置到全部规格"):
            self.assertIn(frag, page.text, frag)
        # 老字段一个没少（tests/test_wms.py::test_72 的口径不能破）
        for frag in ('name="sku_code"', 'name="qty"', 'name="mode"',
                     'name="serial_no"', 'name="category"'):
            self.assertIn(frag, page.text, frag)
        # 规格模板随页面下发（前端按品类推荐用），类目码 → 一级名的对照也在
        self.assertIn("_default", page.text)
        self.assertIn("手机通讯", page.text)
        self.assertIn("家用电器", page.text)
        self.assertIn("SPEC_HINTS", page.text)

    def test_11_price_fields_follow_hub(self):
        """批量设置与规格矩阵的字段照渔枢系统「规格 · 价格 · 库存」（2026-10-12 第三轮）。

        渔枢那五个格子 = 成本价 / 渔货价 / 建议售价 / 市场价 / 库存；
        渔集的输入框名直接用服务端字段名 —— 多规格时当行内留空的默认值，
        不加规格时就是这一条货自己的值。
        """
        page = self.client.get(f"/wms/batches/{self._batch().id}/register").text
        # 批量设置五个字段（一行排开）
        self.assertIn('class="field-row five"', page)
        for frag in ('name="purchase_cost"', 'name="selling_price"',
                     'name="suggest_price"', 'name="base_price"', 'name="qty"'):
            self.assertIn(frag, page, frag)
        for label in ("成本价", "渔货价", "建议售价", "市场价（基准价）", "库存（台）"):
            self.assertIn(label, page, label)
        # 矩阵列头照渔枢（渔集的「货号」= 渔枢的 SKU 编码）
        for label in ("规格组合", "成本价", "渔货价", "建议售价", "市场价",
                      "库存", "货号"):
            self.assertIn(label, page, label)
        for frag in ('id="bQty"', 'id="bCost"', 'id="bBase"', 'id="bModel"'):
            self.assertNotIn(frag, page, "老批量设置那套输入框名已经不用了")
        # 前端的输入框清单与渔枢同序，提交前映射成服务端字段名
        self.assertIn("BATCH_IDS", page)
        self.assertIn("purchase_cost: r.cost", page)
        self.assertIn("selling_price: r.fish", page)
        self.assertIn("suggest_price: r.suggest", page)
        self.assertIn("base_price: r.market", page)

    def test_13_edit_page_keeps_price_block(self):
        """编辑页保留「价格与库存」（这一台自己的价改在这）—— 建档页那块已并进 ②。"""
        batch = self._batch()
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [
                {"brand": "美的", "model": "MB-1", "sku_code": "T-7100", "qty": 1,
                 "purchase_cost": 100, "selling_price": 150, "suggest_price": 200,
                 "base_price": 260}
            ], operator="tester")[0]
            item_id = item.id
        page = self.client.get(f"/wms/items/{item_id}/edit").text
        self.assertIn("价格与库存", page)
        self.assertIn('name="selling_price"', page)
        self.assertIn('name="suggest_price"', page)
        # 回填的是库里的值（不是 0）
        self.assertIn('value="150"', page)
        self.assertIn('value="200"', page)
        # 改价改库存这两格在编辑页是有的；建档页那块矩阵不出现
        self.assertIn('name="qty"', page)
        self.assertNotIn('id="specEditor"', page)

    def test_12_field_order_and_wide_category_fixed(self):
        """输入框重新排位（2026-10-12）：分类不再独占一整行 —— 品牌 / 分类跟在货号右边，
        产品 / 商品名称单独占一行（原来是分类独占一行、宽得很）。"""
        page = self.client.get(f"/wms/batches/{self._batch().id}/register").text
        self.assertIn("id-row", page, "识别行用专门的栅格（货号带按钮所以要宽一点）")
        order = [page.index(f) for f in ('id="m-sku"', 'id="m-brand"', 'id="m-cat"', 'id="m-product"')]
        self.assertEqual(order, sorted(order), "顺序：货号 → 品牌 → 分类 → 产品名称")
        # 分类与品牌同一行（都在 id-row 里），产品名称在 id-row 之后
        row = page[page.index('class="field-row id-row"'):page.index('id="m-product"')]
        for frag in ('id="m-sku"', 'id="m-brand"', 'id="m-cat"'):
            self.assertIn(frag, row, frag)

    def test_21_identity_block_removed(self):
        """型号 / 规格 / 检修后条码 那一块连说明一起删（2026-10-12 晚 用户要求）。

        型号 / 规格 归 ② 的规格维度（一个规格就是一条货）；检修后条码改在
        「货品详情」页贴（那边有 /wms/items/{id}/own-barcode 表单，功能没少）。
        """
        batch = self._batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register").text
        for gone in ('id="m-model"', 'id="m-spec"', 'id="m-own"',
                     'name="model"', 'name="spec"', 'name="own_barcode"',
                     "分类下拉里是三级分类", "多规格建档时，货号自动派生"):
            self.assertNotIn(gone, page, gone)
        self.assertIn("基本资料", page, "基本资料这块还在（产品名称在里面）")
        self.assertIn('id="m-product"', page)
        self.assertIn('id="m-sn"', page, "建档页的序列号还在（单条货那格）")

    def test_22_category_template_button_gated(self):
        """「应用类目规格模板」没选分类时置灰、点了提示（2026-10-12 晚 用户要求）。

        按钮**不能**用真 disabled —— 那样浏览器不派发 click，提示弹不出来；
        所以用 .off 样式置灰 + JS 守卫，分类一变就刷新状态。
        """
        batch = self._batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register").text
        self.assertIn('id="tplBtn"', page)
        self.assertIn('btn sm ghost off', page, "默认（还没选分类）是置灰态")
        self.assertIn("请先选择商品的类目！", page)
        self.assertIn("refreshTplBtn", page)
        self.assertIn("function applyTpl", page)
        self.assertIn("alert('请先选择商品的类目！')", page)
        self.assertIn(".btn.off{opacity:.5;cursor:not-allowed}", page, "置灰样式")
        self.assertIn("addEventListener('input', refreshTplBtn)", page, "分类一变就刷新按钮态")

    def test_23_selling_price_required(self):
        """渔货价必填（2026-10-12 晚 用户要求）：标 * + required（建档页与编辑页都有）。"""
        batch = self._batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register").text
        self.assertIn('渔货价 <span class="req">*</span>', page)
        fish = page[page.index('id="m-fish"'):][:200]
        self.assertIn("required", fish, "输入框带 required，提交前就挡住")
        self.assertIn("必填", fish, "提示语说清为什么必填")
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [
                {"sku_code": "T-9600", "selling_price": 88, "base_price": 100, "qty": 1}
            ], operator="tester")[0]
            item_id = item.id
        edit = self.client.get(f"/wms/items/{item_id}/edit").text
        self.assertIn('渔货价 <span class="req">*</span>', edit)
        self.assertIn("required", edit[edit.index('id="m-fish"'):][:200])

    def test_24_edit_keeps_model_spec_own_barcode(self):
        """编辑页不再有 型号 / 规格 / 检修后条码 这三格，但**保存不能把它们清掉**。

        导入进来的型号 / 规格、已贴的检修后条码都要原样留着（表单没带 ≠ 清空）。
        """
        from wms import intake as _intake
        batch = self._batch()
        with SessionLocal() as s:
            item = _intake.register(s, batch.id, [
                {"sku_code": "T-9700", "model": "MB-9", "spec": "128G 黑",
                 "base_price": 100, "selling_price": 90, "qty": 1}
            ], operator="tester")[0]
            item_id = item.id
            _intake.set_own_barcode(s, s.get(WmsItem, item_id), "WMS-EXIST-1", operator="tester")
        page = self.client.get(f"/wms/items/{item_id}/edit").text
        for gone in ('name="model"', 'name="spec"', 'name="own_barcode"'):
            self.assertNotIn(gone, page, gone)
        resp = self.client.post(f"/wms/items/{item_id}/edit",
                                data={"sku_code": "T-9700", "brand": "美的",
                                      "purchase_cost": "60", "selling_price": "95",
                                      "suggest_price": "120", "base_price": "150", "qty": "1"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            saved = s.get(WmsItem, item_id)
            self.assertEqual(saved.brand, "美的", "该改的改了")
            self.assertEqual(float(saved.selling_price), 95.0)
            self.assertEqual(saved.model, "MB-9", "型号没被清掉")
            self.assertEqual(saved.spec, "128G 黑", "规格没被清掉")
            self.assertEqual(saved.own_barcode, "WMS-EXIST-1", "已贴的检修后条码没被清掉")

    def test_15_edit_page_has_no_specs(self):
        """编辑页是改单台，不该冒出规格矩阵（分区照旧）。"""
        batch = self._batch()
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [
                {"brand": "美的", "model": "MB-1", "sku_code": "T-7000", "qty": 1}
            ], operator="tester")[0]
            item_id = item.id
        page = self.client.get(f"/wms/items/{item_id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("frm-sec", page.text)
        self.assertNotIn('id="specEditor"', page.text, "编辑单台时没有规格矩阵")

    # ---------------------------------------------------------------- 系统生成货号 / 分类合并
    def test_17_system_sku_code(self):
        """系统生成货号（2026-10-12 加 / 2026-10-14 升级为渔货号）：非扫码场景 ——
        页面给按钮，服务端发号。

        **2026-10-14 口径变更（用户拍板）**：渔货号（YS + 日期 + 当天 4 位流水）才是
        **我们自己的商品货号**、一款一号，所以这个按钮升级成发渔货号；
        原来那个「YC + 年月 + 流水」的发号接口（进货货号）作为供应商编号那一套保留。
        按钮照旧要醒目（用户反馈原来那个灰描边小按钮找不到）：主色实心 `.btn.primary`。
        """
        batch = self._batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register").text
        self.assertIn("系统生成渔货号", page, "按钮文案跟着升级")
        self.assertIn("btn primary sku-btn", page, "按钮要显眼（主色实心）")
        self.assertIn("fish_code", page, "建档页有渔货号这一格")
        self.assertIn(f"/wms/batches/{batch.id}/next-fish", page)
        # 进货货号（YC 那套外部编号）的发号接口仍在，只是页面不再摆按钮 ——
        # 供应商清单上有号就手填，没有就用生成的渔货号顶上（见 genFishCode）
        first = self.client.get(f"/wms/batches/{batch.id}/next-sku")
        self.assertEqual(first.status_code, 200)
        code1 = first.json()["sku_code"]
        self.assertTrue(code1.startswith("YC"), code1)
        self.assertEqual(len(code1), 10, "YC + 4 位年月 + 4 位流水")
        self.assertTrue(code1[6:].isdigit(), code1)
        # 只发号不落库：还没建档，连点两次拿到同一个号
        self.assertEqual(
            self.client.get(f"/wms/batches/{batch.id}/next-sku").json()["sku_code"], code1)
        with SessionLocal() as s:
            intake.register(s, batch.id, [
                {"brand": "美的", "model": "MB-1", "sku_code": code1, "qty": 1}
            ], operator="tester")
        code2 = self.client.get(f"/wms/batches/{batch.id}/next-sku").json()["sku_code"]
        self.assertEqual(int(code2[6:]), int(code1[6:]) + 1, "用掉的号要往后让")
        self.assertEqual(
            self.client.get("/wms/batches/999999/next-sku").status_code, 404, "批次不在就 404")

    def test_18_category_single_control(self):
        """分类合成一个控件（2026-10-12）：三级下拉一份候选，不再让人在三格里挑。"""
        batch = self._batch()
        page = self.client.get(f"/wms/batches/{batch.id}/register").text
        self.assertIn('name="category"', page)
        for gone in ('name="category_l1"', 'name="category_l2"', 'name="category_l3"'):
            self.assertNotIn(gone, page, gone)
        # 老口径照旧：一个格子里直接写三级名也认得出来（后端 _category_from_form 没动）
        resp = self._post(batch, sku_code="T-9500", product_name="测试机",
                          category="破壁机", qty="1")
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            item = intake.list_items(s, batch_id=batch.id)[0]
            self.assertEqual(item.category, "HA.KC.PBJ")

    def test_19_spec_hints_map_codes(self):
        """「应用类目规格模板」按类目挑模板：分类里写三级码（HA.KC.DFB）也挑得到「家用电器」那套。

        分类只有一个格子，人可能填码；所以服务端把 类目码 → 一级类目名 的对照一起下发。
        """
        from wms.web.router import _SPEC_HINTS, _SPEC_TEMPLATES
        self.assertEqual(_SPEC_HINTS.get("HA"), "家用电器")
        self.assertEqual(_SPEC_HINTS.get("HA.KC.DFB"), "家用电器")
        # 一级名全都对得上模板 key（只有「其他」走兜底，没有专属模板）
        missing = set(_SPEC_HINTS.values()) - {""} - set(_SPEC_TEMPLATES)
        self.assertLessEqual(missing, {"其他"}, f"这些一级类目没有规格模板：{missing}")

    # ---------------------------------------------------------------- 多规格
    def test_20_one_item_per_spec(self):
        """一个规格 = 一条货 = 一个条码；台数记在各自那条上，价格也记在各自那条上。"""
        batch = self._batch()
        specs = [
            {"spec": "黑色 / 128G", "qty": "2", "model": "", "sku_code": "",
             "purchase_cost": "", "selling_price": "", "suggest_price": "",
             "base_price": ""},
            {"spec": "白色 / 256G", "qty": "3", "model": "MB-2", "sku_code": "",
             "purchase_cost": "400", "selling_price": "520", "suggest_price": "700",
             "base_price": "600"},
        ]
        resp = self._post(batch, sku_code="T-8000", product_name="测试破壁机",
                          brand="美的", category_l3="破壁机", model="MB-1",
                          purchase_cost="389", selling_price="450", suggest_price="660",
                          base_price="599", specs_json=json.dumps(specs))
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            self.assertEqual(len(items), 2, "两个规格 = 两条货")
            by = {it.spec: it for it in items}
            self.assertEqual(int(by["黑色 / 128G"].qty or 0), 2)
            self.assertEqual(int(by["白色 / 256G"].qty or 0), 3)
            self.assertEqual(len({it.barcode for it in items}), 2, "一规格一码")
            # 货号派生（跟渔枢 SKU 编码派生同一套口径）
            self.assertEqual(by["黑色 / 128G"].sku_code, "T-8000-01")
            self.assertEqual(by["白色 / 256G"].sku_code, "T-8000-02")
            # 行里留空的回落「批量设置」那份公共值，行里填了的照行的走
            self.assertEqual(float(by["黑色 / 128G"].purchase_cost), 389.0)
            self.assertEqual(float(by["黑色 / 128G"].selling_price), 450.0)
            self.assertEqual(float(by["黑色 / 128G"].suggest_price), 660.0)
            self.assertEqual(float(by["黑色 / 128G"].base_price), 599.0)
            self.assertEqual(by["黑色 / 128G"].model, "MB-1")
            self.assertEqual(float(by["白色 / 256G"].purchase_cost), 400.0)
            self.assertEqual(float(by["白色 / 256G"].selling_price), 520.0)
            self.assertEqual(float(by["白色 / 256G"].suggest_price), 700.0)
            self.assertEqual(float(by["白色 / 256G"].base_price), 600.0)
            self.assertEqual(by["白色 / 256G"].model, "MB-2")
            self.assertEqual(by["黑色 / 128G"].category, "HA.KC.PBJ")
            # 台数合计进批次，入库流水也按台数
            self.assertEqual(int(s.get(WmsBatch, batch.id).qty_actual or 0), 5)
            led = s.scalars(select(WmsStockLedger).where(
                WmsStockLedger.item_id == by["白色 / 256G"].id)).first()
            self.assertEqual(int(led.qty_delta or 0), 3)

    def test_30_row_sku_code_wins(self):
        """规格表里手填的货号照填的走，不再派生。"""
        batch = self._batch()
        specs = [{"spec": "黑", "qty": "1", "sku_code": "AAA-1"},
                 {"spec": "白", "qty": "1", "sku_code": "AAA-2"}]
        resp = self._post(batch, sku_code="T-9000", category="破壁机",
                          specs_json=json.dumps(specs))
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            got = {it.spec: it.sku_code
                   for it in intake.list_items(s, batch_id=batch.id)}
            self.assertEqual(got, {"黑": "AAA-1", "白": "AAA-2"})

    def test_40_single_row_no_suffix(self):
        """只有一个规格时不加 -01 后缀（跟老的单条建档一个价）。"""
        batch = self._batch()
        resp = self._post(batch, sku_code="T-9100", category="破壁机",
                          specs_json=json.dumps([{"spec": "默认规格", "qty": "4"}]))
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].sku_code, "T-9100")
            self.assertEqual(int(items[0].qty or 0), 4)

    def test_50_no_specs_keeps_old_behaviour(self):
        """不带规格清单 = 一条货：数量与价格取「批量设置」那块（2026-10-12 第三轮）。"""
        batch = self._batch()
        resp = self._post(batch, sku_code="T-9200", category="破壁机", qty="2",
                          purchase_cost="100", selling_price="150",
                          suggest_price="260", base_price="300")
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            items = intake.list_items(s, batch_id=batch.id)
            self.assertEqual(len(items), 1)
            self.assertEqual(int(items[0].qty or 0), 2)
            self.assertEqual(items[0].sku_code, "T-9200")
            # 四个价格（成本价 / 渔货价 / 建议售价 / 市场价）都落库了
            self.assertEqual(float(items[0].purchase_cost), 100.0)
            self.assertEqual(float(items[0].selling_price), 150.0)
            self.assertEqual(float(items[0].suggest_price), 260.0)
            self.assertEqual(float(items[0].base_price), 300.0)

    def test_80_manual_suggest_price_wins(self):
        """建议售价人填了就以人填的为准（不被系数盖掉）：出价封顶 + 渔枢带出都认它。"""
        batch = self._batch()
        with SessionLocal() as s:
            item = intake.register(s, batch.id, [
                {"category": "HA.KC.PBJ", "model": "MB-1", "sku_code": "T-9400",
                 "product_name": "测试破壁机", "qty": 1, "purchase_cost": 100,
                 "base_price": 1000, "suggest_price": 1500}
            ], operator="tester")[0]
            # 系数算出来是 1000 × 系数（< 1500），人填的 1500 优先
            quote = grading.price_of(s, item.grade, 1000.0, 100.0, manual_suggest=1500.0)
            self.assertEqual(float(quote["suggest_price"]), 1500.0)
            self.assertTrue(quote["manual_suggest"])
            # 市场价抬到很高时，渔货价仍被「人填的建议售价」封顶
            high = grading.price_of(s, item.grade, 10000.0, 100.0, manual_suggest=1500.0)
            self.assertLessEqual(float(high["price"]), 1500.0)
            # 没填就还是「市场价 × 建议售价系数」那套（回归：老口径不能变）
            auto = grading.price_of(s, item.grade, 1000.0, 100.0)
            self.assertFalse(auto["manual_suggest"])
            self.assertAlmostEqual(
                float(auto["suggest_price"]),
                round(1000.0 * float(auto["suggest_factor"]), 2), places=2)
        # 渔枢按条码带出时也用这个人填的建议售价
        from app.services.wms_bridge import lookup_item
        got = lookup_item("T-9400")
        self.assertIsNotNone(got)
        self.assertEqual(float(got["suggest_price"]), 1500.0)
        self.assertEqual(float(got["market_price"]), 1000.0)

    def test_60_bad_json_gives_hint_not_500(self):
        """规格清单坏了给提示条回来，不能炸成 500。"""
        batch = self._batch()
        resp = self._post(batch, sku_code="T-9300", category="破壁机",
                          specs_json="{坏掉的")
        self.assertIn(resp.status_code, (200, 303))
        with SessionLocal() as s:
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 0)

    def test_70_sku_still_required(self):
        """货号必填 —— 多规格也一样（扫码检测认的是它）。"""
        batch = self._batch()
        resp = self._post(batch, sku_code="", category="破壁机",
                          specs_json=json.dumps([{"spec": "黑", "qty": "1"}]))
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(intake.list_items(s, batch_id=batch.id)), 0)


if __name__ == "__main__":
    unittest.main()
