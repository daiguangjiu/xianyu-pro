"""渔货栈「个人中心 · 地址管理」（2026-10-03）：收货地址簿。

口径：
1. 归属与采购域一致（owner = u{登录用户 id}），各人各的地址；
2. 第一条自动成为默认，设默认会把其它条清掉，删掉默认时剩下第一条顶上；
3. 结算页能从地址簿一键带入（下拉选一条 → 填进收货人 / 手机号 / 详细地址），
   下单时落到采购单的 receiver_* 上 —— 地址以后改了，历史采购单不受影响。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import StockOrder, SupplierItem, UserAddress  # noqa: E402

ITEM_ID = "T-ADDR-1"
ADMIN = {"username": "admin", "password": "xianyu2026"}

ADDR1 = {
    "receiver_name": "戴广久",
    "phone": "13800000001",
    "region": "上海市 闵行区",
    "address": "虹桥镇 万源路 2161 弄 150 号 306 室",
    "tag": "家",
}
ADDR2 = {
    "receiver_name": "仓库收货",
    "phone": "13800000002",
    "region": "广东省 广州市 白云区",
    "address": "石井街道 仓库 A 栋 3 层",
    "tag": "仓库",
}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(UserAddress).delete(synchronize_session=False)
        s.query(StockOrder).delete(synchronize_session=False)
        s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete(
            synchronize_session=False
        )
        s.commit()


def _make_item() -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-1",
                title="测试-全棉四件套 1.5m",
                category_id="ht_bedding",
                main_image="http://127.0.0.1:9/a.jpg",
                images=["http://127.0.0.1:9/a.jpg"],
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                stock=100,
                sku_list={"skus": [{"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": 45.5, "stock": 60}]},
                source_type="third_party",
                audit_status="approved",
                is_listed=True,
                item_code="YHZ-TEST-ADDR",
            )
        )
        s.commit()


class SelfAddressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()
        _make_item()

    def tearDown(self):
        _wipe()

    # ------------------------------------------------ 页面与新增
    def test_10_page_and_empty_state(self):
        page = self.client.get("/self/addresses")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("地址管理", page.text)
        self.assertIn("还没有收货地址", page.text, "空态要给「去新增」的指引")

    def test_20_add_and_first_is_default(self):
        resp = self.client.post("/self/addresses", data=ADDR1, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        self.assertIn("msg=", resp.headers.get("location", ""))

        html = self.client.get("/self/addresses").text
        for text in ("戴广久", "13800000001", "万源路 2161 弄", "默认地址", "家"):
            self.assertIn(text, html, f"地址列表应有「{text}」")
        with SessionLocal() as s:
            row = s.query(UserAddress).one()
            self.assertTrue(row.is_default, "第一条自动成为默认地址")
            self.assertEqual(row.region, "上海市 闵行区")

    def test_25_required_fields(self):
        """收货人 / 手机号 / 详细地址缺一样就拦下，不给存半条地址。"""
        for missing in ("receiver_name", "phone", "address"):
            data = dict(ADDR1)
            data[missing] = ""
            resp = self.client.post("/self/addresses", data=data, follow_redirects=False)
            self.assertEqual(resp.status_code, 303, resp.text[:200])
            self.assertIn("msg=", resp.headers.get("location", ""), f"{missing} 为空应给提示")
        with SessionLocal() as s:
            self.assertEqual(s.query(UserAddress).count(), 0, "三条都没存进去")

    # ------------------------------------------------ 改 / 默认 / 删
    def test_30_update_and_set_default(self):
        self.client.post("/self/addresses", data=ADDR1)
        self.client.post("/self/addresses", data=ADDR2)
        with SessionLocal() as s:
            rows = {r.phone: r for r in s.query(UserAddress).all()}
            id1, id2 = rows["13800000001"].id, rows["13800000002"].id
            self.assertTrue(rows["13800000001"].is_default, "第一条仍是默认")

        # 把第二条设成默认：第一条要自动取消
        self.client.post(f"/self/addresses/{id2}/default", follow_redirects=False)
        with SessionLocal() as s:
            self.assertFalse(s.get(UserAddress, id1).is_default)
            self.assertTrue(s.get(UserAddress, id2).is_default)

        # 改第二条的地址
        self.client.post(
            f"/self/addresses/{id2}/update",
            data={"receiver_name": "仓库收货", "phone": "13800000002",
                  "region": "广东省 广州市 白云区", "address": "石井街道 仓库 B 栋 5 层", "tag": "仓库"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertIn("B 栋", s.get(UserAddress, id2).address)

    def test_40_delete_promotes_another_default(self):
        self.client.post("/self/addresses", data=ADDR1)
        self.client.post("/self/addresses", data=ADDR2)
        with SessionLocal() as s:
            rows = {r.phone: r for r in s.query(UserAddress).all()}
            id1, id2 = rows["13800000001"].id, rows["13800000002"].id
        self.client.post(f"/self/addresses/{id1}/default", follow_redirects=False)

        self.client.post(f"/self/addresses/{id1}/delete", follow_redirects=False)
        with SessionLocal() as s:
            left = s.query(UserAddress).all()
            self.assertEqual(len(left), 1)
            self.assertEqual(left[0].id, id2)
            self.assertTrue(left[0].is_default, "删掉默认后，剩下那条顶成默认")

    # ------------------------------------------------ 结算页带入 + 下单落库
    def test_50_checkout_offers_address_book(self):
        self.client.post("/self/addresses", data=ADDR1)
        html = self.client.get(f"/self/checkout?item_id={ITEM_ID}&sku_id=s1&qty=1").text
        self.assertIn("从地址簿选择", html, "结算页应能从地址簿选")
        self.assertIn("13800000001", html)
        # 默认地址自动回填
        self.assertIn("戴广久", html)
        self.assertIn("万源路 2161 弄", html)

    def test_60_order_carries_picked_address(self):
        """下单带 address_id：收货信息落到采购单上（前端已填好，服务端再兜一道）。"""
        self.client.post("/self/addresses", data=ADDR1)
        with SessionLocal() as s:
            addr_id = s.query(UserAddress).one().id

        resp = self.client.post(
            "/self/checkout/submit",
            data={"source": "buy_now", "item_id": ITEM_ID, "sku_id": "s1", "qty": 1,
                  "address_id": addr_id},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        order_no = resp.headers["location"].rsplit("/", 1)[-1]
        with SessionLocal() as s:
            order = s.query(StockOrder).filter(StockOrder.order_no == order_no).one()
            self.assertEqual(order.receiver_name, "戴广久")
            self.assertEqual(order.receiver_phone, "13800000001")
            self.assertIn("万源路 2161 弄", order.receiver_address or "")
            self.assertIn("上海市 闵行区", order.receiver_address or "")

        # 地址改了，历史采购单不受影响
        with SessionLocal() as s:
            addr = s.get(UserAddress, addr_id)
            addr.address = "虹桥镇 新地址 1 号"
            s.commit()
        with SessionLocal() as s:
            order = s.query(StockOrder).filter(StockOrder.order_no == order_no).one()
            self.assertIn("万源路 2161 弄", order.receiver_address or "", "历史单上的地址不跟着改")

    # ------------------------------------------------ 入口 / 接口
    def test_70_me_popup_has_entry(self):
        html = self.client.get("/self/orders").text
        me = html[html.find('class="sy-me"') : html.find("</details>", html.find('class="sy-me"'))]
        self.assertIn("地址管理", me, "「我的」弹窗里应有地址管理入口")
        self.assertIn('href="/self/addresses"', me)

    def test_75_address_page_visuals(self):
        """地址页视觉口径（2026-10-03 用户拍板）：
        1) 粘贴识别框要够高（placeholder 不再被裁），并说明能识别哪些字段；
        2) 标签（家 / 公司 / 仓库）做成带图标的徽章，摆进姓名那一行；
        3) 默认地址重点强调（左侧橙条 + 浅橙底 + 橙色徽章 + 列表头摘要），
           前提是本页自己声明 --pdp-brand（base 里没有这个变量，缺了橙色标记会「无色」）。
        """
        self.client.post("/self/addresses", data=ADDR1)
        self.client.post("/self/addresses", data=ADDR2)
        html = self.client.get("/self/addresses").text

        # 1) 粘贴识别框
        i = html.find(".paste-box textarea {")
        self.assertGreater(i, -1, "粘贴框样式应该在")
        rule = html[i : html.find("}", i)]
        self.assertIn("min-height: 96px", rule, "粘贴框要够高，placeholder 不被裁")
        self.assertIn('rows="4"', html)
        self.assertIn("全角标点都能认", html, "给出「能识别什么」的说明")

        # 2) 标签徽章（家 / 公司 / 仓库 带图标，与姓名同行）
        self.assertIn('<span class="addr-tag tag-name">', html)
        self.assertIn("bi-house-door-fill", html, "「家」要有图标")
        self.assertIn("bi-box-seam-fill", html, "「仓库」要有图标")
        self.assertIn('id="addrTagList"', html, "标签有可选项")
        self.assertIn("addrSetTag('家')", html, "标签可点选")

        # 3) 默认地址强调
        self.assertIn("--pdp-brand: #FF6A00", html, "本页要自己声明主色")
        self.assertIn(
            ".addr-row.is-default::before { background: var(--pdp-brand); }",
            html,
            "默认那条左侧要有橙色竖条",
        )
        self.assertIn('<span class="addr-tag def">', html)
        self.assertIn("addr-head-def", html)
        self.assertIn("默认：", html, "列表头给出默认地址摘要")

    def test_80_json_api(self):
        self.client.post("/self/addresses", data=ADDR1)
        data = self.client.get("/self/addresses.json").json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["addresses"]), 1)
        self.assertEqual(data["addresses"][0]["phone"], "13800000001")
        self.assertIn("万源路", data["addresses"][0]["full"])


class AddressParseTest(unittest.TestCase):
    """粘贴一整段收货信息 → 认人 / 认电话 / 认省市区 / 认详细地址（2026-10-03）。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()

    def _parse(self, text: str) -> dict:
        from src.self_purchase.address import parse_address_text

        return parse_address_text(text)

    def test_10_plain_one_line(self):
        """一行直给：姓名 电话 省市区+详细地址。"""
        r = self._parse("张三 13800000001 上海市闵行区虹桥镇万源路2161弄150号306室")
        self.assertTrue(r["ok"])
        self.assertEqual(r["receiver_name"], "张三")
        self.assertEqual(r["phone"], "13800000001")
        self.assertEqual(r["region"], "上海市 闵行区")
        self.assertEqual(r["address"], "虹桥镇万源路2161弄150号306室")
        self.assertEqual(r["missing"], [])

    def test_20_labelled_text_with_dashed_phone(self):
        """带标签的转发文本：收货人 / 手机号 / 收货地址，手机号里带横线也要认。"""
        r = self._parse(
            "收货人：李四 手机号：138-0000-0002 收货地址：广东省广州市白云区石井街道仓库A栋3层"
        )
        self.assertEqual(r["receiver_name"], "李四")
        self.assertEqual(r["phone"], "13800000002", "138-0000-0002 归一成 11 位")
        self.assertEqual(r["region"], "广东省 广州市 白云区")
        self.assertEqual(r["address"], "石井街道仓库A栋3层")
        self.assertEqual(r["tag"], "仓库", "文本里写了仓库就带上标签")
        self.assertEqual(r["missing"], [])

    def test_30_name_after_address(self):
        """姓名 / 电话写在地址后面也要认出来，且不把地址当人名。"""
        r = self._parse("上海市 闵行区 万源路 2161 弄 150 号 306 室 戴广久 13800000001")
        self.assertEqual(r["receiver_name"], "戴广久")
        self.assertEqual(r["phone"], "13800000001")
        self.assertEqual(r["region"], "上海市 闵行区")
        self.assertEqual(r["address"], "万源路 2161 弄 150 号 306 室")

        r2 = self._parse("上海市浦东新区张江镇科苑路 88 号 2 号楼 501 室，赵六，13911112222")
        self.assertEqual(r2["receiver_name"], "赵六")
        self.assertEqual(r2["address"], "张江镇科苑路 88 号 2 号楼 501 室")

    def test_40_missing_fields_are_reported(self):
        """只有地址（没电话没人名）：别硬猜，缺什么列出来让用户补。"""
        r = self._parse("只有一段地址 江苏省南京市鼓楼区中山北路 200 号")
        self.assertTrue(r["ok"])
        self.assertEqual(r["region"], "江苏省 南京市 鼓楼区")
        self.assertEqual(r["phone"], "")
        self.assertIn("phone", r["missing"])
        self.assertIn("receiver_name", r["missing"])

    def test_50_empty_text(self):
        r = self._parse("   ")
        self.assertFalse(r["ok"])
        self.assertEqual(r["receiver_name"], "")

    def test_60_parse_api(self):
        """页面上的「智能识别并填入」走这个接口：只解析，不落库。"""
        client = TestClient(app)
        resp = client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]
        with SessionLocal() as s:
            before = s.query(UserAddress).count()
        data = client.post(
            "/self/addresses/parse",
            json={"text": "王五 13900000003 浙江省杭州市西湖区文三路 100 号 5 幢 302 室"},
        ).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["receiver_name"], "王五")
        self.assertEqual(data["region"], "浙江省 杭州市 西湖区")
        self.assertEqual(data["address"], "文三路 100 号 5 幢 302 室")
        with SessionLocal() as s:
            self.assertEqual(s.query(UserAddress).count(), before, "解析不落库")

    def test_70_address_page_has_paste_box(self):
        """地址管理页要有粘贴框与识别按钮（前端把它填进新增表单）。"""
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        html = client.get("/self/addresses").text
        self.assertIn('id="pasteText"', html)
        self.assertIn("智能识别并填入", html)
        self.assertIn("/self/addresses/parse", html)
        self.assertIn('id="newAddrForm"', html)


if __name__ == "__main__":
    unittest.main()
