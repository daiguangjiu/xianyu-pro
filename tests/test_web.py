"""Web 端到端冒烟测试（含飞书事件回调链路）。

运行：python -m unittest tests.test_web
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import func, select  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, OrderImage, PushBatch, Supplier  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
from app.services import stats as stats_service  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402

ORDER_TEXT = (
    "【闲鱼订单】\n订单号：2026010112345678\n买家：小鱼儿\n商品：iPhone 13 128G 午夜黑\n"
    "数量：1\n金额：¥2,999.00\n成本：2400\n收货人：李四\n电话：13800138000\n"
    "地址：广东省深圳市南山区科技园1号\n供应商：华强北数码\n"
)


def _event(message_id: str, content: str, msg_type: str = "text") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
            "token": "",
            "app_id": "cli_test",
            "tenant_key": "t",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user", "tenant_key": "t"},
            "message": {
                "message_id": message_id,
                "create_time": str(int(time.time() * 1000)),
                "chat_id": "oc_test",
                "chat_type": "group",
                "message_type": msg_type,
                "content": content,
            },
        },
    }


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        # 后台要登录：用默认管理员账号（Cookie 由 TestClient 自动带上）
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def test_01_pages(self):
        for path in (
            "/", "/orders", "/batches", "/suppliers", "/messages",
            "/users", "/logs", "/rules", "/settings", "/api/health",
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} 返回 {resp.status_code}")

    def test_02_challenge(self):
        resp = self.client.post("/feishu/event", json={"challenge": "abc", "token": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("challenge"), "abc")

    def test_03_im_event_creates_order(self):
        import json

        payload = _event("om_smoke_1", json.dumps({"text": ORDER_TEXT}, ensure_ascii=False))
        resp = self.client.post("/feishu/event", json=payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("ok"), body)
        self.assertIsNotNone(body.get("order_id"), body)

        with SessionLocal() as s:
            order = s.query(Order).filter(Order.order_no == "2026010112345678").one()
            self.assertEqual(order.sale_amount, 2999.0)
            self.assertEqual(order.supplier_name, "华强北数码")
            self.assertEqual(order.supplier_status, "pending")

    def test_04_order_detail_and_edit(self):
        with SessionLocal() as s:
            order = s.query(Order).filter(Order.order_no == "2026010112345678").one()
            order_id = order.id
        resp = self.client.get(f"/orders/{order_id}")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={"order_no": "2026010112345678", "cost_amount": "2500", "quantity": "1"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).cost_amount, 2500.0)

    def test_05_batch_and_export(self):
        with SessionLocal() as s:
            order = s.query(Order).filter(Order.order_no == "2026010112345678").one()
            batch = supplier_service.create_batch(s, "华强北数码", [order.id])
            batch_id = batch.id
            batch_no = batch.batch_no
        resp = self.client.get(f"/batches/{batch_id}")
        self.assertEqual(resp.status_code, 200)
        csv_resp = self.client.get(f"/batches/{batch_id}/export.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("订单号", csv_resp.text)
        xlsx_resp = self.client.get(f"/batches/{batch_id}/export.xlsx")
        self.assertEqual(xlsx_resp.status_code, 200)
        self.assertGreater(len(xlsx_resp.content), 1000)

        resp = self.client.post(
            f"/batches/{batch_id}/status", data={"status": "pushed"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(PushBatch, batch_id).status, "pushed")
            order = s.query(Order).filter(Order.order_no == "2026010112345678").one()
            self.assertEqual(order.supplier_status, "pushed")

    def test_06_parse_api(self):
        resp = self.client.post("/api/parse-test", json={"text": ORDER_TEXT})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fields"]["order_no"], "2026010112345678")
        self.assertFalse(data["needs_review"])

    def test_07_image_attach_without_download(self):
        """截图消息应挂到上一条订单文本上；无凭证时下载失败但不影响归档。"""
        import json

        payload = _event(
            "om_smoke_img_1",
            json.dumps({"image_key": "img_v2_smoke"}, ensure_ascii=False),
            msg_type="image",
        )
        resp = self.client.post("/feishu/event", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"), resp.json())

        with SessionLocal() as s:
            img = s.query(OrderImage).filter(OrderImage.file_key == "img_v2_smoke").one()
            self.assertEqual(img.message_id, "om_smoke_img_1")
            self.assertIsNotNone(img.order_id)
            self.assertIn(img.ocr_status, ("failed", "skipped", "done"))
            order = s.get(Order, img.order_id)
            self.assertEqual(order.order_no, "2026010112345678")

    def test_08_real_template_without_order_no(self):
        """真实群模板：没有订单截图时先落临时编号（时间+产品），并标记待人工确认。"""
        import json

        text = (
            "产品：亚朵四代单枕头\n\n收货人:贲\n手机号码:14790021215\n"
            "地址信息: 安徽省 滁州市 琅琊区 清流街道 滁州市琅琊区凤凰湖畔18栋\n"
            "                                                             售价：115\n"
            "出货闲鱼号：卖货的小姑娘\n回款支付宝归属人：卫玉珍\n"
        )
        payload = _event("om_real_1", json.dumps({"text": text}, ensure_ascii=False))
        resp = self.client.post("/feishu/event", json=payload)
        self.assertTrue(resp.json().get("ok"), resp.json())
        order_id = resp.json().get("order_id")
        self.assertIsNotNone(order_id)

        with SessionLocal() as s:
            order = s.get(Order, order_id)
            # 没有订单截图时用「时间 + 产品信息」生成的临时编号，等补到截图再换成真实订单编号
            self.assertTrue(order.order_no.startswith("TMP-"))
            self.assertEqual(order.temp_order_no, order.order_no)
            self.assertTrue(order.needs_review)
            self.assertEqual(order.product_name, "亚朵四代单枕头")
            self.assertEqual(order.receiver_name, "贲")
            self.assertEqual(order.receiver_phone, "14790021215")
            self.assertEqual(order.sale_amount, 115.0)
            self.assertEqual(order.xianyu_account, "卖货的小姑娘")
            self.assertEqual(order.payee_name, "卫玉珍")
            self.assertEqual(order.quantity, 1)

        # 人工补上订单号后即可进入待推单发货流程
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={
                "order_no": "33169016650470478780",
                "xianyu_account": "卖货的小姑娘",
                "payee_name": "卫玉珍",
                "needs_review": "0",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.order_no, "33169016650470478780")
            self.assertFalse(order.needs_review)

    def test_09_filters_and_stats(self):
        resp = self.client.get("/orders?account=卖货的小姑娘&payee=卫玉珍")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("33169016650470478780", resp.text)

        with SessionLocal() as s:
            payees = stats_service.by_payee(s)
            self.assertTrue(any(p["payee"] == "卫玉珍" for p in payees))
            accounts = stats_service.by_account(s)
            self.assertTrue(any(a["account"] == "卖货的小姑娘" for a in accounts))

    def test_10_dashboard_with_orders(self):
        """有订单数据后首页（含资金统计）必须正常，不能因日期处理崩掉。"""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("订单", r.text)

    def test_11_supplier_crud(self):
        """供应商档案：新增 / 重名校验 / 编辑 / 删除，并按名称统计关联订单。"""
        # 群消息里识别出的供应商会自动建档（见 supplier.ensure_order_supplier）：
        # 前面的用例已经把「华强北数码」建好了，这里先清掉再走新增流程
        with SessionLocal() as s:
            existed = s.scalar(select(Supplier).where(Supplier.name == "华强北数码"))
        if existed is not None:
            self.client.post(f"/suppliers/{existed.id}/delete", follow_redirects=False)
        resp = self.client.post(
            "/suppliers",
            data={
                "name": "华强北数码",
                "city": "深圳",
                "category": "数码配件",
                "contact_name": "王经理",
                "contact_info": "13800138000",
                "has_group": "1",
                "group_name": "华强北对接群",
                "note": "日结",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        with SessionLocal() as s:
            row = s.scalar(select(Supplier).where(Supplier.name == "华强北数码"))
            supplier_id = row.id
            self.assertEqual(row.city, "深圳")
            self.assertEqual(row.category, "数码配件")
            self.assertEqual(row.contact_name, "王经理")
            self.assertEqual(row.contact_info, "13800138000")
            self.assertTrue(row.has_group)
            self.assertEqual(row.group_name, "华强北对接群")
            # 名称与订单的「所属供应链」一致 -> 自动统计到已有订单
            stat = supplier_service.order_stats_by_name(s).get("华强北数码")
            self.assertGreaterEqual(stat["orders"], 1)
            order_count = stat["orders"]

        # 重名与空名称都不应落库
        for bad in ({"name": "华强北数码"}, {"name": "   "}):
            r = self.client.post("/suppliers", data=bad, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(func.count(Supplier.id)).where(Supplier.name == "华强北数码")),
                1,
            )

        resp = self.client.get(f"/suppliers/{supplier_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("华强北对接群", resp.text)

        # 编辑：取消勾选微信群时群名要一起清空，避免留下脏数据
        resp = self.client.post(
            f"/suppliers/{supplier_id}/edit",
            data={
                "name": "华强北数码",
                "city": "广州",
                "category": "数码配件",
                "contact_name": "李经理",
                "contact_info": "13900139000",
                "note": "周结",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Supplier, supplier_id)
            self.assertEqual(row.city, "广州")
            self.assertEqual(row.contact_name, "李经理")
            self.assertFalse(row.has_group)
            self.assertEqual(row.group_name, "")

        resp = self.client.post(f"/suppliers/{supplier_id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.get(Supplier, supplier_id))
            # 只删档案，订单不受影响
            self.assertEqual(
                s.scalar(select(func.count(Order.id)).where(Order.supplier_name == "华强北数码")),
                order_count,
            )

    @classmethod
    def tearDownClass(cls):
        pass


if __name__ == "__main__":
    unittest.main()
