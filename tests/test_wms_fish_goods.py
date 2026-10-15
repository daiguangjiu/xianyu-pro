"""渔货号升级为**商品维度主 key**（2026-10-14 第二轮）。

用户四条：

  1. 商品出入库架构变更 —— 渔货号是**我们自己的商品货号**，批次建档时就产生
     这条「以商品为维度」的记录；导入是按单品一行一台导的，所以**支持货号合并**；
  2. 商品详情页照库存台账的货品详情页做，主 key 就是渔货号（一款一页）；
  3. 扫码检测页捕捉到「这台货属于某个渔货号」时弹窗提示，人选了就并进去
     —— 并进去 = 那一款多一台（按汇总加库存）；
  4. 手动建档页的「系统生成货号」升级为发**渔货号**；检测台重点显示渔货号。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from wms import importer, intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import STATUS_SELLABLE, WmsAuditLog, WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class FishGoodsTest(unittest.TestCase):
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

    def _batch(self, qty: int = 5, supplier: str = "渔货号商品测试") -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s,
                {"category": "HA", "source_type": "purchase", "qty_expected": qty,
                 "supplier_name": supplier},
                operator="tester",
            )
            return s.get(WmsBatch, batch.id)

    def _register(self, batch: WmsBatch, rows: list[dict]) -> list[int]:
        with SessionLocal() as s:
            return [it.id for it in intake.register(s, batch.id, rows, operator="tester")]

    # ------------------------------------------------------ ④ 建档页发渔货号
    def test_10_next_fish_endpoint(self):
        """建档页「系统生成渔货号」：服务端发 ``YS + 日期 + 当天 4 位流水``。"""
        batch = self._batch()
        resp = self.client.get(f"/wms/batches/{batch.id}/next-fish")
        self.assertEqual(resp.status_code, 200)
        code = resp.json()["fish_code"]
        self.assertTrue(code.startswith("YS"), code)
        self.assertEqual(len(code), 14, "YS + 8 位日期 + 4 位流水")
        self.assertTrue(code[10:].isdigit(), code)
        # 只发号不落库：连点两次还是同一个号
        self.assertEqual(self.client.get(
            f"/wms/batches/{batch.id}/next-fish").json()["fish_code"], code)
        self.assertEqual(
            self.client.get("/wms/batches/999999/next-fish").status_code, 404)

    def test_15_register_with_given_fish_code(self):
        """建档表单里填了渔货号 → 这台货就归到那一款下面（不另发号）。"""
        batch = self._batch()
        with SessionLocal() as s:
            head = intake.register(s, batch.id, [
                {"sku_code": "FG-1", "product_name": "指定归属性的一款"}
            ], operator="tester")[0]
            fish = str(head.fish_code or "")
        self.assertTrue(fish.startswith("YS"))
        # 走页面提交建档（带 fish_code）→ 落在同一款下
        resp = self.client.post(
            f"/wms/batches/{batch.id}/items",
            data={"sku_code": "FG-2", "product_name": "指定归属性的一款",
                  "fish_code": fish, "qty": 1, "mode": "single"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            rows = intake.find_items_by_fish(s, fish)
            self.assertEqual(len(rows), 2, "填了号就该归到这一款下（合并）")
            self.assertIn("FG-2", [str(r.sku_code or "") for r in rows])

    # ------------------------------------------------------ ① 导入合并
    def test_20_fish_plan_merges_same_name_rows(self):
        """导入预检：一行一台的表按商品名归并，报出「几款 / 几款并进已有号」。"""
        batch = self._batch()
        with SessionLocal() as s:
            intake.register(s, batch.id, [
                {"sku_code": "PLAN-0", "product_name": "清单里的同款货"}
            ], operator="tester")
            plan = importer.fish_plan(s, [
                {"product_name": "清单里的同款货", "qty": 2},
                {"product_name": "清单里的同款货", "qty": 3},
                {"product_name": "清单里第一次出现的新款", "qty": 1},
            ])
        self.assertEqual(plan["total"], 2, plan)
        self.assertEqual(plan["reuse"], 1, "同名那款并进库里已有的号")
        self.assertEqual(plan["fresh"], 1, "第一次出现的款要新发号")
        head = [g for g in plan["groups"] if g["name"] == "清单里的同款货"][0]
        self.assertEqual(head["qty"], 5, "两行合计 5 台")
        self.assertTrue(head["fish_code"].startswith("YS"), head)
        self.assertFalse(head["fresh"])

    def test_25_import_can_merge_into_one_fish_code(self):
        """导入支持合并：整批并入指定渔货号（同一款被拆成几张表导时用）。"""
        batch = self._batch()
        with SessionLocal() as s:
            target = str(intake.register(s, batch.id, [
                {"sku_code": "TG-0", "product_name": "被并入的目标款"}
            ], operator="tester")[0].fish_code or "")
        result = importer.apply_import(
            SessionLocal(), batch.id,
            headers=["货号", "商品名称", "品类"],
            rows=[["M-1", "另外一款货", "电饭煲"], ["M-2", "另外一款货", "电饭煲"]],
            mapping={0: "sku_code", 1: "product_name", 2: "category"},
            operator="tester", fish_code=target,
        )
        self.assertEqual(int(result.get("ok") or 0), 2, result)
        with SessionLocal() as s:
            rows = intake.find_items_by_fish(s, target)
            self.assertEqual(len(rows), 3, "原本 1 台 + 导进来 2 台都在这一款下")
            codes = sorted(str(r.sku_code or "") for r in rows)
            self.assertEqual(codes, ["M-1", "M-2", "TG-0"])

    # ------------------------------------------------------ ③ 检测台归属弹窗
    def test_30_item_json_candidates(self):
        """检测台要能捕捉到「库里还有别的渔货号跟这台货同名」。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "CD-1", "product_name": "同名不同号的一款"},
            {"sku_code": "CD-2", "product_name": "同名不同号的一款"},
        ])
        with SessionLocal() as s:
            # 本来同一款（认亲），把第二台拆出来 —— 造出「同名不同号」
            intake.set_fish_code(s, s.get(WmsItem, ids[1]), "", operator="tester")
            first = s.get(WmsItem, ids[0])
            second = s.get(WmsItem, ids[1])
            self.assertNotEqual(str(first.fish_code or ""), str(second.fish_code or ""))
            barcode = first.barcode
            other = str(second.fish_code or "")
            # 自己现在挂的那个号不该出现在候选里
            self.assertEqual(intake.fish_candidates(s, first)[0]["fish_code"], other)
            self.assertEqual(intake.fish_candidates(s, s.get(WmsItem, ids[1]))[0]["fish_code"],
                             str(first.fish_code or ""))
        data = self.client.get(f"/wms/api/item.json?code={barcode}").json()
        self.assertIn("fish_candidates", data["item"])
        self.assertEqual(data["item"]["fish_candidates"][0]["fish_code"], other)

    def test_35_inspect_page_shows_fish_and_dialog(self):
        """检测台：渔货号摆在第一格并高亮，同款归属弹窗要在页面上。"""
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="fishMask"', page.text, "同款归属弹窗")
        self.assertIn("并进这一款", page.text)
        self.assertIn("fish-hot", page.text, "渔货号要高亮显示")
        self.assertIn("渔货号", page.text)
        self.assertIn("/wms/api/item/fish-code", page.text)

    def test_40_api_merge_item_into_fish_code(self):
        """检测台点「并进这一款」：这台货归过去，那一款的台数跟着加。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "MG-1", "product_name": "合并来源款", "qty": 2},
            {"sku_code": "MG-2", "product_name": "合并来源款", "qty": 3},
        ])
        with SessionLocal() as s:
            intake.set_fish_code(s, s.get(WmsItem, ids[1]), "", operator="tester")
            target = str(s.get(WmsItem, ids[0]).fish_code or "")
        resp = self.client.post(
            "/wms/api/item/fish-code",
            json={"item_id": ids[1], "fish_code": target})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["fish_code"], target)
        self.assertEqual(body["items"], 2, "那一款下面 2 个条码商品")
        with SessionLocal() as s:
            rows = intake.find_items_by_fish(s, target)
            self.assertEqual(sum(int(r.qty or 0) for r in rows), 5, "合计 5 台（加了库存）")
        # 不存在的货品 / 空号拆出：空号 = 单独发新号
        self.assertEqual(self.client.post(
            "/wms/api/item/fish-code",
            json={"item_id": 99999999, "fish_code": target}).status_code, 404)

    # ------------------------------------------------------ ② 商品详情页
    def test_50_goods_detail_facts(self):
        """商品详情页（主 key = 渔货号）：库存分布按台数聚合。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "DT-1", "product_name": "详情页聚合款", "qty": 2},
            {"sku_code": "DT-2", "product_name": "详情页聚合款", "qty": 3},
        ])
        with SessionLocal() as s:
            for i in ids:
                it = s.get(WmsItem, i)
                it.status = STATUS_SELLABLE
            s.commit()
            fish = str(s.get(WmsItem, ids[0]).fish_code or "")
        page = self.client.get(f"/wms/goods/{fish}")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("商品信息", page.text)
        self.assertIn("库存分布", page.text)
        self.assertIn("合计 5 台", page.text)
        self.assertIn("可售 5 台", page.text)
        self.assertIn(fish, page.text)
        for it_code in ("DT-1", "DT-2"):
            self.assertIn(it_code, page.text, "这一款下的条码商品都要在")

    # ------------------------------------------------------ ⑤ 独立渔货号重新建档
    def test_45_standalone_button_and_dialog_on_inspect(self):
        """检测台「② 商品入库信息」右上角要有「独立渔货号重新建档」入口与确认弹窗。"""
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="btnFishStandalone"', page.text)
        self.assertIn("独立渔货号重新建档", page.text)
        self.assertIn('id="saMask"', page.text, "建档前要把影响摆清楚的确认弹窗")
        self.assertIn("/wms/api/item/fish-standalone", page.text)

    def test_46_item_json_reports_fish_mates(self):
        """item.json 要把「这个渔货号下几个条码商品 / 合计几台」给检测台。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "SA-1", "product_name": "独立建档款", "qty": 2},
            {"sku_code": "SA-2", "product_name": "独立建档款", "qty": 3},
        ])
        with SessionLocal() as s:
            barcode = s.get(WmsItem, ids[0]).barcode
        item = self.client.get(f"/wms/api/item.json?code={barcode}").json()["item"]
        self.assertEqual(item["fish_items"], 2)
        self.assertEqual(item["fish_qty"], 5)

    def test_47_api_standalone_gets_new_fish_code(self):
        """点「独立建档」：这一台另发一个新渔货号 → 原号少一台、新号就它自己。"""
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "SB-1", "product_name": "要拆出来的一款", "qty": 2},
            {"sku_code": "SB-2", "product_name": "要拆出来的一款", "qty": 3},
        ])
        with SessionLocal() as s:
            old = str(s.get(WmsItem, ids[1]).fish_code or "")
            self.assertEqual(str(s.get(WmsItem, ids[0]).fish_code or ""), old,
                             "同一款一开始是同一个渔货号")
        resp = self.client.post("/wms/api/item/fish-standalone", json={"item_id": ids[1]})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertTrue(body["ok"], body)
        new = str(body["fish_code"])
        self.assertTrue(new.startswith("YS"), new)
        self.assertNotEqual(new, old, "独立建档必须另发一个新号")
        self.assertEqual(body["from_code"], old)
        self.assertEqual(body["items"], 1)
        self.assertEqual(body["qty"], 3, "这台货 3 台一起走")
        with SessionLocal() as s:
            self.assertEqual(len(intake.find_items_by_fish(s, new)), 1)
            mates = intake.find_items_by_fish(s, old)
            self.assertEqual([str(r.sku_code) for r in mates], ["SB-1"], "原号只剩另一台")
            # 审计留痕：看得出这次是「独立渔货号重新建档」，不是普通换绑
            log = s.scalars(select(WmsAuditLog).where(
                WmsAuditLog.entity == "item",
                WmsAuditLog.entity_id == str(ids[1]),
                WmsAuditLog.action == "fish_code",
            ).order_by(WmsAuditLog.id.desc())).first()
            self.assertIsNotNone(log)
            self.assertEqual(str(log.note or ""), "独立渔货号重新建档")
            self.assertEqual(str(log.before or ""), old)
            self.assertEqual(str(log.after or ""), new)

    def test_48_standalone_when_alone_or_without_code(self):
        """本来就独占一个号的货不空转发号；压根没号的货点它就是补发一个新号。"""
        batch = self._batch()
        ids = self._register(batch, [{"sku_code": "SC-1", "product_name": "独苗款"}])
        with SessionLocal() as s:
            before = str(s.get(WmsItem, ids[0]).fish_code or "")
        self.assertTrue(before.startswith("YS"))
        resp = self.client.post("/wms/api/item/fish-standalone", json={"item_id": ids[0]})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["already_alone"])
        self.assertEqual(body["fish_code"], before, "号不许换")
        with SessionLocal() as s:
            self.assertEqual(str(s.get(WmsItem, ids[0]).fish_code or ""), before)
            # 没号的货：建档口子照开（补发）
            it = s.get(WmsItem, ids[0])
            it.fish_code = ""
            s.commit()
        body = self.client.post(
            "/wms/api/item/fish-standalone", json={"item_id": ids[0]}).json()
        self.assertTrue(body["ok"], body)
        self.assertFalse(body.get("already_alone"))
        self.assertTrue(str(body["fish_code"]).startswith("YS"), body)
        self.assertEqual(body["items"], 1)
        with SessionLocal() as s:
            self.assertEqual(str(s.get(WmsItem, ids[0]).fish_code or ""),
                             str(body["fish_code"]))
        # 不存在的货品
        self.assertEqual(self.client.post(
            "/wms/api/item/fish-standalone", json={"item_id": 99999999}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
