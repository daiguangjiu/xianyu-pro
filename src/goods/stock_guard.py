"""库存为 0 的货源：自动下架 + 通知飞书群（2026-10-11 用户拍板）。

用户原话：「如果库存是0，则系统自动下架处理，并通知飞书群 @牛磊 @胡子萍
xxx商品库存为0，请知晓，做好商品上架事宜。」

口径：

1. **在架且库存 <= 0** 的货源（supplier_items）自动从渔货栈下架 —— 写
   `is_listed=False` / `unlisted_at` / `unlisted_reason` / `unlisted_by`，
   学员在货源广场看不到、详情页也点不了「立即下单」（详情页那两个按钮另有置灰）；
2. 往飞书群发一条通知，@ 配置里的人（默认「牛磊、胡子萍」），文案见 `build_text`；
3. **只对本次新下架的货发通知**（幂等）：已经是下架状态的不再重复通知，
   所以这个巡检跑多勤都不会刷群；
4. 每一条都写一条 `exception_logs`（type=`stock-zero-unlist`），
   渔货栈的「异常日志」页能查到是系统自动下架的。

触发方式：应用启动跑一次 + 后台线程按 `ZERO_STOCK_INTERVAL_SECONDS`（默认 10 分钟）
巡检一次；也可以手动调 `GET/POST /api/v1/goods/unlist-zero-stock`（带 token 的运维口子）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.timeutil import utc_now

from ..models import SupplierItem

logger = logging.getLogger(__name__)

# 自动下架的原因（页面上的「已下架」提示条会把这句话原文显示出来）
AUTO_UNLIST_REASON = "库存为 0，系统自动下架"
AUTO_UNLIST_BY = "系统"
# 通知里最多列几款（剩下的只说条数，免得群里刷屏）
NOTIFY_MAX_LINES = 20
# 一次巡检最多处理几款（防止历史脏数据一次性把群刷爆，多跑几次就清完了）
SCAN_LIMIT = 200


def zero_stock_items(session: Session, *, limit: int = SCAN_LIMIT) -> list[SupplierItem]:
    """在架、库存 <= 0 的货源（新的在前）。"""
    stmt = (
        select(SupplierItem)
        .where(SupplierItem.is_listed.is_(True))
        .where((SupplierItem.stock <= 0) | (SupplierItem.stock.is_(None)))
        .order_by(SupplierItem.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def _brief_of(item: SupplierItem) -> dict[str, Any]:
    return {
        "item_id": str(item.item_id or ""),
        "title": str(item.title or "").strip(),
        "stock": int(item.stock or 0),
    }


def unlist_zero_stock(
    session: Session, *, notify: bool = True, limit: int = SCAN_LIMIT
) -> dict[str, Any]:
    """把「库存为 0 但在架」的货源下架；返回 `{unlisted, notified, errors}`。

    幂等：下架后再跑就查不到了（条件里带 `is_listed`），所以不会重复通知。
    """
    rows = zero_stock_items(session, limit=limit)
    result: dict[str, Any] = {"unlisted": [], "notified": False, "errors": []}
    if not rows:
        return result

    now = utc_now()
    briefs: list[dict[str, Any]] = []
    for row in rows:
        row.is_listed = False
        row.unlisted_at = now
        row.unlisted_reason = AUTO_UNLIST_REASON
        row.unlisted_by = AUTO_UNLIST_BY
        briefs.append(_brief_of(row))
    session.commit()
    result["unlisted"] = briefs
    logger.warning("库存为 0 自动下架 %s 款货源：%s", len(briefs), [b["item_id"] for b in briefs])

    _log_exceptions(session, briefs, now)

    if notify:
        ok, errors = notify_zero_stock(briefs)
        result["notified"] = ok
        result["errors"] = errors
    return result


def _log_exceptions(session: Session, briefs: list[dict[str, Any]], at: Any) -> None:
    """每条自动下架都留一条异常日志（渔货栈「异常日志」页可查）。"""
    try:
        from ..models import ExceptionLog  # noqa: PLC0415

        for b in briefs:
            session.add(
                ExceptionLog(
                    type="stock-zero-unlist",
                    payload={
                        "item_id": b["item_id"],
                        "title": b["title"],
                        "stock": b["stock"],
                        "at": at.isoformat() if hasattr(at, "isoformat") else str(at),
                    },
                    error=AUTO_UNLIST_REASON,
                    resolved=True,  # 已经处理完了（下架就是处理动作），不用再让运营点
                )
            )
        session.commit()
    except Exception as exc:  # noqa: BLE001  日志写不进去不能影响下架本身
        logger.debug("写库存为 0 的异常日志失败：%s", exc)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def target_chat_ids(session: Session) -> list[str]:
    """通知发到哪些群：专用配置 > 日报推送群 > FEISHU_CHAT_IDS > 已加入的同步群。"""
    ids = [x.strip() for x in settings.stock_out_chat_ids if x.strip()]
    if not ids:
        ids = [x.strip() for x in settings.daily_summary_chat_ids if x.strip()]
    if not ids:
        ids = [x.strip() for x in settings.feishu_chat_ids if x.strip()]
    if not ids:
        try:
            from app.models import SyncState  # noqa: PLC0415

            ids = [c for c in session.scalars(select(SyncState.chat_id)).all() if c]
        except Exception as exc:  # noqa: BLE001
            logger.debug("读同步群失败：%s", exc)
            ids = []
    out: list[str] = []
    for cid in ids:
        if cid and cid not in out:
            out.append(cid)
    return out


def build_text(briefs: list[dict[str, Any]], at_ids: list[str] | None = None) -> str:
    """通知文案（用户原话口径：「xxx商品库存为0，请知晓，做好商品上架事宜」）。"""
    if not briefs:
        return ""
    at = "".join(f'<at user_id="{x}"></at>' for x in (at_ids or []) if x)
    lines: list[str] = []
    if at:
        lines.append(at)
    lines.append(
        f"【库存为 0 · 已自动下架】{len(briefs)} 款商品库存为 0，"
        "已从渔货栈下架，请知晓，做好商品上架事宜："
    )
    for i, b in enumerate(briefs[:NOTIFY_MAX_LINES], 1):
        title = b["title"] or "（无标题）"
        lines.append(f"{i}. {title}（{b['item_id']}）")
    if len(briefs) > NOTIFY_MAX_LINES:
        lines.append(f"…… 还有 {len(briefs) - NOTIFY_MAX_LINES} 款，在渔枢系统「商品管理」里补库存后再上架")

    body = "\n".join(lines)
    tpl = (settings.stock_out_template or "").strip()
    if tpl:
        try:
            return tpl.format(
                条数=len(briefs),
                明细="\n".join(lines[1 + (1 if at else 0):]),
                提示="已从渔货栈下架，请知晓，做好商品上架事宜",
                at=at,
            )
        except Exception as exc:  # noqa: BLE001  模板写错就退回内置文案
            logger.warning("库存为 0 通知模板渲染失败，改用内置文案：%s", exc)
    return body


def notify_zero_stock(
    briefs: list[dict[str, Any]], *, client: Any = None, session: Session | None = None
) -> tuple[bool, list[str]]:
    """把「库存为 0 已下架」推到群里；@ 配置的人。返回 `(是否有群发成功, 错误列表)`。"""
    if not briefs:
        return False, []
    from app.db import SessionLocal  # noqa: PLC0415  局部导入避免初始化顺序问题

    owns = session is None
    if owns:
        session = SessionLocal()
    errors: list[str] = []
    try:
        chats = target_chat_ids(session)
        if not chats:
            return False, ["没有可推送的群：请在系统设置里填「汇总推送群」或 FEISHU_CHAT_IDS"]

        if client is None:
            from app.feishu.client import get_client  # noqa: PLC0415

            client = get_client()
        if not client.enabled:
            return False, ["未配置飞书凭证，库存为 0 通知发不出去"]

        sent = 0
        for cid in chats:
            at_ids: list[str] = []
            try:
                from app.services import notify as notify_service  # noqa: PLC0415

                at_ids = notify_service.open_ids_by_names(
                    list(settings.stock_out_cc_names),
                    cid,
                    client,
                    preset=list(settings.stock_out_cc_open_ids),
                    label="库存为0通知",
                )
            except Exception as exc:  # noqa: BLE001  查不到人就只发文字
                logger.debug("查库存通知 @ 的人失败（%s）：%s", cid, exc)
            try:
                client.send_text(cid, build_text(briefs, at_ids), receive_id_type="chat_id")
                sent += 1
            except Exception as exc:  # noqa: BLE001  单群失败不影响别的群
                errors.append(f"{cid}: {exc}"[:300])
        return sent > 0, errors
    finally:
        if owns:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def run_once(*, notify: bool = True) -> dict[str, Any]:
    """开自己的会话跑一次巡检（后台线程 / 启动补跑用）。"""
    if not settings.zero_stock_auto_unlist:
        return {"skipped": "未开启自动下架（ZERO_STOCK_AUTO_UNLIST=0）", "unlisted": []}
    from app.db import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as session:
            return unlist_zero_stock(session, notify=notify)
    except Exception as exc:  # noqa: BLE001  巡检失败绝不能拖垮服务
        logger.warning("库存为 0 巡检失败：%s", exc)
        return {"unlisted": [], "notified": False, "errors": [str(exc)[:300]]}


def guard_loop() -> None:
    """后台线程：按 ZERO_STOCK_INTERVAL_SECONDS 巡检（0 = 只跑启动那一次）。"""
    import time  # noqa: PLC0415

    interval = max(int(settings.zero_stock_interval_seconds or 0), 60)
    while True:
        time.sleep(interval)
        out = run_once()
        if out.get("unlisted"):
            logger.warning("库存为 0 巡检：自动下架 %s 款", len(out["unlisted"]))
