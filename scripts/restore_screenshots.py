"""恢复订单截图：把磁盘上丢失的原图从飞书重新下载回来，并按原图重裁商品图。

背景：线上 data/media/<年月>/ 下只剩商品图（*_product.png），原图文件不见了，
订单列表「截图」列因此全部变成破图（商品图还在，但可能是从错的图裁出来的）。

原图来源是飞书图片资源（message_id + file_key），数据库里都存着，直接重新拉取即可恢复。

用法：
    docker exec <app容器> python /app/restore_screenshots.py           # 恢复 + 重裁
    docker exec <app容器> python /app/restore_screenshots.py --dry-run # 只看缺多少张
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.feishu.client import get_client, save_bytes
from app.models import OrderImage
from app.services import ingest as ingest_service


def restore_images(session, dry_run: bool = False) -> tuple[int, int]:
    """重新下载文件缺失的订单截图，返回 (恢复数, 失败数)。"""
    images = list(session.scalars(select(OrderImage).order_by(OrderImage.id)).all())
    missing = [
        img
        for img in images
        if not img.local_path or not Path(img.local_path).exists()
    ]
    print(f"订单截图记录 {len(images)} 张，其中原图文件缺失 {len(missing)} 张")
    if dry_run:
        for img in missing[:20]:
            print(f"  待恢复 id={img.id} order_id={img.order_id} {img.file_key or '(无 file_key)'}")
        return 0, len(missing)

    client = get_client()
    if not client.enabled:
        print("飞书应用凭证未配置，无法重新下载（请到系统设置页填好凭证后重试）")
        return 0, len(missing)

    restored = failed = 0
    for img in missing:
        if not (img.message_id and img.file_key):
            print(f"  跳过 id={img.id}：缺 message_id / file_key（多半是手工上传的图）")
            failed += 1
            continue
        try:
            data = client.download_resource(img.message_id, img.file_key, "image")
        except Exception as exc:  # noqa: BLE001  单张失败不影响其它
            print(f"  下载失败 id={img.id} {img.file_key}: {exc}")
            failed += 1
            continue
        if not data:
            failed += 1
            continue
        if img.local_path:
            path = Path(img.local_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        else:
            # 记录里没存过路径的：按飞书消息规则重新落盘并回填
            path = save_bytes(data, f"{img.message_id}_{img.file_key[:12]}.png")
            img.local_path = str(path)
        img.size = len(data)
        restored += 1
    session.commit()
    print(f"已恢复 {restored} 张，失败 {failed} 张")
    return restored, failed


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    with SessionLocal() as session:
        restore_images(session, dry_run=dry_run)
        if dry_run:
            print("dry-run 结束，未改动任何数据")
            return
        # 商品图跟着恢复后的原图重裁：之前的图多有过期 / 重复，重裁一次对齐
        stats = ingest_service.backfill_product_images(session, force=True)
        print(
            "商品图重裁：扫描 {scanned} 笔，重裁 {cropped} 笔，跳过 {skipped} 笔".format(**stats)
        )


if __name__ == "__main__":
    main()
