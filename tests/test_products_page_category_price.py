"""供应链商品列表页三条口径 + 价格派生 + 订单备份频率（2026-10-07 用户拍板）：

1. 列表页**分页**：默认每页 50 条（可选 20 / 50 / 100 / 200），筛选条件跟着翻页走；
2. **批量审核 / 上架 / 下架**照旧能用，另加**批量匹配类目**：按商品名 / 品牌在类目树里
   认领类目，只补还没填类目的商品（已填的一律不动，认不出来不硬塞「其他」）；
3. **价格口径**：渔货价 = 成本价 × 1.15，建议售价 = 渔货价 × 1.35
   （发布页自动带出 + 后端兜底 + SKU 行派生，手填过的不动）；
4. **订单备份**：每天只备份一次（以前文件名用 UTC、判定用北京时间，
   北京 00:00~08:00 会每分钟刷一份）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductCategory, ProductSku  # noqa: E402
from app.services import backup as backup_service  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
PAGE_PREFIX = "PG7"


def _reset() -> None:
    with SessionLocal() as s:
        s.query(ProductSku).delete()
        s.query(Product).filter(Product.code.like(f"{PAGE_PREFIX}%")).delete(
            synchronize_session=False
        )
        s.query(Product).filter(Product.name.like("分页测试%")).delete(synchronize_session=False)
        s.query(ProductCategory).filter(
            ProductCategory.name.in_(("家用电器", "手机配件", "移动电源", "其他"))
        ).delete(synchronize_session=False)
        s.commit()


def _categories() -> None:
    """一级 家用电器 → 二级 手机配件 → 三级 移动电源（另加一个「其他」兜底）。"""
    with SessionLocal() as s:
        top = ProductCategory(level=1, name="家用电器", sort_order=1, source_id="pg7_top")
        s.add(top)
        s.flush()
        sub = ProductCategory(
            level=2, parent_id=top.id, name="手机配件", sort_order=1, source_id="pg7_sub"
        )
        s.add(sub)
        s.flush()
        s.add(
            ProductCategory(
                level=3, parent_id=sub.id, name="移动电源", sort_order=1, source_id="pg7_sub_l3_0"
            )
        )
        s.add(ProductCategory(level=1, name="其他", sort_order=99, source_id="hub_other"))
        s.commit()


def _make(name: str, **kwargs) -> Product:
    form = {"name": name, "code": f"{PAGE_PREFIX}{abs(hash(name)) % 100000:05d}"}
    form.update(kwargs)
    with SessionLocal() as s:
        row = product_service.create_product(s, form, created_by="test")
        return row


class ProductListPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        _reset()
        _categories()

    def tearDown(self) -> None:
        _reset()

    # ------------------------------------------------------------ 分页
    def test_10_default_page_size_50(self):
        """默认每页 50 条：造 60 款，第 1 页 50、第 2 页 10。"""
        for i in range(60):
            _make(f"分页测试商品{i:03d}")
        resp = self.client.get("/products")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("共 60 款", html)  # sub 里 stats.total 不适用，用分页条信息
        self.assertIn("第 1 / 2 页", html)
        page2 = self.client.get("/products?page=2")
        self.assertIn("第 2 / 2 页", page2.text)

    def test_20_per_page_switch(self):
        """每页条数可切 20：第 1 页只出 20 条，翻页带着 per_page 走。"""
        for i in range(25):
            _make(f"分页测试商品{i:03d}")
        resp = self.client.get("/products?per_page=20")
        self.assertIn("第 1 / 2 页", resp.text)
        self.assertIn("per_page=20", resp.text)
        # 乱填的档位回落默认 50：补到 60 款后仍是 2 页（按 999 就只有 1 页了）
        for i in range(25, 60):
            _make(f"分页测试商品{i:03d}")
        resp2 = self.client.get("/products?per_page=999")
        self.assertIn("第 1 / 2 页", resp2.text)

    def test_30_filter_keeps_in_page_links(self):
        """筛选条件要跟着翻页：带 q 翻页时链接里仍有 q。"""
        for i in range(60):
            _make(f"分页测试商品{i:03d}")
        resp = self.client.get("/products?q=分页测试&per_page=20&page=2")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("q=%E5%88%86%E9%A1%B5%E6%B5%8B%E8%AF%95", resp.text)  # q=分页测试

    # ------------------------------------------------------------ 批量
    def test_40_bulk_list_and_unlist(self):
        """批量上架 / 下架。"""
        rows = [_make(f"分页测试批量{i}") for i in range(3)]
        ids = [str(r.id) for r in rows]
        resp = self.client.post(
            "/products/bulk", data={"ids": ids, "action": "list"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(
                s.scalars(
                    select(Product).where(
                        Product.id.in_([r.id for r in rows]), Product.status == "on_sale"
                    )
                ).all().__len__(),
                3,
            )
        self.client.post("/products/bulk", data={"ids": ids, "action": "unlist"})
        with SessionLocal() as s:
            self.assertEqual(
                len(
                    s.scalars(
                        select(Product).where(
                            Product.id.in_([r.id for r in rows]), Product.status == "off_sale"
                        )
                    ).all()
                ),
                3,
            )

    def test_50_bulk_approve_and_reject(self):
        """批量审核通过 / 驳回。"""
        rows = [_make(f"分页测试审核{i}") for i in range(2)]
        ids = [str(r.id) for r in rows]
        self.client.post("/products/bulk", data={"ids": ids, "action": "approve"})
        with SessionLocal() as s:
            self.assertEqual(
                len(
                    s.scalars(
                        select(Product).where(
                            Product.id.in_([r.id for r in rows]),
                            Product.audit_status == "approved",
                        )
                    ).all()
                ),
                2,
            )
        self.client.post(
            "/products/bulk", data={"ids": ids, "action": "reject", "note": "资料不全"}
        )
        with SessionLocal() as s:
            self.assertEqual(
                len(
                    s.scalars(
                        select(Product).where(
                            Product.id.in_([r.id for r in rows]),
                            Product.audit_status == "rejected",
                        )
                    ).all()
                ),
                2,
            )

    def test_60_bulk_match_category(self):
        """批量匹配类目：按商品名认领三级类目，已填类目的不动，认不出来的不硬塞。"""
        power = _make("分页测试 小米移动电源 20000毫安")
        kept = _make("分页测试 已分类商品", category="家用电器 / 手机配件")
        unknown = _make("分页测试 稀奇古怪的名字xyz")
        ids = [str(power.id), str(kept.id), str(unknown.id)]
        resp = self.client.post(
            "/products/bulk", data={"ids": ids, "action": "category"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            got = {
                s.get(Product, power.id).category,
                s.get(Product, kept.id).category,
                s.get(Product, unknown.id).category,
            }
        self.assertIn("家用电器 / 手机配件 / 移动电源", got)
        self.assertIn("家用电器 / 手机配件", got)  # 已填的原样保留
        self.assertIn("", got)  # 认不出来留空，不塞「其他」

    # ------------------------------------------------------------ 价格
    def test_70_derive_prices_from_cost(self):
        """只填成本价：渔货价 = 成本 × 1.15，建议售价 = 渔货价 × 1.35。"""
        row = _make("分页测试 定价商品", cost_amount="100")
        self.assertAlmostEqual(row.fish_price, 115.0, places=2)
        self.assertAlmostEqual(row.price, 155.25, places=2)

    def test_80_manual_prices_not_overwritten(self):
        """手填的渔货价 / 建议售价不被覆盖。"""
        row = _make("分页测试 手填价格", cost_amount="100", fish_price="120", price="150")
        self.assertAlmostEqual(row.fish_price, 120.0, places=2)
        self.assertAlmostEqual(row.price, 150.0, places=2)

    def test_85_sku_rows_derive(self):
        """SKU 行留空：按商品级成本派生渔货价与建议售价。"""
        row = _make("分页测试 SKU价格", cost_amount="200")
        with SessionLocal() as s:
            product = s.get(Product, row.id)
            values = product_service._sku_values({"spec": "默认"}, product)
        self.assertAlmostEqual(values["fish_price"], 230.0, places=2)
        self.assertAlmostEqual(values["price"], 310.5, places=2)


class BackupFrequencyTest(unittest.TestCase):
    """订单备份：一天只该有一份（以前 UTC / 北京时间混用会每分钟刷一份）。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.directory = backup_service.backup_dir()
        self.made: list[str] = []

    def tearDown(self) -> None:
        for name in self.made:
            backup_service.delete_backup(name)
        # 顺手把测试期间产生的其它备份清掉，别污染别的用例
        for path in self.directory.glob("orders_*.json"):
            if not path.name.endswith(".meta.json"):
                backup_service.delete_backup(path.name)

    def test_90_filename_is_beijing_time(self):
        """文件名用北京时间：与「今天备份过没」的判定同一口径。"""
        meta = backup_service.create_backup(label="test")
        self.made.append(meta["file"])
        stamp = backup_service._file_time(meta["file"])
        self.assertIsNotNone(stamp)
        # 与北京时间相差不超过 2 分钟（容一台机器跑测试的耗时）
        from app.timeutil import beijing_now

        self.assertLess(abs((beijing_now() - stamp).total_seconds()), 120)

    def test_95_run_if_due_only_once_a_day(self):
        """同一天跑两次：第二次不再生成新文件（把时间钉在北京 10:00，避开 hour 闸门）。"""
        from unittest import mock

        fake = datetime(2026, 10, 7, 10, 0, 0)
        with mock.patch.object(backup_service, "beijing_now", return_value=fake):
            first = backup_service.run_if_due(force=True)
            self.assertIsNotNone(first)
            self.made.append(first["file"])
            # 文件名也是北京时间：日期能对上「今天」，判定才不会失忆
            self.assertTrue(first["file"].startswith("orders_20261007_"))
            before = len(backup_service.list_backups())
            second = backup_service.run_if_due()
            self.assertIsNone(second)
            self.assertEqual(len(backup_service.list_backups()), before)

    def test_96_not_due_before_backup_hour(self):
        """没到每日备份点（北京时间 03:00 前）不跑。"""
        from unittest import mock

        fake = datetime(2026, 10, 7, 1, 30, 0)
        with mock.patch.object(backup_service, "beijing_now", return_value=fake):
            self.assertIsNone(backup_service.run_if_due())


if __name__ == "__main__":
    unittest.main()
