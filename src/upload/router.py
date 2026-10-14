"""本地图片上传（演示）：存项目根 uploads/，返回 /uploads/... URL。"""
from __future__ import annotations
import secrets
import time
from pathlib import Path
from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter()
_UPLOAD_DIR = Path("uploads")
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_MAX_SIZE = 10 * 1024 * 1024


@router.post("/image", summary="上传单张图片，返回可访问的 URL")
async def upload_image(file: UploadFile = File(...)) -> dict:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(400, "不支持的图片格式: " + (ext or "空"))
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > _MAX_SIZE:
        raise HTTPException(400, "图片超过 10MB 上限")
    day = time.strftime("%Y%m%d")
    folder = _UPLOAD_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    fname = str(int(time.time() * 1000)) + "_" + secrets.token_hex(4) + ext
    (folder / fname).write_bytes(data)
    return {"url": "/uploads/" + day + "/" + fname}
