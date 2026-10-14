"""渔集 · 芯烨条码云打印（XP-420B）：接入适配 + 打印设置可配置化。

云端部署时服务碰不到现场打印机的网口，所以打印走芯烨条码云平台中转
（官方接口：https://www.yuque.com/yikezhineng/tcgo6t/xz045prd11ogpvtl）。

这里测的都是**不联网也能验的事**：
  · 签名算法（user + UserKEY + timestamp 拼起来 sha1）与请求体长什么样；
  · 官方错误码有没有翻成现场看得懂的中文；
  · 账号 / 打印机 / 模板是不是真的存库、改完立刻生效；
  · 模板占位符替换与 XML 转义（品名里带 & 不能把标签打废）；
  · 下发成功留订单号、失败留原因（脏数据不能悄悄吞掉）。
"""
from __future__ import annotations

import hashlib
import json as _json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import cloud_print  # noqa: E402
from wms import intake  # noqa: E402
from wms import labeling  # noqa: E402
from wms import nav as wms_nav  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms import print_settings  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    WmsItem,
    WmsLabelPrint,
    WmsPrintConfig,
    WmsPrinter,
    WmsRole,
)


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsPrinter).delete(synchronize_session=False)
        s.query(WmsPrintConfig).delete(synchronize_session=False)
        s.commit()


def _drop_template() -> None:
    """收尾：把库里的模板配置删掉。

    几个测试文件共用同一个测试库 —— 用例自己写进去的模板（尤其是「故意写坏」的那种）
    不收拾干净，后面测预览的用例就会拿着它渲染，报一串看不懂的失败。
    """
    with SessionLocal() as s:
        for key in ("template", "template_v"):
            row = s.scalar(select(WmsPrintConfig).where(WmsPrintConfig.key == key))
            if row is not None:
                s.delete(row)
        s.commit()


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakeHttpx:
    """假 httpx：只记下请求内容，按预设返回（`cloud_print` 在函数里 import httpx，
    所以塞进 sys.modules 就能拦住，不用真联网）。"""

    calls: list[dict] = []
    reply: dict = {"code": 0, "msg": "ok", "data": "SMI-TEST-0001"}
    boom: Exception | None = None

    @classmethod
    def reset(cls, reply: dict | None = None, boom: Exception | None = None) -> None:
        cls.calls = []
        cls.reply = reply or {"code": 0, "msg": "ok", "data": "SMI-TEST-0001"}
        cls.boom = boom

    @staticmethod
    def post(url, json=None, headers=None, timeout=None, content=None):
        # 现在发的是自己 dumps 的 UTF-8 字节（content=），测试里照样按字典看
        if json is None and content is not None:
            json = _json.loads(bytes(content).decode("utf-8"))
        _FakeHttpx.calls.append(
            {"url": url, "json": json or {}, "headers": headers or {},
             "timeout": timeout, "content": content}
        )
        if _FakeHttpx.boom:
            raise _FakeHttpx.boom
        return _FakeResp(_FakeHttpx.reply)


class CloudPrintApiTest(unittest.TestCase):
    """适配层：签名、请求体、错误码（全部离线验）。"""

    def setUp(self):
        self._old = sys.modules.get("httpx")
        sys.modules["httpx"] = _FakeHttpx
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is not None:
            sys.modules["httpx"] = self._old
        else:
            sys.modules.pop("httpx", None)

    def test_10_sign_is_sha1_of_user_key_stamp(self):
        """官方口径：sha1(user + UserKEY + timestamp)，40 位小写。"""
        self.assertEqual(
            cloud_print.sign_of("acc", "abc", "acbc"),
            hashlib.sha1(b"accabcacbc").hexdigest(),
        )
        self.assertEqual(len(cloud_print.sign_of("u", "k", "1565417654")), 40)

    def test_11_request_body_has_public_params(self):
        """请求体必须带上三件套，请求头按官方要求写死 JSON + UTF-8。"""
        _FakeHttpx.reset({"code": 0, "msg": "ok", "data": "SMI22062414383892298255"})
        order = cloud_print.print_label(
            "XPY123456789A", "<PAGE/>", copies=2, mode=1, idempotent="WMS1-formal",
            user="tester", key="secret", timeout=5.0,
        )
        self.assertEqual(order, "SMI22062414383892298255")
        call = _FakeHttpx.calls[-1]
        body = call["json"]
        self.assertTrue(call["url"].endswith("/printLabel"), call["url"])
        self.assertEqual(call["headers"]["Content-Type"], "application/json;charset=UTF-8")
        self.assertEqual(body["sn"], "XPY123456789A")
        self.assertEqual(body["copies"], 2)
        self.assertEqual(body["mode"], 1)
        self.assertEqual(body["idempotent"], "WMS1-formal")
        self.assertEqual(body["user"], "tester")
        self.assertEqual(len(str(body["timestamp"])), 10, "时间戳是 10 位秒")
        self.assertEqual(
            body["sign"],
            hashlib.sha1(f"testersecret{body['timestamp']}".encode()).hexdigest(),
        )

    def test_12_error_code_turns_into_chinese_hint(self):
        """code != 0 一律抛错，且把官方错误码翻成人话（1002 = 没绑打印机）。"""
        _FakeHttpx.reset({"code": 1002, "msg": "PRINTER_NOT_REGISTER", "data": None})
        with self.assertRaises(cloud_print.CloudPrintError) as ctx:
            cloud_print.print_label("XPY123", "<PAGE/>", user="t", key="k")
        self.assertIn("PRINTER_NOT_REGISTER", str(ctx.exception))
        self.assertIn("绑定", ctx.exception.hint)
        # 常见错误码都要有排错指引，别让现场只看到一个数字
        for code in (429, 1001, 1002, 1003, 1005, 1008, 1012, 1013, 1015):
            self.assertTrue(cloud_print.ERRORS.get(code), code)

    def test_13_network_problem_is_one_sentence(self):
        _FakeHttpx.reset(boom=RuntimeError("timeout"))
        with self.assertRaises(cloud_print.CloudPrintError) as ctx:
            cloud_print.query_printer_status("XPY123", user="t", key="k")
        self.assertIn("连不上", str(ctx.exception))

    def test_14_missing_account_refuses_to_call(self):
        """账号没填就别发请求（省得现场看到一堆莫名其妙的失败）。"""
        _FakeHttpx.reset()
        with self.assertRaises(cloud_print.CloudPrintError) as ctx:
            cloud_print.print_label("XPY123", "<PAGE/>", user="", key="")
        self.assertIn("开发者ID", str(ctx.exception))
        self.assertEqual(_FakeHttpx.calls, [], "没填账号就不该发请求")

    def test_15_printer_status_words(self):
        _FakeHttpx.reset(
            {"code": 0, "msg": "ok",
             "data": {"status": 2, "version": "XP-420B:1.033:019", "printerType": "XP-420B"}}
        )
        info = cloud_print.query_printer_status("XPY123", user="t", key="k")
        self.assertEqual(info["status_text"], "在线异常（多半是缺纸）")
        self.assertEqual(info["model"], "XP-420B")
        self.assertFalse(info["online"])


class PrintSettingsTest(unittest.TestCase):
    """打印设置页：账号 / 打印机 / 模板，改完立刻生效。"""

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

    def _item(self) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "打印测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "spec": "32-75 英寸",
                  "serial_no": "SN0001", "purchase_cost": 389, "base_price": 599}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def _printer(self, sn: str = "XPY123456789A", **kw) -> WmsPrinter:
        with SessionLocal() as s:
            row, _msg = print_settings.save_printer(s, sn=sn, name=kw.pop("name", "仓库 1 号"), **kw)
            return s.get(WmsPrinter, row.id)

    def _enable(self, **over) -> None:
        with SessionLocal() as s:
            print_settings.save_config(
                s, {"cloud_user": "tester@xprinter", "cloud_key": "secret",
                    "enabled": "1", **over}
            )

    # ---------------------------------------------------------- 配置
    def test_20_config_defaults_and_save(self):
        with SessionLocal() as s:
            cfg = print_settings.config(s)
            self.assertIn("<PAGE", cfg["template"], "默认要有一份能直接打的模板")
            self.assertFalse(print_settings.ready(cfg), "没填账号就不算就绪")
            print_settings.save_config(
                s, {"cloud_user": "tester@xprinter", "cloud_key": "secret", "enabled": "1"}
            )
            cfg = print_settings.config(s)
            self.assertEqual(cfg["cloud_user"], "tester@xprinter")
            self.assertEqual(cfg["cloud_key"], print_settings.MASKED, "密钥不能回显")
            self.assertTrue(print_settings.ready(print_settings.config(s, mask=False)))

    def test_21_masked_key_does_not_overwrite(self):
        """页面上原样回传掩码 = 不改密钥（不然一保存密钥就被抹了）。"""
        self._enable()
        with SessionLocal() as s:
            print_settings.save_config(s, {"cloud_key": print_settings.MASKED})
            self.assertEqual(print_settings.config(s, mask=False)["cloud_key"], "secret")
            print_settings.save_config(s, {"cloud_key": "new-secret"})
            self.assertEqual(print_settings.config(s, mask=False)["cloud_key"], "new-secret")

    def test_22_page_and_menu(self):
        page = self.client.get("/wms/print-settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("芯烨", page.text)
        self.assertIn("机身编号", page.text)
        self.assertIn("{barcode}", page.text, "模板占位符说明要摆出来")
        self.assertIn("/wms/print-settings", self.client.get("/wms").text, "菜单里要有入口")
        # 写操作必须单独一把钥匙：改模板等于改全场标签
        self.assertEqual(
            wms_nav.needed_perm("/wms/print-settings", "POST"), "wms.print.manage"
        )
        self.assertEqual(
            wms_nav.needed_perm("/wms/print-settings", "GET"), "wms.print.view"
        )
        # 现场换打印机、改标签版式是仓管的活 —— 默认就给他这把钥匙
        with SessionLocal() as s:
            keeper = s.scalar(select(WmsRole).where(WmsRole.code == "keeper"))
            self.assertIn("wms.print.manage", wms_permissions.perms_of_role(s, keeper.id))

    def test_23_save_by_form(self):
        resp = self.client.post("/wms/print-settings", data={
            "cloud_user": "tester@xprinter", "cloud_key": "secret", "enabled": "1",
            "auto_push": "1", "timeout": "8", "copies": "2",
            "switches": "1",  # 带这个标记，后台才认这三个开关
        })
        self.assertEqual(resp.status_code, 200)
        with SessionLocal() as s:
            cfg = print_settings.config(s, mask=False)
            self.assertEqual(cfg["cloud_user"], "tester@xprinter")
            self.assertEqual(cfg["enabled"], "1")
            self.assertEqual(cfg["auto_push"], "1")
            self.assertEqual(cfg["timeout"], "8")

    # ---------------------------------------------------------- 打印机
    def test_30_printer_crud(self):
        with SessionLocal() as s:
            row, msg = print_settings.save_printer(
                s, sn="XPY111", name="1 号机", width_mm=60, height_mm=40, gap_mm=2
            )
            self.assertIn("已添加", msg)
            self.assertTrue(row.is_default, "第一台自动当默认")
            # 换纸张、改份数
            print_settings.save_printer(
                s, printer_id=row.id, sn="XPY111", name="1 号机（改）",
                width_mm=70, height_mm=50, gap_mm=3, copies=2,
            )
            row = s.get(WmsPrinter, row.id)
            self.assertEqual((row.width_mm, row.height_mm, row.gap_mm, row.copies),
                             (70, 50, 3, 2))
            # 编号不能重复
            with self.assertRaises(ValueError):
                print_settings.save_printer(s, sn="XPY111", name="重号机")
            # 第二台 + 切默认
            other, _ = print_settings.save_printer(s, sn="XPY222", name="2 号机")
            print_settings.set_default(s, other.id)
            self.assertTrue(s.get(WmsPrinter, other.id).is_default)
            self.assertFalse(s.get(WmsPrinter, row.id).is_default)
            self.assertEqual(print_settings.default_printer(s).sn, "XPY222")
            # 停用后不再是默认候选
            print_settings.toggle(s, other.id)
            self.assertFalse(s.get(WmsPrinter, other.id).enabled)
            self.assertEqual(print_settings.default_printer(s).sn, "XPY111")
            # 删掉
            print_settings.delete_printer(s, other.id)
            self.assertEqual(len(print_settings.printers(s)), 1)

    def test_31_printer_needs_sn(self):
        with SessionLocal() as s:
            with self.assertRaises(ValueError) as ctx:
                print_settings.save_printer(s, sn="", name="没编号")
            self.assertIn("SN", str(ctx.exception))

    def test_32_add_printer_by_form_and_bind(self):
        """页面添加：勾了「绑定」就要先调 verifyPrinter（官方第一步）。"""
        self._enable()
        # 不能塞假 httpx 进 sys.modules：TestClient 自己就是 httpx 发的请求，会一起被打断
        bound: list[str] = []
        old = cloud_print.verify_printer
        cloud_print.verify_printer = lambda sn, name="", **kw: bound.append(sn) or True
        try:
            resp = self.client.post("/wms/print-settings/printers", data={
                "sn": "XPY333", "name": "3 号机", "kind": "label", "width_mm": "60",
                "height_mm": "40", "gap_mm": "2", "copies": "1", "enabled": "1",
                "verify": "1",
            })
        finally:
            cloud_print.verify_printer = old
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(bound, ["XPY333"], "勾了绑定就得调 verifyPrinter（官方第一步）")
        with SessionLocal() as s:
            self.assertEqual(print_settings.default_printer(s).sn, "XPY333")

    def _restore_httpx(self):
        if getattr(self, "_old", None) is not None:
            sys.modules["httpx"] = self._old
        else:
            sys.modules.pop("httpx", None)

    # ---------------------------------------------------------- 模板
    def test_40_template_render(self):
        xml = print_settings.render(
            print_settings.DEFAULT_TEMPLATE,
            {"barcode": "QCHA26100003", "display_code": "QC-HA-2610-0003",
             "grade": "S", "grade_label": "S 级准新机", "action": "直接上架",
             "category": "HA", "title": "电视支架", "date": "2026-10-08"},
            width=60, height=40, gap=2, copies=2,
        )
        self.assertIn('w="60"', xml)
        self.assertIn('l="40"', xml)
        self.assertIn('ld="2"', xml)
        self.assertIn("QCHA26100003", xml)
        self.assertIn("S 级准新机", xml)
        self.assertIn('x="30"', xml, "条码居中的 x 是纸宽的一半（必须是整数）")
        self.assertNotIn("{", xml, "占位符必须换干净")

    def test_40b_coordinates_are_integers(self):
        """坐标不能带小数点：云端会判 -2 参数无效（x="30.0" 打不出来）。"""
        values = {"barcode": "X", "display_code": "D", "grade": "S",
                  "grade_label": "S", "action": "上架", "category": "HA",
                  "title": "T", "date": "2026-10-09"}
        for width in (60, 76):
            xml = print_settings.render(
                print_settings.DEFAULT_TEMPLATE, values,
                width=width, height=40, gap=2, copies=1,
            )
            self.assertRegex(xml, rf'x="{width // 2}"', f"{width}mm 宽的居中 x 要是整数")
            self.assertNotIn('="{', xml)
            self.assertNotIn('.0"', xml, "坐标里不能出现小数")

    def test_40c_empty_idempotent_is_not_sent(self):
        """空幂等因子不能带 —— 云端把空串当固定值，5 分钟内只能打一张（1013）。"""
        self._enable()
        sent: list[dict] = []
        old = cloud_print.call
        cloud_print.call = lambda action, **kw: sent.append(kw) or "SMI-1"
        try:
            with SessionLocal() as s:
                print_settings.test_print(s, s.get(WmsPrinter, self._printer().id))
        finally:
            cloud_print.call = old
        self.assertNotIn("idempotent", sent[0], "没给幂等因子就别传这个字段")

    def test_41_template_escapes_xml(self):
        """品名里带 & / < 不能直接塞进 XML（会把整张标签打废）。"""
        xml = print_settings.render(
            "<PAGE w=\"{w}\"><LABEL id=\"1\"><TEXT x=\"1\" y=\"1\">{title}</TEXT></LABEL></PAGE>",
            {"barcode": "A", "title": "支架 & 螺丝 <大号>"},
            width=60, height=40,
        )
        self.assertIn("支架 &amp; 螺丝 &lt;大号&gt;", xml)
        self.assertNotIn("<大号>", xml)

    def test_42_preview_on_page(self):
        self._printer()
        page = self.client.get("/wms/print-settings")
        self.assertIn("&lt;PAGE", page.text, "页面要给出渲染后的预览")

    # ---------------------------------------------------------- 下发
    def test_50_send_records_order_no(self):
        self._enable()
        self._printer()
        item = self._item()
        with SessionLocal() as s:
            rows = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="tester")
            row_id = rows[0].id
        calls: list[dict] = []
        old = cloud_print.print_label
        cloud_print.print_label = lambda sn, content, **kw: (
            calls.append({"sn": sn, "content": content, **kw}) or "SMI-ORDER-1"
        )
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.send(s, s.get(WmsLabelPrint, row_id))
        finally:
            cloud_print.print_label = old
        self.assertTrue(ok, msg)
        with SessionLocal() as s:
            row = s.get(WmsLabelPrint, row_id)
            self.assertEqual(row.status, "sent")
            self.assertEqual(row.order_no, "SMI-ORDER-1")
            self.assertEqual(row.transport, "CLOUD")
            self.assertEqual(row.printer_sn, "XPY123456789A")
            self.assertIsNotNone(row.sent_at)
        self.assertEqual(len(calls), 1)
        self.assertIn("<PAGE", calls[0]["content"])
        self.assertEqual(calls[0]["idempotent"], f"WMS{row_id}-formal"[:50])
        # 指令预览给的是 XML（不是 TSPL）
        preview = self.client.get(f"/wms/labels/{row_id}/command").text
        self.assertIn("<PAGE", preview)

    def test_51_send_keeps_failure_reason(self):
        self._enable()
        self._printer()
        item = self._item()
        with SessionLocal() as s:
            row_id = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0].id
        old = cloud_print.print_label
        cloud_print.print_label = lambda *a, **kw: (_ for _ in ()).throw(
            cloud_print.CloudPrintError("PRINTER_OFFLINE（code 1003）", code=1003)
        )
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.send(s, s.get(WmsLabelPrint, row_id))
        finally:
            cloud_print.print_label = old
        self.assertFalse(ok)
        self.assertIn("离线", msg)
        with SessionLocal() as s:
            row = s.get(WmsLabelPrint, row_id)
            self.assertEqual(row.status, "queued", "失败还是待打印，能重发")
            self.assertIn("1003", row.error)
            self.assertEqual(row.printer_sn, "XPY123456789A")

    def test_52_refresh_marks_printed(self):
        self._enable()
        self._printer()
        item = self._item()
        with SessionLocal() as s:
            row_id = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0].id
            row = s.get(WmsLabelPrint, row_id)
            row.order_no = "SMI-ORDER-9"
            row.status = "sent"
            s.commit()
        old = cloud_print.query_order_state
        cloud_print.query_order_state = lambda order_id, **kw: True
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.refresh(s, s.get(WmsLabelPrint, row_id))
        finally:
            cloud_print.query_order_state = old
        self.assertTrue(ok, msg)
        with SessionLocal() as s:
            row = s.get(WmsLabelPrint, row_id)
            self.assertEqual(row.status, "printed")
            self.assertIsNotNone(row.printed_at)

    def test_53_auto_push_on_enqueue(self):
        """开了「排队即下发」：页面上一点，单子就到云打印机了。"""
        self._enable(auto_push="1")
        self._printer()
        item = self._item()
        calls: list[dict] = []
        old = cloud_print.print_label
        cloud_print.print_label = lambda sn, content, **kw: (
            calls.append({"sn": sn}) or "SMI-AUTO-1"
        )
        try:
            resp = self.client.post("/wms/labels/enqueue", data={
                "item_ids": [str(item.id)], "label_type": "formal",
                "dialect": "TSPL", "transport": "CLOUD",
            })
        finally:
            cloud_print.print_label = old
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 1)
        with SessionLocal() as s:
            row = s.scalar(select(WmsLabelPrint))
            self.assertEqual(row.status, "sent")
            self.assertEqual(row.order_no, "SMI-AUTO-1")

    def test_54_manual_push_and_sync_from_page(self):
        self._enable()
        self._printer()
        item = self._item()
        with SessionLocal() as s:
            row_id = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0].id
        old_send, old_state = cloud_print.print_label, cloud_print.query_order_state
        cloud_print.print_label = lambda *a, **kw: "SMI-MANUAL-1"
        cloud_print.query_order_state = lambda *a, **kw: True
        try:
            self.client.post(f"/wms/labels/{row_id}/push")
            self.client.post(f"/wms/labels/{row_id}/sync")
        finally:
            cloud_print.print_label = old_send
            cloud_print.query_order_state = old_state
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsLabelPrint, row_id).status, "printed")

    def test_55_cloud_off_keeps_queue(self):
        """没开云打印：队列照常排，只是不发（老链路不受影响）。"""
        self._printer()
        item = self._item()
        with SessionLocal() as s:
            row_id = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0].id
            ok, msg = print_settings.send(s, s.get(WmsLabelPrint, row_id))
        self.assertFalse(ok)
        self.assertIn("云打印没开", msg)

    def test_56_test_print(self):
        self._enable()
        row = self._printer()
        calls: list[dict] = []
        old = cloud_print.print_label
        cloud_print.print_label = lambda sn, content, **kw: (
            calls.append({"sn": sn, "content": content}) or "SMI-TEST-1"
        )
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.test_print(s, s.get(WmsPrinter, row.id))
        finally:
            cloud_print.print_label = old
        self.assertTrue(ok, msg)
        self.assertIn("<BARC", calls[0]["content"], "测试页要有条码")
        self.assertNotIn("{", calls[0]["content"], "占位符要换干净")

    def test_56b_test_page_matches_preview(self):
        """测试页与页面预览必须用同一份内容 —— 不然照着预览调坐标永远对不上。"""
        self._enable()
        self._printer()
        sent: list[dict] = []
        old = cloud_print.print_label
        cloud_print.print_label = lambda sn, content, **kw: (
            sent.append({"content": content}) or "SMI-TEST-1"
        )
        try:
            with SessionLocal() as s:
                printed = print_settings.test_print(
                    s, s.scalar(select(WmsPrinter))
                )
                preview = print_settings.sample_xml(s)
        finally:
            cloud_print.print_label = old
        self.assertTrue(printed[0])
        self.assertEqual(sent[0]["content"], preview, "打出来的要和预览一模一样")

    # ---------------------------------------------------------- 开关与自检
    def test_57_switches_survive_other_forms(self):
        """「保存模板」「保存回调」不能把勾好的开关清成 0（2026-10-09 线上真出过）。"""
        self.client.post("/wms/print-settings", data={
            "switches": "1", "cloud_user": "tester@xprinter", "cloud_key": "secret",
            "enabled": "1", "auto_push": "1", "mode": "1",
        })
        self.client.post("/wms/print-settings/template", data={"template": "<PAGE>新版式</PAGE>"})
        self.client.post("/wms/print-settings/callback",
                         data={"public_base_url": "https://wms.yusunkeji.com"})
        with SessionLocal() as s:
            cfg = print_settings.config(s, mask=False)
            self.assertEqual(
                (cfg["enabled"], cfg["auto_push"], cfg["mode"]), ("1", "1", "1"),
                "改模板 / 改回调之后，开关必须原样还在",
            )
            self.assertIn("新版式", cfg["template"])
            self.assertEqual(cfg["public_base_url"], "https://wms.yusunkeji.com")

    def test_58_uncheck_turns_switch_off(self):
        """同一个表单里取消勾选要关得掉（提交 switches 但没带 enabled → 关）。"""
        self._enable()
        self.client.post("/wms/print-settings", data={"switches": "1", "auto_push": "1"})
        with SessionLocal() as s:
            cfg = print_settings.config(s, mask=False)
            self.assertEqual(cfg["enabled"], "0")
            self.assertEqual(cfg["auto_push"], "1")

    def test_59_self_test_says_what_is_missing(self):
        """没登记打印机时，自检要直说缺哪一步，别只回一句「失败」。"""
        self._enable()
        with SessionLocal() as s:
            result = print_settings.self_test(s)
        self.assertFalse(result["ok"])
        texts = " ".join(step["text"] for step in result["steps"])
        self.assertIn("还没登记打印机", texts)
        self.assertIn("上次自检没通过", self.client.get("/wms/print-settings").text)

    def test_60_self_test_all_green(self):
        """账号 + 云接口 + 机器在线 + 测试页都通：页面把分步结果摆出来。"""
        self._enable()
        self._printer()
        old_status, old_print = cloud_print.query_printer_status, cloud_print.print_label
        cloud_print.query_printer_status = lambda sn, **kw: {
            "status": 1, "status_text": "在线正常", "model": "XP-420B",
            "version": "V1.0", "online": True,
        }
        cloud_print.print_label = lambda *a, **kw: "SMI-SELFTEST-1"
        try:
            self.client.post("/wms/print-settings/test")
            with SessionLocal() as s:
                result = print_settings.last_test_of(s)
        finally:
            cloud_print.query_printer_status = old_status
            cloud_print.print_label = old_print
        self.assertTrue(result["ok"])
        texts = " ".join(step["text"] for step in result["steps"])
        self.assertIn("云接口通了", texts)
        self.assertIn("在线正常", texts)
        self.assertIn("SMI-SELFTEST-1", texts, "测试页的订单号要报出来")
        self.assertIn("上次自检通过", self.client.get("/wms/print-settings").text)

    def test_60b_title_fits_the_line(self):
        """品名放不下要截掉加 …（不能顶出纸边把条码挤走）；品牌进占位符。"""
        long_name = "美的（Midea）家用多功能破壁机加热全自动豆浆机料理机榨汁辅食机 1.75L"
        xml = print_settings.render(
            print_settings.DEFAULT_TEMPLATE,
            {"barcode": "QCHA26100002", "display_code": "QC-HA-2610-0002", "grade": "S",
             "grade_label": "S 级准新机", "brand": "美的", "action": "直接上架",
             "category": "HA", "title": long_name, "date": "2026-10-09"},
            width=60, height=40, gap=2, copies=1,
        )
        self.assertIn("...", xml, "放不下要加省略号（用英文点，不用 … —— 字库不一定有）")
        self.assertNotIn("…", xml)
        self.assertNotIn(long_name, xml, "整串原样打出来就顶出纸边了")
        # 短品名不该被截
        short = print_settings.render(
            print_settings.DEFAULT_TEMPLATE,
            {"grade": "S", "grade_label": "S 级准新机", "brand": "美的", "title": "美的破壁机",
             "barcode": "B", "display_code": "D", "action": "直接上架", "category": "HA",
             "date": "2026-10-09"},
            width=60, height=40, gap=2, copies=1,
        )
        self.assertIn(">美的破壁机<", short)
        self.assertNotIn("...", short)
        # 品牌靠右：x 用 {rx}（纸宽减 2），且不留未替换的占位符
        self.assertIn('a="right">美的</TEXT>', xml)
        self.assertNotIn("{", xml, "占位符要换干净")
        self.assertTrue(print_settings.title_budget(print_settings.DEFAULT_TEMPLATE, 60) > 10)

    def test_60c_request_body_is_real_utf8(self):
        """发出去的中文必须是真 UTF-8 字节，不能是 \\uXXXX —— 平台不还原就是一片乱码。"""
        import httpx

        captured: dict = {}
        real_post = httpx.post

        def fake_post(url, **kw):
            captured.update(kw)
            captured["url"] = url

            class _Resp:
                def json(self):
                    return {"code": 0, "data": "SMI-UTF8-1"}
            return _Resp()

        httpx.post = fake_post
        try:
            cloud_print.call("printLabel", user="me@x", key="k", base_url="http://x",
                             content="<TEXT>乖巧妻 破壁机</TEXT>")
        finally:
            httpx.post = real_post

        raw = captured.get("content")
        self.assertIsInstance(raw, bytes, "要自己 dumps 成字节发，别用 json=")
        self.assertIn("乖巧妻 破壁机".encode("utf-8"), raw, "中文要原样在包里")
        self.assertNotIn(b"\\u", raw, "\\uXXXX 转义串打出来就是乱码")

    def test_61_verify_sends_user_and_key(self):
        """勾「同时绑定」时接口要收到 user / key —— 直接撒配置字典会缺参数报 500。

        （2026-10-09 线上真出过：save_printer 里写成了 ``**(cfg or {})``，
        而配置里的键叫 cloud_user / cloud_key，接口要的是 user / key。）
        """
        self._enable()
        calls: list[tuple[str, dict]] = []
        old = cloud_print.call
        cloud_print.call = lambda action, **kw: calls.append((action, kw)) or True
        try:
            resp = self.client.post("/wms/print-settings/printers", data={
                "sn": "XPY-VERIFY", "name": "绑定测试机", "kind": "label",
                "width_mm": "60", "height_mm": "40", "gap_mm": "2", "copies": "1",
                "enabled": "1", "verify": "1",
            })
        finally:
            cloud_print.call = old
        self.assertEqual(resp.status_code, 200, "绑定不能 500 —— 看服务端日志")
        self.assertEqual(calls[0][0], "verifyPrinter")
        self.assertEqual(calls[0][1].get("user"), "tester@xprinter")
        self.assertEqual(calls[0][1].get("key"), "secret")
        self.assertEqual(calls[0][1].get("sn"), "XPY-VERIFY")
        with SessionLocal() as s:
            self.assertIsNotNone(
                s.scalar(select(WmsPrinter).where(WmsPrinter.sn == "XPY-VERIFY")),
                "绑定成功要落库",
            )


    def test_62_default_template_uses_chinese_font(self):
        """官方口径：f 取值 [1,8]，**1~6 是英数字体**，带中文的行必须 7 / 8。

        2026-10-10 线上实测：老模板中文行写的是 f=4/5/6 → 打出来中文全变成
        一串拉丁字母（ÊÃ ä Ð），而英文数字与条码完全正常。
        """
        xml = print_settings.render(
            print_settings.DEFAULT_TEMPLATE,
            {"barcode": "QCHA26100002", "display_code": "QC-HA-2610-0002", "grade": "S",
             "grade_label": "S 级准新机", "brand": "美的", "action": "直接上架",
             "category": "HA", "title": "美的破壁机", "date": "2026-10-10"},
            width=60, height=40, gap=2, copies=1,
        )
        self.assertEqual(print_settings.template_warnings(xml), [], "新默认模板不该有字体毛病")
        chinese_lines = 0
        for attrs, inner in re.findall(r"<TEXT\b([^>]*)>([^<]*)</TEXT>", xml):
            if inner.strip().isascii():
                continue
            chinese_lines += 1
            font = re.search(r'f="(\d+)"', attrs)
            self.assertIsNotNone(font, f"中文行要显式写 f：{inner}")
            self.assertIn(int(font.group(1)), (7, 8), f"中文行只能用 7 / 8：{inner}")
        self.assertGreaterEqual(chinese_lines, 4, "等级 / 处置 / 品类这些中文行都要在")

    def test_63_ascii_font_on_chinese_is_flagged(self):
        """中文行写了 f=1~6 要被点名（黄条），英文数字行不受影响。"""
        bad = ('<PAGE w="60" l="40"><LABEL id="1">'
               '<TEXT x="2" y="2" f="6">待检测</TEXT></LABEL></PAGE>')
        warns = print_settings.template_warnings(bad)
        self.assertEqual(len(warns), 1)
        self.assertIn('f="6"', warns[0])
        self.assertIn("英数字体", warns[0])
        ok = ('<PAGE w="60" l="40"><LABEL id="1">'
              '<TEXT x="2" y="2" f="5">QCHA26100002</TEXT>'
              '<TEXT x="2" y="5">待检测</TEXT></LABEL></PAGE>')  # 不写 f = 官方默认 7
        self.assertEqual(print_settings.template_warnings(ok), [])
        # 页面上得看得见这条黄条（不然现场只会再打一张废标签回来说乱码）
        with SessionLocal() as s:
            print_settings.save_config(s, {"template": bad})
        try:
            html = self.client.get("/wms/print-settings").text
            self.assertIn("中文会是乱码", html)
            self.assertIn('f="6"', html)
        finally:
            _drop_template()

    def test_64_ensure_template_upgrades_legacy(self):
        """库里存着老模板就自动换新版；用户自己写的模板一个字都不动。"""
        try:
            with SessionLocal() as s:
                print_settings.save_config(s, {"template": print_settings.LEGACY_TEMPLATES[0]})
                self.assertEqual(print_settings.ensure_template(s), "upgraded")
                self.assertEqual(print_settings._get(s, "template"),
                                 print_settings.DEFAULT_TEMPLATE)
                self.assertEqual(print_settings.ensure_template(s), "current", "再跑一次要幂等")
            mine = ('<PAGE w="60"><LABEL id="1">'
                    '<TEXT x="1" y="1" f="4">我的模板</TEXT></LABEL></PAGE>')
            with SessionLocal() as s:
                print_settings.save_config(s, {"template": mine})
                print_settings._set(s, "template_v", "")
                s.commit()
                self.assertEqual(print_settings.ensure_template(s), "kept", "自写模板不许被覆盖")
                self.assertEqual(print_settings._get(s, "template"), mine)
                self.assertEqual(print_settings._get(s, "template_v"), "",
                                 "有毛病就不记版本，下次还提醒")
        finally:
            _drop_template()


class LabelCopiesTest(unittest.TestCase):
    """标签份数跟数量走（2026-10-10）：这台货 3 台就出 3 张一模一样的标签。

    一个条码底下带 N 台（同一个码），每台身上都得贴一张 —— 份数不再写死在打印机档案里。
    """

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

    def _item(self, qty: int = 1) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": qty,
                    "supplier_name": "份数测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "qty": qty,
                  "purchase_cost": 100, "base_price": 300}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def _printer(self, copies: int = 1) -> WmsPrinter:
        with SessionLocal() as s:
            row, _msg = print_settings.save_printer(
                s, sn="XPY999999999B", name="份数打印机", copies=copies)
            return s.get(WmsPrinter, row.id)

    def _enable(self) -> None:
        with SessionLocal() as s:
            print_settings.save_config(
                s, {"cloud_user": "tester@xprinter", "cloud_key": "secret", "enabled": "1"})

    def test_70_copies_follow_qty(self):
        """入队按数量记份数：3 台 → 3 份；渲染出来的标签指令也是 c="3"。"""
        item = self._item(3)
        with SessionLocal() as s:
            row = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0]
            self.assertEqual(int(row.copies or 0), 3, "3 台就是 3 张")
            xml = print_settings.render_for(s, row, None)
        self.assertIn('c="3"', xml)

    def test_71_send_uses_copies(self):
        """份数**只在报文里写一次**，云 API 的 copies 恒为 1（2026-10-12 事故后改）。

        两边都写 N，云端按 N×N 打：一台 20 台数的货一次吐了几百张，现场只能拔电源。
        现在 ``<LABEL c="3">`` 负责说"印 3 张"，API 的 copies 恒 1 = 这份内容发一次。
        """
        self._enable()
        self._printer(copies=1)
        item = self._item(3)
        with SessionLocal() as s:
            row_id = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0].id
        calls: list[dict] = []
        old = cloud_print.print_label
        cloud_print.print_label = lambda sn, content, **kw: (
            calls.append({"sn": sn, "content": content, **kw}) or "SMI-COPIES-1"
        )
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.send(s, s.get(WmsLabelPrint, row_id))
        finally:
            cloud_print.print_label = old
        self.assertTrue(ok, msg)
        self.assertEqual(
            calls[0]["copies"], 1,
            "云 API 的 copies 必须恒为 1 —— 份数只在报文里写一次，否则两边相乘",
        )
        self.assertIn('c="3"', calls[0]["content"], "印几张由报文说")

    def test_71b_copies_capped(self):
        """数量录错也有个头：单次最多 MAX_COPIES 张（防一整卷纸打光）。"""
        item = self._item(999)
        printer = self._printer(copies=1)
        with SessionLocal() as s:
            row = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0]
            self.assertEqual(int(row.copies or 0), 999, "库里如实记数量")
            self.assertEqual(
                print_settings.copies_of(row, printer), print_settings.MAX_COPIES,
                "但下发时封顶",
            )
            self.assertIn('c="%d"' % print_settings.MAX_COPIES,
                          print_settings.render_for(s, row, printer))

    def test_72_single_qty_uses_printer_default(self):
        """数量 1（或老数据没份数）时，还是用打印机档案上的默认份数（老行为不变）。"""
        item = self._item(1)
        printer = self._printer(copies=2)
        with SessionLocal() as s:
            row = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0]
            self.assertEqual(print_settings.copies_of(row, printer), 2)
            self.assertIn('c="2"', print_settings.render_for(s, row, printer))

    def test_73_reenqueue_follows_new_qty(self):
        """数量后来改了，再排一次队份数跟着变（幂等：还是同一条任务）。"""
        item = self._item(1)
        with SessionLocal() as s:
            first = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")[0]
            first_id = first.id
            self.assertEqual(int(first.copies or 0), 1)
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            row.qty = 4
            s.commit()
        with SessionLocal() as s:
            again = labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].id, first_id, "幂等：不重复排任务")
        with SessionLocal() as s:
            self.assertEqual(int(s.get(WmsLabelPrint, first_id).copies or 0), 4)

    def test_74_queue_page_shows_copies(self):
        """队列页把份数写出来（现场要一眼看到这张任务印几张）。"""
        item = self._item(3)
        with SessionLocal() as s:
            labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")
        resp = self.client.get("/wms/labels")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("份数", resp.text)
        self.assertIn("这台货有几台就打几张", resp.text)


class LabelEmergencyStopTest(unittest.TestCase):
    """强制停止打印（2026-10-12 加）：打印失控时一次动作把还在路上的全掐掉。

    起因：一台 20 台数的货，份数在「云 API 的 copies」和报文 ``<LABEL c>`` 里
    各写了一遍被乘成平方，一次吐了几百张，现场只能拔电源收场。
    这条保证「点完之后不会再多打一张」—— 清云端队列 + 关「入队即下发」+
    本地待打印全部作废，三件一起做，少一件就停不干净。
    """

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

    def _item(self, qty: int = 1) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": qty,
                    "supplier_name": "急停测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "qty": qty,
                  "purchase_cost": 100, "base_price": 300}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def _printer(self) -> WmsPrinter:
        with SessionLocal() as s:
            row, _msg = print_settings.save_printer(
                s, sn="XPY777777777D", name="急停打印机", copies=1)
            return s.get(WmsPrinter, row.id)

    def _enable(self, auto_push: str = "1") -> None:
        with SessionLocal() as s:
            print_settings.save_config(
                s, {"cloud_user": "tester@xprinter", "cloud_key": "secret",
                    "enabled": "1", "auto_push": auto_push})

    def test_75_stop_does_all_three(self):
        """三件套：清云端队列 + 关自动下发 + 本地待打印作废。"""
        self._enable(auto_push="1")
        self._printer()
        item = self._item(3)
        with SessionLocal() as s:
            labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")
        calls: list[str] = []
        old = cloud_print.clear_queue
        cloud_print.clear_queue = lambda sn, **kw: calls.append(sn) or True
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.emergency_stop(s)
        finally:
            cloud_print.clear_queue = old
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "必须去云端清一次待打印队列")
        with SessionLocal() as s:
            self.assertEqual(
                print_settings.config(s, mask=False).get("auto_push"), "0",
                "自动下发必须关掉 —— 不关的话停了还在往外发",
            )
            rows = list(s.scalars(select(WmsLabelPrint)).all())
            self.assertTrue(rows, "任务还在（留痕），只是作废了")
            self.assertTrue(
                all(r.status == "cancelled" for r in rows),
                "待打印的全部作废，再点「全部下发」也不会打",
            )

    def test_76_stop_from_page(self):
        """页面上有「强制停止」按钮，点了确实停（走真路由）。"""
        self._enable(auto_push="1")
        self._printer()
        item = self._item(2)
        with SessionLocal() as s:
            labeling.enqueue(s, [s.get(WmsItem, item.id)], operator="t")
        html = self.client.get("/wms/labels").text
        self.assertIn("/wms/labels/emergency-stop", html, "队列页要有急停入口")
        old = cloud_print.clear_queue
        cloud_print.clear_queue = lambda sn, **kw: True
        try:
            resp = self.client.post("/wms/labels/emergency-stop")
        finally:
            cloud_print.clear_queue = old
        self.assertIn(resp.status_code, (200, 303))
        with SessionLocal() as s:
            self.assertEqual(
                print_settings.config(s, mask=False).get("auto_push"), "0")
            self.assertTrue(all(
                r.status == "cancelled"
                for r in s.scalars(select(WmsLabelPrint)).all()))

    def test_77_stop_clears_cloud_even_when_switch_off(self):
        """急停**不看「云打印」开关**（2026-10-12 现场踩过）。

        现场就是把开关先关掉想止住，结果云端队列还在、机器一联网接着吐 ——
        开关只管我们发不发新的，管不了已经在云端排着的那批；
        所以「强制停止」只要账号密钥在就必须去清云端队列。
        """
        self._printer()
        with SessionLocal() as s:
            print_settings.save_config(
                s, {"cloud_user": "tester@xprinter", "cloud_key": "secret",
                    "enabled": "0", "auto_push": "1"})
            self.assertFalse(print_settings.ready(print_settings.config(s, mask=False)))
            self.assertTrue(print_settings.has_key(print_settings.config(s, mask=False)))
        calls: list[str] = []
        old = cloud_print.clear_queue
        cloud_print.clear_queue = lambda sn, **kw: calls.append(sn) or True
        try:
            with SessionLocal() as s:
                ok, msg = print_settings.emergency_stop(s)
        finally:
            cloud_print.clear_queue = old
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "开关关着也要去云端清队列（否则机器接着吐）")
        self.assertIn("云端队列已清空", msg)


if __name__ == "__main__":
    unittest.main()
