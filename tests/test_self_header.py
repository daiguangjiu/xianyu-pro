"""渔货栈顶栏（2026-10-03 用户拍板，照 1688 首页）：

1. 菜单归集到「自营服务」：原来散在左侧边栏的菜单（工作台 / 货源 / 订单 / 采购 /
   店铺 / 系统）全部收进顶栏一个下拉里，渔货栈不再渲染侧边栏；
2. 右边照 1688：采购车（悬停弹「最近加入的货品」，带降价提示与「剩余货品 N 种」）、
   采购单、异常提醒图标 + 「我的」下拉（账号卡片 / 我的经营 / 我的采购 / 系统 / 退出登录）；
3. 手机（≤900px）：两个下拉都变成整屏浮层，采购车点一下直接进采购车页；
4. 商家端（商家入驻）与平台审核台仍旧走侧边栏，不受影响；
5. 内容区和顶栏一样撑满（渔货栈解除 .app-main 的 1440 上限，两边只留一点留白）；
6. 渔货栈不再渲染面包屑那行（.app-topbar），首页 = 货源广场 /self/harbor
   （/self/ 只做一次 307 跳转，概览挪到 /self/overview）。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src import __version__  # noqa: E402
from src.models import PurchaseCartItem, SupplierItem  # noqa: E402

ITEM_ID = "T-HEADER-1"

# 「自营服务」下拉里该有的菜单（文案 → 链接）
MENU_ITEMS = [
    ("概览", "/self/overview"),
    ("货源广场", "/self/harbor"),
    ("热销榜单", "/self/harbor?tab=hot"),
    ("全部订单", "/self/orders"),
    # 2026-10-16 用户拍板：收口成「对外七态」（内部九个状态合并展示）
    ("待确认", "/self/orders?status=pending"),
    ("待发货", "/self/orders?status=to_ship"),
    ("已发货", "/self/orders?status=shipped"),
    ("已完成", "/self/orders?status=completed"),
    ("已退款", "/self/orders?status=refunded"),
    ("已关闭", "/self/orders?status=closed"),
    ("异常单", "/self/orders?status=exception"),
    ("采购车", "/self/cart"),
    ("采购单", "/self/purchases"),
    ("店铺管理", "/self/shops"),
    # 2026-10-11 改名：以前叫「店铺商品池」
    ("我的店铺商品", "/self/shop-goods"),
    ("异常日志", "/self/logs"),
    ("更新日志", "/self/changelog"),
]


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(PurchaseCartItem).filter(PurchaseCartItem.item_id == ITEM_ID).delete(
            synchronize_session=False
        )
        s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).delete(
            synchronize_session=False
        )
        s.commit()


def _make_item(sku_price: float = 45.5) -> None:
    with SessionLocal() as s:
        s.add(
            SupplierItem(
                item_id=ITEM_ID,
                supplier_id="sup-h",
                title="顶栏测试-全棉四件套",
                category_id="ht_bedding",
                main_image="http://127.0.0.1:9/h.jpg",
                images=["http://127.0.0.1:9/h.jpg"],
                cost_price=Decimal("45.50"),
                fish_price=Decimal("45.50"),
                sale_price=Decimal("99.00"),
                stock=100,
                sku_list={"skus": [
                    {"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": sku_price, "stock": 60},
                ]},
                source_type="third_party",
                audit_status="approved",
                is_listed=True,
                item_code="YHZ-TEST-HD01",
            )
        )
        s.commit()


def _set_sku_price(price: float) -> None:
    with SessionLocal() as s:
        row = s.query(SupplierItem).filter(SupplierItem.item_id == ITEM_ID).first()
        row.sku_list = {"skus": [{"sku_id": "s1", "spec_text": "灰 / 1.5m", "price": price, "stock": 60}]}
        s.commit()


class SelfHeaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post(
            "/login",
            data={"username": "admin", "password": "xianyu2026"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()
        _make_item()

    def tearDown(self):
        _wipe()

    # ------------------------------------------------ 「自营服务」归集菜单
    def test_10_self_end_has_top_nav_instead_of_sidebar(self):
        html = self.client.get("/self/orders").text
        self.assertIn('class="sy-head"', html, "渔货栈顶栏（1688 式）应存在")
        # 2026-10-11 用户拍板：顶栏那个下拉改名「全部服务」
        self.assertIn("全部服务", html)
        self.assertNotIn("自营服务", html)
        self.assertNotIn('class="app-sidebar"', html, "渔货栈不该再渲染左侧边栏")
        self.assertNotIn('id="sidebarToggle"', html, "渔货栈没有侧边栏抽屉")
        # 面包屑那行也去掉了（用户 2026-10-03 拍板），渔货栈只有顶栏这一行
        self.assertNotIn('class="app-topbar"', html, "渔货栈不再有面包屑行")
        self.assertNotIn('class="topbar-crumb"', html)

    def test_20_service_menu_carries_every_menu(self):
        html = self.client.get("/self/harbor").text
        for label, href in MENU_ITEMS:
            self.assertIn(f'href="{href}"', html, f"「{label}」应该指向 {href}")
            self.assertIn(label, html, f"「{label}」应该出现在归集菜单里")

    def test_25_menu_links_never_point_at_hub(self):
        """两端同一个端口：归集菜单里的链接一个裸路径都不能有（裸 /orders 是渔枢系统的页）。"""
        html = self.client.get("/self/orders").text
        nav_start = html.find("<nav")
        nav_end = html.find("</nav>", nav_start)
        self.assertGreater(nav_start, -1, "顶栏菜单应该是 <nav>")
        menu = html[nav_start:nav_end]
        self.assertIn("/self/orders", menu)
        self.assertNotIn('href="/orders"', menu)

    # ------------------------------------------------ 右侧：采购车 / 我的
    def test_30_cart_icon_and_popover(self):
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 2})
        html = self.client.get("/self/harbor").text
        self.assertIn('class="sy-cart"', html, "采购车应该带悬停弹框")
        self.assertIn('href="/self/cart"', html)
        self.assertIn("最近加入的货品", html)
        self.assertIn("顶栏测试-全棉四件套", html)
        self.assertIn("灰 / 1.5m", html)
        self.assertIn("¥45.50 × 2", html)
        self.assertIn("剩余货品：<strong>1</strong> 种", html)
        self.assertIn("syCartRemove", html, "弹框里的删除要走采购车接口")

    def test_35_empty_cart_popover(self):
        html = self.client.get("/self/harbor").text
        self.assertIn("采购车还是空的", html)
        self.assertIn("剩余货品：<strong>0</strong> 种", html)

    def test_40_price_drop_hint(self):
        """加购之后降价：弹框给「比加入时降价 ¥…」（车里存的是加购那一刻的价）。"""
        self.client.post("/self/cart/add", json={"item_id": ITEM_ID, "sku_id": "s1", "qty": 2})
        _set_sku_price(40.0)   # 45.50 → 40.00，两件共降 11.00
        html = self.client.get("/self/harbor").text
        self.assertIn("比加入时降价", html)
        self.assertIn("¥11.00", html)

    def test_50_me_dropdown(self):
        html = self.client.get("/self/harbor").text
        self.assertIn('class="sy-me"', html)
        # 2026-10-11：菜单分组与顶栏「全部服务」统一（工作台 / 货源 / 订单 / 采购 / 店铺 / 系统）
        self.assertIn("货源 · 渔货栈", html)
        self.assertIn("交易 · 订单", html)
        self.assertIn("交易 · 采购", html)
        self.assertIn("我的采购单", html)
        # 2026-10-11：「我的上架 / 已上架店铺」两个入口随页面一起下线
        self.assertNotIn("我的上架", html)
        self.assertNotIn("已上架店铺", html)
        self.assertIn("退出登录", html)
        # 退出登录会改状态：必须是 POST 到渔货栈的 /self/logout
        self.assertIn('action="/self/logout"', html)
        self.assertIn('method="post"', html)
        self.assertIn(f"版本 v{__version__}", html)

    def test_55_avatar_is_default_icon_and_name_lives_in_popup(self):
        """顶栏只留一个系统默认头像，用户名 / 身份 / 店铺数收进「我的」弹窗（用户 2026-10-03 拍板）。"""
        html = self.client.get("/self/harbor").text
        start = html.find('class="sy-me"')
        self.assertGreater(start, -1, "「我的」下拉应该在顶栏上")
        me = html[start : html.find("</details>", start)]
        # 头像 = 人形图标（系统默认），不再拿用户名首字凑
        self.assertIn('<span class="sy-avatar"><i class="bi bi-person-fill"></i></span>', me)
        self.assertNotIn('class="who"', me, "用户名不该再摆在顶栏上占宽度")
        # 顶栏那一个头像上仍留着悬停提示，点开才有账号信息
        trigger = me[: me.find("</summary>")]
        self.assertIn('title="我的：', trigger)
        pop = me[me.find('class="sy-me-pop"') :]
        self.assertIn('<span class="nm">', pop, "用户名在弹窗里")
        self.assertIn('<span class="lv">', pop, "身份在弹窗里")
        self.assertIn("关联店铺", pop)
        self.assertIn('<span class="av"><i class="bi bi-person-fill"></i></span>', pop, "弹窗头像同一套默认头像")

    # ------------------------------------------------ 手机 / 其它端
    def test_60_mobile_layout(self):
        html = self.client.get("/self/harbor").text
        self.assertIn("--sy-head-h: 56px", html)
        self.assertIn("@media (max-width: 900px)", html)
        # 手机上下拉变整屏浮层，采购车不再用悬停
        self.assertIn(".sy-menu { position: static; }", html)
        self.assertIn(".sy-pop { display: none !important; }", html)

    def test_70_merchant_end_keeps_sidebar(self):
        html = self.client.get("/self/merchant/login", follow_redirects=False)
        page = html.text if html.status_code == 200 else ""
        if page:
            self.assertIn('class="app-sidebar"', page, "商家端仍旧用侧边栏")
            self.assertNotIn('class="sy-head"', page)

    def test_80_content_fills_width_like_topbar(self):
        """内容区跟顶栏一样撑满：渔货栈解除 .app-main 的 1440 上限，两边只留 --s-6 留白。

        宽屏（>1440）下不解除的话右边会空一大块，和顶栏对不齐。
        """
        html = self.client.get("/self/harbor").text
        self.assertIn(".app-shell--full .app-main { max-width: none; }", html)

    def test_85_home_is_harbor(self):
        """整个渔货栈以货源广场为首页（2026-10-03 用户拍板）。

        /self/ 只做一次 307 跳转 —— 登录落点、端切换、老书签、顶栏品牌 logo 都打在它上面；
        概览没删，挪到 /self/overview，菜单「工作台 · 概览」指过去。
        """
        from app.hub_bridge import needed_portal
        from app.portals import PORTALS, PORTAL_SELF

        homes = {p["key"]: p["home"] for p in PORTALS}
        self.assertEqual(homes["self"], "/self/harbor", "端首页配置要指到货源广场")
        self.assertEqual(homes["hub"], "/", "渔枢系统首页不动")
        # 首页只是个跳转、货源广场是**选货页** —— 两端账号都要能进（空串=不限端）。
        # 2026-10-11 修：以前这两处归「渔枢系统端」，端只勾了「渔货栈」的学员账号
        # 一登录就被 403 拦在自己的首页上。
        self.assertEqual(needed_portal("/self/"), "", "首页不限端")
        self.assertEqual(needed_portal("/self"), "", "首页不限端")
        self.assertEqual(needed_portal("/self/harbor"), "", "货源广场不限端")
        self.assertEqual(needed_portal("/self/orders"), PORTAL_SELF, "订单页仍旧只给渔货栈")

        resp = self.client.get("/self/", follow_redirects=False)
        self.assertEqual(resp.status_code, 307, resp.status_code)
        self.assertEqual(resp.headers["location"], "/self/harbor")
        # 带提示的地址（退出登录 / 动作结果）跳转时查询串不能丢，否则页面上没提示条
        resp = self.client.get("/self/?msg=已退出登录&kind=ok", follow_redirects=False)
        location = resp.headers["location"]
        self.assertTrue(location.startswith("/self/harbor?"), location)
        query = parse_qs(urlparse(location).query)   # Location 里中文会被百分号编码
        self.assertEqual(query["msg"], ["已退出登录"])
        self.assertEqual(query["kind"], ["ok"])

        page = self.client.get("/self/overview")
        self.assertEqual(page.status_code, 200)
        self.assertIn("今日订单", page.text, "概览页还在，只是换了地址")

        harbor = self.client.get("/self/harbor").text
        self.assertIn('<a class="sy-brand" href="/self/harbor"', harbor, "品牌 logo 回首页")

    def test_86_stats_move_into_me_popup(self):
        """经营数不再占顶栏（2026-10-03 拍板）：顶栏只留图标。

        2026-10-11 用户拍板：「自营服务」改名「全部服务」，并且这个下拉与「我的」
        渲染**同一份菜单**（分组同名、条目同名同链接）—— 以后加菜单只改一处。
        """
        html = self.client.get("/self/orders").text
        h0 = html.find('class="sy-head"')
        m0 = html.find('class="sy-me"')
        # 顶栏可见部分（到「我的」图标为止，不含点开后的弹窗）
        head = html[h0:m0]
        self.assertNotIn('class="sy-stats"', head, "顶栏上不该再有经营数那一块")
        self.assertNotIn("在架货源", head, "数字不摆在顶栏上")
        self.assertIn('class="sy-ic"', head, "顶栏只留图标（采购车 / 采购单 / 提醒）")
        self.assertIn("全部服务", head, "「自营服务」已改名「全部服务」")
        self.assertNotIn("自营服务", html, "旧名字一处都不该留")

        me = html[m0 : html.find("</details>", m0)]
        groups = ("工作台", "货源 · 渔货栈", "交易 · 订单", "交易 · 采购", "店铺", "系统")
        for group in groups:
            self.assertIn(
                f'<div class="sy-col-head">{group}</div>', head, f"顶栏下拉缺分组「{group}」"
            )
            self.assertIn(
                f'<div class="sy-me-group">{group}</div>', me, f"「我的」弹窗缺分组「{group}」"
            )
        # 两处同源：同一条目在两处是同名同链接
        self.assertIn("货源广场", me)
        self.assertIn('href="/self/harbor"', me)
        # 「我的」以前独有的「地址管理」已经并进同一份菜单
        self.assertIn("地址管理", me)
        self.assertIn("地址管理", head)
        self.assertNotIn("我的上架", me, "「我的上架」入口已下线")
        self.assertNotIn("/self/selection", html)

    def test_86b_service_menu_never_overflows_viewport(self):
        """「全部服务」下拉不能把整页撑出横向滚动条（2026-10-12 用户报「页面布局乱了」）。

        以前面板是 left:0 相对按钮向右铺 980px，而这个按钮在顶栏偏右 ——
        1280 的屏上实测面板能顶到 x=1826，整页多出横向滚动、内容被推来推去，
        最右边那组菜单还被切掉。现在改成 fixed 居中 + 两边各夹 16px。
        """
        html = self.client.get("/self/harbor").text
        self.assertIn("width: min(980px, calc(100vw - 32px))", html)
        self.assertIn("max-width: calc(100vw - 32px)", html, "面板宽度要夹在视口里")
        self.assertIn(
            "position: fixed; top: calc(var(--sy-head-h) + 8px); left: 50%;",
            html,
            "桌面端面板改成相对视口居中（不再相对按钮向左/右铺）",
        )
        # 手机上仍是整屏浮层（那条单独写在媒体查询里，别被上面的改动带跑）
        self.assertIn("position: fixed; inset: var(--sy-head-h) 0 0 0;", html)

    def test_87_topbar_search_is_1688_style(self):
        """顶栏那一条（品牌 + 搜索 + 图标）照 1688：橙色描边方角框 + 范围标签 + 橙色实心按钮。"""
        html = self.client.get("/self/orders").text
        f0 = html.find('<form class="sy-search"')
        self.assertGreater(f0, -1, "非首页要有顶栏搜索框")
        form = html[f0 : html.find("</form>", f0)]
        self.assertIn('action="/self/harbor"', form)
        self.assertIn('<span class="scope"', form, "照 1688：框内左侧标出搜索范围")
        self.assertIn("搜货源", form)
        self.assertIn("border: 2px solid var(--sy-brand)", html, "橙色描边（1688 版式）")

    def test_88_hot_words_centered(self):
        """搜索框下方的热搜词居中（1688 首页搜索区那一排）。"""
        html = self.client.get("/self/harbor").text
        r0 = html.find(".harbor-search .hot-words {")
        self.assertGreater(r0, -1, "热搜词样式应该在")
        rule = html[r0 : html.find("}", r0)]
        self.assertIn("justify-content: center", rule, "热搜词要居中")

    def test_89_pick_bar_wraps_on_mobile(self):
        """货源广场「精选货源」那一行（二级类目 + 右侧四个榜单）在手机上不能撑破视口。

        原来 .pick-head 是 flex-wrap: nowrap、右侧 .rank-tabs 又是 flex: 0 0 auto
        不收缩，窄屏下榜单标签把整行顶出屏幕（用户实测「降价 捡漏榜」被裁掉半截），
        页面跟着变宽、横滑错位。
        现在 ≤900px 折成三行：标题 / 榜单**四格等宽平铺**（左起排、一个都不裁）/
        二级类目整行横滑；≤560px 再收一档字号与内边距，保证最长那格也放得下。
        """
        html = self.client.get("/self/harbor").text
        # 整条工具栏允许折行，不再 nowrap
        self.assertIn(".pick-head { flex-wrap: wrap;", html, "工具栏要能折行")
        # 榜单独占一整行（flex-basis 100%），四个标签等宽平分
        self.assertIn(
            ".rank-tabs { order: 2; flex: 1 1 100%; min-width: 0; gap: 2px;",
            html,
            "榜单要独占一行，不再跟标题挤在同一行被裁",
        )
        self.assertIn(
            "flex: 1 1 0; min-width: 0; justify-content: center;",
            html,
            "四个榜单标签等宽平分、居中，一个都不裁",
        )
        # 类目整行排到第三行
        self.assertIn(".pick-tabs { order: 3; flex: 1 1 100%;", html, "类目整行横滑")
        # 窄屏再收一档（最长那格「降价 捡漏榜」也放得下）
        # 字号并回全站阶梯（2026-10-12）：窄屏榜单标签用 12px，不再是 11.5px
        self.assertIn(".rank-tab { height: 28px; padding: 0 4px; font-size: 12px;", html)

    # ------------------------------------------------ 退出登录（放最后：会清掉登录态）
    def test_90_logout_clears_session(self):
        resp = self.client.post("/self/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertTrue(location.startswith("/self/"), location)
        after = self.client.get("/self/orders", follow_redirects=False)
        self.assertIn(after.status_code, (302, 303, 401, 403), after.status_code)
        # 重新登录，免得影响同一进程里的其它用例
        again = self.client.post(
            "/login",
            data={"username": "admin", "password": "xianyu2026"},
            follow_redirects=False,
        )
        self.assertEqual(again.status_code, 303)


if __name__ == "__main__":
    unittest.main()
