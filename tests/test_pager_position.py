"""2026-10-16 用户拍板：分页条（templates/_pager.html）三端**同一个位置** ——
列表卡片下面、靠**右下角**（与渔枢系统口径一致），并注意与列表内容的布局关系。

钉住三点：
1. 三套样式（渔枢 style.css / 渔货栈 base.html / 渔集 wms_design.css）都把 .pager 推到右边；
2. 渔集的列表分页挂在 flush 卡片**外面**（卡片 padding:0，放里面会贴着边、看着像表格的一部分）；
3. 页面渲染出来：分页条在列表（table / 列表容器）之后。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class PagerPositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        assert cls.client.post("/login", data=ADMIN, follow_redirects=False).status_code == 303

    # ------------------------------------------------ ① 三端样式都靠右
    def test_10_three_systems_push_pager_right(self):
        files = {
            "渔枢 style.css": ROOT / "app/static/style.css",
            "渔货栈 base.html": ROOT / "templates/base.html",
            "渔集 wms_design.css": ROOT / "app/static/wms_design.css",
        }
        for name, path in files.items():
            css = path.read_text("utf-8")
            # 只认「.pager-wrap .pager { … }」这条规则本体（别命中 .pager-wrap .pager-info 那种谁都算的选择器串）
            idx = -1
            for marker in (".pager-wrap .pager {", ".pager-wrap .pager{", ".pager-wrap .pager {"):
                idx = css.find(marker)
                if idx > -1:
                    break
            self.assertGreater(idx, -1, f"{name}：缺 .pager-wrap .pager 规则（分页条要靠右下角）")
            block = css[idx:css.find("}", idx) + 1]
            self.assertIn("margin-left:auto", block.replace(" ", ""), f"{name}：.pager 要 margin-left:auto")

    # ------------------------------------------------ ② 渔集分页挂在卡片外
    def test_20_wms_pager_sits_outside_the_flush_card(self):
        for name in ("wms_items.html", "wms_goods.html", "wms_deliveries.html", "wms_outbound.html"):
            src = (ROOT / "wms/templates" / name).read_text("utf-8")
            i = src.find("{{ pager(")
            self.assertGreater(i, -1, f"{name} 要用统一分页组件")
            # 前面最近的一行「真标签」应是卡片的 </div>（跳过 {# … #} 注释行）
            prev = ""
            for line in src[:i].splitlines():
                line = line.strip()
                if not line or line.startswith("{#") or line.startswith("#}"):
                    continue
                prev = line
            self.assertEqual(prev, "</div>", f"{name}：分页条要挂在列表卡片外面（前面一行应是 </div>）")

    # ------------------------------------------------ ③ 渲染出来在列表之后
    def test_30_rendered_pager_comes_after_the_list(self):
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 1,
                    "supplier_name": "分页位置测试"}, operator="tester")
            intake.register(s, batch.id, [
                {"sku_code": "POS-1", "product_name": "位置测试货", "qty": 1},
            ], operator="tester")

        # 注意：渔货栈的 CSS 内联在页面里，裸 find("pager-wrap") 会先命中 CSS 规则 —— 认 class= 开头那个
        marker = 'class="pager-wrap"'
        for path in ("/wms/items", "/wms/goods"):
            html = self.client.get(path).text
            self.assertIn(marker, html, f"{path} 要有分页条")
            self.assertGreater(html.find(marker), html.rfind("</table>"),
                               f"{path}：分页条要在列表后面")
        # 渔货栈订单页同样：卡片关掉之后才是分页条
        orders = self.client.get("/self/orders").text
        if marker in orders:
            self.assertGreater(orders.find(marker), orders.rfind("</table>"))

        # 清理
        with SessionLocal() as s:
            s.query(WmsItem).delete(synchronize_session=False)
            s.query(WmsBatch).delete(synchronize_session=False)
            s.commit()


if __name__ == "__main__":
    unittest.main()
