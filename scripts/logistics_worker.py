"""物流轨迹定时服务（**独立进程**）：定时刷在途单的轨迹，状态变了通知订单发送人。

为什么单独一个进程（2026-10-13 用户拍板）：
    定时任务要"挂掉也不影响日常业务系统"。放在主服务里，一个异常就可能拖累
    订单页面；独立跑之后，它自己挂了只是**物流状态不自动更新**，订单 / 发货 /
    财务这些照常能用，重启一下就回来。

干什么活（每轮）：
    1. 收单：把「有运单号但还没跟踪过」的单号捞进来建档（三端：渔枢订单、
       渔货栈订单、渔集出库单）；
    2. 刷新：取出「该刷新了」的在途单（超过 TTL、非终态），逐个查一次菜鸟；
    3. 通知：状态一变（比如运输中 → 派送中 / 派送失败），飞书通知订单发送人
       （在 logistics_trace 里触发，本脚本只负责把查询跑起来）。

怎么跑：
    python scripts/logistics_worker.py              # 前台跑（看日志）
    python scripts/logistics_worker.py --once       # 只跑一轮（演练 / 排查用）
    docker compose up -d worker                     # 容器里常驻（restart: unless-stopped）

节奏由环境变量定：LOGISTICS_WORKER_INTERVAL_SEC（默认 1800 秒）、
LOGISTICS_WORKER_BATCH（默认一轮 20 单）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import LogisticsTrace  # noqa: E402
from app.services import logistics_trace as trace_service  # noqa: E402
from app.timeutil import now  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("logistics_worker")


def _interval_sec() -> int:
    return max(60, int(getattr(settings, "logistics_worker_interval_sec", 1800) or 1800))


def _batch() -> int:
    return max(1, int(getattr(settings, "logistics_worker_batch", 20) or 20))


def _pending_mail_nos(session, limit: int = 200) -> list[str]:
    """三端里有运单号、但存档里还没有的单号（新单第一次进跟踪）。"""
    existed = {r.mail_no for r in session.query(LogisticsTrace.mail_no).all()}
    found: list[str] = []

    def add(no: str | None) -> None:
        no = (no or "").strip()
        if no and no not in existed and no not in found:
            found.append(no)

    try:  # 渔枢：订单的正向件与退回件
        from app.models import Order

        rows = session.query(Order.tracking_no, Order.return_tracking_no).filter(
            (Order.tracking_no != "") | (Order.return_tracking_no != "")
        ).limit(limit).all()
        for a, b in rows:
            add(a)
            add(b)
    except Exception as exc:  # noqa: BLE001
        logger.warning("收单跳过渔枢订单：%s", exc)

    try:  # 渔货栈：订单表
        from src.models import DownstreamOrder

        for no, in session.query(DownstreamOrder.shipping_no).filter(
            DownstreamOrder.shipping_no.isnot(None)
        ).limit(limit).all():
            add(no)
    except Exception as exc:  # noqa: BLE001
        logger.warning("收单跳过渔货栈订单：%s", exc)

    try:  # 渔集：出库单
        from wms.models import WmsOutboundOrder

        for no, in session.query(WmsOutboundOrder.waybill_no).filter(
            WmsOutboundOrder.waybill_no.isnot(None)
        ).limit(limit).all():
            add(no)
    except Exception as exc:  # noqa: BLE001
        logger.warning("收单跳过渔集出库单：%s", exc)

    return found[:limit]


def enqueue_new(session, limit: int = 200) -> int:
    """新单号建档（先建一条空存档，下一轮或本轮就会被查）。"""
    added = 0
    for mail_no in _pending_mail_nos(session, limit):
        session.add(LogisticsTrace(mail_no=mail_no, next_refresh_at=now()))
        added += 1
    if added:
        session.commit()
    return added


def run_once(session=None) -> dict:
    """跑一轮：收单 → 刷新 →（通知在 lookup 里触发）。"""
    own = session is None
    session = session or SessionLocal()
    try:
        added = enqueue_new(session)
        mails = trace_service.refresh_overdue(session, limit=_batch())
        ok = fail = 0
        for mail_no in mails:
            try:
                out = trace_service.lookup(session, mail_no, force=True)
                session.commit()
                if out.get("ok"):
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:  # noqa: BLE001  单条失败继续跑下一单
                session.rollback()
                fail += 1
                logger.warning("刷新 %s 失败：%s", mail_no, exc)
        stat = {"enqueued": added, "refreshed": len(mails), "ok": ok, "failed": fail}
        logger.info("一轮跑完：%s", stat)
        return stat
    finally:
        if own:
            session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="物流轨迹定时服务（独立进程）")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args()

    init_db()
    if args.once:
        run_once()
        return 0
    interval = _interval_sec()
    logger.info(
        "物流轨迹定时服务已启动：每 %s 秒刷一批（每轮最多 %s 单）", interval, _batch()
    )
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001  整轮炸了也不许进程退出
            logger.warning("一轮跑挂了（%s），%s 秒后重试", exc, interval)
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
