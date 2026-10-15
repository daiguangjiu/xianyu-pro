"""渔货栈 v1.30.0：商品图片「一键下载 / 存手机相册」（2026-10-13）。

以前详情页「下载全部图片」是在浏览器里逐张触发下载 —— 二十张图要点二十次、
手机上更是一张张长按存，现场根本不用。现在两条路：

  ① 电脑：``GET /self/goods/{item_id}/images.zip`` 后端打包（主图/ 与 详情图/ 两个目录）；
  ② 手机：``GET /self/goods/{item_id}/images`` 图片墙，所有图一次铺开，长按每张即存
     （浏览器不许网页直接写相册，这是唯一合规又省事的做法）。
"""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src import deps as src_deps  # noqa: E402
from src.models import Base as SrcBase, ChannelListing, SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
# 1×1 的透明 PNG（免真去抓图）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class GoodsImagesDownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        engine = src_deps._get_engine()
        SrcBase.metadata.create_all(engine)
        src_deps._ensure_columns(engine, SrcBase)
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ChannelListing).delete()
            s.query(SupplierItem).delete()
            s.commit()
        # 2026-10-14 起站内 /media 图不再 httpx 回环抓（那是「空白图」的根因），
        # 打包直接读本地磁盘 —— 测试里把真图落进 media 目录，走的线上同一条链路
        from app.config import settings

        self._media_dir = Path(str(settings.media_path)) / "test"
        self._media_dir.mkdir(parents=True, exist_ok=True)
        self._made_files = []
        for name in ("main.jpg", "2.jpg", "3.jpg", "d1.jpg", "d2.jpg"):
            target = self._media_dir / name
            target.write_bytes(PNG)
            self._made_files.append(target)

    def tearDown(self) -> None:
        for made in getattr(self, "_made_files", []):
            made.unlink(missing_ok=True)
        with SessionLocal() as s:
            s.query(SupplierItem).delete()
            s.commit()

    def _make_item(self, item_id: str = "hub-img-1") -> SupplierItem:
        with SessionLocal() as s:
            item = SupplierItem(
                item_id=item_id, title="测试 无线蓝牙耳机（有图）",
                category_id="dg_audio", source_type="self_operated",
                main_image="/media/test/main.jpg",
                images=["/media/test/2.jpg", "/media/test/3.jpg"],
                detail_images=["/media/test/d1.jpg", "/media/test/d2.jpg"],
                stock=10, cost_price=Decimal("100"), sale_price=Decimal("159"),
                fish_price=Decimal("100"), audit_status="approved", is_listed=True,
            )
            s.add(item)
            s.commit()
            return item

    # ------------------------------------------------- ① 电脑：zip 包
    def test_10_images_zip_contains_both_folders(self):
        item = self._make_item()
        resp = self.client.get(f"/self/goods/{item.item_id}/images.zip")
        self.assertEqual(resp.status_code, 200, "打包下载要通")
        self.assertIn("application/zip", resp.headers["content-type"])
        self.assertIn("attachment", resp.headers["content-disposition"])
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        # 图片文件名 = 渔货号 + 产品名称 + 序号（2026-10-15 用户拍板）
        self.assertIn("主图/测试_无线蓝牙耳机（有图）_01.jpg", names)
        self.assertIn("主图/测试_无线蓝牙耳机（有图）_02.jpg", names)
        self.assertIn("主图/测试_无线蓝牙耳机（有图）_03.jpg", names,
                      "主图 = main_image + images，去重保序")
        self.assertIn("详情图/测试_无线蓝牙耳机（有图）_01.jpg", names)
        self.assertIn("详情图/测试_无线蓝牙耳机（有图）_02.jpg", names)
        self.assertIn("README.txt", names, "包里给一份说明（含没抓到的清单）")
        self.assertIn("测试", zf.read("README.txt").decode("utf-8"), "说明里要有商品标题")
        self.assertEqual(zf.read("主图/测试_无线蓝牙耳机（有图）_01.jpg"), PNG)

    def test_12_default_pack_is_1080_slim(self):
        """默认按 1080 宽打包（2026-10-15 用户拍板）：文件名与 README 都要体现尺寸。

        原图包动辄 90+MB，下起来要等一分钟以上；发闲鱼 1080 完全够用。
        """
        item = self._make_item()
        resp = self.client.get(f"/self/goods/{item.item_id}/images.zip")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("1080w", resp.headers.get("content-disposition", ""),
                      "默认是精简包，文件名带 1080w")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self.assertIn("每张等比压到宽 1080",
                          zf.read("README.txt").decode("utf-8"))

    def test_13_original_pack_still_available(self):
        """要原图传 ?w=0：页面上的「原图」切换就是走这个。"""
        item = self._make_item()
        resp = self.client.get(f"/self/goods/{item.item_id}/images.zip?w=0")
        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self.assertIn("原图直出", zf.read("README.txt").decode("utf-8"))
            self.assertEqual(zf.read("主图/测试_无线蓝牙耳机（有图）_01.jpg"), PNG,
                             "原图包字节不变")

    def test_15_relative_media_path_goes_absolute(self):
        """站内图是 /media/... 相对路径：抓图前必须补成绝对地址，否则一个都抓不到。"""
        from src.web.router import _abs_image_url

        self.assertEqual(
            _abs_image_url("/media/test/a.jpg", None), "http://127.0.0.1:8000/media/test/a.jpg",
            "没 request 时兜本机端口（合并部署就是这么装）",
        )
        self.assertEqual(
            _abs_image_url("https://x.com/a.jpg", None), "https://x.com/a.jpg",
            "本来就是绝对路径的别动它",
        )

    def test_14_zip_named_by_fish_code_and_title(self):
        """下载的包按「渔货号 + 产品名称」命名（2026-10-15 用户拍板）。

        以前叫「商品名 + 图片 + 尺寸 + 货源 ID」—— 下载目录里一堆包分不清谁是谁。
        现在渔货号打头，包内每张图也带同一前缀，散图存手机相册也不会跟别的货混。
        没有渔货号的货（自录入 / 外部档口）就只用产品名，包末尾兜一个货源 ID 防重名。
        """
        from src.goods.service import fish_code_of

        item = self._make_item()

        def zip_name_of(code: str) -> str:
            """改一下商家编码再下一次包，返回 UTF-8 那个文件名。"""
            with SessionLocal() as s:
                row = s.get(SupplierItem, item.id)
                row.item_code = code
                s.commit()
                want = fish_code_of(row)
            resp = self.client.get(f"/self/goods/{item.item_id}/images.zip")
            self.assertEqual(resp.status_code, 200)
            disp = resp.headers.get("content-disposition", "")
            self.assertIn("1080w", disp, "默认的尺寸标记别丢了")
            utf8 = unquote(disp.split("filename*=UTF-8''")[-1])
            # 有渔货号就打头，没有就只用产品名
            self.assertTrue(utf8.startswith(want or "测试_无线蓝牙耳机（有图）"), utf8)
            return utf8

        # ① 有渔货号：YS 打头，后面跟产品名
        self.assertTrue(
            zip_name_of("YS202610150001").startswith("YS202610150001_"),
            "包名要以渔货号打头",
        )
        # ② 没有渔货号（商家编码是 YHZ-…）：只用产品名，尾巴兜货源 ID 防重名
        name = zip_name_of("YHZ-JFJS-0001")
        self.assertTrue(name.startswith("测试_无线蓝牙耳机（有图）"))
        self.assertIn("hub-img-1", name, "没渔货号时兜一个货源 ID")

    def test_16_zip_entry_names_carry_fish_code(self):
        """包内每张图 = 渔货号_产品名称_序号（散图存相册也能认出是哪一款）。"""
        from src.goods.service import fish_code_of

        item = self._make_item()
        with SessionLocal() as s:
            row = s.get(SupplierItem, item.id)
            row.item_code = "YS202610150007"
            s.commit()
            self.assertEqual(fish_code_of(row), "YS202610150007")
        resp = self.client.get(f"/self/goods/{item.item_id}/images.zip?part=head")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
        self.assertIn("头图/YS202610150007_测试_无线蓝牙耳机（有图）_01.jpg", names)
        self.assertTrue(
            all(n.startswith("头图/YS202610150007_测试") for n in names if n.endswith(".jpg")),
            "这一包里的每张图都该带渔货号前缀",
        )

    def test_20_zip_missing_item_is_404(self):
        resp = self.client.get("/self/goods/no-such-item/images.zip")
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------- ② 手机：保存页
    def test_30_image_wall_page(self):
        item = self._make_item()
        page = self.client.get(f"/self/goods/{item.item_id}/images")
        self.assertEqual(page.status_code, 200)
        for text in ("/media/test/main.jpg", "/media/test/d1.jpg",
                     "存储到照片", "长按", "一键逐张下载"):
            self.assertIn(text, page.text, f"保存页应有「{text}」")
        # 详情页上的两个按钮：一个指 zip、一个指保存页
        detail = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("/self/goods/hub-img-1/images.zip", detail)
        self.assertIn("/self/goods/hub-img-1/images\"", detail)
        self.assertNotIn("pdpDownloadImages()", detail, "逐张下载那套已经换掉了")

    def test_40_wall_page_missing_item_is_404(self):
        self.assertEqual(
            self.client.get("/self/goods/no-such-item/images").status_code, 404)


if __name__ == "__main__":
    unittest.main()
