"""引流名片：长链接 H5（OG 标签）+ 免登录对外访问 + 封面图上传。

三条硬规矩在这里被钉死：
1. OG 标签必须出现在**服务端返回的 HTML 源码**里（抖音爬虫不执行 JS）；
2. og:image 必须是**绝对地址**，且那个地址**免登录**能打开（爬虫没 Cookie）；
3. 域名走 HTTPS、停用的卡片对外 404。
"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import tests  # noqa: F401  先建测试库环境（DATABASE_URL / MEDIA_DIR）
from app.config import settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import ShareCard
from app.services import share_card as sc_service
from app.web.auth import ensure_default_admin

ADMIN = {"username": "admin", "password": "xianyu2026"}
# 对外域名：OG 里的地址都按它拼，测完还原
_SITE = "https://wms.yusunkeji.com"
_OLD_SITE = settings.public_base_url
_OLD_MEDIA = settings.media_path


def _wipe() -> None:
    with SessionLocal() as s:
        for row in s.scalars(select(ShareCard)):
            s.delete(row)
        s.commit()


class ShareCardTestCase(unittest.TestCase):
    def setUp(self):
        init_db()
        _wipe()
        ensure_default_admin()
        settings.public_base_url = _SITE
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        # 另一个**没登录**的客户端：模拟抖音爬虫（它不会带 Cookie）
        self.anon = TestClient(app)

    def tearDown(self):
        _wipe()
        settings.public_base_url = _OLD_SITE

    def _create(self, **extra):
        data = {
            "name": "10 月抖音群",
            "og_title": "渔枢二手优品",
            "og_desc": "官方验机 · 当天发货",
            "og_image": "/media/cards/cover.png",
            "body_text": "第一段\n\n第二段",
        }
        data.update(extra)
        resp = self.client.post("/share-cards/create", data=data, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return resp

    # ---------------------------------------------------------------- 基础
    def test_01_list_page_and_empty_state(self):
        html = self.client.get("/share-cards").text
        self.assertEqual(self.client.get("/share-cards").status_code, 200)
        self.assertIn("引流名片", html)
        self.assertIn("还没有名片", html)
        self.assertIn("/share-cards/new", html)

    def test_02_create_makes_slug_and_card(self):
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        self.assertTrue(card.slug, "slug 要自动生成")
        self.assertEqual(card.og_title, "渔枢二手优品")
        # 中文标题 → 拼音首字母（渔枢二手优品 = YSESY? 只断言是纯小写字母数字）
        self.assertRegex(card.slug, r"^[a-z0-9-]+$", f"slug 得是英文短名，实际 {card.slug}")

    def test_03_slug_conflict_gets_suffix(self):
        self._create(name="甲", og_title="渔枢二手优品")
        self._create(name="乙", og_title="渔枢二手优品")
        with SessionLocal() as s:
            slugs = sorted(c.slug for c in s.scalars(select(ShareCard)))
        self.assertEqual(len(slugs), 2)
        self.assertNotEqual(slugs[0], slugs[1], "重名要自动补序号")
        self.assertTrue(slugs[1].endswith("2") or slugs[1][-1].isdigit())

    def test_04_title_required(self):
        resp = self.client.post(
            "/share-cards/create", data={"og_title": "", "name": "x"}, follow_redirects=True
        )
        self.assertIn("卡片标题不能为空", resp.text)

    # ---------------------------------------------------------------- 公开长链接
    def test_05_public_link_needs_no_login(self):
        """爬虫不带 Cookie：链接必须**免登录**能打开，否则卡片是空的。"""
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        resp = self.anon.get(f"/c/{card.slug}")
        self.assertEqual(resp.status_code, 200, "长链接要免登录可访问")
        self.assertIn("text/html", resp.headers["content-type"])

    def test_06_og_tags_in_static_source(self):
        """OG 必须在源码里（爬虫不执行 JS，动态塞的抓不到）。"""
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        html = self.anon.get(f"/c/{card.slug}").text
        head = html.split("</head>")[0]
        self.assertIn('<meta property="og:title" content="渔枢二手优品">', head)
        self.assertIn('<meta property="og:description" content="官方验机 · 当天发货">', head)
        self.assertIn(f'<meta property="og:type" content="website">', head)
        self.assertIn(f'<meta property="og:url" content="{_SITE}/c/{card.slug}">', head)
        # 封面图必须是绝对地址
        self.assertIn(
            f'<meta property="og:image" content="{_SITE}/media/cards/cover.png">', head,
            "og:image 必须是带域名的绝对地址（相对路径爬虫拼不出来）",
        )

    def test_07_landing_page_shows_body_and_counts_hit(self):
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        html = self.anon.get(f"/c/{card.slug}").text
        self.assertIn("第一段", html)
        self.assertIn("第二段", html)
        with SessionLocal() as s:
            self.assertEqual(s.get(ShareCard, card.id).hits, 1)
        self.anon.get(f"/c/{card.slug}")
        with SessionLocal() as s:
            self.assertEqual(s.get(ShareCard, card.id).hits, 2)

    def test_08_disabled_card_is_404(self):
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
            cid = card.id
        self.client.post(f"/share-cards/{cid}/toggle", follow_redirects=False)
        resp = self.anon.get(f"/c/{card.slug}")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("失效", resp.text)
        # 启用后恢复
        self.client.post(f"/share-cards/{cid}/toggle", follow_redirects=False)
        self.assertEqual(self.anon.get(f"/c/{card.slug}").status_code, 200)

    def test_09_unknown_slug_is_404(self):
        self.assertEqual(self.anon.get("/c/not-exist").status_code, 404)

    # ---------------------------------------------------------------- 编辑 / 删除
    def test_10_edit_and_delete(self):
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
            cid, slug = card.id, card.slug
        page = self.client.get(f"/share-cards/{cid}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("渔枢二手优品", page.text)
        self.client.post(
            f"/share-cards/{cid}/edit",
            data={"og_title": "改过的标题", "og_desc": "新简介", "og_image": "/media/cards/x.png"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            card = s.get(ShareCard, cid)
            self.assertEqual(card.og_title, "改过的标题")
            self.assertEqual(card.slug, slug, "没改短名就别动链接")
        self.assertIn("改过的标题", self.anon.get(f"/c/{slug}").text)

        self.client.post(f"/share-cards/{cid}/delete", follow_redirects=False)
        with SessionLocal() as s:
            self.assertIsNone(s.get(ShareCard, cid))
        self.assertEqual(self.anon.get(f"/c/{slug}").status_code, 404)

    # ---------------------------------------------------------------- 风控提醒
    def test_11_risk_words_are_flagged(self):
        resp = self.client.post(
            "/share-cards/create",
            data={"og_title": "加我微信看货", "og_desc": "vx 同号"},
            follow_redirects=True,
        )
        self.assertIn("引流话术", resp.text, "含引流话术要提醒")
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        # 列表页也要看得出来哪张卡有风险
        self.assertIn("有引流话术", self.client.get("/share-cards").text)
        self.assertTrue(sc_service.risk_hits(card.og_title, card.og_desc))

    # ---------------------------------------------------------------- 封面图
    def test_12_cover_upload_and_public_access(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 2048
        resp = self.client.post(
            "/share-cards/upload-image",
            files={"file": ("cover.png", png, "image/png")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 200)
        url = resp.json()["url"]
        self.assertTrue(url.startswith("/media/cards/"), url)
        self.assertEqual(resp.json()["abs"], f"{_SITE}{url}")
        # 爬虫抓图没有 Cookie：这个目录必须裸奔
        got = self.anon.get(url)
        self.assertEqual(got.status_code, 200, "封面图必须免登录可访问，否则卡片空白")
        # 清理
        try:
            (settings.media_path / "cards" / Path(url).name).unlink()
        except OSError:
            pass

    def test_13_cover_rejects_non_image(self):
        resp = self.client.post(
            "/share-cards/upload-image",
            files={"file": ("evil.png", b"<html>not image</html>" * 5, "image/png")},
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不是真图片", resp.json()["error"])

    # ---------------------------------------------------------------- 页面口径
    def test_14_page_shows_full_link_and_refresh_link(self):
        self._create()
        html = self.client.get("/share-cards").text
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        self.assertIn(f"{_SITE}/c/{card.slug}", html)
        self.assertIn("重新抓取", html, "改了内容要有强制重新抓取的链接")
        self.assertIn("?t=", html)

    def test_15_non_https_site_is_warned(self):
        settings.public_base_url = "http://62.234.85.96:8000"
        try:
            html = self.client.get("/share-cards").text
            self.assertIn("不是 HTTPS", html, "http 域名要红字提醒（抖音基本不抓）")
            self.assertIn("抖音爬虫", html)
        finally:
            settings.public_base_url = _SITE


    # ---------------------------------------------------------------- 实时预览
    def test_16_preview_html_renders_without_saving(self):
        """编辑页右侧的预览：按表单内容渲染一份，**不入库**（不用先保存也能看）。"""
        resp = self.client.post(
            "/share-cards/preview-html",
            data={
                "og_title": "预览标题",
                "og_desc": "预览简介",
                "og_image": "/media/cards/pv.png",
                "body_title": "点开后的标题",
                "body_text": "正文一段",
                "slug": "pv-demo",
            },
            headers={"accept": "*/*"},
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()["html"]
        head = html.split("</head>")[0]
        self.assertIn('<meta property="og:title" content="预览标题">', head)
        self.assertIn(f'<meta property="og:image" content="{_SITE}/media/cards/pv.png">', head)
        self.assertIn(f'<meta property="og:url" content="{_SITE}/c/pv-demo">', head)
        self.assertIn("点开后的标题", html)
        self.assertIn("正文一段", html)
        # 关键：预览**不入库**
        with SessionLocal() as s:
            self.assertEqual(len(s.scalars(select(ShareCard)).all()), 0)

    def test_17_preview_does_not_auto_jump(self):
        """预览塞在 iframe 里，不能带着跳转脚本（否则预览框会自己跳走）。"""
        resp = self.client.post(
            "/share-cards/preview-html",
            data={"og_title": "跳", "jump_url": "https://example.com", "jump_seconds": "1"},
            headers={"accept": "*/*"},
        )
        html = resp.json()["html"]
        self.assertNotIn("setTimeout", html, "预览里不该有跳转脚本")
        self.assertNotIn("window.location.href", html)
        # 真发出去的页面**要**有
        self._create(og_title="真页面", jump_url="https://example.com", jump_seconds="1")
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        real = self.anon.get(f"/c/{card.slug}").text
        self.assertIn("window.location.href", real)
        self.assertIn("https://example.com", real)

    def test_18_form_page_has_live_preview(self):
        for path in ("/share-cards/new",):
            html = self.client.get(path).text
            self.assertIn("实时预览", html)
            self.assertIn('id="pvFrame"', html, "要有落地页预览框")
            self.assertIn('id="pvTitle"', html, "要有卡片标题预览")
            self.assertIn("/share-cards/preview-html", html)
        self._create()
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        self.assertIn('id="pvFrame"', self.client.get(f"/share-cards/{card.id}/edit").text)


    # ------------------------------------------------- 直接跳转（没有中间页）
    def test_19_plain_jump_has_no_landing_page(self):
        """只填跳转地址、没填落地页内容 = 点开直接跳走：极简页 + 秒数默认 0。"""
        self._create(og_title="直接跳", og_image="", body_text="",
                     jump_url="https://example.com/shop")
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        self.assertEqual(card.jump_seconds, 0, "不填秒数默认 0（点开立刻跳）")
        html = self.anon.get(f"/c/{card.slug}").text
        head = html.split("</head>")[0]
        self.assertIn('<meta property="og:title" content="直接跳">', head, "卡片标签照样要在")
        self.assertIn("正在跳转到目标页面", html)
        self.assertNotIn('<main class="card">', html, "没有落地页内容就别渲染那一屏")
        self.assertIn("}, 0);", html, "0 秒 = 立刻跳")
        self.assertNotIn("window.onload", html, "等 load 会被大图拖住，0 秒跳变等图加载完才跳")
        self.assertIn('href="https://example.com/shop"', html, "给个兜底链接")

    def test_20_landing_page_kept_when_filled(self):
        """填了大标题 / 正文 / 二维码任一，就照旧渲染那一屏，跳转同时生效。"""
        self._create(og_title="先看一屏", body_text="这里是介绍",
                     jump_url="https://example.com", jump_seconds="3")
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        html = self.anon.get(f"/c/{card.slug}").text
        self.assertIn('<main class="card">', html)
        self.assertIn("这里是介绍", html)
        self.assertIn("}, 3000);", html)
        self.assertNotIn("正在跳转到目标页面", html)

    def test_21_form_puts_direct_jump_first(self):
        """表单页把「直接跳转」摆在主路径上，落地页收进折叠区（选填）。"""
        html = self.client.get("/share-cards/new").text
        self.assertIn("② 点开卡片跳到哪儿", html)
        self.assertIn("没有中间页", html)
        self.assertIn('id="fJumpSec"', html)
        self.assertIn("value=\"0\"", html)
        self.assertIn("④ 链接与备注", html)
        # 落地页字段还在（老名片要有地方改），但默认收起
        self.assertIn("不填 = 没有中间页", html)
        self.assertIn('name="body_text"', html)

    def _upload(self, name: str, size: tuple[int, int]) -> dict:
        """造一张指定尺寸的真图走上传接口（Pillow 在依赖里）。"""
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", size, "#334455").save(buf, "JPEG")
        resp = self.client.post(
            "/share-cards/upload-image",
            files={"file": (name, buf.getvalue(), "image/jpeg")},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_23_cover_size_check(self):
        """封面图体检：竖图 / 小图要提示，合规横图的尺寸要写进 OG 源码。"""
        tall = self._upload("tall.jpg", (960, 1280))
        self.assertEqual((tall["width"], tall["height"]), (960, 1280))
        self.assertIn("竖图", tall["advice"], "竖图必须提示（抖音卡片配图要横图）")

        small = self._upload("small.jpg", (600, 400))
        self.assertIn("600×400", small["advice"])

        ok = self._upload("wide.jpg", (1200, 630))
        self.assertEqual((ok["width"], ok["height"]), (1200, 630))
        self.assertEqual(ok["advice"], "", "1200×630 横图是合规的，不该报警")

        self._create(og_title="横图卡片", og_image=ok["url"], body_text="")
        with SessionLocal() as s:
            slug = s.scalars(select(ShareCard)).one().slug
        head = self.anon.get(f"/c/{slug}").text.split("</head>")[0]
        self.assertIn('<meta property="og:image:width" content="1200">', head)
        self.assertIn('<meta property="og:image:height" content="630">', head)
        self.assertIn('<meta property="og:image:type" content="image/jpeg">', head)
        self.assertIn(f'<meta property="og:site_name" content="{_SITE.split("//")[-1]}">', head)

    def test_24_list_flags_bad_cover(self):
        """列表页给不合规封面打标，并写清「只剩一条纯文本链接」的排查顺序。"""
        square = self._upload("square.jpg", (800, 800))
        self._create(og_title="方图卡片", og_image=square["url"])
        html = self.client.get("/share-cards").text
        self.assertIn("800×800", html)
        self.assertIn("图不合规", html, "方图要打标")
        self.assertIn("没变成卡片", html)
        self.assertIn("纯文本", html, "页面上要写清排查顺序")

    def test_25_edit_page_shows_cover_advice(self):
        """编辑页对已存的竖图当场提示，不用等重新上传。"""
        tall = self._upload("tall2.jpg", (960, 1280))
        self._create(og_title="竖图卡片", og_image=tall["url"])
        with SessionLocal() as s:
            card_id = s.scalars(select(ShareCard)).one().id
        html = self.client.get(f"/share-cards/{card_id}/edit").text
        self.assertIn('id="coverCheck"', html)
        self.assertIn("竖图", html)

    def test_22_public_link_answers_head(self):
        """爬虫常先用 HEAD 探路：探到 405 可能就此不抓正文（线上实测过 405）。"""
        self._create(og_title="HEAD 探路")
        with SessionLocal() as s:
            card = s.scalars(select(ShareCard)).one()
        resp = self.anon.head(f"/c/{card.slug}")
        self.assertEqual(resp.status_code, 200, "HEAD 不能是 405")
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertEqual(self.anon.head("/c/no-such-card").status_code, 404)


if __name__ == "__main__":
    unittest.main()
