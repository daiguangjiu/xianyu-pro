"""命令行工具：初始化、同步、解析测试、启动服务。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import settings
from .db import init_db, session_scope
from .parser.engine import parse_text
from .services import stats as stats_service
from .timeutil import fmt
from .services.ingest import sync_chat


def cmd_init(_args) -> int:
    init_db()
    print(f"数据库已初始化: {settings.database_url}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_sync(args) -> int:
    init_db()
    ids = args.chat_id or settings.feishu_chat_ids
    if not ids:
        print("未指定群：请在 .env 配置 FEISHU_CHAT_IDS 或使用 --chat-id", file=sys.stderr)
        return 1
    start_ms = int((time.time() - args.days * 86400) * 1000)
    for chat_id in ids:
        st = sync_chat(chat_id, start_ms=start_ms)
        print(json.dumps(st.as_dict(), ensure_ascii=False))
    return 0


def cmd_parse(args) -> int:
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()
    result = parse_text(text, platform_key=args.platform or None)

    def _fmt(v):
        return fmt(v) if hasattr(v, "strftime") else v

    print(json.dumps(
        {
            "platform": result.platform,
            "confidence": result.confidence,
            "needs_review": result.needs_review,
            "missing_required": result.missing_required,
            "fields": {k: _fmt(v) for k, v in result.fields.items()},
            "sources": result.sources,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_stats(_args) -> int:
    init_db()
    with session_scope() as session:
        print(json.dumps(stats_service.summary(session), ensure_ascii=False, indent=2))
        print("供应商维度:")
        for row in stats_service.by_supplier(session):
            print(f"  {row['supplier']}: {row['count']} 单 / 销售 {row['sale']} / 毛利 {row['profit']}")
    return 0


def cmd_backup(args) -> int:
    """手动跑一次订单数据备份（等价后台「立即备份」）。"""
    from .services import backup as backup_service

    meta = backup_service.create_backup(label="cli")
    out = backup_service.prune_backups(args.keep_days)
    print(
        json.dumps(
            {
                "file": meta["file"],
                "path": str(backup_service.backup_dir() / meta["file"]),
                "orders": meta["order_count"],
                "size": meta["size"],
                "pruned": out["count"],
                "keep_days": out["keep_days"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_fill_names(args) -> int:
    """按 open_id 反查飞书员工真实姓名，补全历史消息与订单。"""
    from .services.ingest import backfill_sender_names

    with session_scope() as session:
        filled = backfill_sender_names(session, limit=args.limit)
    print(json.dumps({"filled": filled, "limit": args.limit}, ensure_ascii=False))
    return 0


def cmd_reparse(args) -> int:
    """解析规则升级后，把历史订单里「被换行截断」的字段补回来。"""
    from .services.ingest import reparse_truncated

    with session_scope() as session:
        out = reparse_truncated(session, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_reparse_ocr(args) -> int:
    """按订单截图 OCR 补齐历史订单里「只有截图才有」的字段（成交价 / 商品总价 / 运费）。"""
    from .services.ingest import reparse_ocr_fields

    with session_scope() as session:
        out = reparse_ocr_fields(session, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_crop_images(args) -> int:
    """按订单截图补裁 / 重裁商品图（历史订单补齐、算法升级后批量重跑）。"""
    from .services.ingest import backfill_product_images

    with session_scope() as session:
        out = backfill_product_images(session, limit=args.limit, force=args.force)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_import_shops(args) -> int:
    """把「闲鱼自营账号台账」（xlsx）导入成店铺档案（默认只预览，--apply 才写库）。"""
    from .services import shop as shop_service

    with session_scope() as session:
        rows, sheet = shop_service.read_ledger(args.file, sheet=args.sheet)
        if not rows:
            print(f"没读到可导入的账号（工作表：{sheet or '第一个'}）", file=sys.stderr)
            return 1
        out = shop_service.import_ledger(session, rows, apply=args.apply)
    out["sheet"] = sheet
    for item in out["items"]:
        mark = {
            "create": "新建",
            "update": "补全",
            "skip": "跳过",
            "error": "失败",
        }.get(item["action"], item["action"])
        extra = item.get("code") or item.get("reason") or ""
        print(f"{mark}\t{item['name']}\t{extra}")
    print(
        json.dumps(
            {
                "sheet": sheet,
                "total": out["total"],
                "created": out["created"],
                "updated": out["updated"],
                "skipped": out["skipped"],
                "applied": args.apply,
            },
            ensure_ascii=False,
        )
    )
    if not args.apply:
        print("（预览模式：加 --apply 才真正写库）")
    return 0


def cmd_sync_shops(_args) -> int:
    """按历史订单补齐店铺档案：没建档的自动建档，已有的补回款归属人 / 运营人等信息。"""
    from .services import shop as shop_service

    with session_scope() as session:
        out = shop_service.backfill_from_orders(session)
    for line in out["names"][:50]:
        print(line)
    print(
        json.dumps(
            {
                "shops_before": out["shops_before"],
                "created": out["created"],
                "enriched": out["enriched"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_sync_suppliers(_args) -> int:
    """按订单里已有的供应商写法补齐档案 / 统一写法（历史数据对齐，可反复执行）。"""
    from .services import supplier as supplier_service

    with session_scope() as session:
        out = supplier_service.backfill_from_orders(session)
    for line in out["names"][:50]:
        print(line)
    print(
        json.dumps(
            {
                "suppliers_before": out["suppliers_before"],
                "created": out["created"],
                "orders_relabeled": out["changed"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_import_orders(args) -> int:
    """把运营手上的历史登记表（xlsx）按系统结构导入成订单（默认只预览，--apply 才写库）。"""
    from .services import importer

    with session_scope() as session:
        rows, sheet = importer.read_sheet(args.file, sheet=args.sheet)
        if not rows:
            print(f"没读到可导入的记录（工作表：{sheet or '第一个'})", file=sys.stderr)
            return 1
        out = importer.import_rows(session, rows, actor=args.operator, apply=args.apply)
    out["sheet"] = sheet
    for item in out["items"]:
        mark = {"create": "新建", "skip": "跳过", "error": "失败"}.get(item["action"], item["action"])
        extra = item.get("order_no") or item.get("reason") or ""
        print(f"{mark}\t{item['day']}\t{item['product'][:24]}\t{extra}")
    print(
        json.dumps(
            {
                "sheet": sheet,
                "total": out["total"],
                "created": out["created"],
                "skipped": out["skipped"],
                "failed": out["failed"],
                "applied": out["applied"],
            },
            ensure_ascii=False,
        )
    )
    if not args.apply:
        print("（预览模式：加 --apply 才真正写库）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="order-hub", description="渔枢系统 · 小渔数科内部商品运营管理平台")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sync", help="同步飞书群消息")
    s.add_argument("--chat-id", action="append", default=[])
    s.add_argument("--days", type=int, default=1)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("parse", help="测试解析一段文本")
    s.add_argument("--text", default="")
    s.add_argument("--file", default="")
    s.add_argument("--platform", default="")
    s.set_defaults(func=cmd_parse)

    sub.add_parser("stats", help="输出统计概览").set_defaults(func=cmd_stats)

    s = sub.add_parser("fill-names", help="补全飞书发送人真实姓名")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(func=cmd_fill_names)

    s = sub.add_parser(
        "reparse",
        help="解析规则升级后，把历史订单里被换行截断的字段补回来（只补被截短的，不动人工改过的）",
    )
    s.add_argument("--limit", type=int, default=2000, help="最多检查多少单")
    s.set_defaults(func=cmd_reparse)

    s = sub.add_parser(
        "reparse-ocr",
        help="按订单截图 OCR 补齐历史订单里只有截图才有的字段（成交价 / 商品总价 / 运费），并清掉噪声昵称",
    )
    s.add_argument("--limit", type=int, default=2000, help="最多检查多少单")
    s.set_defaults(func=cmd_reparse_ocr)

    s = sub.add_parser("crop-images", help="按订单截图补裁 / 重裁商品图")
    s.add_argument("--limit", type=int, default=0, help="最多处理多少单，0=全部")
    s.add_argument("--force", action="store_true", help="已有商品图的订单也重裁覆盖")
    s.set_defaults(func=cmd_crop_images)

    s = sub.add_parser(
        "import-shops",
        help="把「闲鱼自营账号台账」（xlsx）导入成店铺档案（默认只预览，--apply 才写库）",
    )
    s.add_argument("file", help="xlsx 文件路径")
    s.add_argument("--sheet", default="", help="工作表名，默认第一个")
    s.add_argument("--apply", action="store_true", help="真正写库（不加则只预览）")
    s.set_defaults(func=cmd_import_shops)

    sub.add_parser(
        "sync-shops",
        help="按历史订单补齐店铺档案：没建档的自动建档，已有的补运营人 / 回款归属人等",
    ).set_defaults(func=cmd_sync_shops)

    sub.add_parser(
        "sync-suppliers",
        help="按订单里的供应商写法补齐供应商档案并统一写法（老数据对齐，可反复执行）",
    ).set_defaults(func=cmd_sync_suppliers)

    s = sub.add_parser(
        "import-orders",
        help="把历史登记表（xlsx：供应商/产品/成本价/出货价/出单账号/归属人/付款金额）导入成订单",
    )
    s.add_argument("file", help="xlsx 文件路径")
    s.add_argument("--sheet", default="", help="工作表名，默认第一个")
    s.add_argument("--operator", default="", help="操作人（写进订单处理日志）")
    s.add_argument("--apply", action="store_true", help="真正写库（不加则只预览）")
    s.set_defaults(func=cmd_import_orders)

    s = sub.add_parser("backup", help="备份订单数据并清理过期备份")
    s.add_argument("--keep-days", type=int, default=None, help="覆盖 .env 里的 BACKUP_KEEP_DAYS")
    s.set_defaults(func=cmd_backup)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
