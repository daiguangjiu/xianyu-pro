"""2026-10-16 用户拍板：渔货栈订单**按账号匹配显示所属订单**。

· 管理员与「自营账号组」看全量（可按账号筛选）；
· 其余账号（学员等）只看自己店铺账号（店铺档案 name = 闲鱼账号）的单，
  不能把所有人的单都塞给部分权限的账号 —— 详情页也拦（敲 URL 按「不在你名下」处理）；
· 列表加「账号」列 + 账号筛选下拉 + 统一分页组件。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_ORIGIN_SELF,
    ORDER_STATE_NEW,
    Order,
    Shop,
    User,
)
from app.services import shop as shop_service  # noqa: E402
from app.services import users as users_service  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from src.models import DownstreamOrder  # noqa: E402

ACC_A = "学员鱼号A"
ACC_B = "别人鱼号B"
_STU_USERS = ("stu_acc_a", "stu_acc_none")


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(Order).delete(synchronize_session=False)
        s.query(DownstreamOrder).delete(synchronize_session=False)
        s.query(Shop).delete(synchronize_session=False)
        s.query(User).filter(User.username.in_(_STU_USERS)).delete(synchronize_session=False)
        s.commit()


def _user(username: str) -> int:
    with SessionLocal() as s:
        row = s.scalar(select(User).where(User.username == username))
        if row is not None:
            return row.id
        row = users_service.create_user(
            s, username, "stu-pass-2026", role="user", portals="self",
            display_name=username,
        )
        return row.id


def _shop(name: str, owner_user_id: int) -> int:
    with SessionLocal() as s:
        row = s.scalar(select(Shop).where(Shop.name == name))
        if row is not None:
            if row.owner_user_id != owner_user_id:
                row.owner_user_id = owner_user_id
                s.commit()
            return row.id
        row = shop_service.create_shop(
            s, {"name": name}, source="测试建档",
            owner_user_id=owner_user_id,
        )
        return row.id


def _dso(order_no: str, *, raw_account: str = "") -> None:
    with SessionLocal() as s:
        s.add(DownstreamOrder(
            downstream_order_id=order_no,
            channel="xianyu",
            channel_sku_id="XY-SKU-1",
            buyer_name="李四",
            qty=1,
            sale_price=Decimal("120.00"),
            status="pending",
            raw_json={"shop_name": raw_account} if raw_account else {},
        ))
        s.commit()


def _hub_order(order_no: str, *, account: str = "") -> None:
    with SessionLocal() as s:
        s.add(Order(
            platform="xianyu",
            order_no=order_no,
            order_state=ORDER_STATE_NEW,
            origin=ORDER_ORIGIN_SELF,
            product_name="测试-订单账号匹配",
            quantity=1,
            sale_amount=120.0,
            deal_amount=120.0,
            receiver_name="李四",
            xianyu_account=account,
        ))
        s.commit()


class OrderAccountScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self):
        _wipe()
        # 两笔已建档订单，账号分别在 A / B 名下；再一笔未建档的（raw_json 里记 A）
        _dso("TXYA0001")
        _hub_order("TXYA0001", account=ACC_A)
        _dso("TXYB0001")
        _hub_order("TXYB0001", account=ACC_B)
        _dso("TXYA0002", raw_account=ACC_A)
        # 学员账号 + 只挂 A 这家店
        self.stu_id = _user("stu_acc_a")
        _shop(ACC_A, self.stu_id)

    def tearDown(self):
        _wipe()

    def _get(self, path: str, user_id: int):
        val, _uid = make_session(user_id)
        return self.client.get(path, cookies={COOKIE_NAME: val})

    # ------------------------------------------------------------ 管理员
    def test_10_admin_sees_all_with_account_column_and_filter(self):
        page = self._get("/self/orders", 1)
        self.assertEqual(page.status_code, 200)
        html = page.text
        for no in ("TXYA0001", "TXYB0001", "TXYA0002"):
            self.assertIn(no, html, "管理员看全量")
        self.assertIn(">账号</th>", html, "账号列在")
        self.assertIn("全部账号", html, "账号筛选下拉在")
        self.assertIn(ACC_A, html)
        self.assertIn(ACC_B, html)
        self.assertNotIn("最近 200", html)
        # 按账号筛选：只看 B 名下那笔
        only_b = self._get("/self/orders?account=" + ACC_B, 1).text
        self.assertIn("TXYB0001", only_b)
        self.assertNotIn("TXYA0001", only_b)
        # 统一分页组件（全站同一份 _pager.html）
        self.assertIn("pager-wrap", only_b)

    # ------------------------------------------------------------ 学员
    def test_20_student_sees_only_own_account_orders(self):
        page = self._get("/self/orders", self.stu_id)
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("TXYA0001", html, "自己店铺账号的单看得到")
        self.assertIn("TXYA0002", html, "raw_json 里记的账号也算数（未建档的单）")
        self.assertNotIn("TXYB0001", html, "别人的账号的单不给看")
        self.assertIn("只显示你名下店铺账号的单", html)

    def test_21_student_detail_of_other_account_is_blocked(self):
        resp = self.client.get(
            "/self/orders/TXYB0001",
            cookies={COOKIE_NAME: make_session(self.stu_id)[0]},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, "别人的单按提示打回列表")
        self.assertIn("不在你名下的店铺账号", unquote(resp.headers["location"]))
        own = self._get("/self/orders/TXYA0001", self.stu_id)
        self.assertEqual(own.status_code, 200)
        self.assertIn("TXYA0001", own.text)

    def test_22_student_without_shops_gets_hint(self):
        uid = _user("stu_acc_none")
        page = self._get("/self/orders", uid)
        self.assertEqual(page.status_code, 200)
        self.assertIn("先去「店铺管理」", page.text)
        self.assertNotIn("TXYA0001", page.text, "没挂店铺的账号一单都看不到")


if __name__ == "__main__":
    unittest.main()
