"""采购节点配置区（2026-10-04 用户拍板）：渔枢系统「货源 · 采购节点」页维护活动日历。

以前节点写死在 src/goods/catalog.py 的 PURCHASE_NODES 常量里，改个日期要发版。
现在落表（app/models.py::PurchaseNode）+ 配置页，渔货栈货源广场同库读这张表
（src/goods/catalog.py::hub_purchase_nodes，60 秒缓存），读不到才回落到内置常量。
"""
from __future__ import annotations

import datetime
import sys
import unittest
from datetime import datetime as _dt, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PurchaseNode, PurchaseNodeItem  # noqa: E402
from app.services import purchase_node as node_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import hub_node_items, purchase_node_windows, reset_hub_caches  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _reset() -> None:
    with SessionLocal() as s:
        s.query(PurchaseNodeItem).delete()
        s.query(PurchaseNode).delete()
        s.query(SupplierItem).delete()
        s.commit()


class NodeConfig(unittest.TestCase):
    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        with SessionLocal() as s:
            node_service.ensure_seeds(s)
        # 渔货栈读渔枢系统配置有 60 秒缓存：测试里改完表要立刻看到效果，先清掉
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def test_10_seeded_and_page(self):
        """内置节点播种到了，配置页能打开。"""
        with SessionLocal() as s:
            keys = [n.node_key for n in node_service.list_nodes(s)]
        for want in ("mid_autumn", "national_day", "double11"):
            self.assertIn(want, keys)
        html = self.client.get("/purchase-nodes").text
        self.assertIn("采购节点", html)
        self.assertIn("双11大促", html)
        self.assertIn("当前状态", html, "页面上要给运营看渔货栈看到的样子")

    def test_20_create_edit_toggle_delete(self):
        """增删改停：新增 → 改日期关键词 → 关闭 → 删除。"""
        self.client.post("/purchase-nodes/create", data={
            "name": "开学季", "node_key": "back_school", "icon": "bi-mortarboard",
            "start_md": "8-25", "end_md": "09-10", "keywords": "书包，文具\n台灯", "sort_order": "15",
        })
        with SessionLocal() as s:
            node = s.scalars(select(PurchaseNode).where(PurchaseNode.node_key == "back_school")).first()
        self.assertIsNotNone(node)
        self.assertEqual(node.start_md, "08-25", "MM-DD 要补零")
        self.assertEqual(node.keywords, "书包\n文具\n台灯", "逗号 / 顿号都要拆成一行一个")

        self.client.post(f"/purchase-nodes/{node.id}/edit",
                         data={"name": "开学季（已调整）", "start_md": "08-20", "end_md": "09-15"})
        with SessionLocal() as s:
            node = s.get(PurchaseNode, node.id)
        self.assertEqual(node.name, "开学季（已调整）")
        self.assertEqual(node.start_md, "08-20")
        self.assertEqual(node.end_md, "09-15")

        self.client.post(f"/purchase-nodes/{node.id}/toggle")
        with SessionLocal() as s:
            self.assertFalse(s.get(PurchaseNode, node.id).enabled)
        self.client.post(f"/purchase-nodes/{node.id}/delete")
        with SessionLocal() as s:
            self.assertIsNone(s.get(PurchaseNode, node.id))

    def test_30_bad_dates_rejected(self):
        """日期窗口非法就拦下来，不能把坏数据写进运营位。"""
        html = self.client.post("/purchase-nodes/create", data={
            "name": "乱填的节点", "start_md": "13-40", "end_md": "01-01",
        }, follow_redirects=True).text
        self.assertIn("日期窗口要填成 MM-DD", html)
        with SessionLocal() as s:
            self.assertIsNone(s.scalars(
                select(PurchaseNode).where(PurchaseNode.name == "乱填的节点")).first())

    def test_40_harbor_reads_the_table(self):
        """渔货栈读的是这张表（不是常量）：改日期 → 窗口状态跟着变；关掉 → 不再出现。"""
        with SessionLocal() as s:
            node = s.scalars(select(PurchaseNode).where(PurchaseNode.node_key == "double11")).first()
            node.start_md = "10-01"
            node.end_md = "10-20"
            node.name = "双11（表里改过的）"
            node.sort_order = 0
            off = s.scalars(select(PurchaseNode).where(PurchaseNode.node_key == "new_year")).first()
            off.enabled = False
            s.commit()
        reset_hub_caches()

        windows = purchase_node_windows(datetime.date(2026, 10, 4))
        names = [w["name"] for w in windows]
        self.assertIn("双11（表里改过的）", names)
        self.assertNotIn("年货节", names, "关掉的节点不该出现在渔货栈")
        hit = [w for w in windows if w["name"] == "双11（表里改过的）"][0]
        self.assertEqual(hit["status"], "ongoing")
        self.assertEqual(hit["id"], "double11", "窗口 id 用 node_key，活动链接靠它")

        feed = self.client.get("/self/harbor").text
        self.assertIn("双11（表里改过的）", feed)
        self.assertNotIn("年货节", feed)

    def test_50_sorting_rule(self):
        """排序口径：进行中最前 → 即将开始 → 已结束沉底；同组内按运营排序号。"""
        with SessionLocal() as s:
            node = s.scalars(select(PurchaseNode).where(PurchaseNode.node_key == "double11")).first()
            node.start_md = "10-10"   # 还有 6 天
            node.end_md = "10-20"
            node.sort_order = 1       # 运营把它排到最前
            node.name = "排最前的活动"
            s.commit()
        reset_hub_caches()
        windows = purchase_node_windows(datetime.date(2026, 10, 4))
        first_upcoming = [w for w in windows if w["status"] == "upcoming"][0]
        self.assertEqual(first_upcoming["name"], "排最前的活动")
        self.assertTrue(all(w["status"] != "passed" for w in windows[: len(windows) - 1]),
                        "已结束的节点要沉到底部")

    def test_60_cross_year_window(self):
        """跨年节点（12-25 ~ 01-20）自动落到次年，窗口连续。"""
        windows = purchase_node_windows(datetime.date(2026, 12, 30))
        hit = [w for w in windows if w["id"] == "new_year"][0]
        self.assertEqual(hit["status"], "ongoing")
        self.assertEqual(hit["end"].year, 2027)


    # ------------------------------------------ 手动配置的商品（2026-10-11）
    def _item(self, item_id: str, title: str, *, listed: bool = True) -> None:
        """造一款渔货栈货源（手动配置只能配库里有的货源）。"""
        with SessionLocal() as s:
            s.add(SupplierItem(
                item_id=item_id,
                title=title,
                is_listed=listed,
                audit_status="approved",
                cost_price=Decimal("10.00"),
                stock=100,
                last_synced_at=_dt.now(timezone.utc),
            ))
            s.commit()

    def _node_id(self, key: str = "double11") -> int:
        with SessionLocal() as s:
            row = s.scalars(select(PurchaseNode).where(PurchaseNode.node_key == key)).first()
            return int(row.id) if row else 0

    def test_70_manual_item_add_and_page(self):
        """手动配一款：落库 + 标题快照 + 页面上（节点页）看得到。"""
        self._item("hub-prd-901", "手动配的保温杯")
        node_id = self._node_id()
        resp = self.client.post(
            f"/purchase-nodes/{node_id}/items/add",
            data={"item_id": "hub-prd-901", "note": "今年主推这款"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            rows = node_service.node_items(s, node_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item_id, "hub-prd-901")
        self.assertEqual(rows[0].title, "手动配的保温杯", "配的时候留一份标题快照")
        self.assertEqual(rows[0].note, "今年主推这款")
        self.assertTrue(rows[0].enabled)

        html = self.client.get("/purchase-nodes").text
        self.assertIn("手动配置的商品", html)
        self.assertIn("手动配的保温杯", html)
        self.assertIn("hub-prd-901", html)

    def test_75_duplicate_and_unknown_rejected(self):
        """同一款不能配两遍；渔货栈里没有的编号直接拦下（免得配上一串手滑的编号）。"""
        self._item("hub-prd-902", "只配一次的货")
        node_id = self._node_id()
        self.client.post(f"/purchase-nodes/{node_id}/items/add", data={"item_id": "hub-prd-902"})
        again = self.client.post(
            f"/purchase-nodes/{node_id}/items/add",
            data={"item_id": "hub-prd-902"}, follow_redirects=True,
        )
        self.assertIn("已经在这个节点里了", again.text)
        bad = self.client.post(
            f"/purchase-nodes/{node_id}/items/add",
            data={"item_id": "no-such-item"}, follow_redirects=True,
        )
        self.assertIn("渔货栈里没有编号", bad.text)
        with SessionLocal() as s:
            self.assertEqual(len(node_service.node_items(s, node_id)), 1)

    def test_80_move_and_toggle(self):
        """上下挪位改顺序；停用只是不置顶（配置留着）；撤下才真删。"""
        self._item("A-1", "第一款")
        self._item("A-2", "第二款")
        node_id = self._node_id()
        for item_id in ("A-1", "A-2"):
            self.client.post(f"/purchase-nodes/{node_id}/items/add", data={"item_id": item_id})
        with SessionLocal() as s:
            rows = node_service.node_items(s, node_id)
        self.assertEqual([r.item_id for r in rows], ["A-1", "A-2"])

        self.client.post(f"/purchase-nodes/items/{rows[1].id}/move", data={"dir": "up"})
        with SessionLocal() as s:
            self.assertEqual(
                [r.item_id for r in node_service.node_items(s, node_id)], ["A-2", "A-1"]
            )

        self.client.post(f"/purchase-nodes/items/{rows[0].id}/toggle")
        with SessionLocal() as s:
            self.assertFalse(s.get(PurchaseNodeItem, rows[0].id).enabled)

        self.client.post(f"/purchase-nodes/items/{rows[0].id}/remove")
        with SessionLocal() as s:
            self.assertIsNone(s.get(PurchaseNodeItem, rows[0].id))

    def test_85_harbor_manual_first_and_in_activity(self):
        """渔货栈：手动配置的排最前；标题不命中关键词也算这个活动的货。"""
        node_id = self._node_id()
        with SessionLocal() as s:
            node = s.get(PurchaseNode, node_id)
            node.keywords = "保温杯"          # 关键词规则不变，只认保温杯
            node.start_md, node.end_md = "10-01", "10-20"
            s.commit()
        self._item("hub-prd-903", "大容量保温杯")
        self._item("hub-prd-904", "静音无线鼠标")   # 标题不含关键词，靠手动配进活动
        self.client.post(f"/purchase-nodes/{node_id}/items/add", data={"item_id": "hub-prd-904"})
        reset_hub_caches()

        self.assertEqual(hub_node_items(force=True).get("double11"), ["hub-prd-904"])
        feed = self.client.get("/self/harbor?node=double11").text
        self.assertIn("大容量保温杯", feed, "关键词命中的照旧在")
        self.assertIn("静音无线鼠标", feed, "手动配置的商品也要算这个活动的货")

        # 活动页的「采购节点」板块：手动配置的占据主推位（排在关键词命中的前面）
        act = self.client.get("/self/harbor?node=double11").text
        # 用完整标签定位：页面内联 CSS 里也有 node-main 这几个字
        idx = act.find('<div class="node-main">')
        self.assertGreater(idx, 0, "活动页要有节点板块")
        self.assertIn("静音无线鼠标", act[idx:idx + 1200], "手动配置的要排在最前面（主推位）")

    def test_90_delete_node_clears_items(self):
        """删节点连手动配置一起删，不留孤儿行。"""
        self._item("hub-prd-905", "跟着节点走的货")
        node_id = self._node_id()
        self.client.post(f"/purchase-nodes/{node_id}/items/add", data={"item_id": "hub-prd-905"})
        self.client.post(f"/purchase-nodes/{node_id}/delete")
        with SessionLocal() as s:
            self.assertEqual(len(node_service.node_items(s, node_id)), 0)


if __name__ == "__main__":
    unittest.main()
