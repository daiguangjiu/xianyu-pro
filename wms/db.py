"""检测仓自己的数据库引擎与会话。

**与其它端的关系只有「同一张物理库」这一条**：

  · 单独的 ``Base`` —— 检测仓的表在自己的 metadata 里，渔枢系统的 ``init_db`` 不会
    也不应该去建它们（反之亦然），谁的表谁自己初始化；
  · 默认地址取 ``DATABASE_URL``（合并部署 = 与平台同库，数据互通），设了
    ``WMS_DATABASE_URL`` 就是纯独立库；
  · 会话参数与平台一致（autoflush=False + expire_on_commit=False）：业务代码里
    「commit 之后还能读对象」的写法在两边通用。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import config

logger = logging.getLogger(__name__)


def _build_url(url: str) -> str:
    """相对路径的 sqlite 地址解析成项目内的绝对路径。"""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        raw = url[len("sqlite:///"):]
        path = Path(raw)
        if not path.is_absolute():
            path = (Path(__file__).resolve().parent.parent / raw).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    return url


class Base(DeclarativeBase):
    """检测仓所有表的基类（与 ``app.db.Base`` 是两套 metadata）。"""


def _create_engine():
    url = _build_url(config.database_url)
    if url.startswith("sqlite"):
        return create_engine(url, future=True)
    return create_engine(
        url, future=True, pool_pre_ping=True, pool_recycle=280,
        pool_size=10, max_overflow=20, pool_timeout=10,
    )


engine = _create_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _):
    if engine.dialect.name == "sqlite":
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def dialect_name() -> str:
    """方言名（``sqlite`` / ``postgresql``）：写裸 SQL 表达式必须按它分支。"""
    return engine.dialect.name


def _widen_category_columns() -> int:
    """把 wms_* 表里 category 列放长到 32（三级品类码 HA.KC.DFB 比旧的两位码长）。

    SQLite 不认长度，直接跳过；PostgreSQL 才有 VARCHAR(n) 限制，按方言 ALTER。
    改列宽失败只记日志，不能拖垮启动（同 2026-10-03 的迁移教训）。
    """
    from . import models  # noqa: F401  触发模型注册

    if engine.dialect.name != "postgresql":
        return 0
    tables = [
        t.name for t in Base.metadata.sorted_tables
        if any(c.name == "category" for c in t.columns)
    ]
    widened = 0
    with engine.begin() as conn:
        for name in tables:
            try:
                width = conn.exec_driver_sql(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name=%s AND column_name='category'",
                    (name,),
                ).scalar()
            except Exception:  # noqa: BLE001
                logger.debug("读 %s.category 列宽失败，跳过", name, exc_info=True)
                continue
            if width is not None and int(width) >= 32:
                continue
            try:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{name}" ALTER COLUMN category TYPE VARCHAR(32)'
                )
                widened += 1
            except Exception:  # noqa: BLE001
                logger.warning("放长 %s.category 失败（不影响启动）", name, exc_info=True)
    if widened:
        logger.info("品类列放长完成：%s 张表", widened)
    return widened


# 后加的列（2026-10-08 Excel 导入建档）：``create_all`` 只建新表、**不给老表补列**，
# 所以升级时得自己 ALTER。按方言各写一份（SQLite 的 JSON 是 TEXT、BOOLEAN 是 0/1，
# 且不支持 ``IF NOT EXISTS``），失败只记日志，不能拖垮启动。
_NEW_COLUMNS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # 表名: ((列名, sqlite 定义, postgres 定义), ...)
    "wms_items": (
        ("sku_code", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
        ("extra", "JSON DEFAULT '{}'", "JSON DEFAULT '{}'::json"),
        ("import_id", "INTEGER", "INTEGER"),
        # 2026-10-08 第二批：按供应商清单口径加的「产品名称」+ 检修后自己贴的条码
        ("product_name", "VARCHAR(255) DEFAULT ''", "VARCHAR(255) DEFAULT ''"),
        ("own_barcode", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
        ("own_barcode_at", "DATETIME", "TIMESTAMP"),
        # 2026-10-10：数量 —— 一台货品可以带 N 台（同一个条码，不再按数量拆成 N 个码）
        ("qty", "INTEGER DEFAULT 1", "INTEGER DEFAULT 1"),
        # 2026-10-12：建议售价 —— 手工建档 / 编辑时人工填的建议零售价（照渔枢的价格口径）。
        # 留空（0）就按「市场价 × 建议售价系数」算；渔枢按条码带出资料时优先用它。
        ("suggest_price", "FLOAT DEFAULT 0", "FLOAT DEFAULT 0"),
        # 2026-10-14：渔货号 —— 商品款编码（YS + 日期 + 当天流水），一个渔货号挂多个条码商品
        ("fish_code", "VARCHAR(32) DEFAULT ''", "VARCHAR(32) DEFAULT ''"),
    ),
    # 注意：这张表以前在同一个 dict 里写了两次（后一次把前一次覆盖，mixed 补不上），
    # 现在合并成一个键，两列都会补。
    "wms_batches": (
        ("mixed", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        # 批次可禁用 / 可删除（老库建表时可能还没这列）
        ("enabled", "BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE"),
    ),
    # 2026-10-08 第三批：商品字段记「来源」（哪张表带来的）
    "wms_item_fields": (
        ("source", "VARCHAR(255) DEFAULT ''", "VARCHAR(255) DEFAULT ''"),
    ),
    # 导入失败也要留痕：毛病清单存这里（老库建表时可能还没这列）
    "wms_import_logs": (
        ("errors", "JSON DEFAULT '[]'", "JSON DEFAULT '[]'::json"),
    ),
    # 建议售价系数（2026-10-10）：渔枢 / 渔货栈的建议售价以它为基准
    "wms_price_rules": (
        ("suggest_factor", "FLOAT DEFAULT 1.0", "FLOAT DEFAULT 1.0"),
    ),
    # 仓库与货架（2026-10-08 起页面可维护）：老库的 wms_warehouses 可能缺后加的列
    "wms_warehouses": (
        ("full_path", "VARCHAR(255) DEFAULT ''", "VARCHAR(255) DEFAULT ''"),
        ("grade_zone", "VARCHAR(4) DEFAULT ''", "VARCHAR(4) DEFAULT ''"),
        ("can_out", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        ("enabled", "BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE"),
        ("sort", "INTEGER DEFAULT 100", "INTEGER DEFAULT 100"),
        ("note", "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
        ("updated_at", "DATETIME", "TIMESTAMP"),
    ),
    # 云打印回执（2026-10-08 芯烨条码云）：老库的 wms_label_prints 缺这四个列
    "wms_label_prints": (
        ("order_no", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
        ("printer_sn", "VARCHAR(64) DEFAULT ''", "VARCHAR(64) DEFAULT ''"),
        ("error", "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
        ("sent_at", "DATETIME", "TIMESTAMP"),
        # 份数（2026-10-10）：跟货品数量走，一台货 3 台就打 3 张一样的标签
        ("copies", "INTEGER DEFAULT 1", "INTEGER DEFAULT 1"),
    ),
}


def _ensure_new_columns() -> int:
    """给已经存在的表补后加的列（幂等）。"""
    from sqlalchemy import inspect

    dialect = engine.dialect.name
    added = 0
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            if table not in tables:
                continue  # 表还不存在 → create_all 会按完整模型建，不用补
            have = {c["name"] for c in insp.get_columns(table)}
            for name, sqlite_def, pg_def in cols:
                if name in have:
                    continue
                spec = sqlite_def if dialect == "sqlite" else pg_def
                # 注意 IF NOT EXISTS 的位置：Postgres 是 ADD COLUMN IF NOT EXISTS <列名>，
                # 写成 ADD COLUMN <列名> IF NOT EXISTS 会直接语法错误（SQLite 压根不支持）
                if dialect == "sqlite":
                    sql = "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, spec)
                else:
                    sql = "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s" % (table, name, spec)
                try:
                    conn.exec_driver_sql(sql)
                    added += 1
                except Exception:  # noqa: BLE001
                    logger.warning("补列 %s.%s 失败（不影响启动）", table, name, exc_info=True)
    if added:
        logger.info("检测仓补列完成：%s 列", added)
    return added


def _ensure_suggest_factor() -> int:
    """老库的「建议售价系数」补列后是 1.0 —— 该档渔货价系数比它高的话，出价会被压低

    （例：N 档渔货价 ×1.05、建议售价 ×1.0 → 全新机的渔货价被压成基准价，等于凭空降价）。
    这里把「比渔货价系数还低」的行抬到 max(渔货价系数, 1.0)，保证升级不会莫名其妙降价。
    用户自己配过的值（≥ 系数）一个字不动。
    """
    try:
        with engine.begin() as conn:
            cur = conn.exec_driver_sql(
                "UPDATE wms_price_rules SET suggest_factor = "
                "CASE WHEN factor > 1.0 THEN factor ELSE 1.0 END "
                "WHERE suggest_factor IS NULL OR suggest_factor <= 0 OR suggest_factor < factor"
            )
            return int(cur.rowcount or 0)
    except Exception:  # noqa: BLE001
        logger.warning("补齐建议售价系数失败（不影响启动）", exc_info=True)
        return 0


def init_tables() -> None:
    """建检测仓自己的表（幂等）。必须由检测仓的启动流程调用。"""
    from . import models  # noqa: F401  触发模型注册（业务表 + 权限表）

    Base.metadata.create_all(engine)
    _ensure_new_columns()
    _ensure_suggest_factor()
    _widen_category_columns()


@contextmanager
def session() -> Iterator[Session]:
    """一次性会话（脚本 / 定时任务用）：退出即关，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：一次请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
