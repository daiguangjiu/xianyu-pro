"""采购节点：货源广场顶部「采购节点」运营位那份活动日历的配置。

节点 = 运营日历（日期窗口 + 图标 + 关键词），商品**默认**不写死绑定 —— 靠关键词在货源
标题里实时匹配（空关键词 = 通吃全品类）。

2026-10-11 起运营还可以**手动配置**节点下的商品（PurchaseNodeItem）：手动配置的排在
关键词匹配结果的前面（当作置顶），关键词那套系统规则一点不变。

页面在「货源 · 采购节点」（app/templates/purchase_nodes.html），渔货栈货源广场读取时
走 src/goods/catalog.py::purchase_node_windows()（同库读这张表，改完最多 60 秒生效）。
"""
from __future__ import annotations

import logging
from datetime import date as _date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PurchaseNode, PurchaseNodeItem

logger = logging.getLogger("order-hub.purchase-node")

# 货源（渔货栈 supplier_items）与渔枢系统同一个库，这里只读不写：
# 运营在本页挑的是「哪几款货进这个活动」，货源本身的资料仍在渔货栈维护。
# 用裸 SQL 而不是 ORM 映射：那张表归渔货栈管，渔枢侧不该把它当自己的模型 import。
_ITEM_SQL = (
    "SELECT item_id, COALESCE(title, '') AS title, COALESCE(item_code, '') AS item_code "
    "FROM supplier_items WHERE is_listed "
    "ORDER BY COALESCE(last_synced_at, created_at) DESC"
)
_ITEM_ONE_SQL = (
    "SELECT item_id, COALESCE(title, '') AS title FROM supplier_items WHERE item_id = :item_id"
)
# 下拉里最多给多少款（多了页面扛不住；其余靠手填编号，照样能配）
ITEM_CANDIDATE_LIMIT = 300

# 种子节点：与 src/goods/catalog.py 的 PURCHASE_NODES 同一份（那边是读不到表时的兜底）
NODE_SEEDS: tuple[dict, ...] = (
    {"node_key": "mid_autumn", "name": "中秋节", "start_md": "09-19", "end_md": "09-25",
     "icon": "bi-moon-stars", "keywords": "礼盒\n礼\n茶\n月饼\n坚果\n特产\n果", "sort_order": 10},
    {"node_key": "national_day", "name": "国庆黄金周", "start_md": "09-28", "end_md": "10-07",
     "icon": "bi-suitcase-lg", "keywords": "便携\n户外\n旅行\n帐篷\n背包\n保温杯\n防晒", "sort_order": 20},
    {"node_key": "winter_new", "name": "冬装上新", "start_md": "10-08", "end_md": "10-23",
     "icon": "bi-snow2", "keywords": "冬季\n加厚\n保暖\n珊瑚绒\n羽绒\n睡衣\n毛衣\n绒", "sort_order": 30},
    {"node_key": "double11", "name": "双11大促", "start_md": "11-01", "end_md": "11-11",
     "icon": "bi-lightning-charge-fill", "keywords": "", "sort_order": 40},
    {"node_key": "new_year", "name": "年货节", "start_md": "12-25", "end_md": "01-20",
     "icon": "bi-gift-fill", "keywords": "年货\n坚果\n糖果\n礼盒\n对联\n红包", "sort_order": 50},
)

# 页面上的图标候选（Bootstrap Icons），运营也可以自己填别的
ICON_CHOICES: tuple[tuple[str, str], ...] = (
    ("bi-calendar-event", "通用日历"),
    ("bi-moon-stars", "中秋 / 夜晚"),
    ("bi-suitcase-lg", "出行 / 旅行"),
    ("bi-snow2", "冬季"),
    ("bi-lightning-charge-fill", "大促"),
    ("bi-gift-fill", "送礼 / 年货"),
    ("bi-sun", "夏季"),
    ("bi-umbrella", "雨季"),
    ("bi-heart-fill", "情人节"),
    ("bi-cup-hot", "餐饮 / 茶饮"),
    ("bi-house-heart", "家装 / 家居"),
    ("bi-mortarboard", "开学季"),
)


# ------------------------------------------------------------------ 工具
def split_keywords(text: str | None) -> list[str]:
    """关键词文本 → 列表（一行一个，也认逗号 / 顿号；去重保序）。"""
    raw = str(text or "").replace("，", "\n").replace(",", "\n").replace("、", "\n")
    out: list[str] = []
    for line in raw.splitlines():
        word = line.strip()
        if word and word not in out:
            out.append(word)
    return out


def join_keywords(items) -> str:
    return "\n".join(split_keywords("\n".join(str(x or "") for x in (items or []))))


def normalize_md(value: str | None) -> str:
    """把 9-19 / 09-19 / 0919 之类统一成 MM-DD；不合法的返回空串（由调用方拦）。"""
    raw = str(value or "").strip().replace("/", "-").replace(".", "-")
    if not raw:
        return ""
    parts = raw.split("-")
    if len(parts) == 1 and len(raw) == 4 and raw.isdigit():
        parts = [raw[:2], raw[2:]]
    if len(parts) != 2:
        return ""
    try:
        month, day = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{month:02d}-{day:02d}"


def md_to_date(md: str, year: int) -> _date | None:
    try:
        month, day = (int(x) for x in str(md or "").split("-"))
        return _date(year, month, day)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 读
def list_nodes(session: Session, *, only_enabled: bool = False) -> list[PurchaseNode]:
    stmt = select(PurchaseNode)
    if only_enabled:
        stmt = stmt.where(PurchaseNode.enabled.is_(True))
    return list(
        session.scalars(stmt.order_by(PurchaseNode.sort_order.asc(), PurchaseNode.id.asc()))
    )


def get_node(session: Session, node_id: int) -> PurchaseNode | None:
    return session.get(PurchaseNode, node_id)


def _key_taken(session: Session, key: str, exclude_id: int | None = None) -> bool:
    row = session.scalar(select(PurchaseNode).where(PurchaseNode.node_key == key))
    return bool(row and row.id != exclude_id)


def _slug(text: str) -> str:
    """把中文名转成稳定的英文 key；转不出来就回落 n + 时间戳后缀（保证唯一）。"""
    table = {
        "中秋": "mid_autumn", "国庆": "national_day", "双11": "double11", "双十一": "double11",
        "年货": "new_year", "春节": "spring_festival", "元旦": "new_year_day",
        "情人节": "valentine", "618": "618", "开学": "back_to_school",
    }
    for word, key in table.items():
        if word in (text or ""):
            return key
    import time as _time

    return f"node{int(_time.time())}"


# ------------------------------------------------------------------ 写
def ensure_seeds(session: Session) -> int:
    """播种种子节点（按 node_key 补，缺哪个补哪个）。幂等，启动时调用。"""
    existing = {n.node_key for n in list_nodes(session)}
    created = 0
    for seed in NODE_SEEDS:
        if seed["node_key"] in existing:
            continue
        session.add(
            PurchaseNode(
                node_key=seed["node_key"],
                name=seed["name"],
                icon=seed.get("icon") or "bi-calendar-event",
                start_md=seed["start_md"],
                end_md=seed["end_md"],
                keywords=seed.get("keywords") or "",
                note=seed.get("note") or "",
                sort_order=int(seed.get("sort_order") or 100),
                enabled=True,
                seeded=True,
            )
        )
        created += 1
    if created:
        session.commit()
    return created


def create_node(session: Session, form: dict, *, commit: bool = True) -> tuple[PurchaseNode | None, str]:
    """按表单新增节点；返回 (节点, 错误文案)。"""
    name = str(form.get("name") or "").strip()
    if not name:
        return None, "节点名不能为空"
    start_md = normalize_md(form.get("start_md"))
    end_md = normalize_md(form.get("end_md"))
    if not start_md or not end_md:
        return None, "日期窗口要填成 MM-DD（如 11-01）"
    key = str(form.get("node_key") or "").strip() or _slug(name)
    key = "".join(ch for ch in key if ch.isalnum() or ch in ("_", "-"))[:32] or _slug(name)
    if _key_taken(session, key):
        return None, f"标识「{key}」已被占用，换一个（它出现在活动链接里，建好别改）"
    node = PurchaseNode(
        node_key=key,
        name=name,
        icon=str(form.get("icon") or "bi-calendar-event").strip(),
        start_md=start_md,
        end_md=end_md,
        keywords=join_keywords([str(form.get("keywords") or "")]),
        note=str(form.get("note") or "").strip()[:255],
        sort_order=_to_int(form.get("sort_order")) or 100,
        enabled=str(form.get("enabled", "1")) not in ("0", "false", "False", ""),
        seeded=False,
    )
    session.add(node)
    session.flush()
    if commit:
        session.commit()
    return node, ""


def update_node(session: Session, node: PurchaseNode, form: dict, *, commit: bool = True):
    """按表单更新节点；返回 (节点, 错误文案)。标识（node_key）允许改，但要查重。"""
    if "name" in form:
        name = str(form.get("name") or "").strip()
        if not name:
            return None, "节点名不能为空"
        node.name = name
    if "node_key" in form:
        key = str(form.get("node_key") or "").strip() or node.node_key
        key = "".join(ch for ch in key if ch.isalnum() or ch in ("_", "-"))[:32]
        if not key:
            return None, "标识不能为空"
        if _key_taken(session, key, exclude_id=node.id):
            return None, f"标识「{key}」已被别的节点占用"
        node.node_key = key
    if "icon" in form:
        node.icon = str(form.get("icon") or "bi-calendar-event").strip()
    if "start_md" in form:
        md = normalize_md(form.get("start_md"))
        if not md:
            return None, "开始日期要填成 MM-DD（如 11-01）"
        node.start_md = md
    if "end_md" in form:
        md = normalize_md(form.get("end_md"))
        if not md:
            return None, "结束日期要填成 MM-DD（如 11-11）"
        node.end_md = md
    if "keywords" in form:
        node.keywords = join_keywords([str(form.get("keywords") or "")])
    if "note" in form:
        node.note = str(form.get("note") or "").strip()[:255]
    if "sort_order" in form:
        node.sort_order = _to_int(form.get("sort_order")) or 100
    if "enabled" in form:
        node.enabled = str(form.get("enabled")) not in ("0", "false", "False", "")
    if commit:
        session.commit()
    return node, ""


def toggle_node(session: Session, node: PurchaseNode, *, commit: bool = True) -> PurchaseNode:
    node.enabled = not bool(node.enabled)
    if commit:
        session.commit()
    return node


def delete_node(session: Session, node: PurchaseNode, *, commit: bool = True) -> None:
    # 连这个节点手动配的商品一起删，别留孤儿行（节点没了，配置也没意义）
    session.query(PurchaseNodeItem).filter(PurchaseNodeItem.node_id == node.id).delete()
    session.delete(node)
    if commit:
        session.commit()


# ------------------------------------------------- 节点下的商品（手动配置）
def _sql_rows(session: Session, sql: str, **params) -> list[dict]:
    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    try:
        rows = (
            session.execute(text(sql), params)
            if params
            else session.execute(text(sql))
        )
        return [dict(row._mapping) for row in rows]  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001  表还没建 / 独立部署读不到，都不该把页面打挂
        logger.warning("读取渔货栈货源（supplier_items）失败：%s", exc)
        return []


def item_candidates(
    session: Session, keyword: str = "", limit: int = ITEM_CANDIDATE_LIMIT
) -> list[dict]:
    """可配的商品：渔货栈在架货源（编号 / 标题 / 商家编码），给页面下拉与搜索用。"""
    sql = _ITEM_SQL
    params: dict = {}
    word = str(keyword or "").strip().lower()
    if word:
        sql += (
            " AND (LOWER(COALESCE(title, '')) LIKE :kw "
            "OR LOWER(COALESCE(item_code, '')) LIKE :kw "
            "OR LOWER(COALESCE(item_id, '')) LIKE :kw)"
        )
        params["kw"] = f"%{word}%"
    rows = _sql_rows(session, sql, **params)
    return [
        {
            "item_id": str(r.get("item_id") or ""),
            "title": str(r.get("title") or ""),
            "item_code": str(r.get("item_code") or ""),
        }
        for r in rows
        if str(r.get("item_id") or "")
    ][:limit]


def item_title(session: Session, item_id: str) -> str:
    """货源标题（配商品时留一份快照；找不到返回空串）。"""
    rows = _sql_rows(session, _ITEM_ONE_SQL, item_id=str(item_id or "").strip())
    return str(rows[0].get("title") or "") if rows else ""


def node_items(session: Session, node_id: int) -> list[PurchaseNodeItem]:
    """某节点手动配置的商品（按排序号，小的在前）。"""
    return list(
        session.scalars(
            select(PurchaseNodeItem)
            .where(PurchaseNodeItem.node_id == node_id)
            .order_by(PurchaseNodeItem.sort_order.asc(), PurchaseNodeItem.id.asc())
        )
    )


def items_by_node(session: Session) -> dict[int, list[PurchaseNodeItem]]:
    """一次取全部节点的配置（页面按节点分块展示，避免逐节点查库）。"""
    out: dict[int, list[PurchaseNodeItem]] = {}
    for row in session.scalars(
        select(PurchaseNodeItem).order_by(
            PurchaseNodeItem.node_id.asc(),
            PurchaseNodeItem.sort_order.asc(),
            PurchaseNodeItem.id.asc(),
        )
    ):
        out.setdefault(int(row.node_id or 0), []).append(row)
    return out


def get_item(session: Session, row_id: int) -> PurchaseNodeItem | None:
    return session.get(PurchaseNodeItem, row_id)


def add_item(
    session: Session,
    node: PurchaseNode,
    item_id: str,
    *,
    note: str = "",
    commit: bool = True,
) -> tuple[PurchaseNodeItem | None, str]:
    """给节点手动配一款货；返回 (配置行, 错误文案)。

    同一款货在一个节点里只能配一次（配过了就提示，不重复加）；编号在渔货栈里
    找不到就直接拦下 —— 免得配上一串手滑打错的编号，页面上永远显示不出来。
    """
    key = str(item_id or "").strip()
    if not key:
        return None, "要先选一款货（或填渔货栈的商品编号）"
    exists = session.scalar(
        select(PurchaseNodeItem).where(
            PurchaseNodeItem.node_id == node.id, PurchaseNodeItem.item_id == key
        )
    )
    if exists:
        return None, f"「{exists.title or key}」已经在这个节点里了"
    title = item_title(session, key)
    if not title:
        return None, f"渔货栈里没有编号为「{key}」的货源，先到渔货栈上架再来配"
    last = node_items(session, node.id)
    row = PurchaseNodeItem(
        node_id=node.id,
        item_id=key,
        title=title[:255],
        sort_order=(last[-1].sort_order + 10) if last else 10,
        enabled=True,
        note=str(note or "").strip()[:255],
    )
    session.add(row)
    if commit:
        session.commit()
    return row, ""


def remove_item(session: Session, row: PurchaseNodeItem, *, commit: bool = True) -> None:
    """从节点里撤掉这款货（只是不归它置顶了，货源本身不动）。"""
    session.delete(row)
    if commit:
        session.commit()


def move_item(
    session: Session, row: PurchaseNodeItem, direction: str, *, commit: bool = True
) -> bool:
    """手动配置的上下挪位（dir=up / down）；挪完按新顺序重排序号，不会撞号。"""
    rows = node_items(session, row.node_id)
    idx = next((i for i, r in enumerate(rows) if r.id == row.id), -1)
    if idx < 0:
        return False
    target = idx - 1 if direction == "up" else idx + 1
    if not (0 <= target < len(rows)):
        return False
    rows[idx].sort_order, rows[target].sort_order = (
        rows[target].sort_order,
        rows[idx].sort_order,
    )
    for i, item in enumerate(
        sorted(rows, key=lambda r: (r.sort_order, r.id)), start=1
    ):
        item.sort_order = i * 10
    if commit:
        session.commit()
    return True


def toggle_item(session: Session, row: PurchaseNodeItem, *, commit: bool = True) -> PurchaseNodeItem:
    """临时不放这款货的置顶位（关掉就退回关键词那套，配置留着）。"""
    row.enabled = not bool(row.enabled)
    if commit:
        session.commit()
    return row


def _to_int(value) -> int:
    try:
        return int(str(value or "").strip() or 0)
    except ValueError:
        return 0


# ------------------------------------------------------------------ 状态计算
def _windows_of(rows: list[dict], today: _date | None = None) -> list[dict]:
    """把节点配置算成「进行中 / 即将开始 / 已结束」的窗口（跨年节点自动落到次年）。"""
    day = today or _date.today()
    out: list[dict] = []
    for node in rows:
        start = md_to_date(node.get("start_md") or "", day.year)
        end = md_to_date(node.get("end_md") or "", day.year)
        if start is None or end is None:
            continue
        if end < start:  # 跨年：窗口落到下一年（如 12-25 ~ 01-20）
            end = _date(day.year + 1, end.month, end.day)
        ongoing = start <= day <= end
        days_to_start = (start - day).days
        out.append({
            "id": node.get("id") or node.get("node_key") or "",
            "name": node.get("name") or "",
            "icon": node.get("icon") or "bi-calendar-event",
            "keywords": tuple(node.get("keywords") or ()),
            "start": start,
            "end": end,
            "date_text": f"{start.month}.{start.day}-{end.month}.{end.day}",
            "status": "ongoing" if ongoing else ("upcoming" if days_to_start >= 0 else "passed"),
            "days_to_start": days_to_start,
            "days_left": (end - day).days if ongoing else 0,
            "sort_order": int(node.get("sort_order") or 0),
        })
    # 与渔货栈货源广场同一套排序：进行中最前 → 即将开始 → 已结束沉底，同组内按运营排序号
    out.sort(key=lambda n: (
        n["status"] == "passed",
        n["status"] != "ongoing",
        n["sort_order"],
        abs(n["days_to_start"]),
    ))
    return out


def windows(today: _date | None = None) -> list[dict]:
    """货源广场要的节点窗口：优先读表；读不到（表没建 / 库断了）回落种子常量。"""
    rows: list[dict] = []
    try:
        from ..db import SessionLocal  # noqa: PLC0415  延迟导入，模块 import 时不碰库

        with SessionLocal() as session:
            rows = [
                {
                    "id": n.node_key,
                    "name": n.name,
                    "icon": n.icon,
                    "start_md": n.start_md,
                    "end_md": n.end_md,
                    "keywords": split_keywords(n.keywords),
                    "sort_order": int(n.sort_order or 0),
                }
                for n in list_nodes(session, only_enabled=True)
            ]
    except Exception as exc:  # noqa: BLE001  读不到不该把货源广场打挂
        logger.warning("读取采购节点配置失败，改用种子节点：%s", exc)
        rows = []
    if not rows:
        rows = [
            {
                "id": s["node_key"],
                "name": s["name"],
                "icon": s.get("icon") or "bi-calendar-event",
                "start_md": s["start_md"],
                "end_md": s["end_md"],
                "keywords": split_keywords(s.get("keywords")),
                "sort_order": int(s.get("sort_order") or 0),
            }
            for s in NODE_SEEDS
        ]
    return _windows_of(rows, today)


__all__ = [
    "NODE_SEEDS",
    "ICON_CHOICES",
    "split_keywords",
    "join_keywords",
    "normalize_md",
    "list_nodes",
    "get_node",
    "ensure_seeds",
    "create_node",
    "update_node",
    "toggle_node",
    "delete_node",
    "windows",
    # 节点下的商品（手动配置）
    "ITEM_CANDIDATE_LIMIT",
    "item_candidates",
    "item_title",
    "node_items",
    "items_by_node",
    "get_item",
    "add_item",
    "remove_item",
    "move_item",
    "toggle_item",
]
