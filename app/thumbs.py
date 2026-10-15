"""按需缩略图（2026-10-15）。

现场的真实情况：商品图是从 1688 素材搬下来的原图，平均一张接近 2MB、
最大 5MB（服务器 media 目录 2.4GB / 1289 张）。可它们在列表 / 购物车 /
结算页里只占 36~160 像素，等于一次拉几十 MB 的原图去画小方格 ——
这就是「页面图一多就慢」的根因。

所以加一层「按需裁图」：`/thumb/<相对 media 的路径>?w=<宽度>`
  · 按请求的宽度等比缩一次，落盘缓存到 media/_thumbs/<宽度>/ 下，
    第二次起就是静态文件级响应（几十 KB）；
  · 缓存文件名带上源图 mtime —— 商品图重裁后自动换新，不会继续发旧图；
  · 源图比目标宽度还小、或不是 PIL 认得的格式 → 直接回源图，不做任何转换；
  · 只允许读 media 目录内的文件（路径穿越一律 404）。

for The rest: 缩略图地址由 app.utils.thumb_url() 生成，模板里不用手拼。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

from .config import settings

_CACHE_DIR = "_thumbs"
_MIN_W = 24
_MAX_W = 1600


def _clamp(width: int) -> int:
    try:
        w = int(width)
    except (TypeError, ValueError):
        w = 320
    return max(_MIN_W, min(_MAX_W, w))


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _target(src: Path, rel: str, width: int) -> Path:
    """缓存路径：<media>/_thumbs/<宽度>/<原目录>/<文件名>.<源图 mtime>.webp。"""
    base = Path(settings.media_path).resolve() / _CACHE_DIR / str(width)
    try:
        stamp = int(src.stat().st_mtime)
    except OSError:
        stamp = 0
    return base / Path(rel).parent / f"{Path(rel).stem}.{stamp}.webp"


def ensure_thumb(rel: str, width: int = 320) -> tuple[Path, str]:
    """返回（要发出的文件, content-type）。

    rel：相对 media 目录的路径；width：目标宽度（会被夹到 24~1600）。
    """
    media_root = Path(settings.media_path).resolve()
    try:
        src = (media_root / rel).resolve()
    except (ValueError, OSError):
        raise HTTPException(404, "图片不存在")
    if src != media_root and media_root not in src.parents:
        raise HTTPException(404, "图片不存在")
    if not src.is_file():
        raise HTTPException(404, "图片不存在")

    width = _clamp(width)
    out = _target(src, rel, width)
    if out.exists():
        return out, "image/webp"

    try:
        with Image.open(src) as raw:
            exif_fixed = ImageOps.exif_transpose(raw)
            im = exif_fixed if exif_fixed is not None else raw
            w0, h0 = im.size
            if w0 <= width:            # 源图比要的还小：不放大，直接发原图
                return src, _content_type(src)
            height = max(1, int(round(h0 * (width / float(w0)))))
            resized = im.convert("RGB").resize((width, height), Image.LANCZOS)
            out.parent.mkdir(parents=True, exist_ok=True)
            resized.save(out, "WEBP", quality=82, method=4)
            return out, "image/webp"
    except HTTPException:
        raise
    except Exception:                  # 非图片 / 损坏文件：原样回源，不让页面开天窗
        return src, _content_type(src)


def install_thumb_route(app) -> None:
    """把 /thumb/... 挂到应用上（静态资源级别的缓存，URL 带宽度就够辨识）。"""
    media_root = Path(settings.media_path)
    media_root.mkdir(parents=True, exist_ok=True)

    @app.get("/thumb/{rel:path}", include_in_schema=False)
    def _thumb(rel: str, w: int = 320) -> FileResponse:
        path, ctype = ensure_thumb(rel, w)
        return FileResponse(
            path,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=2592000, immutable"},
        )
