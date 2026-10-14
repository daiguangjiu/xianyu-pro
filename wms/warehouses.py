"""仓库与货架（库位）维护（2026-10-08 用户要求）。

一张 ``wms_warehouses`` 表存五级，用 ``parent_code`` 串起来：

  1 仓库（默认 7 个，见 ``models.WAREHOUSE_DEFAULTS``）→ 2 区 → 3 货架 → 4 层 → 5 位

货品上两个字段指向它：``warehouse_code``（第 1 级）与 ``location_code``（第 2~5 级，也就是
检测台填的「货架位置」）。用户在页面上能**添加 / 修改 / 删除**，其它业务页面里这两项
**只能下拉选** —— 手打编码一旦写错，货就能入到一个不存在的仓里，账立刻散。

硬规矩（在 service 里守住，页面只是提示）：

  · 内置 7 仓不许删（分级路由 ``GRADE_ROUTE`` 写死了它们的码），只许改名 / 停用；
  · **编码不许改**（货品、流水、批次、盘点单上都存着它）—— 要换码就停用旧的、新建一个；
  · **层级不许改**（改了子孙的路径全错）—— 同理，停用旧的 + 新建；
  · 还在被引用的（子级 / 在库货品 / 批次 / 盘点单）不许删，提示改用「停用」。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    BUILTIN_WAREHOUSE_CODES,
    GRADE_LABELS,
    WAREHOUSE_DEFAULTS,
    WAREHOUSE_LEVEL_LABELS,
    WAREHOUSE_MAX_LEVEL,
    WmsBatch,
    WmsItem,
    WmsStocktake,
    WmsWarehouse,
    register_warehouse_names,
)
from .timeutil import now as _now


def _code(value) -> str:
    return str(value or "").strip().upper()


def level_choices() -> list[dict]:
    """层级下拉（含说明），页面与提示语共用一处。"""
    hints = {
        1: "仓库：货在哪个仓（分级路由的目标）",
        2: "区：仓里的分区（如 S 区 / A 区）",
        3: "货架：区里的某一排货架",
        4: "层：货架的第几层",
        5: "位：具体库位（货品挂的就是它）",
    }
    return [
        {"value": lv, "label": f"{lv} 级 · {WAREHOUSE_LEVEL_LABELS[lv]}", "hint": hints[lv]}
        for lv in range(1, WAREHOUSE_MAX_LEVEL + 1)
    ]


def all_rows(session: Session, include_disabled: bool = True) -> list[WmsWarehouse]:
    stmt = select(WmsWarehouse)
    if not include_disabled:
        stmt = stmt.where(WmsWarehouse.enabled.is_(True))
    return list(
        session.scalars(
            stmt.order_by(
                WmsWarehouse.level.asc(), WmsWarehouse.sort.asc(), WmsWarehouse.id.asc()
            )
        ).all()
    )


def _by_code(rows) -> dict[str, WmsWarehouse]:
    return {_code(r.code): r for r in rows}


def item_counts(session: Session) -> dict[str, int]:
    """每个编码上挂着多少台货：在仓（warehouse_code）与库位（location_code）都算。"""
    out: dict[str, int] = {}
    for col in (WmsItem.warehouse_code, WmsItem.location_code):
        rows = session.execute(
            select(func.upper(col), func.count())
            .select_from(WmsItem)
            .where(func.coalesce(col, "") != "")
            .group_by(func.upper(col))
        ).all()
        for code, count in rows:
            key = _code(code)
            out[key] = out.get(key, 0) + int(count or 0)
    return out


def child_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(func.upper(WmsWarehouse.parent_code), func.count())
        .where(func.coalesce(WmsWarehouse.parent_code, "") != "")
        .group_by(func.upper(WmsWarehouse.parent_code))
    ).all()
    return {_code(code): int(count or 0) for code, count in rows}


def _root_code(by_code: dict[str, WmsWarehouse], code: str) -> str:
    """沿 parent 往上走到第 1 级（仓库），前端按下拉过滤要用。"""
    row = by_code.get(code)
    seen: set[str] = set()
    while row is not None and _code(row.parent_code) and _code(row.code) not in seen:
        seen.add(_code(row.code))
        row = by_code.get(_code(row.parent_code))
    return _code(row.code) if row is not None else code


def page_rows(session: Session, q: str = "", level: int = 0) -> list[dict]:
    """维护页的行：带上层级名 / 上级 / 在库台数 / 子级数 / 能否删。"""
    rows = all_rows(session)
    codes = _by_code(rows)
    items = item_counts(session)
    kids = child_counts(session)
    keyword = str(q or "").strip().lower()
    want_level = int(level or 0)
    out: list[dict] = []
    for row in rows:
        code = _code(row.code)
        lv = int(row.level or 1)
        if want_level and lv != want_level:
            continue
        name = str(row.name or "")
        full = str(row.full_path or name or code)
        if keyword and keyword not in code.lower() and keyword not in name.lower() \
                and keyword not in full.lower():
            continue
        parent = codes.get(_code(row.parent_code))
        out.append(
            {
                "row": row,
                "code": code,
                "name": name,
                "level": lv,
                "level_label": WAREHOUSE_LEVEL_LABELS.get(lv, "仓库"),
                "parent_code": _code(row.parent_code),
                "parent_name": str(parent.name) if parent is not None else "",
                "full_path": full,
                "grade_zone": _code(row.grade_zone),
                "can_out": bool(row.can_out),
                "enabled": bool(row.enabled),
                "sort": int(row.sort or 0),
                "note": str(row.note or ""),
                "item_count": items.get(code, 0),
                "child_count": kids.get(code, 0),
                "builtin": code in BUILTIN_WAREHOUSE_CODES,
            }
        )
    return out


def _sibling_index(rows) -> dict[str, tuple[int, int, str]]:
    """每个编码 → (层级, 同层内第几个, 上级编码)。

    序号按 ``sort`` 排（维护页的 ↑↓ 改的就是它），所以**短码顺序与页面顺序一致** ——
    把常用的区 / 货架排到前面，短码也跟着变小。
    """
    groups: dict[tuple[int, str], list] = {}
    for row in rows:
        groups.setdefault((int(row.level or 1), _code(row.parent_code)), []).append(row)
    out: dict[str, tuple[int, int, str]] = {}
    for (level, parent), siblings in groups.items():
        siblings.sort(key=lambda r: (int(r.sort or 0), int(r.id or 0)))
        for idx, row in enumerate(siblings, start=1):
            out[_code(row.code)] = (level, idx, parent)
    return out


def _short_code(code: str, index: dict[str, tuple[int, int, str]]) -> str:
    """短码 = 从仓库到本级的层级序号，如 ``7-1-1``（第 7 个仓 › 第 1 个区 › 第 1 个货架）。

    现场用数字键盘敲 ``711`` 就能选中，不用去念那一长串中文全路径。
    """
    parts: list[str] = []
    key = _code(code)
    seen: set[str] = set()
    while key and key in index and key not in seen:
        seen.add(key)
        _level, idx, parent = index[key]
        parts.append(str(idx))
        key = parent
    return "-".join(reversed(parts))


def root_index(session: Session) -> list[dict]:
    """短码第 1 位「第几个仓」的对照表（2026-10-11 用户要看「1~7 各是哪个仓」）。

    序号口径必须和 :func:`_sibling_index` **一字不差**：同层按 ``sort``（维护页 ↑↓
    改的就是它）再按 id 排，**停用的仓照样占一个号**（它也在那张 index 表里）——
    否则页面写「3 = 维修仓」而短码里的 3 指的是另一个仓，比不给说明还糟。
    """
    rows = [r for r in all_rows(session) if int(r.level or 1) == 1]
    rows.sort(key=lambda r: (int(r.sort or 0), int(r.id or 0)))
    return [
        {
            "n": idx,
            "code": _code(row.code),
            "name": str(row.name or _code(row.code)),
            "enabled": bool(row.enabled),
            "builtin": _code(row.code) in BUILTIN_WAREHOUSE_CODES,
        }
        for idx, row in enumerate(rows, start=1)
    ]


def _initials(text: str) -> str:
    """拼音首字母：字表在平台侧，只经桥要（拿不到就返回空串）。"""
    from . import bridge

    return bridge.text_initials(text)


def location_options(session: Session) -> list[dict]:
    """库位选择器的数据：第 2 级及以下、启用的（业务页面只能从这里选，不许手打）。

    2026-10-11：只有 ``full_path`` 时（「可售仓 › 可售区 1 区 › 货架 1」）一屏几十条太长，
    现场挑不过来，所以每条再给一份**联想用的料**：

      · ``short``       层级序号短码（``7-1-1``）—— 敲数字就命中；
      · ``search``      一次 ``indexOf`` 能比的搜索串（短码 + 全路径 + 名称 + 拼音首字母）；
        **名字不能叫 keys** —— Jinja 的 ``loc.keys`` 会取到 dict 的 ``.keys`` 方法，
        渲染出来是 ``<built-in method keys …>``（2026-10-11 踩过）；
      · ``use``         当前挂在这个库位上的台数 —— **常用的排前面**；
      · ``parent_path`` 上级路径（列表里省掉叶子名，行短一半）；
      · ``root_name``   第 1 级仓库名（列表里当分组标签用）。
    """
    every = all_rows(session)
    codes = _by_code(every)
    index = _sibling_index(every)
    used = item_counts(session)
    out: list[dict] = []
    for row in every:
        lv = int(row.level or 1)
        if lv < 2 or not bool(row.enabled):
            continue
        code = _code(row.code)
        name = str(row.name or "")
        full = str(row.full_path or name or code)
        parent = codes.get(_code(row.parent_code))
        root_key = _root_code(codes, code)
        root = codes.get(root_key)
        short = _short_code(code, index)
        search = " ".join(
            x
            for x in (
                short,
                short.replace("-", ""),
                full,
                name,
                str(getattr(root, "name", "") or ""),
                _initials(full),
            )
            if x
        )
        out.append(
            {
                "code": code,
                "name": name,
                "level": lv,
                "level_label": WAREHOUSE_LEVEL_LABELS.get(lv, ""),
                "full_path": full,
                "parent_path": str(parent.full_path or parent.name) if parent is not None else "",
                "root_code": root_key,
                "root_name": str(getattr(root, "name", "") or ""),
                "grade_zone": _code(row.grade_zone),
                "short": short,
                "search": search,
                "use": int(used.get(code, 0)),
            }
        )
    return out


def check_location(session: Session, code, current: str = "") -> str:
    """校验业务页面传来的库位：必须是维护表里第 2 级及以下、且启用的行。

    页面已经是下拉了，这里是**后端兜底** —— 直连接口塞一个不存在的库位，
    货就挂在一个永远查不到的地方了（2026-10-08）。空值原样返回（不放库位是合法的）。

    ``current`` 是货品当前挂着的库位：**和它一样就直接放行** —— 老数据里有当年手打的
    自由文本（下拉时代之前填的），改别的东西时不该因为一个历史值把整单卡住。
    """
    key = _code(code)
    if not key:
        return ""
    if key == _code(current):
        return key
    row = session.scalar(
        select(WmsWarehouse).where(
            func.upper(WmsWarehouse.code) == key, WmsWarehouse.level >= 2
        )
    )
    if row is None:
        raise ValueError(f"库位 {key} 不在「仓库与货架」维护表里 —— 先去那里加一个")
    if not row.enabled:
        raise ValueError(f"库位「{row.name}」已停用，换一个")
    return _code(row.code)


# ---------------------------------------------------------------- 增 / 改
def _parent_of(session: Session, parent_code: str, level: int) -> WmsWarehouse | None:
    if int(level or 1) <= 1:
        return None
    want = _code(parent_code)
    label = WAREHOUSE_LEVEL_LABELS.get(int(level), "库位")
    parent_label = WAREHOUSE_LEVEL_LABELS.get(int(level) - 1, "上级")
    if not want:
        raise ValueError(f"{label}要选一个上级{parent_label}")
    parent = session.scalar(
        select(WmsWarehouse).where(func.upper(WmsWarehouse.code) == want)
    )
    if parent is None:
        raise ValueError(f"上级 {want} 不存在，先建它")
    if int(parent.level or 1) != int(level) - 1:
        raise ValueError(f"上级层级不对：{label}的上级必须是{parent_label}")
    return parent


def _next_sort(rows, level: int, parent_code: str) -> int:
    same = [
        int(r.sort or 0)
        for r in rows
        if int(r.level or 1) == int(level) and _code(r.parent_code) == _code(parent_code)
    ]
    return (max(same) if same else 0) + 10


def _next_code(rows, level: int, parent_code: str) -> str:
    """编码留空时自动生成：仓库 ``WH01``，下级 ``上级码-01``。"""
    prefix = f"{_code(parent_code)}-" if _code(parent_code) else "WH"
    used = {_code(r.code) for r in rows}
    for idx in range(1, 1000):
        code = f"{prefix}{idx:02d}"
        if code not in used:
            return code
    return f"{prefix}{len(used) + 1}"


def rebuild_paths(session: Session) -> None:
    """重算全部 full_path（改名 / 换上级后子级路径要跟着变）。"""
    rows = list(session.scalars(select(WmsWarehouse)).all())
    codes = _by_code(rows)
    for row in sorted(rows, key=lambda r: int(r.level or 1)):
        parent = codes.get(_code(row.parent_code))
        if parent is not None and str(parent.full_path or "").strip():
            row.full_path = f"{parent.full_path} › {row.name}"
        else:
            row.full_path = str(row.name or _code(row.code))
        session.add(row)
    session.flush()
    register_warehouse_names(rows)


def save(
    session: Session,
    *,
    wh_id: int = 0,
    code: str = "",
    name: str = "",
    level: int = 1,
    parent_code: str = "",
    grade_zone: str = "",
    can_out: bool = False,
    sort=None,
    enabled: bool = True,
    note: str = "",
) -> tuple[WmsWarehouse, str]:
    """新增 / 修改。返回 ``(行, 提示语)``；不合法抛 ``ValueError``。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    rows = all_rows(session)
    row = session.get(WmsWarehouse, int(wh_id)) if int(wh_id or 0) else None
    if row is not None:
        # 编码与层级锁定：前者货品 / 流水 / 批次上都存着，后者改了子孙路径全错
        level = max(1, min(int(row.level or 1), WAREHOUSE_MAX_LEVEL))
    else:
        level = max(1, min(int(level or 1), WAREHOUSE_MAX_LEVEL))
    parent = _parent_of(session, parent_code, level)
    zone = _code(grade_zone)
    if zone and zone not in GRADE_LABELS:
        raise ValueError(f"等级区只能填 {' / '.join(GRADE_LABELS)}，或留空")

    if row is None:
        new_code = _code(code) or _next_code(rows, level, _code(parent_code))
        if any(_code(r.code) == new_code for r in rows):
            raise ValueError(f"编码 {new_code} 已存在")
        row = WmsWarehouse(
            code=new_code,
            level=level,
            sort=_next_sort(rows, level, _code(parent_code)) if sort in (None, "") else int(sort),
            created_at=_now(),
        )
        session.add(row)
        action = f"新增{WAREHOUSE_LEVEL_LABELS.get(level, '库位')}「{name}」"
    else:
        action = f"保存{WAREHOUSE_LEVEL_LABELS.get(level, '库位')}「{name}」"

    row.name = name
    row.parent_code = _code(parent.code) if parent is not None else ""
    row.grade_zone = zone
    row.can_out = bool(can_out)
    row.enabled = bool(enabled)
    row.note = str(note or "").strip()
    if sort not in (None, ""):
        row.sort = int(sort)
    row.updated_at = _now()
    session.flush()
    rebuild_paths(session)
    session.commit()
    return row, action


def move(session: Session, wh_id: int, direction: str = "up") -> None:
    """同层同父里上 / 下移一位（比手填排序号省事，也不会撞号）。"""
    row = session.get(WmsWarehouse, int(wh_id))
    if row is None:
        raise ValueError("库位不存在")
    level = int(row.level or 1)
    parent_code = _code(row.parent_code)
    siblings = [
        r
        for r in all_rows(session)
        if int(r.level or 1) == level and _code(r.parent_code) == parent_code
    ]
    siblings.sort(key=lambda r: (int(r.sort or 0), int(r.id or 0)))
    idx = next((i for i, r in enumerate(siblings) if r.id == row.id), -1)
    if idx < 0:
        raise ValueError("库位不存在")
    swap = idx - 1 if str(direction or "up") == "up" else idx + 1
    if swap < 0 or swap >= len(siblings):
        raise ValueError("已经在最前 / 最后了")
    other = siblings[swap]
    row.sort, other.sort = int(other.sort or 0), int(row.sort or 0)
    if row.sort == other.sort:  # 排序号撞了：错开一位，保证顺序真的会动
        row.sort = int(other.sort or 0) + (1 if swap > idx else -1)
    row.updated_at = _now()
    session.commit()


def toggle(session: Session, wh_id: int) -> WmsWarehouse:
    row = session.get(WmsWarehouse, int(wh_id))
    if row is None:
        raise ValueError("库位不存在")
    row.enabled = not bool(row.enabled)
    row.updated_at = _now()
    session.commit()
    return row


def delete(session: Session, wh_id: int) -> str:
    """删除（被引用就拦下，提示改用停用）。返回提示语。"""
    row = session.get(WmsWarehouse, int(wh_id))
    if row is None:
        raise ValueError("库位不存在")
    code = _code(row.code)
    name = str(row.name or code)
    if code in BUILTIN_WAREHOUSE_CODES:
        raise ValueError(f"{name} 是内置仓（分级路由按它入仓），不能删 —— 要腾地方就「停用」")

    def _count(stmt) -> int:
        return int(session.scalar(stmt) or 0)

    refs: list[str] = []
    kids = _count(
        select(func.count()).select_from(WmsWarehouse).where(
            func.upper(WmsWarehouse.parent_code) == code
        )
    )
    if kids:
        refs.append(f"{kids} 个子级")
    in_wh = _count(
        select(func.coalesce(func.sum(WmsItem.qty), 0)).where(
            func.upper(WmsItem.warehouse_code) == code
        )
    )
    if in_wh:
        refs.append(f"{in_wh} 台货在这个仓")
    in_loc = _count(
        select(func.coalesce(func.sum(WmsItem.qty), 0)).where(
            func.upper(WmsItem.location_code) == code
        )
    )
    if in_loc:
        refs.append(f"{in_loc} 台货在这个库位")
    batches = _count(
        select(func.count()).select_from(WmsBatch).where(
            func.upper(WmsBatch.warehouse_code) == code
        )
    )
    if batches:
        refs.append(f"{batches} 个批次挂着它")
    takes = _count(
        select(func.count()).select_from(WmsStocktake).where(
            func.upper(WmsStocktake.warehouse_code) == code
        )
    )
    if takes:
        refs.append(f"{takes} 张盘点单用它")
    if refs:
        raise ValueError(f"{name} 还在用（{'、'.join(refs)}），不能删 —— 改成「停用」")
    session.delete(row)
    session.commit()
    return f"{name} 已删除"


def ensure_defaults(session: Session) -> int:
    """兜底：内置 7 个仓一个都不能少（老库 / 手工清过表的情况）。"""
    created = 0
    have = {_code(r.code) for r in all_rows(session)}
    for idx, (code, name, desc, can_out) in enumerate(WAREHOUSE_DEFAULTS):
        if code in have:
            continue
        session.add(
            WmsWarehouse(
                code=code,
                name=name,
                level=1,
                parent_code="",
                full_path=name,
                grade_zone="",
                can_out=can_out,
                enabled=True,
                sort=(idx + 1) * 10,
                note=desc,
                created_at=_now(),
            )
        )
        created += 1
    if created:
        session.commit()
        register_warehouse_names(all_rows(session))
    return created
