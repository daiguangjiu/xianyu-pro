"""FastAPI 依赖注入。

集中管理 DB Session、Redis Client 等"全应用共享"的依赖。
业务接口通过 Depends(deps._get_db) 获取 Session。
"""
from contextlib import contextmanager
from typing import Generator

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from src.config import get_settings


_settings = get_settings()

# ====== 数据库引擎（懒加载） ======
_engine = None
_SessionLocal: sessionmaker | None = None


def _get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = _settings.database_url
        # SQLite 不支持 max_overflow / pool_size，开发脱 PG 时跳过这些参数
        kwargs: dict = {"future": True}
        if not url.startswith("sqlite"):
            kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(
            bind=_engine, autocommit=False, autoflush=False, expire_on_commit=False
        )
        # 懒加载首次拿到引擎时自动建表：本项目不用 alembic，迁移就靠「自动建表 + 下面的兜底补列」
        try:
            from src.models import Base  # noqa: WPS433  延迟 import 避免循环

            Base.metadata.create_all(_engine)
            _ensure_columns(_engine, Base)
            _migrate_refund_sign(_engine)
            _migrate_listing_owner(_engine)
            # 时间口径（2026-10-03）：历史北京时间 → UTC。渔货栈与渔枢系统共用同一个库，
            # 迁移实现放在 app/db.py，这里直接复用（两端只跑一次，靠 schema_migrations 标记）
            try:
                from app.db import _migrate_utc_time as _hub_migrate_utc_time

                _hub_migrate_utc_time()
            except Exception as exc:  # noqa: BLE001  迁移失败不能拖垮启动
                import logging

                logging.getLogger(__name__).warning("时间口径迁移跳过：%s", exc)
            # 商品流水 ID（XYSK + 日期 + 序号）：同一个库，复用渔枢系统端那份迁移
            try:
                from app.db import (
                    _migrate_product_serial_code as _hub_migrate_serial_code,
                )

                _hub_migrate_serial_code()
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("商品流水 ID 迁移跳过：%s", exc)
            # 规格行的渔货价 / 市场价回填（价格跟着规格走）：同一个库，复用渔枢系统端那份迁移
            try:
                from app.db import _migrate_sku_prices as _hub_migrate_sku_prices

                _hub_migrate_sku_prices()
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning("SKU 价格回填跳过：%s", exc)
        except Exception as exc:  # pragma: no cover  仅防御
            import logging

            logging.getLogger(__name__).warning("Base.metadata.create_all failed: %s", exc)
    return _engine


# 新增字段的兜底迁移：模型里加了列但库里还没建，就在这里补上。
# 与渔枢系统 app/db.py 的 _ensure_columns() 同一套做法：只补列，不改已有列。
_COLUMN_DEFAULTS = {str: "''", int: "0", float: "0", bool: "0"}


def _ensure_columns(engine, Base) -> None:
    """为已经存在的表补上模型里新增的列（幂等：已存在就跳过）。"""
    import logging

    from sqlalchemy import inspect

    logger = logging.getLogger(__name__)
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
            # 自定义类型（如加密列 EncryptedText）没有 python_type，取不到就按「不给默认值」处理；
            # 不 try 住的话会抛 NotImplementedError，后面几张表的新列就全补不上了
            try:
                py_type = column.type.python_type
            except NotImplementedError:
                py_type = None
            default = _COLUMN_DEFAULTS.get(py_type) if py_type is not None else None
            if default is not None:
                ddl += f" DEFAULT {default}"
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(ddl)
                logger.info("已为表 %s 补上列 %s", table.name, column.name)
            except Exception as exc:  # noqa: BLE001  多进程抢着加同一列时「已存在」不算错
                if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower():
                    logger.warning("补列失败 %s.%s：%s", table.name, column.name, exc)


_REFUND_SIGN_MIGRATION = "2026-10-refund-sign"


def _migrate_refund_sign(engine) -> None:
    """一次性把历史的退款金额翻成负值（幂等）。

    符号口径（2026-10-03）：订单上「给出去的钱」存负值 —— downstream_orders.refund_amount
    正数一律取负。与渔枢系统 app/db.py 的同名迁移共用同一个标记，一套库只跑一次。
    """
    import logging

    from sqlalchemy import inspect, text

    logger = logging.getLogger(__name__)
    try:
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
        if "downstream_orders" not in tables:
            return
        columns = {c["name"] for c in inspector.get_columns("downstream_orders")}
        if "refund_amount" not in columns:
            return
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE downstream_orders SET refund_amount = -refund_amount "
                "WHERE refund_amount IS NOT NULL AND refund_amount > 0"
            )
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (:n, CURRENT_TIMESTAMP)"
                ),
                {"n": _REFUND_SIGN_MIGRATION},
            )
    except Exception as exc:  # noqa: BLE001  迁移失败不能拖垮页面
        logger.warning("退款金额符号迁移跳过：%s", exc)


_LISTING_OWNER_MIGRATION = "2026-10-listing-owner"


def _migrate_listing_owner(engine) -> None:
    """铺货记录按账号隔离（2026-10-13，用户拍板）。

    老表只有一条 (item_id, channel) 的记录、谁都看得见，于是「A 账号入池 → 所有账号
    在货源广场都显示已上架」。改造：
        1) owner 列（_ensure_columns 已补，这里再兜一次底）；
        2) 老行先一律刷成 ''（平台级，不算任何人的铺货）；
        3) 再按店铺商品池 shop_goods 的归属把记录还给对应账号 —— 谁真的入过池就归谁，
           没入过池的老记录保持平台级，任何账号都不会平白看到「已铺货」；
        4) Postgres 上把老的 uq_item_channel 换成 uq_item_channel_owner
           （SQLite 不支持 DROP CONSTRAINT，老库就先留着老约束）。
    """
    import logging

    from sqlalchemy import inspect, text

    logger = logging.getLogger(__name__)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP)"
            )
            done = conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE name = :n"),
                {"n": _LISTING_OWNER_MIGRATION},
            ).first()
            if done:
                return

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "channel_listings" not in tables:
            return
        if "owner" not in {c["name"] for c in inspector.get_columns("channel_listings")}:
            return

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE channel_listings SET owner = '' WHERE owner IS NULL"
            )
            # 谁把这件货放进过自己的店铺商品池，这条铺货记录就归谁
            if "shop_goods" in tables:
                pairs = conn.execute(
                    text(
                        "SELECT item_id, owner FROM shop_goods "
                        "WHERE owner IS NOT NULL AND owner <> ''"
                    )
                ).fetchall()
                for item_id, g_owner in pairs:
                    conn.execute(
                        text(
                            "UPDATE channel_listings SET owner = :o "
                            "WHERE item_id = :i AND (owner IS NULL OR owner = '')"
                        ),
                        {"o": str(g_owner), "i": str(item_id)},
                    )
            if engine.dialect.name == "postgresql":
                try:
                    conn.exec_driver_sql(
                        "ALTER TABLE channel_listings "
                        "DROP CONSTRAINT IF EXISTS uq_item_channel"
                    )
                    conn.exec_driver_sql(
                        "ALTER TABLE channel_listings ADD CONSTRAINT "
                        "uq_item_channel_owner UNIQUE (item_id, channel, owner)"
                    )
                except Exception as exc:  # noqa: BLE001  新库已带新约束
                    logger.info("铺货记录唯一约束已是新版：%s", exc)
            elif engine.dialect.name == "sqlite":
                # SQLite 不支持 DROP CONSTRAINT，但命名唯一约束就是同名索引
                try:
                    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_item_channel")
                    conn.exec_driver_sql(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_item_channel_owner "
                        "ON channel_listings (item_id, channel, owner)"
                    )
                except Exception as exc:  # noqa: BLE001  历史脏数据撞键就不强求
                    logger.info("SQLite 铺货记录约束跳过：%s", exc)
            conn.execute(
                text("INSERT INTO schema_migrations (name, applied_at) VALUES (:n, CURRENT_TIMESTAMP)"),
                {"n": _LISTING_OWNER_MIGRATION},
            )
            logger.info("铺货记录已按账号隔离（owner 列 + 历史归属回填）")
    except Exception as exc:  # noqa: BLE001  迁移失败不能拖垮页面
        logger.warning("铺货记录归属迁移跳过：%s", exc)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供 SQLAlchemy Session，请求结束自动关闭。"""
    _get_engine()
    assert _SessionLocal is not None
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """上下文管理器：用于 Celery 任务 / 脚本（无 request 上下文）。"""
    _get_engine()
    assert _SessionLocal is not None
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ====== Redis Client（懒加载） ======
_redis_client = None


def get_redis():
    """FastAPI 依赖：返回 redis.asyncio.Redis 实例。

    TODO: 接入 redis.asyncio，封装令牌桶 / 滑动窗口限流。
    """
    global _redis_client
    if _redis_client is None:
        # TODO: import redis.asyncio; _redis_client = redis.asyncio.from_url(...)
        raise NotImplementedError("Redis 客户端待接入（参考 selection/limiter.py）")
    return _redis_client


__all__ = ["get_db", "session_scope", "get_redis", "Depends"]