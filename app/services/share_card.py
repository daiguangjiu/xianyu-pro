"""引流名片：一份带 OG 标签的 H5 长链接（贴到抖音群里能渲染成卡片）。

为什么要有这个模块
------------------
抖音 / 微信发一条链接时，后台爬虫会去抓这个页面 ``<head>`` 里的 OG 标签，
抓到就把链接渲染成一张卡片（封面图 + 标题 + 简介）—— 比干巴巴一条网址显眼得多。
三条硬规矩：

1. **爬虫不执行 JS**：OG 标签必须写在服务端返回的 HTML **源码**里，
   用 JS 动态塞进去的一律抓不到（卡片就是一片空白）。所以这里是服务端拼好整页。
2. **og:image 必须是 HTTPS 绝对地址**：相对路径、内网图、需要登录才能看的图都不行。
   封面图因此单独落到 ``media/cards`` 下 —— 这个前缀在 ``web/auth.py`` 里
   **免登录放行**（爬虫没有 Cookie，卡在登录页就是空白卡片）。
3. **域名要备案 + 走 HTTPS**：这是抖音那边的硬门槛，做不到就别折腾卡片。
   页面会拿 ``settings.public_base_url`` 拼地址，配的是 http 就红字提醒。

长链接方案（不做短链）：直接用 ``https://域名/c/<slug>``。
改了内容抖音有缓存不会立刻更新 —— 页面上另给一个带 ``?t=时间戳`` 的「重新抓取」链接，
换个地址就等于重新抓一次。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ShareCard
from ..timeutil import now
from .pinyin import initials_of

# 封面图存的目录（相对 media_path）—— /media/cards/ 已免登录放行
CARD_MEDIA_DIR = "cards"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_IMAGE_MAGIC: tuple[tuple[bytes, int], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0),
    (b"\xff\xd8\xff", 0),
    (b"GIF87a", 0),
    (b"GIF89a", 0),
    (b"RIFF", 0),
)

# 风控词：落地页里出现这些话术，抖音可以吞卡片 / 拦截消息 / 限流。
# **只提醒不拦** —— 这是灰色引流，用不用由人自己决定，但得让用的人知道代价。
RISK_WORDS = (
    "微信", "weixin", "vx", "v信", "企微", "加我", "加好友", "加个好友",
    "私聊", "扫我", "扫码加", "扫一扫加", "扣扣", "qq", "加微信", "加v",
)


# ------------------------------------------------------------------ 地址
def site_base() -> str:
    """对外域名（去尾斜杠）：OG 里的地址都用它拼成绝对地址。"""
    return str(settings.public_base_url or "").rstrip("/")


def is_https() -> bool:
    """对外域名是不是 HTTPS —— 抖音爬虫对 http 地址基本不抓。"""
    return site_base().startswith("https://")


def abs_url(value: str) -> str:
    """把「/media/...」这类站内地址补成带域名的绝对地址；已经是外链就原样返回。

    OG 标签里的图址必须是绝对地址（爬虫不会拿你的域名去拼相对路径）。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    base = site_base()
    if not base:
        return raw
    return base + (raw if raw.startswith("/") else "/" + raw)


# ---------------------------------------------------------------- 封面图体检
# 抖音卡片配图口径：横图、宽 ≥1200、高 ≥630（约 1.91:1）。
# 平台侧对爬下来的封面图有尺寸门槛，竖图 / 小图常常直接不渲染卡片，只留一条纯文本链接。
COVER_MIN_WIDTH = 1200
COVER_MIN_HEIGHT = 630
COVER_MIN_RATIO = 1.4   # 低于这个宽高比就算「竖图 / 方图」
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _site_name() -> str:
    """og:site_name：用对外域名当站点名（卡片底部显示的那行也是它）。"""
    return site_base().split("//")[-1].split("/")[0] or "引流名片"


def _image_mime(value: str) -> str:
    return _MIME_BY_SUFFIX.get(Path(str(value or "")).suffix.lower(), "image/jpeg")


def cover_size(store_path: str) -> tuple[int, int] | None:
    """读本站封面图的像素尺寸；外链图 / 文件不在 / Pillow 没装都返回 None。"""
    raw = str(store_path or "").strip()
    prefix = f"/media/{CARD_MEDIA_DIR}/"
    if not raw.startswith(prefix):
        return None
    path = Path(settings.media_path) / CARD_MEDIA_DIR / Path(raw[len(prefix):]).name
    try:
        from PIL import Image  # noqa: PLC0415  可选依赖，读不到就当没这回事

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001
        return None


def cover_advice(store_path: str) -> str:
    """封面图体检：返回一句人话（空串 = 没问题，或读不到不瞎猜）。"""
    size = cover_size(store_path)
    if not size:
        return ""
    width, height = size
    ratio = (width / height) if height else 0
    # 先看竖图：960×1280 这种「宽不够还竖着」的图最常见，说「竖图」比说「小图」更贴切
    if ratio < COVER_MIN_RATIO:
        return (f"这张图是 {width}×{height} 的竖图，抖音卡片配图要横图（约 1.91:1，如 1200×630），"
                "竖图常常不出卡片")
    if width < COVER_MIN_WIDTH or height < COVER_MIN_HEIGHT:
        return (f"这张图只有 {width}×{height}，抖音卡片配图要 ≥{COVER_MIN_WIDTH}×{COVER_MIN_HEIGHT} 的横图，"
                "小图很可能只显示一条纯文本链接")
    return ""


def cover_info(store_path: str) -> dict:
    """封面图体检结果（列表页渲染用）：读不到尺寸就给空值，不瞎标不合规。"""
    size = cover_size(store_path)
    if not size:
        return {"width": 0, "height": 0, "ok": False, "advice": ""}
    width, height = size
    ratio = (width / height) if height else 0
    ok = width >= COVER_MIN_WIDTH and height >= COVER_MIN_HEIGHT and ratio >= COVER_MIN_RATIO
    return {"width": width, "height": height, "ok": ok, "advice": cover_advice(store_path)}


def card_url(card: ShareCard) -> str:
    """这张卡片的长链接（直接贴到群里那个）。"""
    return f"{site_base()}/c/{card.slug}" if site_base() else f"/c/{card.slug}"


def fresh_url(card: ShareCard) -> str:
    """「强制重新抓取」链接：改了内容抖音还显示旧卡片时用这个（换个地址 = 重新抓）。"""
    stamp = (card.updated_at or now()).strftime("%Y%m%d%H%M")
    return f"{card_url(card)}?t={stamp}"


# ------------------------------------------------------------------ 风控
def risk_hits(*texts: str) -> list[str]:
    """文案里命中的引流话术（提醒用，不拦）。"""
    blob = " ".join(str(t or "") for t in texts).lower()
    return [word for word in RISK_WORDS if word.lower() in blob]


# ------------------------------------------------------------------ slug
def _slugify(session: Session, text: str, skip_id: int = 0) -> str:
    """英文名：中文取拼音首字母，重名自动补序号（/c/xxxx 里那段）。"""
    letters = initials_of(str(text or ""), keep_alnum=True).lower()
    base = re.sub(r"[^a-z0-9]+", "-", letters).strip("-")[:32] or "card"
    slug, idx = base, 1
    while True:
        stmt = select(ShareCard).where(ShareCard.slug == slug)
        if skip_id:
            stmt = stmt.where(ShareCard.id != skip_id)
        if session.scalar(stmt) is None:
            return slug
        idx += 1
        slug = f"{base}{idx}"


# ------------------------------------------------------------------ 查询
def list_cards(session: Session, q: str = "") -> list[ShareCard]:
    keyword = str(q or "").strip()
    stmt = select(ShareCard)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                ShareCard.name.like(like),
                ShareCard.slug.like(like),
                ShareCard.og_title.like(like),
                ShareCard.og_desc.like(like),
            )
        )
    return list(
        session.scalars(stmt.order_by(ShareCard.enabled.desc(), ShareCard.id.desc()))
    )


def get_card(session: Session, card_id: int) -> ShareCard | None:
    return session.get(ShareCard, card_id)


def by_slug(session: Session, slug: str) -> ShareCard | None:
    return session.scalar(select(ShareCard).where(ShareCard.slug == str(slug or "")))


# ------------------------------------------------------------------ 写
def _clean(form: dict, key: str, limit: int = 0) -> str:
    value = str(form.get(key) or "").strip()
    return value[:limit] if limit else value


def create_card(session: Session, form: dict, *, commit: bool = True) -> tuple[ShareCard | None, str]:
    """新建名片：返回 (名片, 错误文案)，错误文案非空 = 没建成。"""
    title = _clean(form, "og_title", 120)
    if not title:
        return None, "卡片标题不能为空（就是抖音卡片上那行大字）"
    name = _clean(form, "name", 64) or title[:32]
    card = ShareCard(
        slug="",  # 先占位：slug 要按中文名生成，得等对象进会话后再查重
        name=name,
        og_title=title,
        og_desc=_clean(form, "og_desc", 255),
        og_image=_clean(form, "og_image", 500),
        body_title=_clean(form, "body_title", 120),
        body_text=str(form.get("body_text") or "").strip(),
        qr_image=_clean(form, "qr_image", 500),
        jump_url=_clean(form, "jump_url", 500),
        jump_seconds=_form_int(form.get("jump_seconds"), default=0, low=0, high=30),
        enabled=True,
    )
    session.add(card)
    session.flush()
    card.slug = _clean(form, "slug", 64) or _slugify(session, card.og_title or card.name)
    if commit:
        session.commit()
    return card, ""


def update_card(session: Session, card: ShareCard, form: dict, *, commit: bool = True) -> tuple[ShareCard, str]:
    """保存修改：表单里给了哪个字段就改哪个。"""
    if "og_title" in form and not _clean(form, "og_title"):
        return card, "卡片标题不能为空"
    for key in ("name", "og_title", "body_title"):
        if key in form:
            setattr(card, key, _clean(form, key, 120 if key != "name" else 64))
    for key in ("og_desc", "og_image", "qr_image", "jump_url"):
        if key in form:
            setattr(card, key, _clean(form, key, 255 if key == "og_desc" else 500))
    if "body_text" in form:
        card.body_text = str(form.get("body_text") or "").strip()
    if "jump_seconds" in form:
        card.jump_seconds = _form_int(form.get("jump_seconds"), default=0, low=0, high=30)
    if "slug" in form:
        want = _clean(form, "slug", 64)
        if want and want != card.slug:
            card.slug = _slugify(session, want, skip_id=card.id)
    if not card.name:
        card.name = (card.og_title or "")[:32]
    card.updated_at = now()
    if commit:
        session.commit()
    return card, ""


def toggle_card(session: Session, card: ShareCard, *, commit: bool = True) -> bool:
    """停启用：停用的卡片对外 404（旧链接失效），列表里保留。"""
    card.enabled = not card.enabled
    card.updated_at = now()
    if commit:
        session.commit()
    return card.enabled


def delete_card(session: Session, card: ShareCard, *, commit: bool = True) -> None:
    session.delete(card)
    if commit:
        session.commit()


def count_hit(session: Session, card: ShareCard, *, commit: bool = True) -> None:
    """页面被打开一次（爬虫来抓也算，所以只看趋势，别当真实访客数）。"""
    card.hits = int(card.hits or 0) + 1
    card.last_hit_at = now()
    if commit:
        session.commit()


def _form_int(value, *, default: int, low: int, high: int) -> int:
    try:
        num = int(str(value or "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


# ------------------------------------------------------------------ 封面图
def _is_real_image(data: bytes) -> bool:
    head = bytes(data or b"")[:16]
    for magic, offset in _IMAGE_MAGIC:
        if head[offset:offset + len(magic)] == magic:
            if magic == b"RIFF":
                return head[8:12] == b"WEBP"
            return True
    return False


def save_cover(data: bytes, filename: str) -> tuple[str, str]:
    """封面图落盘 → (站内地址, 错误文案)。

    为什么单独一套而不用 /media/products：卡片封面必须**免登录**可访问，
    而 media 下的其它目录默认要登录（订单截图不能裸奔）。存到 cards/ 下，
    ``web/auth.py`` 里对这个前缀单独放行。
    """
    name = str(filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return "", "只支持 png / jpg / jpeg / webp / gif"
    limit_mb = int(getattr(settings, "upload_image_max_mb", 5) or 0)
    if limit_mb and len(data) > limit_mb * 1024 * 1024:
        return "", f"封面图超过 {limit_mb}MB，压缩后再传（建议 1200×630）"
    if not _is_real_image(data):
        return "", "这个文件不是真图片（内容对不上），换一张再传"
    folder = Path(settings.media_path) / CARD_MEDIA_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{uuid4().hex[:16]}{suffix}"
        target.write_bytes(data)
    except OSError as exc:  # noqa: BLE001 写盘失败不能让页面 500
        return "", f"保存失败：{exc}"
    return f"/media/{CARD_MEDIA_DIR}/{target.name}", ""


# ------------------------------------------------------------------ 渲染
def _esc(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _paragraphs(text: str) -> str:
    """正文：空行分段，单换行转 <br>（跟商品详情那套一致的直觉）。"""
    blocks = [block.strip() for block in str(text or "").split("\n\n") if block.strip()]
    out = []
    for block in blocks:
        out.append("<p>" + "<br>".join(_esc(line) for line in block.split("\n")) + "</p>")
    return "".join(out)


def preview_from_form(form: dict) -> str:
    """按表单当前内容渲染一份（**不入库**），给编辑页右侧的实时预览用。

    拿一个内存里的 ShareCard 喂给同一个 render_html —— 预览看到的
    就是真发出去的那一份（不是另画一套），免得「预览好看、卡片出不来」。
    """
    return render_html(
        ShareCard(
            slug=_clean(form, "slug", 64) or "preview",
            name=_clean(form, "name", 64),
            og_title=_clean(form, "og_title", 120) or "（还没填标题）",
            og_desc=_clean(form, "og_desc", 255),
            og_image=_clean(form, "og_image", 500),
            body_title=_clean(form, "body_title", 120),
            body_text=str(form.get("body_text") or "").strip(),
            qr_image=_clean(form, "qr_image", 500),
            jump_url=_clean(form, "jump_url", 500),
            jump_seconds=_form_int(form.get("jump_seconds"), default=0, low=0, high=30),
        ),
        nojump=True,
    )


def render_html(card: ShareCard, *, nojump: bool = False) -> str:
    """渲染这张名片的 H5 页面（**OG 全在静态源码里**，爬虫不执行 JS 也能抓到）。

    返回完整 HTML 字符串：公开路由 /c/<slug> 直接吐它；管理页的「预览」也用它。

    ``nojump=True`` 时不输出跳转脚本 —— 后台预览是塞在 iframe 里的，
    让它真跳走就没法盯着看了（跳转只影响真人打开页面，爬虫本来就看不到）。
    """
    title = card.og_title or card.name or "名片"
    desc = card.og_desc or ""
    image = abs_url(card.og_image)
    url = card_url(card)
    body_title = card.body_title or title
    qr = abs_url(card.qr_image)
    jump = str(card.jump_url or "").strip()

    og_image_tag = f'<meta property="og:image" content="{_esc(image)}">\n' if image else ""
    if image:
        # 补上图片自身的元信息：爬虫（含抖音）拿不到尺寸时常按「无图」处理
        size = cover_size(card.og_image)
        if size:
            og_image_tag += (
                f'<meta property="og:image:width" content="{size[0]}">\n'
                f'<meta property="og:image:height" content="{size[1]}">\n'
                f'<meta property="og:image:type" content="{_image_mime(card.og_image)}">\n'
            )
    og_site_tag = f'<meta property="og:site_name" content="{_esc(_site_name())}">\n'
    twitter_tag = (
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{_esc(title)}">\n'
        f'<meta name="twitter:description" content="{_esc(desc)}">\n'
        f'<meta name="twitter:image" content="{_esc(image)}">\n'
        if image else ""
    )
    # 跳转用 JS：爬虫只读 HTML 不执行 JS，卡片预览不受影响。
    # 注意 script 里**不能**用 HTML 转义（& 会变成 &amp;，地址就废了）：
    # 用 json.dumps 生成 JS 字面量，再把 < 换成 \u003c 防提前闭合 script 标签
    seconds = max(0, min(int(card.jump_seconds or 0), 30))
    # 没填任何落地页内容（大标题 / 正文 / 二维码都没有）+ 填了跳转地址 =
    # 用户要的是「点开就进我的页面」，那就别渲染那一屏中间页
    plain_jump = bool(jump) and not (card.body_title or card.body_text or card.qr_image)
    jump_tag = ""
    if jump and not nojump:
        jump_js = json.dumps(jump).replace("<", "\\u003c")
        # **不能挂 window.onload**：封面图大时 onload 要等图下载完才触发，
        # 「0 秒跳」就变成「等图加载完才跳」，白屏好几秒
        jump_tag = (
            "<script>\n"
            f"setTimeout(function(){{ window.location.href = {jump_js}; }}, {seconds * 1000});\n"
            "</script>\n"
        )

    cover_html = f'<img class="cover" src="{_esc(image)}" alt="">' if image else ""
    qr_html = f'<img class="qr" src="{_esc(qr)}" alt="二维码">' if qr else ""
    jump_html = (
        f'<a class="go" href="{_esc(jump)}">继续浏览 →</a>'
        if jump else ""
    )
    body_html = _paragraphs(card.body_text)
    desc_html = f'<p class="desc">{_esc(desc)}</p>' if desc else ""
    if plain_jump:
        # 极简页：只有一句「正在跳转」+ 兜底链接，别的一律不渲染。
        # 0 秒跳时这一屏基本看不见，但省下的渲染/图片请求让跳转更快、不闪。
        main_html = (
            '<main class="plain">\n'
            "  <p>正在跳转到目标页面…</p>\n"
            f'  <p><a href="{_esc(jump)}">没跳过去？点这里</a></p>\n'
            "</main>"
        )
    else:
        main_html = (
            '<main class="card">\n'
            f"  {cover_html}\n"
            '  <div class="inner">\n'
            f"    <h1>{_esc(body_title)}</h1>\n"
            f"    {desc_html}\n"
            f'    <div class="body">{body_html}</div>\n'
            f"    {qr_html}\n"
            f"    {jump_html}\n"
            "  </div>\n"
            "</main>\n"
            f'<p class="foot">{_esc(card.name or "")}</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}">

<!-- ===== OG：抖音 / 微信爬虫只读这一段渲染卡片，必须静态写在源码里 ===== -->
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{_esc(desc)}">
{og_image_tag}{og_site_tag}<meta property="og:type" content="website">
<meta property="og:url" content="{_esc(url)}">
{twitter_tag}<!-- ================================================================ -->

<style>
  *{{box-sizing:border-box}}
  body{{margin:0;padding:24px 16px 40px;background:#f4f5f7;
       font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       color:#1f2933;line-height:1.7}}
  .card{{max-width:520px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;
         box-shadow:0 10px 30px rgba(15,23,42,.08)}}
  .cover{{display:block;width:100%;height:auto}}
  .inner{{padding:22px 22px 26px}}
  h1{{font-size:20px;margin:0 0 10px;line-height:1.4}}
  .desc{{font-size:14px;color:#52606d;margin:0 0 14px}}
  .body{{font-size:15px}}
  .body p{{margin:0 0 12px}}
  .qr{{display:block;width:180px;height:180px;margin:18px auto 4px;object-fit:contain}}
  .go{{display:block;margin-top:18px;padding:12px 16px;border-radius:10px;text-align:center;
       background:#2563eb;color:#fff;text-decoration:none;font-size:15px;font-weight:600}}
  .foot{{max-width:520px;margin:14px auto 0;text-align:center;font-size:12px;color:#9aa5b1}}
  .plain{{max-width:520px;margin:80px auto 0;text-align:center;font-size:14px;color:#52606d}}
  .plain a{{color:#2563eb}}
  @media (max-width:420px){{ body{{padding:14px 10px 30px}} .inner{{padding:18px 16px 22px}} }}
</style>
{jump_tag}</head>
<body>
  {main_html}
</body>
</html>
"""
