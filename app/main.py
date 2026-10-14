"""应用入口。"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from . import timeutil
from .config import settings
from .db import SessionLocal, init_db
from .feishu.events import handle_event
from .models import SyncState
from .services.ingest import backfill_order_source, sync_chat
from .services.notify import flush_held_notifications
from .web.api import router as api_router
from .web.auth import AuthMiddleware, HtmlNoCacheMiddleware, ensure_default_admin
from .web.pages import router as pages_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("order-hub")


def _repair_daily_indexes(reason: str = "") -> dict:
    """「今日第 x 单」修复：撤回 / 撤销撤回后把供应商当天的排队号修回去。"""
    from .services import push as push_service

    with SessionLocal() as session:
        out = push_service.repair_daily_indexes(session)
    if out["filled"] or out["fixed"]:
        logger.info(
            "「今日第 x 单」修复%s：回填 %s 笔，修正 %s 笔",
            f"（{reason}）" if reason else "",
            out["filled"],
            out["fixed"],
        )
    return out


def _sync_loop() -> None:
    """后台轮询同步：事件回调不可用时的兜底方案。"""
    interval = max(settings.sync_interval_seconds, 30)
    while True:
        time.sleep(interval)
        try:
            # 先做一次单号修复：同步过程中发现的撤回 / 撤销撤回在下个周期内即可反映出来
            _repair_daily_indexes("定时")
            with SessionLocal() as session:
                chat_ids = [c for c in settings.feishu_chat_ids]
                for row in session.query(SyncState).all():
                    if row.chat_id and row.chat_id not in chat_ids:
                        chat_ids.append(row.chat_id)
            if not chat_ids:
                continue
            for chat_id in chat_ids:
                # 不传起点：新群自动回补历史，老群从「已同步到的最后一条」续拉，停摆后也不漏
                st = sync_chat(chat_id)
                logger.info(
                    "自动同步 %s: 拉取 %s 新增 %s 解析 %s",
                    chat_id, st.fetched, st.created, st.parsed,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动同步异常: %s", exc)


def _backup_loop() -> None:
    """每日定时备份订单数据（默认北京时间 03:00），并清理超期备份。"""
    from .services.backup import backup_loop

    backup_loop()


def _voice_prune_loop() -> None:
    """定期清理过期的语音播报事件（只服务于实时提醒，保留 24 小时足够）。"""
    from .services import voice as voice_service

    while True:
        time.sleep(1800)
        try:
            with SessionLocal() as session:
                voice_service.prune(session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理语音播报事件异常: %s", exc)


def _notify_flush_loop() -> None:
    """静默期/限流挂起的回执：每分钟检查一次，恢复后合并补发。"""
    while True:
        time.sleep(60)
        try:
            with SessionLocal() as session:
                out = flush_held_notifications(session)
            if out.get("sent"):
                logger.info("补发挂起回执 %s 条（%s 条合并）", out["sent"], out["batches"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("补发挂起回执异常: %s", exc)


def _daily_summary_loop() -> None:
    """每日出单汇总：到点按「人」汇总当天出单 / 退单并推到群里。"""
    from .services.daily_summary import summary_loop

    summary_loop()


def _stock_guard_loop() -> None:
    """渔货栈库存巡检：库存为 0 的货源自动下架 + 通知飞书群（2026-10-11 用户拍板）。

    真身是 `src/goods/stock_guard.py` —— 这里只是把线程挂起来；它自己按
    ZERO_STOCK_INTERVAL_SECONDS 控制节奏，设置页 / .env 改了下一轮就生效。
    """
    from src.goods import stock_guard

    stock_guard.guard_loop()


def _reocr_loop() -> None:
    """截图重识别兜底：还是临时单号（TMP-）的订单，隔一段时间重跑一次截图 OCR。

    截图明明配上了、单号却一直没更新时，靠这个自动补上，不用人工盯。
    """
    from .services import ingest as ingest_service

    while True:
        interval = max(int(settings.ocr_reocr_minutes or 0), 0)
        if interval <= 0:
            time.sleep(3600)  # 关掉了：一小时看一次设置，开了就生效
            continue
        time.sleep(interval * 60)
        try:
            with SessionLocal() as session:
                out = ingest_service.reocr_pending(session)
            if out.get("checked"):
                logger.info(
                    "截图重识别：检查 %s 单，升级 %s 单，疑似 %s 单，跳过 %s 单",
                    out["checked"],
                    out["upgraded"],
                    out["hint"],
                    out["skipped"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("截图重识别异常: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 后台账号体系：至少保证有一个管理员（默认 admin / xianyu2026）
    admin = ensure_default_admin()
    logger.info("管理员账号已就绪：%s", admin.username)
    # 老订单补全上行消息维度（发送人 / 发送时间 / 群名）
    try:
        with SessionLocal() as s:
            filled = backfill_order_source(s)
        if filled:
            logger.info("已为 %s 个订单补全发送人与发送时间", filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("订单来源信息补全失败: %s", exc)
    # 买家标识：历史订单的「买家」一栏补成「收货人 + 手机号」（跨单认人 / 用户管理用）
    try:
        from .services import buyer as buyer_service

        with SessionLocal() as s:
            buyer_filled = buyer_service.ensure_buyer_labels(s)
        if buyer_filled:
            logger.info("已为 %s 个订单补全买家标识（收货人 + 手机号）", buyer_filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("买家标识补全失败: %s", exc)
    # 系统默认规则：① 每个一级类目一套「类目特性规则」② 运费模板 / 发货方式各一条全局默认
    try:
        from .services import (
            category as category_service,
            shipping as shipping_service,
            return_addr as return_addr_service,
        )

        with SessionLocal() as s:
            category_service.ensure_seeded(s)
            rules_created = category_service.ensure_default_rules(s)
            shipping_created = shipping_service.ensure_defaults(s)
            # 类目口径改成只到一级后，把历史规则里关联的二 / 三级类目收敛到一级
            scope_fixed = shipping_service.ensure_top_level_scope(s)
            addr_created = return_addr_service.ensure_default(s)
            # 发货地址与退货地址同表同逻辑（kind 区分），也各播一条默认
            ship_addr_created = return_addr_service.ensure_default(
                s, kind=return_addr_service.KIND_SHIP
            )
        if rules_created or shipping_created or scope_fixed or addr_created or ship_addr_created:
            logger.info(
                "已补齐系统默认规则：类目特性规则 %s 条、运费 / 发货方式默认 %s 条、"
                "类目口径收敛 %s 条、退货地址默认 %s 条、发货地址默认 %s 条",
                rules_created, shipping_created, scope_fixed, addr_created, ship_addr_created,
            )
    except Exception as exc:  # noqa: BLE001  默认规则失败不影响启动
        logger.warning("系统默认规则播种失败: %s", exc)
    # 内置角色（管理员 / 普通成员）：账号的角色权限都挂在这两个上，缺了会没人能配权限
    try:
        from .db import SessionLocal as _SessionLocal
        from .web import permissions

        with _SessionLocal() as s:
            roles_created = permissions.ensure_builtin_roles(s)
        if roles_created:
            logger.info("已播种内置角色 %s 个（管理员 / 普通成员）", roles_created)
    except Exception as exc:  # noqa: BLE001  角色播种失败不影响启动
        logger.warning("内置角色播种失败: %s", exc)
    # 资金结算：按现有订单补全 / 校正结算流水（公司自营应收、公司自营应付）
    try:
        from .services import finance as finance_service

        with SessionLocal() as s:
            finance_service.sync_all(s)
    except Exception as exc:  # noqa: BLE001  结算流水补全失败不影响启动
        logger.warning("结算流水补全失败: %s", exc)
    # 退单处理状态：老退单只落了订单状态，这里按状态补上处理状态（状态口径统一的前提）
    try:
        from .services import refund as refund_service

        with SessionLocal() as s:
            filled = refund_service.backfill_refund_status(s)
        if filled:
            logger.info("已为 %s 笔老退单补全退单处理状态", filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("退单处理状态补全失败: %s", exc)
    # 顺带把记错的处理状态改回来：还没推给供应商的不该是「待拦截」，已关闭的不该挂着「待拦截」
    try:
        from .services import refund as refund_service

        with SessionLocal() as s:
            fixed = refund_service.fix_intercept_status(s)
        if fixed:
            logger.info("已修正 %s 笔退单的处理状态（未推单 / 已关闭的不再显示为待拦截）", fixed)
    except Exception as exc:  # noqa: BLE001  修正失败不影响启动
        logger.warning("退单处理状态修正失败: %s", exc)
    # 老数据补「已叫停 / 已收到退回件」两个事实字段（阶段二新增，之后由操作与物流轨迹写入）
    try:
        from .services import refund as refund_service

        with SessionLocal() as s:
            filled = refund_service.backfill_refund_facts(s)
        if filled:
            logger.info("已为 %s 笔老退单补全退单步骤事实（已叫停 / 已收到退回件）", filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("退单步骤事实补全失败: %s", exc)
    # 平台技术服务费：老订单只在资金页现算、订单上没落金额，这里按店铺费率（默认 0.6%）补上
    try:
        from .services import finance as finance_service

        with SessionLocal() as s:
            filled = finance_service.backfill_service_fee(s)
        if filled:
            logger.info("已为 %s 个订单补上平台技术服务费金额", filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("平台技术服务费补全失败: %s", exc)
    # 订单状态（新建 / 已推单 / 已发货 / 已撤单 / 已退单）：老订单按当前事实补一次
    try:
        from .services import order_state as order_state_service

        with SessionLocal() as s:
            filled = order_state_service.backfill(s)
        if filled:
            logger.info("已为 %s 个订单补全订单状态", filled)
    except Exception as exc:  # noqa: BLE001  补全失败不影响启动
        logger.warning("订单状态补全失败: %s", exc)
    # 超时兜底：已发货超过 N 天还没签收的自动确认收货（正向终态「已完成」）
    try:
        from .services import order_state as order_state_service

        with SessionLocal() as s:
            filled = order_state_service.auto_complete_transit(s)
        if filled:
            logger.info("已为 %s 个订单自动确认收货（超过 %s 天）", filled,
                        order_state_service.AUTO_COMPLETE_DAYS)
    except Exception as exc:  # noqa: BLE001  兜底失败不影响启动
        logger.warning("超时自动确认收货失败: %s", exc)
    # 飞书员工真实姓名：有通讯录/群成员权限时自动补全一小批历史数据
    if settings.feishu_enabled:
        try:
            from .services.ingest import backfill_sender_names

            with SessionLocal() as s:
                filled = backfill_sender_names(s, limit=100)
            if filled:
                logger.info("已补全 %s 条飞书发送人姓名", filled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送人姓名补全失败: %s", exc)
    # 「今日第 x 单」：启动时先校验一次（撤回 / 撤销撤回导致的单号变化在这里兜底修回）
    try:
        _repair_daily_indexes("启动")
    except Exception as exc:  # noqa: BLE001  修复失败不影响启动
        logger.warning("「今日第 x 单」修复失败: %s", exc)
    if settings.auto_sync and settings.feishu_enabled:
        threading.Thread(target=_sync_loop, daemon=True).start()
        logger.info("后台自动同步已启动（间隔 %s 秒）", settings.sync_interval_seconds)
    if settings.feishu_enabled:
        threading.Thread(target=_notify_flush_loop, daemon=True).start()
    # 订单数据备份：启动时补跑一次（当天还没备份过才跑），之后由后台线程每天定时跑
    if settings.backup_enabled:
        try:
            from .services import backup as backup_service

            with SessionLocal() as s:
                # 先按保留天数清一遍（历史遗留的重复备份也一并清掉），再判断今天要不要补跑
                removed = backup_service.prune_backups()
                if removed["count"]:
                    logger.info("启动清理过期备份 %s 个", removed["count"])
                meta = backup_service.run_if_due(s)
            if meta:
                logger.info("启动补跑备份：%s（%s 单）", meta["file"], meta["order_count"])
        except Exception as exc:  # noqa: BLE001  备份失败不影响启动
            logger.warning("启动备份失败: %s", exc)
        threading.Thread(target=_backup_loop, daemon=True).start()
        threading.Thread(target=_voice_prune_loop, daemon=True).start()
    # 每日出单汇总：启动时补跑一次（到点且当天还没推过），之后由后台线程每天定时推
    if settings.daily_summary_enabled:
        try:
            from .services import daily_summary as daily_summary_service

            meta = daily_summary_service.run_if_due()
            if meta and meta.get("sent"):
                logger.info("启动补推每日出单汇总：%s", meta["date"])
        except Exception as exc:  # noqa: BLE001  补推失败不影响启动
            logger.warning("每日出单汇总补推失败: %s", exc)
    # 检测仓（WMS）：**独立子系统**，平台只负责在启动时喊它一声，
    # 建表 / 内置角色 / 规则播种 / 报修超时打标都在它自己的 ``wms.startup`` 里，
    # 权限与菜单也完全是它自己的 —— 平台这里不掺和任何判断。
    try:
        from wms.startup import startup as wms_startup

        meta = wms_startup()
        if meta.get("seed"):
            logger.info("检测仓启动就绪：%s", meta)
        elif meta.get("error"):
            logger.warning("检测仓启动未完成：%s", meta["error"])
    except Exception as exc:  # noqa: BLE001  子系统没起来也不该把平台拖挂
        logger.warning("检测仓（WMS）启动失败: %s", exc)
    # 线程无条件启动：管理员在系统设置页开启后立即生效，不必重启服务
    threading.Thread(target=_daily_summary_loop, daemon=True).start()
    # 截图重识别：临时单号的订单定时重跑截图 OCR（设置页改间隔后立即生效）
    threading.Thread(target=_reocr_loop, daemon=True).start()
    # 渔货栈：确保存在平台管理员（审核台没人能操作就废了）
    if settings.hub_enabled:
        try:
            from .hub_bridge import ensure_hub_admin

            ensure_hub_admin()
        except Exception as exc:  # noqa: BLE001
            logger.warning("渔货栈初始化跳过：%s", exc)
    # 渔货栈库存巡检：库存为 0 的货源自动下架 + 通知飞书群（2026-10-11 用户拍板）。
    # 启动先跑一次（上线后历史遗留的 0 库存货当场清掉），之后由后台线程定时跑。
    if settings.hub_enabled and settings.zero_stock_auto_unlist:
        try:
            from src.goods import stock_guard

            meta = stock_guard.run_once()
            if meta.get("unlisted"):
                logger.warning("启动巡检：库存为 0 自动下架 %s 款", len(meta["unlisted"]))
            for err in meta.get("errors") or []:
                logger.warning("库存为 0 通知发送失败：%s", err)
        except Exception as exc:  # noqa: BLE001  巡检失败不影响启动
            logger.warning("库存为 0 巡检跳过：%s", exc)
        threading.Thread(target=_stock_guard_loop, daemon=True).start()
    yield


class TimezoneMiddleware:
    """把本次请求要用的时区写进 contextvar（给拿不到 request 的 `human_time()` 用）。

    2026-10-13 起**默认写服务端统一时区**（`settings.display_tz`，北京）：
    浏览器带进来的 `?tz=` / cookie `tz` 只在开了 `TZ_FOLLOW_BROWSER` 时才采纳，
    否则全站时间都是同一个点，不会因浏览者时区不同而变。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        wanted = (
            timeutil.tz_from_scope(scope)
            if timeutil.follow_browser()
            else timeutil.display_tz()
        )
        token = timeutil.set_current_tz(wanted)
        try:
            await self.app(scope, receive, send)
        finally:
            timeutil.reset_current_tz(token)


def _page_title_prefix(path: str) -> str:
    """浏览器标签页要带的系统前缀（2026-10-14 用户拍板：三套系统各带各的）。

    渔枢 / 渔集（检测仓）/ 渔货栈跑在同一个进程里，浏览器开着一堆标签时
    光看标题认不出是哪套。错误页**刻意不走模板**（模板本身可能坏了），
    所以前缀得自己拼 —— 按路径判断自己属于哪套。
    """
    p = str(path or "")
    if p.startswith("/wms"):
        return "【渔集】"
    if p.startswith("/self") or p.startswith("/api/v1"):
        return "【渔货栈】"
    return "【渔枢】"


def _error_page_html(log_id: int | None, prefix: str = "【渔枢】") -> str:
    """500 页面：**不依赖模板**（模板本身也可能坏），也不把堆栈甩给用户。

    给一句人话 + 一个编号 —— 编号就是「异常日志」里的那条记录，报给我时带上它，
    一眼就能定位到堆栈，不用再靠「大概几点钟」猜。
    """
    no = f"#{log_id}" if log_id else "已记录"
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{prefix}系统出了点问题</title></head>"
        "<body style=\"margin:0;font-family:-apple-system,'PingFang SC',sans-serif;"
        "background:#f6f7f9;color:#1f2933\">"
        "<div style=\"max-width:520px;margin:12vh auto;padding:28px 30px;background:#fff;"
        "border-radius:14px;box-shadow:0 8px 30px rgba(15,23,42,.08)\">"
        "<div style=\"font-size:34px;line-height:1\">⚠️</div>"
        "<h1 style=\"font-size:19px;margin:14px 0 8px\">这一步没做成，服务器出错了</h1>"
        "<p style=\"font-size:13.5px;color:#52606d;line-height:1.75;margin:0 0 16px\">"
        "出错信息已经自动记进「异常日志」（编号 " + no + "），"
        "把编号告诉管理员就能查到具体原因。刷新页面可以再试一次。</p>"
        "<a href=\"/\" style=\"display:inline-block;padding:9px 16px;border-radius:9px;"
        "background:#2563eb;color:#fff;text-decoration:none;font-size:13.5px\">回到首页</a>"
        "</div></body></html>"
    )


def _http_error_page_html(status: int, detail: str, prefix: str = "【渔枢】") -> str:
    """400~599 的异常在**浏览器**里看到的人话页（不依赖模板，模板本身也可能坏）。

    文案就是异常自己的中文原因（例如「没有该页面的访问权限，请联系管理员…」），
    用户能直接照着去问管理员；下面给个回首页的口子，不至于卡死在空白页上。
    """
    title = {401: "请先登录", 403: "没有访问权限", 404: "页面不存在"}.get(status, "这一步没做成")
    icon = {401: "🔑", 403: "🔒", 404: "🧭"}.get(status, "⚠️")
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{prefix}{status} · {title}</title></head>"
        "<body style=\"margin:0;font-family:-apple-system,'PingFang SC',sans-serif;"
        "background:#f6f7f9;color:#1f2933\">"
        "<div style=\"max-width:520px;margin:12vh auto;padding:28px 30px;background:#fff;"
        "border-radius:14px;box-shadow:0 8px 30px rgba(15,23,42,.08)\">"
        f"<div style=\"font-size:34px;line-height:1\">{icon}</div>"
        f"<h1 style=\"font-size:19px;margin:14px 0 8px\">{status} · {title}</h1>"
        "<p style=\"font-size:13.5px;color:#52606d;line-height:1.75;margin:0 0 16px\">"
        f"{escape(detail or title)}</p>"
        "<a href=\"/\" style=\"display:inline-block;padding:9px 16px;border-radius:9px;"
        "background:#2563eb;color:#fff;text-decoration:none;font-size:13.5px\">回到首页</a>"
        "</div></body></html>"
    )


def _install_error_handler(app: FastAPI) -> None:
    """全局异常兜底（2026-10-09）：未捕获异常一律**记进异常日志 + 返回 JSON**。

    为什么必须做：三个子系统（渔枢 / 检测仓 / 渔货栈）挂的是**同一个 app**，
    而以前谁都没兜底 —— 某个接口一抛异常，前端拿到的是 Starlette 的默认 HTML 错误页，
    ``r.json()`` 直接报 ``Unexpected token '<'``，现场只剩一句「上传失败」，
    真正的堆栈只在容器日志里滚几屏就没了。现在：① 落库（堆栈 + 请求现场 + 账号）
    ② 该给 JSON 的给 JSON、该给页面的给人话页面。改这一处，三个子系统一起生效。
    """
    from .services import error_log as error_log_service

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        logger.exception("未捕获异常：%s %s", request.method, request.url.path)
        log_id = error_log_service.record_request(request, exc)
        if error_log_service.wants_json(request):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "服务器处理出错，已记入「异常日志」",
                    "log_id": log_id,
                },
                status_code=500,
            )
        return HTMLResponse(_error_page_html(log_id, _page_title_prefix(request.url.path)),
                            status_code=500)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):  # noqa: ANN202
        """400~599 的异常响应（2026-10-11 加）。

        以前这类响应直接由 Starlette 返回 ``JSONResponse``：``Content-Type`` 是
        ``application/json``（**没带 charset**），浏览器按本地编码解中文就成了乱码，
        而且浏览器导航（不是接口）时用户看到的就是一坨 JSON —— 现场学员账号被 403
        拦在货源广场上，屏幕上只有一串生僻字，报都报不清。现在：
        ① 接口（``wants_json``）仍旧 JSON，但补上 ``charset=utf-8``；
        ② 浏览器导航给人话页面（状态码 + 一句中文原因 + 回首页按钮）；
        ③ 3xx（登录跳转这类）原样透传 ``Location``，不做任何包装。
        """
        detail = str(getattr(exc, "detail", "") or "")
        headers = getattr(exc, "headers", None) or None
        status = int(getattr(exc, "status_code", 500) or 500)
        if 300 <= status < 400 or (headers and any(
            str(k).lower() == "location" for k in headers
        )):
            return Response(status_code=status, headers=headers)
        if error_log_service.wants_json(request):
            return JSONResponse(
                {"detail": detail},
                status_code=status,
                headers=headers,
                media_type="application/json; charset=utf-8",
            )
        return HTMLResponse(
            _http_error_page_html(status, detail, _page_title_prefix(request.url.path)),
            status_code=status, headers=headers
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="渔枢系统",
        description="飞书群消息 -> 订单管理 -> 供应链发货 -> 资金分析",
        version="0.1.0",
        lifespan=lifespan,
    )
    # 后台登录：账号存库，管理员默认 admin / xianyu2026
    app.add_middleware(AuthMiddleware)
    # 页面 HTML 不缓存（静态资源仍走 ?v= 版本号缓存）：升级后刷新即可看到新菜单/新样式
    app.add_middleware(HtmlNoCacheMiddleware)
    # gzip 压缩：页面 HTML 与样式 / 脚本压掉七成以上，跨公网访问（用量比较大的
    # 轻量机带宽有限）时首屏明显更快；太小的响应（如轮询接口）不压，省 CPU
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # 时间口径（2026-10-03）：页面按用户时区渲染，时区进 contextvar 供 human_time 等读取
    app.add_middleware(TimezoneMiddleware)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    _install_error_handler(app)

    settings.media_path.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(settings.media_path)), name="media")

    app.include_router(api_router)
    app.include_router(pages_router)
    # 检测仓（WMS）：**合并部署时把它挂进来而已** —— 它是独立业务系统，
    # 有自己的 Base / 菜单 / 角色权限 / 模板；平台既不给它菜单位置也不给权限，
    # 挂不上（子系统没放进来 / 依赖没装全）只告警，不影响渔枢系统。
    try:
        from wms.web import install as wms_install
        from wms.web.router import public_router as wms_public_router
        from wms.web.router import router as wms_router

        app.include_router(wms_router)
        # 云打印回调：不带登录态的公网入站（芯烨云平台推过来没有 Cookie）
        app.include_router(wms_public_router)
        wms_install(app)
    except Exception as exc:  # noqa: BLE001
        logger.warning("检测仓（WMS）路由挂载失败（渔枢系统不受影响）：%s", exc)

    # 渔货栈（自营选品平台）：与渔枢系统同一个进程、同一个端口，页面挂在 /self 下。
    # 挂不上（比如渔枢系统的依赖没装全）只告警，不能拖垮渔枢系统。
    if settings.hub_enabled:
        try:
            from .hub_bridge import mount_hub

            mount_hub(app)
        except Exception as exc:  # noqa: BLE001
            logger.exception("渔货栈挂载失败（渔枢系统不受影响）：%s", exc)

    @app.post("/feishu/event")
    async def feishu_event(request: Request):
        """飞书事件回调地址：http(s)://<host>/feishu/event"""
        payload = await request.json()
        try:
            # handle_event 是同步重活（解析 + 图片 OCR + 飞书推送）。在 async 路由里
            # 直接改走线程池：直接调会把 uvicorn 事件循环整条堵死 —— 2026-10-12 线上
            # 真出过：一条飞书事件让全站 8 分钟无响应（nginx 全线 499）。
            return await run_in_threadpool(handle_event, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理飞书事件失败")
            return {"ok": False, "error": str(exc)}

    @app.get("/feishu/event")
    def feishu_event_check():
        return {"ok": True, "hint": "飞书事件回调地址，请使用 POST"}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
