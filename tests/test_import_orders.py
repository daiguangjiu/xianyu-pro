"""历史订单导入：登记表（xlsx）按系统结构落成订单 + 结算流水。

业务口径（2026-09）：登记表只有供应商 / 产品 / 成本 / 出货价 / 出单账号 / 归属人 / 付款金额，
没有订单号也没有收货信息 —— 导入时补成系统认得的结构（见 app/services/importer.py）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试环境（独立 sqlite 测试库）

from openpyxl import Workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, Settlement  # noqa: E402
from app.services import finance as finance_service  # noqa: E402
from app.services import importer  # noqa: E402

HEADER = [
    "日期",
    "供应商",
    "产品",
    "型号",
    "颜色",
    "成本价",
    "出货价",
    "毛利",
    "出单账号",
    "带教归属人",
    "付款金额",
    "备注",
]


def _write_xlsx(rows: list[list]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "付款"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    path = Path(tempfile.mkdtemp()) / "登记表.xlsx"
    wb.save(path)
    return str(path)


class ImportOrdersTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self._clean()
        self.path = _write_xlsx(
            [
                [
                    datetime(2026, 9, 24),
                    "梦想电商",
                    "倍思 Q2电充电宝\n20000毫安 30w",
                    "·",
                    "白色",
                    45,
                    50,
                    5,
                    "允川不吃大米饭",
                    "范志强",
                    45,
                    "",
                ],
                [
                    datetime(2026, 9, 24),
                    "梦想电商",
                    "亚朵深睡枕4.0，枕头➕枕套",
                    "·",
                    "蓝色",
                    140,
                    158,
                    18,
                    "珊瑚家的令狐冲",
                    "令狐冲",
                    0,
                    "还没付款",
                ],
            ]
        )

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(Order).delete()
            s.commit()

    def _import(self, apply: bool = True) -> dict:
        with SessionLocal() as s:
            rows, sheet = importer.read_sheet(self.path, sheet="付款")
            self.assertEqual(sheet, "付款")
            return importer.import_rows(s, rows, actor="admin", apply=apply)

    def test_10_preview_does_not_write(self):
        """不加 --apply 只出计划，不动库。"""
        out = self._import(apply=False)
        self.assertEqual(out["created"], 2)
        self.assertFalse(out["applied"])
        with SessionLocal() as s:
            self.assertEqual(len(s.scalars(select(Order)).all()), 0)

    def test_20_fields_are_mapped(self):
        """登记表的列 -> 系统字段（产品 / 成本 / 出货价 / 供应商 / 账号 / 归属人）。"""
        self._import()
        with SessionLocal() as s:
            order = s.scalar(
                select(Order).where(Order.xianyu_account == "允川不吃大米饭")
            )
            self.assertIsNotNone(order)
            self.assertEqual(order.product_name, "倍思 Q2电充电宝 20000毫安 30w")
            self.assertEqual(order.spec, "白色", "型号为「·」时只留颜色")
            self.assertEqual(order.cost_amount, 45)
            self.assertEqual(order.sale_amount, 50)
            self.assertEqual(order.supplier_name, "梦想电商")
            self.assertEqual(order.payee_name, "范志强", "带教归属人 = 回款支付宝归属人")
            self.assertEqual(order.ordered_at, datetime(2026, 9, 24), "业务日按表格日期")
            self.assertTrue(order.order_no.startswith("HIS-"), "历史单号不用 TMP-（不会被催图）")

    def test_30_paid_and_unpaid_transfer(self):
        """付款金额 > 0 -> 已转账（应付流水核销）；为 0 -> 未转账，留给人工核销。"""
        self._import()
        with SessionLocal() as s:
            paid = s.scalar(select(Order).where(Order.xianyu_account == "允川不吃大米饭"))
            self.assertEqual(paid.transfer_status, finance_service.TRANSFER_PAID)
            row = s.scalar(
                select(Settlement).where(
                    Settlement.order_id == paid.id,
                    Settlement.kind == finance_service.SETTLEMENT_SUPPLIER,
                )
            )
            self.assertEqual(row.status, finance_service.SETTLEMENT_SETTLED, "已付款的应付要核销")

            unpaid = s.scalar(select(Order).where(Order.xianyu_account == "珊瑚家的令狐冲"))
            self.assertEqual(unpaid.transfer_status, "unpaid")
            row = s.scalar(
                select(Settlement).where(
                    Settlement.order_id == unpaid.id,
                    Settlement.kind == finance_service.SETTLEMENT_SUPPLIER,
                )
            )
            self.assertEqual(row.status, finance_service.SETTLEMENT_PENDING)

    def test_40_state_is_pushed_and_settlement_created(self):
        """已付款的历史订单：状态「已推单」（不占待办）+ 生成应收 / 应付两条流水。"""
        self._import()
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.xianyu_account == "允川不吃大米饭"))
            self.assertEqual(order.order_state, "pushed")
            self.assertEqual(order.supplier_status, "pushed")
            self.assertIsNotNone(order.pushed_at)
            kinds = {
                r.kind
                for r in s.scalars(select(Settlement).where(Settlement.order_id == order.id))
            }
            self.assertEqual(
                kinds, {finance_service.SETTLEMENT_INITIATOR, finance_service.SETTLEMENT_SUPPLIER}
            )

    def test_50_reimport_is_idempotent(self):
        """同一个文件重复导入不会重复建单。"""
        self._import()
        out = self._import()
        self.assertEqual(out["created"], 0)
        self.assertEqual(out["skipped"], 2)
        with SessionLocal() as s:
            self.assertEqual(len(s.scalars(select(Order)).all()), 2)


if __name__ == "__main__":
    unittest.main()
