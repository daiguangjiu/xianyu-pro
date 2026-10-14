"""订单数据备份：每日定时把订单导出成 JSON 归档，超期自动清理。

业务口径（2026-09）：
  1 每天北京时间 03:00 自动备份一次订单数据（防止系统性 bug 导致数据丢失）；
  2 备份文件只保留最近 7 天（BACKUP_KEEP_DAYS 可配），超期自动删除；
  3 后台「数据备份」页可查看 / 下载 / 手动备份 / 删除备份文件。

备份内容是订单表的完整快照（JSON），与数据库解耦，
即使数据库被误删也能按文件重新核对或导入。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..models import Order
from ..timeutil import beijing_now, fmt, now

logger = logging.getLogger(__name__)

# 文件名：orders_年月日_时分秒.json（同一秒内重复备份时追加 _2、_3 …）
FILE_RE = re.compile(r"^orders_(\d{8})_(\d{6})(?:_\d+)?\.json$")

# 两次自动备份之间的最小间隔（小时）：每天只该跑一次，这是防刷屏的兜底闸门
MIN_INTERVAL_HOURS = 20

# 订单快照里导出的字段（与 models.Order 对应，含金额/物流/资金归属等关键维度）
ORDER_FIELDS = (
    "id",
    "platform",
    "order_no",
    "temp_order_no",
    "source_message_id",
    "chat_id",
    "chat_name",
    "sender_id",
    "sender_name",
    "msg_time",
    "buyer_nick",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    "product_name",
    "spec",
    "quantity",
    "sale_amount",
    "deal_amount",
    "total_amount",
    "cost_amount",
    "shipping_fee",
    "platform_fee",
    "profit_amount",
    "supplier_name",
    "xianyu_account",
    "payee_name",
    "supplier_status",
    "transfer_status",
    "tracking_no",
    "tracking_synced_at",
    "order_status",
    "order_state",
    "ordered_at",
    "pushed_at",
    "shipped_at",
    "parse_confidence",
    "needs_review",
    "note",
    # 撤单 / 退单留痕：谁、什么时候、因为哪条消息撤的
    "is_recalled",
    "recalled_at",
    "recalled_message_id",
    "recalled_by",
    "recall_reason",
    "refund_status",
    "refund_at",
    "refund_keyword",
    "refund_note",
    "refund_by",
    "created_at",
    "updated_at",
)


def backup_dir() -> Path:
    path = settings.backup_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        # 按服务端统一时区出（北京时间），别把库里的 UTC 值直接打出来
        return fmt(value, "%Y-%m-%d %H:%M:%S")
    return "" if value is None else str(value)


def _order_row(order: Order) -> dict:
    return {name: _iso(getattr(order, name)) for name in ORDER_FIELDS}


def create_backup(session=None, label: str = "") -> dict:
    """导出一次订单快照，返回备份文件信息。

    session 为空时自己开会话（定时任务 / CLI 场景）；
    传入 session 时不关闭它，只借用查询（Web 手动备份场景）。
    """
    owns_session = session is None
    if owns_session:
        from ..db import SessionLocal

        session = SessionLocal()
    try:
        rows = session.scalars(select(Order).order_by(Order.id.asc())).all()
        # 文件名与「到点判定」必须是同一个时间口径 —— 都按北京时间。
        # 以前这里用 now()（UTC），而 run_if_due 用 beijing_now() 判「今天备份过没」：
        # 北京 00:00~08:00 时 UTC 还停在昨天，文件名日期永远追不上今天 → 每分钟备份一次。
        ts = beijing_now()
        stamp = ts.strftime("%Y%m%d_%H%M%S")
        directory = backup_dir()
        target = directory / f"orders_{stamp}.json"
        seq = 1
        while target.exists():  # 同一秒内重复备份：追加序号，不覆盖上一份
            seq += 1
            target = directory / f"orders_{stamp}_{seq}.json"
        filename = target.name

        payload = {
            "generated_at": _iso(ts),
            "kind": "orders",
            "source": label or ("manual" if not owns_session else "auto"),
            "order_count": len(rows),
            "orders": [_order_row(o) for o in rows],
        }
        # 先写临时文件再改名，避免备份过程中断留下半截文件
        tmp = directory / f"{filename}.tmp"
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
        meta = {
            "file": filename,
            "generated_at": _iso(ts),
            "order_count": len(rows),
            "size": target.stat().st_size,
            "source": payload["source"],
        }
        (directory / f"{filename}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("订单备份完成：%s（%s 单，%s 字节）", filename, len(rows), meta["size"])
        return meta
    finally:
        if owns_session:
            session.close()


def list_backups() -> list[dict]:
    """列出备份文件（按时间倒序），读取同名 .meta.json 里的订单数。"""
    directory = backup_dir()
    out: list[dict] = []
    for path in directory.glob("orders_*.json"):
        if path.name.endswith(".meta.json"):
            continue
        meta_path = directory / f"{path.name}.meta.json"
        order_count = None
        generated_at = ""
        source = ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                order_count = meta.get("order_count")
                generated_at = meta.get("generated_at", "")
                source = meta.get("source", "")
            except (OSError, ValueError):
                pass
        m = FILE_RE.match(path.name)
        if m and not generated_at:
            generated_at = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}"
        out.append(
            {
                "file": path.name,
                "size": path.stat().st_size,
                "size_text": _human_size(path.stat().st_size),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "generated_at": generated_at or "-",
                "order_count": order_count,
                "source": source,
            }
        )
    out.sort(key=lambda x: x["file"], reverse=True)
    return out


def _human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _file_time(name: str) -> datetime | None:
    m = FILE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def prune_backups(keep_days: int | None = None) -> dict:
    """删除超过保留天数的备份（含同名 .meta.json），返回删除结果。"""
    days = max(int(keep_days if keep_days is not None else settings.backup_keep_days), 1)
    deadline = now() - timedelta(days=days)
    directory = backup_dir()
    removed: list[str] = []
    for path in sorted(directory.glob("orders_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        stamp = _file_time(path.name)
        if stamp is None:
            stamp = datetime.fromtimestamp(path.stat().st_mtime)
        if stamp >= deadline:
            continue
        for extra in (path, directory / f"{path.name}.meta.json"):
            try:
                extra.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001
                logger.warning("删除过期备份失败 %s: %s", extra.name, exc)
        removed.append(path.name)
    # 顺手清掉中断留下的临时文件
    for tmp in directory.glob("orders_*.json.tmp"):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001
            pass
    if removed:
        logger.info("已清理过期备份 %s 个（保留 %s 天）", len(removed), days)
    return {"removed": removed, "count": len(removed), "keep_days": days}


def delete_backup(name: str) -> bool:
    """删除指定备份文件（只允许备份目录下的 orders_*.json）。"""
    if not FILE_RE.match(name or ""):
        return False
    directory = backup_dir()
    target = directory / name
    if not target.exists():
        return False
    target.unlink(missing_ok=True)
    try:
        (directory / f"{name}.meta.json").unlink(missing_ok=True)
    except OSError:  # noqa: BLE001
        pass
    return True


def backup_file(name: str) -> Path | None:
    """下载用：校验文件名并返回绝对路径，非法名字返回 None。"""
    if not FILE_RE.match(name or ""):
        return None
    target = backup_dir() / name
    return target if target.exists() else None


def latest_backup() -> dict | None:
    rows = list_backups()
    return rows[0] if rows else None


def next_backup_time() -> datetime:
    """下次自动备份时间（北京时间）。"""
    current = now()
    target = current.replace(
        hour=max(min(int(settings.backup_hour), 23), 0), minute=0, second=0, microsecond=0
    )
    if target <= current:
        target += timedelta(days=1)
    return target


def run_if_due(session=None, force: bool = False) -> dict | None:
    """到点就跑一次备份（同一天只跑一次），返回备份信息或 None。"""
    if not settings.backup_enabled and not force:
        return None
    # 定时规则看北京时间（库里存 UTC，别拿 UTC 的小时判断到点没有）
    current = beijing_now()
    if not force and current.hour < max(min(int(settings.backup_hour), 23), 0):
        return None
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    latest = latest_backup()
    if not force and latest:
        stamp = _file_time(latest["file"])
        if stamp is None:
            stamp = datetime.fromtimestamp((backup_dir() / latest["file"]).stat().st_mtime)
        # 今天（北京时间）已经备份过就不重复
        if stamp >= today:
            return None
        # 兜底：两次自动备份至少隔 MIN_INTERVAL_HOURS 小时，防止时间口径再打架时刷屏
        if current - stamp < timedelta(hours=MIN_INTERVAL_HOURS):
            return None
    meta = create_backup(session=session, label="auto")
    prune_backups()
    return meta


def backup_loop(interval_seconds: int = 60) -> None:
    """后台定时备份线程：每分钟检查一次，到点备份并清理过期文件。"""
    while True:
        time.sleep(max(int(interval_seconds), 20))
        try:
            meta = run_if_due()
            if meta:
                logger.info(
                    "定时备份完成：%s（%s 单）", meta["file"], meta["order_count"]
                )
        except Exception as exc:  # noqa: BLE001  备份失败不影响主服务
            logger.warning("定时备份异常: %s", exc)
