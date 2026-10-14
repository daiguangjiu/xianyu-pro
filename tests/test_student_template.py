"""学员模板（2026-10-11）：群里学员出的单 + 店铺「性质 / 所属带教」。

口径（用户拍板）：
  · 新模板在老模板后面多了三行：学员姓名 / 学员店铺名称 / 带教（老字段照旧认）；
  · 订单上**不存**这三个字段，只用来在店铺档案里建档：
    学员店铺名称 = 店铺名（这种单没有出货闲鱼号，它同时当这单的出货账号），
    学员姓名 = 账号运营人，带教 = 所属带教；
  · 店铺管理新增「店铺性质」下拉（自营店铺 / 学员店铺），与老的「店铺类型」
    （闲鱼认证身份）是两个维度、各管各的；老店铺一律「自营店铺」，不动；
  · 只有学员店铺才有「所属带教」这一栏（页面里选了学员店铺才放出来）；
  · 退单 / 撤单消息里带同样的字段时，学员店铺一样认领 / 建档。
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
    SHOP_NATURE_DEFAULT,
    SHOP_NATURE_STUDENT,
    Order,
    Shop,
)
from app.parser.engine import parse_text  # noqa: E402
from app.services import shop as shop_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 用户给的学员模板（售价靠截图补，模板里没有；备注字段老规则已认）
STUDENT_TEXT = """产品：Wilson威尔胜小熊网球拍 103型号2号柄 全碳素一体黑色
收货人：皮皮
手机号码：15308253706
地址信息：广东省 深圳市 坪山区 坪山街道 和平社区黄果坜村5-3号万家和公寓103号房

成本价：95

所属供应链：BH001
学员姓名：王小美
学员店铺名称：小美的闲置小店
带教：李老师
"""

# 老模板（有出货闲鱼号、没有学员那三行）
NORMAL_TEXT = """产品：倍思Q2 充电宝
收货人：张三
手机号码：13800138000
地址信息：安徽省阜阳市临泉县高塘镇范兴集街道
售价：59
成本价：45
出货闲鱼号：太阳味汽水
所属供应链：BH001
"""


class StudentTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Shop).delete()
            s.commit()

    def tearDown(self):
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Shop).delete()
            s.commit()

    # ---------------------------------------------------------------- 解析
    def test_10_parse_student_fields(self):
        """学员模板：三行新字段都要取到，老字段照旧（收货人不能被「学员姓名」抢走）。"""
        fields = parse_text(STUDENT_TEXT).fields
        self.assertEqual(fields.get("student_name"), "王小美")
        self.assertEqual(fields.get("student_shop"), "小美的闲置小店")
        self.assertEqual(fields.get("coach"), "李老师")
        # 老字段照旧：收货人是「皮皮」而不是学员姓名（这条正则没有行首锚定，吃过一次亏）
        self.assertEqual(fields.get("receiver_name"), "皮皮")
        self.assertEqual(fields.get("receiver_phone"), "15308253706")
        self.assertEqual(fields.get("cost_amount"), 95.0)
        self.assertEqual(fields.get("supplier_name"), "BH001")
        self.assertIn("网球拍", str(fields.get("product_name") or ""))

    def test_12_normal_template_has_no_student(self):
        """老模板不许凭空冒出学员字段。"""
        fields = parse_text(NORMAL_TEXT).fields
        self.assertNotIn("student_shop", fields)
        self.assertNotIn("student_name", fields)
        self.assertNotIn("coach", fields)
        self.assertEqual(fields.get("xianyu_account"), "太阳味汽水")

    # ------------------------------------------------------------ 自动建档
    def test_20_student_order_builds_student_shop(self):
        """学员出的单：按学员店铺名称建档，性质=学员店铺、运营人=学员姓名、带教=所属带教。"""
        from app.services.ingest import _ensure_shop_of_message

        result = parse_text(STUDENT_TEXT)
        with SessionLocal() as s:
            order = Order(order_no="STU-0001", product_name="Wilson 网球拍")
            s.add(order)
            s.flush()
            _ensure_shop_of_message(s, result, "飞书发送人", order=order)
            s.commit()
            row = s.scalar(select(Shop).where(Shop.name == "小美的闲置小店"))
            self.assertIsNotNone(row, "学员店铺要自动建档")
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT)
            self.assertEqual(row.operator, "王小美", "学员姓名 -> 账号运营人")
            self.assertEqual(row.coach, "李老师", "带教 -> 所属带教")
            self.assertTrue(row.is_student_shop)
            # 这种单没有出货闲鱼号：学员店铺名称补上去，订单才挂得上店铺
            self.assertEqual(order.xianyu_account, "小美的闲置小店")

    def test_22_normal_order_keeps_self_run(self):
        """老模板出的单：店铺性质默认自营，运营人 = 飞书发送人（老口径不变）。"""
        from app.services.ingest import _ensure_shop_of_message

        result = parse_text(NORMAL_TEXT)
        with SessionLocal() as s:
            order = Order(order_no="NRM-0001", product_name="倍思充电宝")
            s.add(order)
            s.flush()
            _ensure_shop_of_message(s, result, "周玉萍", order=order)
            s.commit()
            row = s.scalar(select(Shop).where(Shop.name == "太阳味汽水"))
            self.assertEqual(row.shop_nature, SHOP_NATURE_DEFAULT)
            self.assertEqual(row.operator, "周玉萍")
            self.assertFalse(row.is_student_shop)

    def test_24_reverse_message_also_builds(self):
        """退单 / 撤单消息（不建订单）里带着学员店铺信息时，店铺一样认领 / 建档。"""
        from app.services.ingest import _ensure_shop_of_message

        result = parse_text(STUDENT_TEXT)
        with SessionLocal() as s:
            _ensure_shop_of_message(s, result, "飞书发送人")
            s.commit()
            row = s.scalar(select(Shop).where(Shop.name == "小美的闲置小店"))
            self.assertIsNotNone(row)
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT)
            self.assertEqual(row.coach, "李老师")

    def test_26_existing_shop_marked_student(self):
        """店铺本来就存在（别的渠道建的）时，学员出单把它标成学员店铺、补带教。"""
        with SessionLocal() as s:
            shop_service.create_shop(s, {"name": "小美的闲置小店", "operator": ""})
        with SessionLocal() as s:
            row = shop_service.ensure_student_shop(
                s, "小美的闲置小店", student_name="王小美", coach="李老师"
            )
            s.commit()
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT)
            self.assertEqual(row.operator, "王小美")
            self.assertEqual(row.coach, "李老师")

    # ---------------------------------------------------------------- 档案
    def test_30_default_nature_is_self_run(self):
        """手工建档 / 台账导入没选性质：默认自营店铺（老数据一个都不动）。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "自营一号店"})
            self.assertEqual(row.shop_nature, SHOP_NATURE_DEFAULT)
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, {"name": "学员二号店", "shop_nature": SHOP_NATURE_STUDENT, "coach": "王老师"}
            )
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT)
            self.assertEqual(row.coach, "王老师")
        with SessionLocal() as s:
            # 乱值回落默认，不抛错
            row = shop_service.create_shop(s, {"name": "乱填三号店", "shop_nature": "加盟店"})
            self.assertEqual(row.shop_nature, SHOP_NATURE_DEFAULT)

    def test_32_filter_by_nature(self):
        with SessionLocal() as s:
            shop_service.create_shop(s, {"name": "自营一号店"})
            shop_service.create_shop(
                s, {"name": "学员二号店", "shop_nature": SHOP_NATURE_STUDENT, "coach": "王老师"}
            )
            self.assertEqual(len(shop_service.list_shops(s, nature=SHOP_NATURE_STUDENT)), 1)
            self.assertEqual(len(shop_service.list_shops(s, nature=SHOP_NATURE_DEFAULT)), 1)
            self.assertEqual(len(shop_service.list_shops(s)), 2)

    # ---------------------------------------------------------------- 页面
    def _shop_id(self, name: str) -> int:
        with SessionLocal() as s:
            row = s.scalar(select(Shop).where(Shop.name == name))
            return int(row.id)

    def test_40_list_page_has_nature(self):
        with SessionLocal() as s:
            shop_service.create_shop(
                s, {"name": "学员二号店", "shop_nature": SHOP_NATURE_STUDENT, "coach": "王老师"}
            )
        page = self.client.get("/shops").text
        self.assertIn("店铺性质", page)
        self.assertIn('name="shop_nature"', page)
        self.assertIn('name="nature"', page, "筛选里也要有店铺性质")
        self.assertIn("学员店铺", page)
        self.assertIn("自营店铺", page)

    def test_42_detail_and_edit(self):
        """详情页：学员店铺显示「所属带教」，编辑下拉能改性质与带教。"""
        with SessionLocal() as s:
            shop_service.create_shop(s, {"name": "自营一号店"})
        sid = self._shop_id("自营一号店")
        page = self.client.get(f"/shops/{sid}").text
        self.assertIn("店铺性质", page)
        self.assertIn('name="shop_nature"', page)
        self.assertIn('id="f-coach-field"', page)
        self.assertNotIn("所属带教</div><div class=\"v\">王老师", page, "自营店铺不该展示带教")

        resp = self.client.post(
            f"/shops/{sid}/edit",
            data={
                "name": "自营一号店",
                "shop_nature": SHOP_NATURE_STUDENT,
                "coach": "王老师",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, sid)
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT)
            self.assertEqual(row.coach, "王老师")
        page = self.client.get(f"/shops/{sid}").text
        self.assertIn("所属带教", page)
        self.assertIn("王老师", page)

    def test_44_edit_without_nature_keeps_it(self):
        """编辑表单没带性质那一栏（比如只改微信密码那个表单）：性质保持原值。"""
        with SessionLocal() as s:
            shop_service.create_shop(
                s, {"name": "学员二号店", "shop_nature": SHOP_NATURE_STUDENT, "coach": "李老师"}
            )
        sid = self._shop_id("学员二号店")
        self.client.post(
            f"/shops/{sid}/edit", data={"name": "学员二号店", "wechat": "wxid-1"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            row = s.get(Shop, sid)
            self.assertEqual(row.shop_nature, SHOP_NATURE_STUDENT, "没带的字段不许被清掉")
            self.assertEqual(row.coach, "李老师")
            self.assertEqual(row.wechat, "wxid-1")


if __name__ == "__main__":
    unittest.main()
