"""1688 遗留代码清理 + 销量展示统一回归（2026-10-13）。

三条口径：
1. 渔货栈所有面向用户展示销量的地方统一为「展示基数 + 实际销量」（sold_shown）：
   feed 卡片「已售 N」、趋势飙升榜「月销 N 件」、采购节点小卡「月销 N 件」。
   排序 / 热销榜计数 / 详情页「在售闲鱼号」仍用真实值（test_sourcing_display_base 锁定）。
2. 类目弹出面板高度按内容收紧（fitFly）：三级类目少时面板不再留大片空白。
3. 1688 遗留逻辑下线：OAuth 授权、商品同步、ali_client、物流主动拉取、自动推单；
   PurchaseOrder 采购草稿与模型保留（渔枢成本回溯第一档，test_dso_sync 锁定）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class CleanupBase(unittest.TestCase):
    def setUp(self):
        init_db()
        ensure_default_admin()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def _harbor(self) -> str:
        r = self.client.get("/self/harbor")
        self.assertEqual(r.status_code, 200)
        return r.text


class SoldShownTest(CleanupBase):
    def test_10_harbor_uses_sold_shown_everywhere(self):
        """所有销量展示（卡片 + 趋势榜 + 采购节点）都用 sold_shown。"""
        # 模板源断言（feed 空时页面没有卡片可渲染，页面级断言会假阴性）
        tpl = (ROOT / "templates" / "harbor.html").read_text(encoding="utf-8")
        # feed 卡片「已售」+ 趋势飙升榜「月销」+ 采购节点小卡「月销」= 至少 3 处
        self.assertGreaterEqual(tpl.count("sold_shown"), 3)
        self.assertNotIn("月销 {{ it.sold }}", tpl)
        self.assertNotIn("月销 <em>{{ it.sold }}</em>", tpl)
        self.assertNotIn("已售 {{ c.sold }}", tpl)
        # 页面冒烟：有卡片时渲染的是 sold_shown
        self._harbor()

    def test_20_panel_height_fits_content(self):
        """类目弹出面板按内容收紧（fitFly），三级少时不再留大片空白。"""
        html = self._harbor()
        self.assertIn("fitFly", html)
        self.assertIn("FLY_MAX_H", html)
        # show() 切换三级列的时机要收
        self.assertIn("fitFly(fly);  // 切了三级列", html)


class LegacyRoutesGoneTest(CleanupBase):
    def test_30_oauth_page_removed(self):
        """/self/oauth 1688 授权页已下线。"""
        r = self.client.get("/self/oauth")
        self.assertEqual(r.status_code, 404)

    def test_31_auth_api_removed(self):
        """/api/v1/auth/* 1688 授权 API 已下线。"""
        self.assertEqual(self.client.get("/api/v1/auth/login").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/auth/callback").status_code, 404)

    def test_32_goods_sync_removed(self):
        """/api/v1/goods/sync 1688 商品同步入口已下线。"""
        r = self.client.post("/api/v1/goods/sync", json={"full": True})
        self.assertEqual(r.status_code, 404)

    def test_33_logistics_sync_removed(self):
        """/api/v1/logistics/sync/{ali_order_id} 主动拉取已下线；回调照旧。"""
        self.assertEqual(
            self.client.post("/api/v1/logistics/sync/whatever").status_code, 404
        )

    def test_34_no_1688_words_on_user_pages(self):
        """概览页不再出现 1688 业务文案（模板版式注释不在此断言范围）。"""
        r = self.client.get("/self/overview")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("1688 商品快照", r.text)
        self.assertNotIn("1688 授权", r.text)


class PurchaseDraftKeptTest(CleanupBase):
    def test_40_purchase_api_alive(self):
        """采购草稿 API 保留（渔枢成本回溯依赖），DSO 不存在仍 404 而非路由缺失。"""
        r = self.client.get("/api/v1/purchase")
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post(
            "/api/v1/purchase/push",
            json={"downstream_order_id": "NO_SUCH_ORDER_1688CLEANUP"},
        )
        self.assertEqual(r2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
