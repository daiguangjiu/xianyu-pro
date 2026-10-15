"""渔集 · 标签上的等级必须跟**实际检测分级**一致（2026-10-13 用户报障：打印都是 W 待测）。

链路上的两个坑：
  · 标签内容是「入队那一刻」生成的（grade / 处置 / 打印指令都存进了 wms_label_prints），
    货是入库就排的队、那会儿还没检测 → 纸上永远是 W。定级之后必须把**还没打出去**的
    记录按新等级刷一遍（``labeling.sync_queue_grade``，检测提交与退回入库都会调）；
  · 本地链路（TSPL / ZPL）给的是库里那份 command 快照 —— 等级早就变了它也照样打老的。
    现在取指令时按货品当前事实重算一份（顺手写回库）。

已经打出去的不动：那是既成事实，要改走补打（队列页会提示「纸上 X 档 ≠ 实判 Y 档」）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import inspection  # noqa: E402
from wms import intake  # noqa: E402
from wms import labeling  # noqa: E402
from wms import print_settings  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import GRADE_W, TEST_PASS, WmsItem, WmsLabelPrint  # noqa: E402

_PHONE_RESULTS = {
    "boot": TEST_PASS, "screen": TEST_PASS, "touch": TEST_PASS, "charge": TEST_PASS,
    "camera": TEST_PASS, "speaker": TEST_PASS, "mic": TEST_PASS, "button": TEST_PASS,
    "wifi": TEST_PASS, "bluetooth": TEST_PASS, "appear": TEST_PASS,
}


class LabelGradeSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        ensure_default_admin()
        cls.client = TestClient(app)
        cls.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(WmsLabelPrint).delete(synchronize_session=False)
            # 检测记录也要清：SQLite 复用 item id，残留记录会让新货被当成「复检第 2 轮」
            from wms.models import WmsTestItem, WmsTestRecord

            s.query(WmsTestItem).delete(synchronize_session=False)
            s.query(WmsTestRecord).delete(synchronize_session=False)
            s.query(WmsItem).delete(synchronize_session=False)
            s.commit()

    def tearDown(self) -> None:
        self.setUp()

    def _item(self) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "标签等级测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "spec": "32 英寸",
                  "serial_no": "SN0001", "purchase_cost": 389, "base_price": 599}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def test_10_grade_after_enqueue_syncs_queue(self):
        """入库就排队（W）→ 检测定级 A：队列里那张跟着变成 A，指令也重算。"""
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            self.assertEqual(it.grade, GRADE_W)
            row = labeling.enqueue(s, [it], operator="tester")[0]
            self.assertEqual(row.grade, GRADE_W, "入队时还没检测，确实是 W")
            print_id = row.id
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            inspection.submit(s, it, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, final_grade="A", flaw_desc="外壳轻微划痕", override_reason="测试：人工定级")
            row = s.get(WmsLabelPrint, print_id)
            self.assertEqual(it.grade, "A")
            self.assertEqual(row.grade, "A", "定级后队列里没打的标签要跟着改")
            self.assertIn('"A"', row.command or "", "打印指令也得重算，不能还是老的")
            self.assertEqual(labeling.label_data(it)["grade"], "A")

    def test_20_already_printed_not_touched(self):
        """已经打出去的那张是既成事实 —— 不偷偷改它（要改走补打）。"""
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            row = labeling.enqueue(s, [it], operator="tester")[0]
            labeling.mark_printed(s, row.id, operator="tester")
            print_id = row.id
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            inspection.submit(s, it, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, final_grade="B", flaw_desc="外壳轻微划痕", override_reason="测试：人工定级")
            row = s.get(WmsLabelPrint, print_id)
            self.assertEqual(it.grade, "B")
            self.assertEqual(row.grade, GRADE_W, "打过的那张保持原样（提示去补打）")

    def test_30_command_route_uses_current_grade(self):
        """本地链路取指令：按货品当前等级现算，不给人库里那份过期的快照。"""
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            row = labeling.enqueue(s, [it], operator="tester",
                                   transport=labeling.TRANSPORT_NET)[0]
            print_id = row.id
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            inspection.submit(s, it, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, final_grade="A", flaw_desc="外壳轻微划痕", override_reason="测试：人工定级")
        resp = self.client.get(f"/wms/labels/{print_id}/command")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"A"', resp.text, "指令里得是新的等级")
        self.assertNotIn('"W"', resp.text)

    def test_40_sample_preview_uses_real_item(self):
        """打印设置页的预览要拿真货渲染 —— 以前队列空时造假货，等级是兜底的 W，误导人。"""
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            inspection.submit(s, it, tester="t", results=dict(_PHONE_RESULTS),
                              battery_health=90, final_grade="S", flaw_desc="外壳轻微划痕", override_reason="测试：人工定级")
            xml = print_settings.sample_xml(s)
        self.assertIn(">S<", xml, "预览是这台货的真实等级")
        self.assertNotIn("W 待测", xml)

    def test_50_items_list_shows_barcode(self):
        """库存台账列表：条码那一列要真的打出条码（用户要它顶掉货号）。"""
        item = self._item()
        page = self.client.get("/wms/items").text
        self.assertIn(">条码<", page, "表头是「条码」")
        self.assertIn(item.barcode, page, "单元格里是这台货的条码")

    def test_60_enqueue_with_suggested_grade(self):
        """检测台「提交前即时打标」按引擎建议档出（2026-10-13 用户三催）：
        货还没定级（W），传 grade="S" 入队 —— 纸上必须是 S，不能永远印「W 待测」。"""
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            self.assertEqual(it.grade, GRADE_W)
            row = labeling.enqueue(s, [it], operator="tester", grade="S")[0]
            self.assertEqual(row.grade, "S", "指定档就按指定档入队")
            self.assertEqual(row.action_text, labeling.GRADE_ACTION["S"])
            self.assertIn('"S"', row.command or "", "打印指令也按指定档生成")
            pid = row.id
        # 已排过队的（W），再按新档入队一次 —— 记录跟着换档，不冒第二条
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)
            rows = labeling.enqueue(s, [it], operator="tester", grade="B")
            self.assertEqual(len(rows), 1, "幂等：还是同一条记录")
            row = s.get(WmsLabelPrint, pid)
            self.assertEqual(row.grade, "B", "档位变了队列跟着换")
            self.assertIn('"B"', row.command or "")

    def test_70_label_vars_prefers_row_grade(self):
        """渲染取值：row 上带了与货品不同的档（预览 / 提交前打印）→ 以 row 的档为准。

        引擎建议 S、货品还在 W —— 以前 label_vars 只认货品，纸上永远是「W 待测」，
        跟引擎建议对不上（用户截图红框就是它）。
        """
        from wms.print_settings import label_vars
        item = self._item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item.id)

            class Ghost:  # 不进库的假打印记录（与 _label_preview_of 同款）
                grade = "S"

            v = label_vars(it, Ghost())
            self.assertEqual(v["grade"], "S")
            self.assertEqual(v["grade_label"], labeling.grade_label("S"))
            self.assertEqual(v["action"], labeling.GRADE_ACTION["S"])
            # 档一致（或 row 没带档）→ 照旧按货品现算
            self.assertEqual(label_vars(it, None)["grade"], GRADE_W)
            self.assertEqual(label_vars(it, None)["action"], "待检测")
