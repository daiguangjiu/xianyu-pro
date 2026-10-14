"""流程说明弹窗、关闭完成子页面、订单详情的区块权限。

业务口径：
  1 流程说明就在当前页面弹开（内容与 /help 说明页同源：app/services/help.py 一份）；
  2 订单归档下有「关闭完成」子页面：逆向终态 + 推单已完成的订单，不再参与推单与统计；
  3 订单详情按人给内容：页面上的 8 个板块（订单提示、退单处理、推单信息、订单处理日志、
    订单信息、订单截图、编辑订单、合并订单）都能在角色里单独勾（见 web/permissions.BLOCKS）；
    没勾的板块只看到一张「没有查看…的权限」的占位卡，改数据的动作（编辑 / 推单 / 合并）
    直接 POST 也会被拦下。
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
from app.models import (  # noqa: E402
    ORDER_STATE_CLOSED,
    OperationLog,
    Order,
    OrderImage,
    PushBatch,
    PushBatchItem,
    RawMessage,
    Role,
    User,
)
from app.timeutil import now  # noqa: E402
from app.web import permissions  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
SUB = {"username": "blk01", "password": "xianyu2026", "display_name": "区块测试号"}
# 测试里建的角色：每个用例跑完清掉
ROLE_NAMES = ("区块测试角色", "只看资料", "只读角色", "可编辑")


class HelpDialogClosedBlocksTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            # 测试账号与角色也清掉：留着会串到下一个用例（同一个账号挂上别人的角色）
            s.query(User).filter(User.username.like("blk%")).delete()
            s.query(Role).filter(Role.name.in_(ROLE_NAMES)).delete()
            s.commit()

    def _order(
        self,
        order_no: str,
        *,
        order_state: str = "pushed",
        supplier_status: str = "pushed",
        refund_status: str = "",
        pushed: bool = True,
    ) -> int:
        with SessionLocal() as s:
            order = Order(
                order_no=order_no,
                product_name="测试商品",
                receiver_name="张三",
                receiver_phone="13800000000",
                sale_amount=100,
                cost_amount=60,
                supplier_name="测试供应商",
                pushed_at=now() if pushed else None,
                supplier_status=supplier_status,
                order_state=order_state,
                refund_status=refund_status,
            )
            s.add(order)
            s.commit()
            return order.id

    # ---------------------------------------------------------------- 流程说明弹窗
    def test_help_dialog_opens_inline(self):
        """说明在当前页面弹开：弹窗本体 + 触发按钮都在页面上，内容就是那套流程与状态。"""
        order_id = self._order("HD-1")
        # 退单订单：退单区块（含它的说明按钮）只在退单时出现
        refund_id = self._order(
            "HD-REFUND", order_state="refund_request", supplier_status="pushed", refund_status="pending"
        )
        pages = (
            ("/orders", ("orders",)),
            (f"/orders/{order_id}", ("push",)),
            (f"/orders/{refund_id}", ("push", "refund")),
            ("/batches", ("push",)),
        )
        for url, keys in pages:
            html = self.client.get(url).text
            for key in keys:
                self.assertIn(f'id="help-{key}"', html, f"{url} 缺 {key} 弹窗")
                self.assertIn(f'data-open-modal="help-{key}"', html, f"{url} 缺 {key} 触发按钮")
            self.assertIn('class="help-modal"', html)

        detail = self.client.get(f"/orders/{order_id}").text
        self.assertIn("复制推单文案", detail)  # 推单流程
        self.assertIn("已退款给客户", detail)  # 退单流程
        # 独立说明页照旧在（系统板块下的入口）
        self.assertEqual(self.client.get("/help/push").status_code, 200)

    def test_help_dialog_script_is_versioned(self):
        """脚本要带版本号：不带的话浏览器一直吃缓存里的旧 app.js，点了弹不出来（2026-10-01 的坑）。"""
        html = self.client.get("/orders").text
        self.assertRegex(html, r'src="/static/app\.js\?v=[^"]+"')
        self.assertIn("data-close-modal", html)  # 弹窗里有「知道了 / 关闭」按钮

    # ---------------------------------------------------------------- 关闭完成子页面
    def test_closed_subpage_only_finished_orders(self):
        """/orders?closed=1：关闭完成（终态）+ 推单已完成，在途订单不出现。"""
        self._order(
            "CL-CLOSED", order_state=ORDER_STATE_CLOSED, supplier_status="pushed", refund_status="closed"
        )
        self._order("CL-DONE", order_state="shipped", supplier_status="done")
        self._order("CL-OPEN", order_state="pushed", supplier_status="pushed")

        html = self.client.get("/orders?closed=1").text
        self.assertIn("CL-CLOSED", html)
        self.assertIn("CL-DONE", html)
        self.assertNotIn("CL-OPEN", html)
        self.assertIn("订单管理 · 关闭完成", html)
        self.assertIn("不再参与推单", html)  # 页面上要写明这些单不用再操作

    # ---------------------------------------------------------------- 区块权限
    def _role(self, name: str, blocks: list[str], menus: list[str] | None = None) -> int:
        """建角色：菜单默认给订单归档，区块按传入的勾（空列表 = 一个都不勾）。"""
        with SessionLocal() as s:
            role = permissions.save_role(s, name=name, menus=menus or ["orders"], blocks=blocks)
            return int(role.id)

    def _sub(self, role_id: int, username: str = SUB["username"]) -> TestClient:
        """挂上角色并登录，返回一个子账号客户端。"""
        self.client.post(
            "/users",
            data={**SUB, "username": username, "role": "user", "role_id": str(role_id)},
            follow_redirects=False,
        )
        client = TestClient(app)
        client.post(
            "/login", data={"username": username, "password": SUB["password"]},
            follow_redirects=False,
        )
        return client

    def test_blocks_hidden_without_permission(self):
        """一个区块都没勾：订单详情的受限板块只给占位提示，内容不出现在页面上。"""
        order_id = self._order("BP-1")
        html = self._sub(self._role("区块测试角色", blocks=[])).get(f"/orders/{order_id}").text

        self.assertNotIn('id="push-text"', html, "没勾「推单信息」就不该看到推单文案")
        self.assertNotIn('name="merge_order_no"', html, "没勾「合并订单」就不该看到合并表单")
        self.assertNotIn('name="order_no"', html, "没勾「编辑订单」就不该看到编辑表单")
        self.assertNotIn("张三", html, "没勾「订单信息」就不该看到收货人")
        self.assertIn("没有查看", html, "要给一句「没有查看…的权限」并说明去哪儿申请")
        for title in ("推单信息", "订单处理日志", "订单信息", "订单截图", "编辑订单", "合并订单"):
            self.assertIn(title, html, f"缺「{title}」板块的占位提示")

    def test_blocks_are_configured_one_by_one(self):
        """按板块分别授权：只勾「订单信息 + 订单截图」，这两个能看、其余仍锁着。"""
        order_id = self._order("BP-2")
        html = (
            self._sub(self._role("只看资料", blocks=["order_info", "order_images"]))
            .get(f"/orders/{order_id}")
            .text
        )
        self.assertIn("张三", html, "勾了「订单信息」就该看到收货人")
        self.assertNotIn('id="push-text"', html, "没勾「推单信息」依旧看不到推单文案")
        self.assertNotIn('name="order_no"', html, "没勾「编辑订单」依旧看不到编辑表单")

    def test_write_actions_need_block_permission(self):
        """页面把表单藏了，直接 POST 也得拦住：编辑 / 推单 / 合并各过一遍区块权限。"""
        order_id = self._order("BP-3")
        sub = self._sub(self._role("只读角色", blocks=["order_info"]))
        for url in (f"/orders/{order_id}/edit", f"/orders/{order_id}/push"):
            self.assertEqual(sub.post(url, data={}).status_code, 403, f"{url} 应被拦下")
        self.assertEqual(
            sub.post(f"/orders/{order_id}/merge", data={"merge_order_no": "X"}).status_code,
            403,
        )
        # 勾上「编辑订单」就能提交（不再被 403 拦下）
        editor = self._sub(self._role("可编辑", blocks=["order_edit"]), username="blk02")
        self.assertNotEqual(
            editor.post(f"/orders/{order_id}/edit", data={}).status_code,
            403,
            "勾了「编辑订单」就该能保存",
        )

        # 管理员不受影响
        admin_html = self.client.get(f"/orders/{order_id}").text
        self.assertIn('id="push-text"', admin_html)
        self.assertIn('name="merge_order_no"', admin_html)

    def test_blocks_granted_by_role(self):
        """角色里勾了区块，账号就看得见：勾一个给一个。"""
        order_id = self._order("BP-2")
        self.client.post(
            "/roles/create",
            data={"name": "推单员", "description": "能看推单", "menus": ["orders"], "blocks": ["order_push"]},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            role_id = s.scalar(select(Role.id).where(Role.name == "推单员"))
        self.client.post(
            "/users",
            data={"username": "blk02", "password": "xianyu2026", "display_name": "推单员", "role": "user", "role_id": str(role_id)},
            follow_redirects=False,
        )
        sub = TestClient(app)
        sub.post("/login", data={"username": "blk02", "password": "xianyu2026"}, follow_redirects=False)
        html = sub.get(f"/orders/{order_id}").text
        self.assertIn('id="push-text"', html, "勾了「推单信息」就该看到")
        self.assertNotIn('name="merge_order_no"', html, "没勾「合并订单」就仍看不到")


if __name__ == "__main__":
    unittest.main()
