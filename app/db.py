"""数据库引擎与会话管理。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import BASE_DIR, settings


def _build_url(url: str) -> str:
    """把相对路径的 sqlite 地址解析为项目内的绝对路径。"""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        raw = url[len("sqlite:///"):]
        path = Path(raw)
        if not path.is_absolute():
            path = (BASE_DIR / raw).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    return url


class Base(DeclarativeBase):
    pass


def _create_engine():
    url = _build_url(settings.database_url)
    if url.startswith("sqlite"):
        return create_engine(url, future=True)
    # PostgreSQL：断连自愈 + 回收长期空闲连接（云数据库/负载均衡常见）
    return create_engine(url, future=True, pool_pre_ping=True, pool_recycle=280)


engine = _create_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _):
    if settings.database_url.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def dialect_name() -> str:
    """当前数据库方言名（``sqlite`` / ``postgresql``）。

    写 SQL 表达式时必须按它分支：``date(col, '+8 hours')`` 是 SQLite 独有写法，
    PostgreSQL 里两参数的 ``date()`` 根本不存在（报 UndefinedFunction → 页面 500）；
    反过来 ``to_char`` 在 SQLite 里也没有。
    """
    return engine.dialect.name


_COLUMN_DEFAULTS = {str: "''", int: "0", float: "0", bool: "0"}


def _ensure_columns() -> None:
    """轻量迁移：为已存在的表补上模型里新增的列，避免手工删库。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        current = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in current:
                continue
            ddl = (
                f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                f"{column.type.compile(engine.dialect)}"
            )
            fallback = _COLUMN_DEFAULTS.get(column.type.python_type)
            if column.default is not None and not column.default.is_callable:
                value = column.default.arg
                if isinstance(value, bool):
                    # 布尔值两边写法不一样：SQLite 存 0/1，Postgres 只认 false/true
                    value = ("1" if value else "0") if engine.dialect.name == "sqlite" else ("true" if value else "false")
                    ddl += f" DEFAULT {value}"
                else:
                    ddl += f" DEFAULT {value!r}" if isinstance(value, str) else f" DEFAULT {value}"
            elif fallback is not None:
                ddl += f" DEFAULT {fallback}"
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(ddl)
            except Exception as exc:  # noqa: BLE001
                # 多个进程同时启动时会抢着加同一列，报「已存在」就是别人刚加好了，跳过即可；
                # 其它错误（权限、磁盘）才需要暴露出来。
                if "duplicate column" not in str(exc).lower() and "already exists" not in str(
                    exc
                ).lower():
                    raise


# 旧版本把「系统时间」按 UTC 写入，展示出来比北京时间少 8 小时。
# 这里做一次性平移：把所有系统时间字段 +8 小时，之后统一按北京时间写读。
# 注意：飞书消息时间（raw_messages.msg_time / orders.msg_time / orders.ordered_at /
#      sync_states.last_message_time）本来就是按东八区换算的，不能动。
_BEIJING_SHIFT_COLUMNS: list[tuple[str, str]] = [
    ("raw_messages", "created_at"),
    ("raw_messages", "notified_at"),
    ("raw_messages", "missing_image_notified_at"),
    ("orders", "created_at"),
    ("orders", "updated_at"),
    ("orders", "pushed_at"),
    ("order_images", "created_at"),
    ("push_batches", "created_at"),
    ("push_batches", "pushed_at"),
    ("push_batch_items", "created_at"),
    ("suppliers", "created_at"),
    ("suppliers", "updated_at"),
    ("users", "created_at"),
    ("users", "updated_at"),
    ("users", "last_login_at"),
    ("operation_logs", "created_at"),
    ("sync_states", "updated_at"),
    ("sync_states", "last_synced_at"),
]

_BEIJING_MIGRATION = "2026-09-beijing-time"


def _migrate_beijing_time() -> None:
    """一次性把历史 UTC 时间字段平移成北京时间（幂等：迁移过的库不再重复执行）。"""
    from sqlalchemy import inspect, text

    dialect = engine.dialect.name
    with engine.begin() as conn:
        # 迁移标记表：记录已经执行过的时间口径迁移
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _BEIJING_MIGRATION},
        ).first()
        if done:
            return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    # 有些列（如 pushed_at）是本次版本才加的，按实际列名过滤，避免迁移报错
    columns: dict[str, set[str]] = {
        t: {c["name"] for c in inspector.get_columns(t)} for t in tables
    }
    with engine.begin() as conn:
        for table, column in _BEIJING_SHIFT_COLUMNS:
            if table not in tables or column not in columns.get(table, set()):
                continue
            if dialect == "sqlite":
                sql = (
                    f"UPDATE {table} SET {column} = "
                    f"strftime('%Y-%m-%d %H:%M:%f', datetime({column}, '+8 hours')) "
                    f"WHERE {column} IS NOT NULL"
                )
            else:
                sql = (
                    f"UPDATE {table} SET {column} = {column} + INTERVAL '8 hours' "
                    f"WHERE {column} IS NOT NULL"
                )
            conn.exec_driver_sql(sql)
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP)"
            ),
            {"n": _BEIJING_MIGRATION},
        )


_DEMO_DROP_MIGRATION = "2026-10-drop-demo-products"


def _drop_demo_products() -> None:
    """一次性清理「演示商品」：数据删掉，演示标记列一起去掉。

    演示商品是早期版本内置的 10 条示例（``products.is_demo = true``），现在不需要了：
    把挂单关系解开、商品与它的 SKU 删掉、播种标记 ``demo_products_v1`` 也清掉，
    最后把 ``products.is_demo`` 这一列删掉 —— 免得模型与库对不上。
    幂等：做过一次就记在 schema_migrations 里。
    """
    import logging

    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _DEMO_DROP_MIGRATION},
        ).first()
        if done:
            return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    columns = (
        {c["name"] for c in inspector.get_columns("products")} if "products" in tables else set()
    )
    with engine.begin() as conn:
        # 老库（模型里已没有 is_demo）启动时列可能已经不在了，按实际列名判断
        if "is_demo" in columns:
            # ``WHERE is_demo``：SQLite（0/1）与 PostgreSQL（true/false）都认
            if "orders" in tables:
                conn.exec_driver_sql(
                    "UPDATE orders SET product_id = NULL WHERE product_id IN "
                    "(SELECT id FROM products WHERE is_demo)"
                )
            if "product_skus" in tables:
                conn.exec_driver_sql(
                    "DELETE FROM product_skus WHERE product_id IN "
                    "(SELECT id FROM products WHERE is_demo)"
                )
            conn.exec_driver_sql("DELETE FROM products WHERE is_demo")
        if "system_flags" in tables:
            conn.exec_driver_sql("DELETE FROM system_flags WHERE name = 'demo_products_v1'")
        try:
            conn.exec_driver_sql("ALTER TABLE products DROP COLUMN is_demo")
        except Exception as exc:  # noqa: BLE001  列已经不在 / 老库不支持删列都不影响使用
            logging.getLogger("order-hub").debug("products.is_demo 删列跳过：%s", exc)
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP)"
            ),
            {"n": _DEMO_DROP_MIGRATION},
        )


_PRODUCT_ALIAS_DROP_MIGRATION = "2026-10-drop-product-aliases"


def _drop_product_aliases() -> None:
    """一次性删掉「别名」列（2026-10-11 用户拍板：这个字段没在用）。

    别名原本用于「按别名认订单」（services/product.py 的 match_product / link_orders）
    与上架时同步给渔货栈；功能整体下线后两端的列都不再使用，这里一并删掉 ——
    ``products.aliases``（渔枢系统商品）与 ``supplier_items.aliases``（渔货栈货源）。
    幂等：做过一次就记在 schema_migrations 里；列本来就不在（全新库）直接跳过。
    """
    import logging

    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _PRODUCT_ALIAS_DROP_MIGRATION},
        ).first()
        if done:
            return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in ("products", "supplier_items"):
            if table not in tables:
                continue
            if "aliases" not in {c["name"] for c in inspector.get_columns(table)}:
                continue
            try:
                conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN aliases")
            except Exception as exc:  # noqa: BLE001  老库不支持删列 / 被约束引用都不影响使用
                logging.getLogger("order-hub").debug("%s.aliases 删列跳过：%s", table, exc)
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP)"
            ),
            {"n": _PRODUCT_ALIAS_DROP_MIGRATION},
        )


_REFUND_SIGN_MIGRATION = "2026-10-refund-sign"


def _migrate_refund_sign() -> None:
    """一次性把历史的退款金额翻成负值（幂等：迁移过的库不再重复执行）。

    符号口径（2026-10-03）：订单上「给出去的钱」存负值 —— 退款给客户（refund_amount）
    由正转负，供应商退款（supplier_refund_amount）本来就是正值，不动。
    """
    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _REFUND_SIGN_MIGRATION},
        ).first()
        if done:
            return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    columns: dict[str, set[str]] = {
        t: {c["name"] for c in inspector.get_columns(t)} for t in tables
    }
    with engine.begin() as conn:
        # 两端共用一套库：渔枢系统的 orders 与渔货栈的 downstream_orders 一起迁
        for table in ("orders", "downstream_orders"):
            if table not in columns or "refund_amount" not in columns[table]:
                continue
            conn.exec_driver_sql(
                f"UPDATE {table} SET refund_amount = -refund_amount "
                f"WHERE refund_amount IS NOT NULL AND refund_amount > 0"
            )
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP)"
            ),
            {"n": _REFUND_SIGN_MIGRATION},
        )


_UTC_TIME_MIGRATION = "2026-10-utc-time"

# 渔枢系统的表：时间列全部由 app.timeutil.now() 写入，改口径前是**北京时间**，
# 迁移时整表 −8 小时（改完之后这些列存 UTC，展示按用户时区 +8 回来，用户无感）
_HUB_TIME_TABLES = (
    "raw_messages", "orders", "order_images", "push_batches", "push_batch_items",
    "suppliers", "shops", "settlements", "roles", "users", "order_logs",
    "parse_feedbacks", "parse_phrases", "operation_logs", "voice_events", "sync_states",
    "daily_summary_logs", "product_merges", "products", "product_categories",
    "shipping_rules", "return_addresses", "return_address_products", "product_skus",
    "system_flags", "parse_corrections",
)

# 渔货栈的列：只有这几列是 app.timeutil.now() 写的北京时间，其余时间列本来就是
# UTC（模型用 server_default now()，或代码里显式 datetime.now(timezone.utc)），别去动
_SELF_TIME_COLUMNS = {
    "channel_listings": ("handoff_at",),
    "stock_orders": ("created_at", "paid_at", "cancelled_at", "shipped_at", "completed_at"),
    "shop_goods": ("created_at", "materials_at"),
}


def _migrate_utc_time() -> None:
    """一次性把历史时间从北京时间改存 UTC（−8h），幂等。

    口径（2026-10-03 定稿）：库里一律存 UTC naive，页面按用户时区展示。
    · 渔枢系统的表时间列全是北京时间 → 整表 −8 小时；
    · 渔货栈只有少数列是北京时间（见 _SELF_TIME_COLUMNS），其余本来就是 UTC，不动；
    ·     迁移后页面显示的时间与迁移前完全一致（存 −8、渲染 +8 回来）。
    """
    import logging

    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _UTC_TIME_MIGRATION},
        ).first()
        if done:
            return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    # 目标列：表 -> [(列, 是不是 DateTime)]
    targets: list[tuple[str, str]] = []
    for table in _HUB_TIME_TABLES:
        if table not in tables:
            continue
        for col in inspector.get_columns(table):
            if "DATE" in str(col["type"]).upper() or "TIME" in str(col["type"]).upper():
                targets.append((table, col["name"]))
    for table, columns in _SELF_TIME_COLUMNS.items():
        if table not in tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table)}
        targets.extend((table, c) for c in columns if c in have)

    # 两种库两套写法：SQLite 用 strftime/datetime 函数，Postgres 直接减 interval
    # （线上是 Postgres，写成 SQLite 语法会因为 '%Y' 被当成占位符而启动失败）
    dialect = engine.dialect.name
    if dialect == "sqlite":
        expr_of = lambda c: (  # noqa: E731
            f"strftime('%Y-%m-%d %H:%M:%f', datetime({c}, '-8 hours'))"
        )
    else:
        expr_of = lambda c: f"({c} - INTERVAL '8 hours')"  # noqa: E731

    with engine.begin() as conn:
        for table, column in targets:
            try:
                conn.exec_driver_sql(
                    f"UPDATE {table} SET {column} = {expr_of(column)} "
                    f"WHERE {column} IS NOT NULL"
                )
            except Exception as exc:  # noqa: BLE001
                # 迁移只是把历史时间搬成 UTC，搬不动也不能拖垮启动（页面照旧按 UTC 渲染）
                logging.getLogger("order-hub").warning(
                    "UTC 时间迁移跳过 %s.%s：%s", table, column, exc
                )
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP)"
            ),
            {"n": _UTC_TIME_MIGRATION},
        )


_SERIAL_CODE_MIGRATION = "2026-10-product-serial-code"


def _migrate_product_serial_code() -> None:
    """一次性把商品编码换成流水 ID：``XYSK + 日期 + 4 位序号``，并按新编码重排 SKU 编码。

    口径（2026-10-03 定稿）：商品的流水 ID 就是商家编码，前缀固定 XYSK，
    序号**全局累计不按天重置**（第 15 个商品 = XYSK202610030015）；
    SKU 编码由商家编码派生（``XYSK202610030015-01``），不手填。

    老编码（``YHZ-JYDQ-…-0001``、``SPD0001``）在这里统一换成新规则：
    按建档时间排序依次编号，日期取该商品的建档日（北京时间）。
    幂等：做过一次就记在 schema_migrations 里；搬不动只记日志，不拖垮启动。
    """
    import logging
    from datetime import timezone

    from sqlalchemy import inspect, select, text

    log = logging.getLogger("order-hub")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _SERIAL_CODE_MIGRATION},
        ).first()
        if done:
            return

    try:
        from .models import Product, ProductSku
        from .services import product as product_service
        from .timeutil import beijing_now, to_beijing

        if "products" not in set(inspect(engine).get_table_names()):
            return
        with SessionLocal() as db:
            rows = list(db.scalars(select(Product).order_by(Product.created_at, Product.id)).all())
            serial = 0
            for code in db.scalars(select(Product.code)).all():
                serial = max(serial, product_service.serial_of(code))
            for row in rows:
                if product_service.is_serial_code(row.code):
                    continue  # 已经是新规则，别动
                serial += 1
                # 库里存的是 UTC naive（见 _migrate_utc_time），先认成 UTC 再折回北京时间取日期
                born = row.created_at
                if born is not None and born.tzinfo is None:
                    born = born.replace(tzinfo=timezone.utc)
                day = to_beijing(born) or beijing_now()
                row.code = (
                    f"{product_service.CODE_PREFIX}{day:%Y%m%d}"
                    f"{serial:0{product_service.CODE_SERIAL_WIDTH}d}"
                )
            db.commit()
            for row in rows:
                skus = list(
                    db.scalars(
                        select(ProductSku)
                        .where(ProductSku.product_id == row.id)
                        .order_by(ProductSku.id)
                    ).all()
                )
                for idx, sku in enumerate(skus, start=1):
                    want = product_service.sku_code_of(row, idx)
                    if (sku.sku_code or "").strip() != want:
                        sku.sku_code = want
            db.commit()
            log.info("商品流水 ID 迁移完成：%s 款商品", len(rows))
    except Exception as exc:  # noqa: BLE001  编码回填失败也不能拖垮启动
        logging.getLogger("order-hub").warning("商品流水 ID 迁移跳过：%s", exc)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            ),
            {"n": _SERIAL_CODE_MIGRATION},
        )


_SKU_PRICE_MIGRATION = "2026-10-product-sku-prices"


def _migrate_sku_prices() -> None:
    """一次性把商品级的价补到规格行上（新口径：价与库存跟着规格走）。

    2026-10-04 之前价格只存在商品上，规格行没有渔货价 / 市场价 ——
    详情页与渔货栈会一片「—」。这里按商品级的值往回填一次，**只填空着的**
    （运营单独填过的、后来改过的都不动）。幂等：做过就记进 schema_migrations。
    """
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("order-hub")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
        )
        done = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE name = :n"),
            {"n": _SKU_PRICE_MIGRATION},
        ).first()
        if done:
            return

    try:
        from sqlalchemy import select

        from .models import Product, ProductSku

        if "product_skus" not in set(inspect(engine).get_table_names()):
            return  # 老库还没有规格表，没什么可填的
        with SessionLocal() as db:
            products = {int(p.id): p for p in db.scalars(select(Product)).all()}
            touched = 0
            for sku in db.scalars(select(ProductSku)).all():
                product = products.get(int(sku.product_id or 0))
                if product is None:
                    continue
                cost = float(product.cost_amount or 0)
                if float(sku.cost_amount or 0) <= 0:
                    sku.cost_amount = cost
                # 渔货价：商品渔货价优先，没有就用商品成本价（保证渔货栈不是 0）
                if float(sku.fish_price or 0) <= 0:
                    sku.fish_price = float(product.fish_price or 0) or cost
                if float(sku.price or 0) <= 0:
                    sku.price = float(product.price or 0) or float(sku.fish_price or 0)
                if float(sku.market_price or 0) <= 0:
                    sku.market_price = float(product.original_price or 0)
                touched += 1
            db.commit()
            log.info("SKU 价格回填完成：%s 条规格", touched)
    except Exception as exc:  # noqa: BLE001  回填失败也不能拖垮启动
        logging.getLogger("order-hub").warning("SKU 价格回填跳过：%s", exc)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (:n, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            ),
            {"n": _SKU_PRICE_MIGRATION},
        )


def init_db() -> None:
    import logging

    from . import models  # noqa: F401
    # 渔货栈（页面挂在 /self 下，代码在仓库根 src/）的模型与渔枢系统共用同一个
    # declarative base —— 这里先导入一次，把那边的表注册进同一份 metadata，
    # 下面一次 create_all 就把两端的表都建好（一套库）。
    # 渔货栈依赖没装全时只跳过，不能拖垮渔枢系统。
    try:
        import src.models  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("order-hub").warning("渔货栈模型加载跳过：%s", exc)
    # 检测仓（WMS）的表**不在这里建**：它是独立子系统，有自己的 Base 与初始化
    # 流程（``wms.startup.startup()``），平台的 init_db 不该也不必感知它。
    Base.metadata.create_all(engine)
    _ensure_columns()
    _migrate_beijing_time()
    _migrate_refund_sign()
    _migrate_utc_time()
    _drop_demo_products()
    _drop_product_aliases()
    _migrate_product_serial_code()
    _migrate_sku_prices()
    _seed_product_tags()
    _seed_purchase_nodes()


def _seed_product_tags() -> None:
    """播种商品标签字典（源头工厂 / 现货速发 …），缺哪个补哪个；失败不影响启动。"""
    import logging

    try:
        from .services import tag as tag_service

        with SessionLocal() as session:
            created = tag_service.ensure_seeds(session)
        if created:
            logging.getLogger("order-hub").info("商品标签播种：新增 %s 个", created)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("order-hub").warning("商品标签播种跳过：%s", exc)


def _seed_purchase_nodes() -> None:
    """播种采购节点（中秋 / 国庆 / 双11 …），缺哪个补哪个；失败不影响启动。"""
    import logging

    try:
        from .services import purchase_node as node_service

        with SessionLocal() as session:
            created = node_service.ensure_seeds(session)
        if created:
            logging.getLogger("order-hub").info("采购节点播种：新增 %s 个", created)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("order-hub").warning("采购节点播种跳过：%s", exc)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖项。"""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
