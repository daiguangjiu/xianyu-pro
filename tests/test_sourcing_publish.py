"""渔枢系统商品「上架到渔货栈」（2026-10-02 定的口径）。

口径：
1. 上架 = **直接把商品写进渔货栈商品池 `supplier_items`**，并按商品类目落到
   渔货栈对应的二级类目下（渔枢系统类目文本 → product_categories → 渔货栈类目 id）；
2. 类目对不上（没填类目 / 只填到一级但该一级下没有二级 / 类目名不存在）→ 拒绝上架并说明原因；
3. 上架后渔货栈（/self/harbor）立刻能看到、搜得到（标题 / 品牌 / 商家编码 / 别名）；
4. 幂等：同一款商品重复上架只更新资料，不重复建；下架只置 is_listed=False。
"""
from __future__ import annotations

import html
import sys
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductCategory  # noqa: E402
from app.services import sourcing_publish as sourcing_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import purchase_node_windows  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _feed(text: str) -> str:
    """只取 feed 流那一段（顶部的趋势榜 / 采购节点位用的是全量货源，不算筛选结果）。"""
    start = text.find('class="fish-grid feed-grid"')
    end = text.find('class="harbor-pager"')
    if start < 0:
        return text
    return text[start:end] if end > start else text[start:]


def _fresh(text: str) -> str:
    """「最新上架的货源」独立区块 2026-10-04 已取消（合并进 feed）。

    保留这个函数是为了让老断言读得出「这一块确实没了」：区块删掉后返回空串。
    """
    start = text.find('class="fresh-grid"')
    end = text.find('class="fish-grid feed-grid"')
    if start < 0:
        return ""
    return text[start:end] if end > start else text[start:]


_CAT_NAMES = [
    # 一级
    "家用电器", "家纺家饰", "女装男装",
    # 二级
    "手机配件", "电脑外设", "生活电器", "家居饰品", "床上用品", "服饰配件", "女装",
    # 三级（测试里动态加的）
    "连衣裙",
]


def _reset() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id.like("hub-prd-%")).delete(
            synchronize_session=False
        )
        s.query(Product).filter(Product.code.like("SPD9%")).delete(synchronize_session=False)
        s.query(ProductCategory).filter(ProductCategory.name.in_(_CAT_NAMES)).delete(
            synchronize_session=False
        )
        s.commit()


def _categories() -> None:
    """三个一级 + 对应二级（二级带渔货栈 source_id，模拟从渔货栈同步过来的类目）。

    类目名用渔货栈真实的叫法（家用电器 / 家纺家饰 / 女装男装），
    这样才能验证「老类目名兜底」那条路径（渔枢系统历史商品写的是「数码配件」这类自由文本）。
    """
    with SessionLocal() as s:
        digital = ProductCategory(level=1, name="家用电器", sort_order=1, source_id="digital")
        textile = ProductCategory(level=1, name="家纺家饰", sort_order=2, source_id="home_textile")
        wear = ProductCategory(level=1, name="女装男装", sort_order=3, source_id="apparel")
        s.add_all([digital, textile, wear])
        s.flush()
        s.add_all(
            [
                ProductCategory(level=2, parent_id=digital.id, name="手机配件", sort_order=1, source_id="dg_phone"),
                ProductCategory(level=2, parent_id=digital.id, name="电脑外设", sort_order=2, source_id="dg_pc"),
                ProductCategory(level=2, parent_id=digital.id, name="生活电器", sort_order=3, source_id="dg_life"),
                ProductCategory(level=2, parent_id=textile.id, name="家居饰品", sort_order=1, source_id="ht_deco"),
                ProductCategory(level=2, parent_id=textile.id, name="床上用品", sort_order=2, source_id="ht_bed"),
                ProductCategory(level=2, parent_id=wear.id, name="服饰配件", sort_order=1, source_id="ap_acc"),
                ProductCategory(level=2, parent_id=wear.id, name="女装", sort_order=2, source_id="ap_women"),
            ]
        )
        s.commit()


def _product(**kw) -> int:
    data = {
        "name": "测试无线键盘",
        "code": "SPD9001",
        "category": "家用电器 / 手机配件",
        "brand": "雷柏",
        "fish_price": 88.0,
        "cost_amount": 60.0,
        "price": 199.0,
        "stock": 10,
        "source_type": "self_operated",
        "audit_status": "approved",
    }
    data.update(kw)
    with SessionLocal() as s:
        row = Product(**data)
        s.add(row)
        s.commit()
        return row.id


def _item(item_id: str) -> SupplierItem | None:
    with SessionLocal() as s:
        return s.scalars(select(SupplierItem).where(SupplierItem.item_id == item_id)).first()


class CategoryMapping(unittest.TestCase):
    """渔枢系统类目文本 → 渔货栈类目 id。"""

    def setUp(self):
        init_db()
        _reset()
        _categories()

    def tearDown(self):
        _reset()

    def test_10_two_level_text(self):
        pid = _product(category="家用电器 / 手机配件")
        with SessionLocal() as s:
            hit = sourcing_service.resolve_category(s, s.get(Product, pid))
        self.assertTrue(hit.ok)
        self.assertEqual(hit.category_id, "dg_phone")
        self.assertEqual(hit.text, "家用电器 / 手机配件")
        self.assertEqual(hit.note, "", "新口径精确对上的不该有多余提示")

    def test_15_legacy_category_names(self):
        """老商品写的是「数码配件」「家居家纺」「服饰鞋包」这类自由文本 —— 就近认领并提示核对。"""
        cases = {
            "数码配件": "dg_phone",
            "家居家纺": "ht_deco",
            "服饰鞋包": "ap_acc",
        }
        for name, expect in cases.items():
            pid = _product(name=f"测试老类目{name}", code=f"SPD91{abs(hash(name)) % 100:02d}", category=name)
            with SessionLocal() as s:
                hit = sourcing_service.resolve_category(s, s.get(Product, pid))
            self.assertTrue(hit.ok, f"{name} 应该能被兜底认领：{hit.note}")
            self.assertEqual(hit.category_id, expect, name)
            self.assertIn("老写法", hit.note, "兜底认领要提醒运营核对")

    def test_20_three_level_text_falls_back_to_second(self):
        """三级类目也能认：三级 source_id 形如 ap_women_l3_0 → 二级 id 是 ap_women。"""
        with SessionLocal() as s:
            wear = s.scalars(select(ProductCategory).where(ProductCategory.name == "女装")).first()
            s.add(ProductCategory(level=3, parent_id=wear.id, name="连衣裙", source_id="ap_women_l3_0"))
            s.commit()
        pid = _product(name="测试女装连衣裙", code="SPD9002", category="女装男装 / 女装 / 连衣裙")
        with SessionLocal() as s:
            hit = sourcing_service.resolve_category(s, s.get(Product, pid))
        self.assertTrue(hit.ok)
        self.assertEqual(hit.category_id, "ap_women")

    def test_30_only_first_level_uses_first_child_with_note(self):
        pid = _product(name="测试只填一级", code="SPD9003", category="家用电器")
        with SessionLocal() as s:
            hit = sourcing_service.resolve_category(s, s.get(Product, pid))
        self.assertTrue(hit.ok)
        self.assertEqual(hit.category_id, "dg_phone")
        self.assertIn("只填到", hit.note)

    def test_40_unknown_category_rejected(self):
        pid = _product(name="测试没类目", code="SPD9004", category="不存在的类目 / 子")
        with SessionLocal() as s:
            hit = sourcing_service.resolve_category(s, s.get(Product, pid))
        self.assertFalse(hit.ok)
        self.assertIn("找不到", hit.note)
        ok, text = sourcing_service.publish(s, s.get(Product, pid))
        self.assertFalse(ok)
        self.assertIn("找不到", text)
        self.assertIsNone(_item(f"hub-prd-{pid}"), "类目对不上不能落库")


class PublishToSourcing(unittest.TestCase):
    """上架 / 幂等 / 下架。"""

    def setUp(self):
        init_db()
        _reset()
        _categories()
        self.pid = _product()

    def tearDown(self):
        _reset()

    def test_10_publish_writes_supplier_item(self):
        with SessionLocal() as s:
            ok, text = sourcing_service.publish(s, s.get(Product, self.pid), actor="tester")
            s.commit()
        self.assertTrue(ok, text)
        item = _item(f"hub-prd-{self.pid}")
        self.assertIsNotNone(item, "上架后渔货栈商品池里得有这条")
        self.assertEqual(item.title, "测试无线键盘")
        self.assertEqual(item.category_id, "dg_phone", "落到渔货栈对应类目")
        self.assertTrue(item.is_listed)
        self.assertEqual(item.audit_status, "approved")
        self.assertEqual(item.source_type, "self_operated")
        self.assertEqual(float(item.fish_price), 88.0)
        self.assertEqual(item.item_code, "SPD9001")
        self.assertEqual(item.brand, "雷柏")
        self.assertIsNotNone(item.listed_at)
        with SessionLocal() as s:
            product = s.get(Product, self.pid)
            self.assertEqual(product.status, "on_sale", "渔枢系统侧同步为在售")

    def test_20_publish_is_idempotent_and_updates(self):
        with SessionLocal() as s:
            sourcing_service.publish(s, s.get(Product, self.pid))
            s.commit()
        with SessionLocal() as s:
            product = s.get(Product, self.pid)
            product.fish_price = 99.0
            product.name = "测试无线键盘 二代"
            s.commit()
        with SessionLocal() as s:
            ok, text = sourcing_service.publish(s, s.get(Product, self.pid))
            s.commit()
        self.assertTrue(ok, text)
        self.assertIn("已更新", text)
        with SessionLocal() as s:
            count = len(s.scalars(select(SupplierItem).where(SupplierItem.item_id == f"hub-prd-{self.pid}")).all())
        self.assertEqual(count, 1, "重复上架不能建出两条")
        item = _item(f"hub-prd-{self.pid}")
        self.assertEqual(float(item.fish_price), 99.0)
        self.assertEqual(item.title, "测试无线键盘 二代")

    def test_30_unpublish_keeps_record(self):
        with SessionLocal() as s:
            sourcing_service.publish(s, s.get(Product, self.pid))
            s.commit()
        with SessionLocal() as s:
            ok, text = sourcing_service.unpublish(s, s.get(Product, self.pid))
            s.commit()
        self.assertTrue(ok, text)
        item = _item(f"hub-prd-{self.pid}")
        self.assertIsNotNone(item, "下架保留商品资料")
        self.assertFalse(item.is_listed)
        with SessionLocal() as s:
            self.assertEqual(s.get(Product, self.pid).status, "off_sale")

    def test_35_unpublish_keeps_snapshot_of_the_listed_look(self):
        """下架把「下架前看到的样子」存成快照：渔货栈提示用；重新上架要清掉。"""
        with SessionLocal() as s:
            sourcing_service.publish(s, s.get(Product, self.pid), actor="tester")
            s.commit()
        with SessionLocal() as s:
            ok, text = sourcing_service.unpublish(
                s, s.get(Product, self.pid), actor="tester", reason="档口断货"
            )
            s.commit()
        self.assertTrue(ok, text)
        item = _item(f"hub-prd-{self.pid}")
        self.assertIsNotNone(item.unlisted_at, "下架要记时间")
        self.assertEqual(item.unlisted_reason, "档口断货")
        self.assertEqual(item.unlisted_by, "tester")
        snap = item.unlisted_snapshot or {}
        self.assertEqual(snap.get("title"), "测试无线键盘")
        self.assertEqual(float(snap.get("fish_price") or 0), 88.0, "快照记下架前的渔货价")
        self.assertEqual(float(snap.get("sale_price") or 0), 199.0)
        self.assertEqual(snap.get("category"), "家用电器 / 手机配件")
        self.assertEqual(snap.get("item_code"), "SPD9001")

        # 重新上架：下架标记与快照清掉（渔货栈不再看到下架提示）
        with SessionLocal() as s:
            sourcing_service.publish(s, s.get(Product, self.pid), actor="tester")
            s.commit()
        item = _item(f"hub-prd-{self.pid}")
        self.assertIsNone(item.unlisted_at)
        self.assertEqual(item.unlisted_reason or "", "")
        self.assertEqual(item.unlisted_snapshot or {}, {})


class VisibleInHarbor(unittest.TestCase):
    """上架后渔货栈能看到、搜得到（页面链路）。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _categories()
        self.pid = _product()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def test_10_visible_and_searchable(self):
        r = self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        page = self.client.get("/self/harbor")
        self.assertEqual(page.status_code, 200)
        self.assertIn("测试无线键盘", page.text, "上架后货源广场能看到")

        # 搜索：标题 / 品牌 / 商家编码 / 别名都要能搜到
        for word in ("测试无线键盘", "雷柏", "SPD9001", "机械键盘"):
            hit = self.client.get(f"/self/harbor?q={word}")
            self.assertIn("测试无线键盘", hit.text, f"按「{word}」搜不到")

        # 归类：带上类目 id 后，按渔货栈类目筛也能命中
        by_cat = self.client.get("/self/harbor?cat=家用电器&sub=手机配件")
        self.assertIn("测试无线键盘", by_cat.text, "按类目筛选应命中")

    def test_20_sourcing_admin_page(self):
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        page = self.client.get("/sourcing")
        self.assertEqual(page.status_code, 200)
        self.assertIn("渔货栈发布管理", page.text)
        self.assertIn("测试无线键盘", page.text)
        self.assertIn("家用电器 / 手机配件", page.text, "显示落到的渔货栈类目")

    def test_30_category_failed_shows_reason(self):
        """类目对不上的商品：页面上标出来并说明原因（不静默落到「其他」）。"""
        with SessionLocal() as s:
            product = s.get(Product, self.pid)
            product.category = "不存在的类目 / 子"
            s.commit()
        page = self.client.get("/sourcing?state=category_failed")
        self.assertIn("测试无线键盘", page.text)
        self.assertIn("找不到", page.text)

    def test_40_batch_publish(self):
        second = _product(name="测试鼠标", code="SPD9005")
        r = self.client.post(
            "/sourcing/publish",
            data={"product_id": [str(self.pid), str(second)]},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(_item(f"hub-prd-{self.pid}"))
        self.assertIsNotNone(_item(f"hub-prd-{second}"))

    def test_60_unlisted_tip_on_reseller_side(self):
        """下架后渔货栈：详情页给提示 + 下架前的样子；货源广场不再显示这一款。"""
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        r = self.client.post(f"/products/{self.pid}/sourcing-unpublish", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        detail = self.client.get(f"/self/goods/detail/hub-prd-{self.pid}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("已由渔枢系统下架", detail.text, "下架提示")
        self.assertIn("渔枢系统下架（商品档案已下架）", detail.text, "默认下架原因")
        self.assertIn("测试无线键盘", detail.text, "下架前的标题快照")
        self.assertIn("88.00", detail.text, "下架前的渔货价快照")
        self.assertIn("已下架，不可上架", detail.text, "下架后不能再上架到店铺")

        # 2026-10-13 用户口径：下架的货源直接从货源广场隐藏（原来只是给卡片打个
        # 「已下架」标，结果 55 款里有 47 款下架货把列表塞满）。想看下架前的样子
        # 就进商品详情页 —— 那边按 item_id 单独取，不受这条过滤影响。
        harbor = self.client.get("/self/harbor?q=SPD9001")
        self.assertEqual(harbor.status_code, 200)
        self.assertNotIn("测试无线键盘", _feed(harbor.text), "已下架的货源不该再出现在货源广场")

    def test_65_listed_item_still_in_feed(self):
        """下架被隐藏的同时，在架的货源照常出现在广场（别一刀切全滤没）。"""
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        harbor = self.client.get("/self/harbor?q=SPD9001")
        self.assertEqual(harbor.status_code, 200)
        self.assertIn("测试无线键盘", _feed(harbor.text), "在架的货源要能出现在货源广场")

        # 下架之后立刻消失
        self.client.post(f"/products/{self.pid}/sourcing-unpublish", follow_redirects=False)
        harbor = self.client.get("/self/harbor?q=SPD9001")
        self.assertNotIn("测试无线键盘", _feed(harbor.text), "一下架就该从广场消失")

    def test_70_filters_carry_into_feed_links(self):
        """切类目 / 切榜单 / 点热搜词 / 换活动页：当前搜索条件都跟着带进下面的 feed。"""
        page = self.client.get("/self/harbor?cat=家用电器&sub=手机配件&q=键盘")
        self.assertEqual(page.status_code, 200)
        text = unquote(html.unescape(page.text))
        # 榜单 tab：换榜单不清空类目与搜索词
        self.assertIn("/self/harbor?tab=hot&cat=家用电器&sub=手机配件&q=键盘", text)
        # 热搜词：只换关键词，类目带下去
        self.assertIn("/self/harbor?cat=家用电器&sub=手机配件&q=保温杯", text)
        # 类目链接：换类目保留搜索词
        self.assertIn("/self/harbor?cat=家纺家饰&q=键盘", text)
        # 活动节点：切活动也不丢类目与搜索词
        self.assertIn("&node=", text)

    def test_80_activity_node_filters_feed(self):
        """选了活动页（采购节点）→ 节点关键词当搜索条件，feed 直接出该活动的货源。"""
        node_ids = [n["id"] for n in purchase_node_windows(date.today())[:3]]
        if "national_day" not in node_ids:
            self.skipTest(f"今天不在国庆节点窗口内：{node_ids}")
        hit_pid = _product(name="户外便携保温杯", code="SPD9006")
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        self.client.post(f"/products/{hit_pid}/sourcing-publish", follow_redirects=False)

        all_page = self.client.get("/self/harbor")
        self.assertIn("户外便携保温杯", _feed(all_page.text), "没选活动时都在")

        node_page = self.client.get("/self/harbor?node=national_day")
        self.assertEqual(node_page.status_code, 200)
        feed = _feed(node_page.text)
        self.assertIn("户外便携保温杯", feed, "命中活动关键词的要在 feed 里")
        self.assertNotIn("测试无线键盘", feed, "没命中活动关键词的不该出现")
        self.assertIn("国庆黄金周", node_page.text, "结果栏要说明当前是哪个活动")

    def test_90_feed_default_sorted_by_newest(self):
        """feed 默认按「最新上架」倒序：刚上架的货排第一（2026-10-04：独立区块合并进 feed）。"""
        # 先上几款高毛利大库存的：按「推荐」排序它们会压在最前面
        for i in range(4):
            pid = _product(
                name=f"高毛利老货源{i}", code=f"SPD90{10 + i}",
                fish_price=1.0, price=999.0, stock=500,
            )
            self.client.post(f"/products/{pid}/sourcing-publish", follow_redirects=False)
        # 最后再上这一款 → last_synced_at 最新，feed 首个就该是它
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)

        page = self.client.get("/self/harbor")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(_fresh(page.text), "", "顶部那块独立的「最新上架的货源」已经取消")
        feed = _feed(page.text)
        self.assertIn("测试无线键盘", feed, "刚上架的货要在 feed 里")
        self.assertLess(
            feed.index("测试无线键盘"), feed.index("高毛利老货源0"),
            "feed 默认按上架时间倒序：刚上的要排在老货源前面",
        )
        # 带上类目筛选时，排序跟着当前筛选走（不是全局一把抓）
        # 别名下线后类目归类只看商品名 / 卖点（2026-10-11），按它实际落到的类目筛
        by_cat = self.client.get("/self/harbor?cat=数码、电脑")
        self.assertIn("测试无线键盘", _feed(by_cat.text), "按类目筛选时 feed 也要带着它")
        # 切到「推荐」才按利润率 × 库存排：老货源压回前面
        rec = _feed(self.client.get("/self/harbor?tab=recommend").text)
        self.assertIn("高毛利老货源0", rec, "推荐排序下高毛利老货源仍在 feed 里")

    def test_95_feed_card_action_and_no_stray_comment(self):
        """feed 卡片：按钮叫「铺货到店铺」、图片 hover 文案「查看详情」、模板注释不许泄漏到页面。"""
        self.client.post(f"/products/{self.pid}/sourcing-publish", follow_redirects=False)
        html = self.client.get("/self/harbor").text
        self.assertIn("铺货到店铺", html, "feed 列表按钮改叫「铺货到店铺」")
        self.assertNotIn("上架到店铺", html, "老按钮名不该还在货源广场上")
        self.assertIn('title="查看详情"', html, "图片鼠标放上去的提示是「查看详情」")
        self.assertIn("查看详情", html)
        self.assertNotIn(
            "============ #}", html,
            "模板注释残行（删除区块时漏掉的那行 ============ #}）不许渲染到页面上",
        )

    def test_50_detail_card_no_xianyu_publish(self):
        """商品详情页的「上架到渔货栈」不再指向闲鱼发布。"""
        page = self.client.get(f"/products/{self.pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("上架到渔货栈", page.text)
        self.assertNotIn("goofish.com/publish", page.text, "渔枢系统端不该再跳闲鱼发布页")
        self.assertNotIn("带入闲鱼发布页", page.text)
        self.assertIn(f"/products/{self.pid}/sourcing-publish", page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
