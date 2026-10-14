"""清理演示 / 1688 同步货源脚本的回归测试（scripts/purge_demo_items.py）。

2026-10-04：不再同步 1688 货源，线上 2546 条货源里 2542 条是演示数据
（1688_* 2442 条 + self_0001~0100 100 条），要一次性清掉，
真实货源（hub-prd-* / M1-* / SPU*）一条都不能动。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from app.db import SessionLocal, init_db  # noqa: E402
from scripts.purge_demo_items import DEMO_PREFIXES, KEEP_PREFIXES, purge  # noqa: E402
from src.models import (  # noqa: E402
    ChannelListing,
    PurchaseCartItem,
    ShopGoods,
    SkuMapping,
    SupplierItem,
)

# 造出来的测试货源：前 4 条是演示数据（该删），后 3 条是真实货源（该留）
DEMO_IDS = ["1688_t_purge_a", "1688_t_purge_b", "self_t_purge", "DEMO-T-PURGE"]
KEEP_IDS = ["hub-prd-t-purge", "M1-t-purge", "SPU20261004PURGE"]
ALL_IDS = DEMO_IDS + KEEP_IDS


def _wipe() -> None:
    init_db()
    with SessionLocal() as s:
        for model in (SkuMapping, ChannelListing, ShopGoods, PurchaseCartItem):
            s.query(model).filter(model.item_id.in_(ALL_IDS)).delete(synchronize_session=False)
        s.query(SupplierItem).filter(SupplierItem.item_id.in_(ALL_IDS)).delete(
            synchronize_session=False
        )
        s.commit()


class PurgeDemoItemsTest(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        with SessionLocal() as s:
            for item_id in ALL_IDS:
                s.add(SupplierItem(item_id=item_id, title=f"T-{item_id}", cost_price=10))
            s.flush()
            for item_id in ALL_IDS:
                s.add(SkuMapping(item_id=item_id, ali_sku_id=f"ali-{item_id}"))
                s.add(ChannelListing(item_id=item_id, channel="xianyu"))
                s.add(ShopGoods(owner="u1", shop_id="s1", item_id=item_id))
                s.add(PurchaseCartItem(owner="u1", item_id=item_id, sku_id=""))
            s.commit()

    def tearDown(self) -> None:
        _wipe()

    def _ids(self) -> set[str]:
        with SessionLocal() as s:
            return {r[0] for r in s.query(SupplierItem.item_id).filter(
                SupplierItem.item_id.in_(ALL_IDS)
            ).all()}

    def test_10_prefix_config(self):
        """前缀口径：演示数据 vs 真实货源（真实前缀一条都不能被当成演示数据）。"""
        self.assertTrue(any("1688_t_purge_a".startswith(p) for p in DEMO_PREFIXES))
        self.assertTrue(any("self_t_purge".startswith(p) for p in DEMO_PREFIXES))
        for kept in KEEP_IDS:
            self.assertTrue(
                any(kept.startswith(p) for p in KEEP_PREFIXES),
                f"{kept} 是真实货源，必须在保留前缀里",
            )

    def test_20_preview_changes_nothing(self):
        """不带 --apply 只预览：一条都不删。"""
        purge(apply=False, only=set(ALL_IDS))
        self.assertEqual(self._ids(), set(ALL_IDS), "预览不该动数据")

    def test_30_apply_removes_only_demo_items(self):
        """--apply：只删演示 / 1688 同步那批，真实货源一条不动。"""
        purge(apply=True, only=set(ALL_IDS))
        left = self._ids()
        self.assertEqual(left, set(KEEP_IDS), "只保留真实货源")
        for demo_id in DEMO_IDS:
            self.assertNotIn(demo_id, left, f"{demo_id} 该被清掉")

    def test_40_orphan_relations_cleaned(self):
        """挂在被删货源下面的关联记录一起清，真实货源的关联留着。"""
        purge(apply=True, only=set(ALL_IDS))
        with SessionLocal() as s:
            for model in (SkuMapping, ChannelListing, ShopGoods, PurchaseCartItem):
                rows = {r.item_id for r in s.query(model).filter(model.item_id.in_(ALL_IDS)).all()}
                self.assertEqual(rows, set(KEEP_IDS), f"{model.__tablename__} 里的孤儿关联要清掉")

    def test_50_idempotent(self):
        """跑第二遍不报错、也不误伤。"""
        purge(apply=True, only=set(ALL_IDS))
        purge(apply=True, only=set(ALL_IDS))
        self.assertEqual(self._ids(), set(KEEP_IDS))


if __name__ == "__main__":
    unittest.main()
