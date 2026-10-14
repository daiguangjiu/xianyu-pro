"""渔货栈内部账号**第一次登录自动关联渔枢店铺**，以及之后的双向持续同步。

业务背景（2026-10-13 用户拍板）：

- 内部人员（运营 / 带教 / 学员）在渔枢系统里已经有店铺档案（`shops` 表），
  进渔货栈还要再手填一遍 —— 同一家店填两遍，两边还容易不一致；
- 所以：**第一次进渔货栈就按手机号 / 姓名把他名下的店铺自动带过来**，
  之后渔枢那边新增或改了店铺，渔货栈这边也跟着动，不用再人工同步。

**店铺数据本身不复制**：渔货栈「店铺管理」读写的就是渔枢那张 `shops` 表
（见 `src/web/hub_shops.py`），这里只负责三件事：

 1. 找到「这个账号该看到哪些店」（匹配规则，见 `match_shops`）；
 2. 把找到的店铺挂到他名下（`claim_shop` 认领无主 / `link_self_shop` 只关联）；
 3. 记台账（`shop_user_links`）与同步标记（`shop_sync_marks`）—— **首登只做一次**，
    之后按 `SYNC_INTERVAL` 节流增量补，页面也有「立即同步」入口。

两个方向都走同一套匹配：
  · 渔货栈 → 渔枢：本文件 `sync_user()`（进渔货栈时触发）；
  · 渔枢 → 渔货栈：`sync_shop()`（渔枢端店铺建档 / 改档后回调，见 `app/services/shop.py`）。

渔货栈单独部署（进程里没有渔枢那套代码）时 `available()` 为假，所有函数安全返回空。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 增量同步的节流间隔：进了渔货栈也不会每个请求都扫一遍库
SYNC_INTERVAL_SECONDS = 300

try:  # pragma: no cover - 取决于部署形态（合并部署才有 app 包）
    from app.db import SessionLocal as _hub_session
    from app.models import Shop as _HubShop
    from app.models import User as _HubUser
    from app.services import shop as _hub_shop
except Exception:  # noqa: BLE001
    _hub_session = None
    _HubShop = None
    _HubUser = None
    _hub_shop = None

from src.models.shop_link import (  # noqa: E402  与渔枢那套无关，自己包里的
    LINK_SOURCE_AUTO_NAME,
    LINK_SOURCE_AUTO_OWNER,
    LINK_SOURCE_AUTO_PHONE,
    LINK_SOURCE_MANUAL,
    ShopSyncMark,
    ShopUserLink,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """统一成「带时区的 UTC」再比大小。

    那两个时间列是 `DateTime(timezone=True)`：**Postgres 读回来带时区、SQLite 读回来不带**，
    直接拿两个时间相减，必然在其中一种方言上抛 TypeError（naive 与 aware 不能相减）。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def now() -> datetime:
    """当前时间（UTC）：与渔货栈采购域那套口径一致（见 app.timeutil）。"""
    from app.timeutil import utc_now

    return _as_utc(utc_now())


def available() -> bool:
    """当前部署里有没有渔枢那套（没有就谈不上同步）。"""
    return _hub_session is not None and _HubShop is not None and _hub_shop is not None


def _safe(default):
    """同步是锦上添花：台账表还没建好 / 查询出错时按缺省值返回，绝不往上抛。

    店铺本身在渔枢系统的 `shops` 表里，这里只是「记哪个账号看得到哪家店」——
    失败顶多少带几家店，不该让店铺列表页打不开。
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("店铺同步 %s 失败（不影响使用）：%s", fn.__name__, exc)
                return default() if callable(default) else default

        return wrapper

    return deco


# ---------------------------------------------------------------- 匹配
def _norm(value: Any) -> str:
    return str(value or "").strip()


def match_conditions(user: Any) -> tuple[list, str]:
    """按渔枢账号拼匹配条件（手机号 / 姓名），返回 (条件列表, 命中的口径码)。

    口径刻意保守 —— **对不上就不关联**，宁可让用户在页面上补一次，
    也不能把别人的店挂到他名下：
      · 手机号（登录名就是手机号）：店铺绑的手机号 / 联系电话；
      · 姓名（渔枢「用户管理」里填的显示名）：店铺的实际运营人 / 闲鱼会员名。
    """
    phone = _norm(getattr(user, "phone", "")) or _norm(getattr(user, "username", ""))
    # 姓名只认「用户管理」里填的显示名 —— **User 表没有 nickname 字段**（2026-10-11 踩过一次）
    name = _norm(getattr(user, "display_name", ""))
    conds: list = []
    source = ""
    if phone:
        conds.append(
            or_(_HubShop.phone == phone, _HubShop.contact_phone == phone)
        )
        source = LINK_SOURCE_AUTO_PHONE
    if name:
        conds.append(
            or_(_HubShop.operator == name, _HubShop.member_name == name)
        )
        source = source or LINK_SOURCE_AUTO_NAME
    return conds, source


def match_shops(db: Session, user: Any) -> list[tuple[Any, str]]:
    """这个账号该自动关联的店铺：``[(店铺, 命中口径), ...]``。

    手机号 / 姓名对上、且不属于别人、也没被他自己解过关联的才算命中。
    """
    conds, _source = match_conditions(user)
    if not conds:
        return []
    uid = int(getattr(user, "id", 0) or 0)
    phone = _norm(getattr(user, "phone", "")) or _norm(getattr(user, "username", ""))
    name = _norm(getattr(user, "display_name", ""))
    rows = list(db.scalars(select(_HubShop).where(or_(*conds))).all())
    out: list[tuple[Any, str]] = []
    for row in rows:
        if row.owner_user_id == uid or row.self_linked_user_id == uid:
            continue  # 已经是他的，不用再关联
        if row.self_linked_user_id and row.self_linked_user_id != uid:
            continue  # 已经被别的渔货栈账号关联走了，不动
        if _is_unlinked(db, uid, int(row.id or 0)):
            continue  # 他自己解过关联，别再给关联回来
        hit_phone = bool(phone) and phone in {_norm(row.phone), _norm(row.contact_phone)}
        source = LINK_SOURCE_AUTO_PHONE if hit_phone else LINK_SOURCE_AUTO_NAME
        if not hit_phone and not (bool(name) and name in {_norm(row.operator), _norm(row.member_name)}):
            continue  # 理论上到不了这里：兜个底，认不出口径就不关联
        out.append((row, source))
    return out


def _is_unlinked(db: Session, user_id: int, shop_id: int) -> bool:
    row = db.scalar(
        select(ShopUserLink).where(
            ShopUserLink.user_id == user_id,
            ShopUserLink.shop_id == shop_id,
            ShopUserLink.unlinked_at.is_not(None),
        )
    )
    return row is not None


# ---------------------------------------------------------------- 同步
def _link_row(db: Session, user_id: int, shop_id: int, source: str, claimed: bool) -> None:
    row = db.scalar(
        select(ShopUserLink).where(
            ShopUserLink.user_id == user_id, ShopUserLink.shop_id == shop_id
        )
    )
    if row is None:
        db.add(
            ShopUserLink(
                user_id=user_id, shop_id=shop_id, source=source, claimed=claimed
            )
        )
        return
    row.unlinked_at = None
    row.claimed = claimed or bool(row.claimed)
    if row.source == LINK_SOURCE_MANUAL and source != LINK_SOURCE_MANUAL:
        row.source = source


@_safe(lambda: {
    "ok": False, "first": False, "new": 0, "total": 0,
    "claimed": 0, "linked": 0, "skipped": False,
})
def sync_user(user_id: int, *, force: bool = False) -> dict[str, Any]:
    """把这个账号在渔枢系统里的店铺同步过来（幂等，可反复调用）。

    - 首登（`shop_sync_marks` 里没记录）→ 全量匹配一次，记 `first_synced_at`；
    - 之后 → 按 `SYNC_INTERVAL_SECONDS` 节流增量补（渔枢新建 / 改了运营人的店铺自动跟上），
      `force=True` 时无视节流（页面「立即同步」按钮）。
    """
    empty = {
        "ok": False, "first": False, "new": 0, "total": 0,
        "claimed": 0, "linked": 0, "skipped": False,
    }
    if not available() or not user_id:
        return empty
    with _hub_session() as db:
        user = db.get(_HubUser, int(user_id))
        if user is None:
            return empty
        mark = db.scalar(select(ShopSyncMark).where(ShopSyncMark.user_id == user_id))
        first = mark is None
        if (
            not force
            and not first
            and mark is not None
            and mark.last_synced_at is not None
        ):
            last = _as_utc(mark.last_synced_at)
            if last is not None and (now() - last).total_seconds() < SYNC_INTERVAL_SECONDS:
                return {
                    "ok": True, "first": False, "new": 0,
                    "total": _count_links(db, user_id),
                    "claimed": 0, "linked": 0, "skipped": True,
                }
        if mark is None:
            mark = ShopSyncMark(user_id=user_id)
            db.add(mark)

        # 1) 渔枢端建档时提交人就是他的（owner_user_id）：记台账，不需要改字段
        mine = list(
            db.scalars(select(_HubShop).where(_HubShop.owner_user_id == user_id)).all()
        )
        for row in mine:
            _link_row(db, user_id, int(row.id or 0), LINK_SOURCE_AUTO_OWNER, True)

        # 2) 按手机号 / 姓名匹配的：无主的认领过来，有主的只关联不改归属
        claimed = linked = 0
        for row, source in match_shops(db, user):
            if not row.owner_user_id:
                _hub_shop.claim_shop(db, row, user_id)
                claimed += 1
                _link_row(db, user_id, int(row.id or 0), source, True)
            else:
                _hub_shop.link_self_shop(db, row, user_id)
                linked += 1
                _link_row(db, user_id, int(row.id or 0), source, False)

        new = claimed + linked
        mark.last_new_count = new
        mark.last_synced_at = now()
        if first:
            mark.first_synced_at = mark.last_synced_at
        db.flush()
        db.commit()
        total = _count_links(db, user_id)
    return {
        "ok": True, "first": first, "new": new, "total": total,
        "claimed": claimed, "linked": linked, "skipped": False,
    }


def sync_shop(shop_id: int) -> int:
    """渔枢端店铺建档 / 改了归属信息后，反向同步给该看到的渔货栈账号。

    渔枢端新建一家店（或把运营人改成某个人）时调一次：按手机号 / 姓名找出
    对应的渔货栈账号，给他建关联 —— **他不用再进渔货栈点一次同步**。
    返回这次新关联上的账号数。
    """
    if not available() or not shop_id:
        return 0
    with _hub_session() as db:
        row = db.get(_HubShop, int(shop_id))
        if row is None:
            return 0
        # 集合（不是列表）：下面要用 & 求交集判断「到底是哪一门口径对上的」
        keys = {v for v in (_norm(row.phone), _norm(row.contact_phone)) if v}
        names = {v for v in (_norm(row.operator), _norm(row.member_name)) if v}
        if not keys and not names:
            return 0
        users = list(
            db.scalars(
                select(_HubUser).where(
                    or_(
                        _HubUser.phone.in_(keys) if keys else False,  # type: ignore[arg-type]
                        _HubUser.username.in_(keys) if keys else False,  # type: ignore[arg-type]
                        _HubUser.display_name.in_(names) if names else False,  # type: ignore[arg-type]
                    )
                )
            ).all()
        )
        hit = 0
        for user in users:
            uid = int(getattr(user, "id", 0) or 0)
            if not uid:
                continue
            if keys & {_norm(user.phone), _norm(user.username)}:
                pass
            elif names & {_norm(user.display_name)}:
                pass
            else:
                continue
            if row.owner_user_id == uid or row.self_linked_user_id == uid:
                continue
            if row.self_linked_user_id and row.self_linked_user_id != uid:
                continue
            if _is_unlinked(db, uid, int(row.id or 0)):
                continue
            if not row.owner_user_id:
                _hub_shop.claim_shop(db, row, uid)
                _link_row(db, uid, int(row.id or 0), LINK_SOURCE_AUTO_PHONE, True)
            else:
                _hub_shop.link_self_shop(db, row, uid)
                _link_row(db, uid, int(row.id or 0), LINK_SOURCE_AUTO_NAME, False)
            hit += 1
        if hit:
            db.flush()
            db.commit()
        return hit


def _count_links(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(ShopUserLink.id)).where(
                ShopUserLink.user_id == user_id,
                ShopUserLink.unlinked_at.is_(None),
            )
        )
        or 0
    )


# ---------------------------------------------------------------- 台账读写
@_safe(lambda: {
    "synced": False, "first_at": None, "last_at": None,
    "new": 0, "auto": 0, "manual": 0, "total": 0,
})
def mark_of(user_id: int) -> dict[str, Any]:
    """这个账号的同步状态（页面展示用）。"""
    out = {
        "synced": False, "first_at": None, "last_at": None,
        "new": 0, "auto": 0, "manual": 0, "total": 0,
    }
    if not available() or not user_id:
        return out
    with _hub_session() as db:
        mark = db.scalar(select(ShopSyncMark).where(ShopSyncMark.user_id == user_id))
        if mark is not None:
            out["synced"] = True
            out["first_at"] = mark.first_synced_at
            out["last_at"] = mark.last_synced_at
            out["new"] = int(mark.last_new_count or 0)
        rows = list(
            db.scalars(
                select(ShopUserLink).where(
                    ShopUserLink.user_id == user_id,
                    ShopUserLink.unlinked_at.is_(None),
                )
            ).all()
        )
    out["total"] = len(rows)
    out["auto"] = sum(1 for r in rows if r.source != LINK_SOURCE_MANUAL)
    out["manual"] = sum(1 for r in rows if r.source == LINK_SOURCE_MANUAL)
    return out


def source_of(user_id: int, shop_id: int) -> str:
    """这家店是怎么到这个账号名下的（没有台账返回空串）。"""
    if not available() or not user_id or not shop_id:
        return ""
    with _hub_session() as db:
        row = db.scalar(
            select(ShopUserLink).where(
                ShopUserLink.user_id == user_id, ShopUserLink.shop_id == shop_id
            )
        )
        return str(row.source or "") if row else ""


def note_manual(user_id: int, shop_id: int, claimed: bool = False) -> None:
    """渔货栈手动添加 / 认领店铺后补一条台账（手动来源优先，不被自动改写）。"""
    if not available() or not user_id or not shop_id:
        return
    with _hub_session() as db:
        _link_row(db, user_id, shop_id, LINK_SOURCE_MANUAL, claimed)
        db.flush()
        db.commit()


def note_unlink(user_id: int, shop_id: int) -> None:
    """解除关联：台账上打时间，避免下次同步又给关联回来。"""
    if not available() or not user_id or not shop_id:
        return
    with _hub_session() as db:
        row = db.scalar(
            select(ShopUserLink).where(
                ShopUserLink.user_id == user_id, ShopUserLink.shop_id == shop_id
            )
        )
        if row is not None:
            row.unlinked_at = now()
            db.flush()
            db.commit()


def ensure_synced(hub: Any) -> dict[str, Any]:
    """进渔货栈就调一次（幂等 + 节流）：首登自动关联，之后增量补。

    异常一律吞掉 —— 同步是锦上添花，不该把页面打挂。
    """
    try:
        return sync_user(int(getattr(hub, "id", 0) or 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("渔货栈店铺自动同步失败（不影响使用）：%s", exc)
        return {"ok": False, "first": False, "new": 0, "total": 0,
                "claimed": 0, "linked": 0, "skipped": False}
