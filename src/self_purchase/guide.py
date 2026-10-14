"""渔货栈新手指引（2026-10-11）：外部学员第一次进自营端，按

    建店铺 → 选品铺货 → 下载素材上架 → 出第一单

走一遍。四步的完成情况**按事实算**，不另记表 —— 做了就是做了，没做就是没做：

  1 新建店铺   —— 渔枢系统的「店铺管理」里有属于他的店铺
  2 选品铺货   —— 「我的店铺商品」（shop_goods）里至少有一件货
  3 下载素材   —— 池子里有下载过素材的货（materials_at 有值）
  4 出第一单   —— 采购单（stock_orders）里至少有一笔

没走完之前每次进首页都提醒一遍；关掉弹窗（点步骤 / 点 ✕ / 点跳过 / 点遮罩）只是
关掉这一次 —— 没做完下次登录还弹。**勾了「以后不再弹出」就永远不弹**（2026-10-11
用户拍板），这条偏好记在服务端 self_guide_prefs（换设备一样生效）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.timeutil import utc_now

logger = logging.getLogger(__name__)

# 四步引导：顺序即引导页里的顺序（改这里就等于改页面）
STEPS: tuple[dict[str, str], ...] = (
    {
        "key": "shop",
        "title": "新建店铺",
        "desc": "把你的闲鱼店铺建进系统：店铺名填闲鱼昵称，手机号填闲鱼账号里那个手机号",
        "url": "/shops/new",
        "btn": "去建店铺",
        "icon": "bi-shop",
    },
    {
        "key": "pool",
        "title": "选品铺货到店铺",
        "desc": "去货源广场挑几款货，加进「我的店铺商品」",
        "url": "/harbor",
        "btn": "去选品",
        "icon": "bi-bag-plus",
    },
    {
        "key": "materials",
        "title": "下载素材上架店铺",
        "desc": "打开商品详情，把文案与图片下载下来，拿去闲鱼发布",
        "url": "/shop-goods",
        "btn": "去下素材",
        "icon": "bi-download",
    },
    {
        "key": "order",
        "title": "完成第一单下单",
        "desc": "闲鱼出单后，在「我的店铺商品」里直接下单，后面的发货与售后都在系统里走",
        "url": "/orders/new",
        "btn": "去下单",
        "icon": "bi-receipt",
    },
)


# 角色名里带这些字的账号一律按「学员」处理（渔枢「权限管理」里的角色组叫「学员组」）
STUDENT_ROLE_KEYS: tuple[str, ...] = ("学员",)


def is_student(user_id: Any) -> bool:
    """这个渔枢账号要不要走新手引导（外部学员）。

    两条任一命中就算（2026-10-11 补第二条）：
      1. 身份字段是「外部学员」（渔枢系统「用户管理」里改，最精确）；
      2. 挂着「学员组」这类带「学员」字样的角色 —— 线上学员是按角色分组建的，
         光看身份字段的话没人去改那个下拉，引导永远弹不出来。

    读不到（渔枢那边还没这个字段 / 独立部署）就当内部人员 —— 别给所有人弹引导。
    """
    uid = int(user_id or 0)
    if not uid:
        return False
    try:
        from app.db import SessionLocal  # noqa: PLC0415
        from app.models import Role, User  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("读不到渔枢账号表：%s", exc)
        return False
    try:
        with SessionLocal() as db:
            row = db.get(User, uid)
            if row is None:
                return False
            if bool(getattr(row, "is_student", False)):
                return True
            role_id = getattr(row, "role_id", None)
            if not role_id:
                return False
            role = db.get(Role, int(role_id))
            role_name = str(getattr(role, "name", "") or "")
            return any(key in role_name for key in STUDENT_ROLE_KEYS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("读账号身份失败（user=%s）：%s", uid, exc)
        return False


def _count(db: Session, model, *conds) -> int:
    try:
        stmt = select(func.count()).select_from(model)
        for cond in conds:
            stmt = stmt.where(cond)
        return int(db.scalar(stmt) or 0)
    except Exception as exc:  # noqa: BLE001  表还没建 / 查询失败：这一步算没做，不该打挂页面
        logger.debug("引导步骤统计失败（%s）：%s", getattr(model, "__tablename__", model), exc)
        return 0


def status_of(db: Session, user_id: Any) -> dict[str, Any]:
    """四步各自做完没有（按事实算）。"""
    uid = int(user_id or 0)
    owner = f"u{uid}"
    done = {"shop": False, "pool": False, "materials": False, "order": False}
    if uid:
        # 1 建店铺：店铺在渔枢系统那张表上（owner_user_id = 渔枢账号 id）
        try:
            from ..web.hub_shops import my_shop_stats  # noqa: PLC0415

            done["shop"] = int(my_shop_stats(uid).get("total", 0) or 0) > 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("读店铺数失败：%s", exc)
            done["shop"] = False
        try:
            from ..models import ShopGoods, StockOrder  # noqa: PLC0415

            done["pool"] = _count(db, ShopGoods, ShopGoods.owner == owner) > 0
            done["materials"] = (
                _count(
                    db,
                    ShopGoods,
                    ShopGoods.owner == owner,
                    ShopGoods.materials_at.is_not(None),
                )
                > 0
            )
            done["order"] = _count(db, StockOrder, StockOrder.owner == owner) > 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("读引导进度失败：%s", exc)

    steps = [{**step, "done": bool(done.get(step["key"]))} for step in STEPS]
    finished = all(step["done"] for step in steps)
    return {
        "steps": steps,
        "finished": finished,
        "done": sum(1 for step in steps if step["done"]),
        "total": len(steps),
        # 下一步（引导页里高亮它）：全做完就是 None
        "next_step": next((step for step in steps if not step["done"]), None),
    }


def _owner_of(user_id: Any) -> str:
    return f"u{int(user_id or 0)}"


def is_hidden(db: Session, user_id: Any) -> bool:
    """这个人是不是勾过「以后不再弹出」（勾了就永远不弹，见 GuidePref）。"""
    uid = int(user_id or 0)
    if not uid:
        return False
    try:
        from ..models import GuidePref  # noqa: PLC0415

        row = db.execute(
            select(GuidePref).where(GuidePref.owner == _owner_of(uid))
        ).scalars().first()
        return bool(row and row.hidden)
    except Exception as exc:  # noqa: BLE001  读不到就当没勾过（宁可多提醒一次）
        logger.debug("读引导偏好失败（user=%s）：%s", uid, exc)
        return False


def hide(db: Session, user_id: Any) -> bool:
    """记下「以后不再弹出」：弹窗里勾了那个框再关掉就会走这里。

    落在服务端（不是 localStorage）—— 换浏览器 / 换设备也不再弹他。
    """
    uid = int(user_id or 0)
    if not uid:
        return False
    try:
        from ..models import GuidePref  # noqa: PLC0415

        owner = _owner_of(uid)
        row = db.execute(
            select(GuidePref).where(GuidePref.owner == owner)
        ).scalars().first()
        if row is None:
            row = GuidePref(owner=owner, hidden=True)
            db.add(row)
        row.hidden = True
        row.hidden_at = utc_now()
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录引导偏好失败（user=%s）：%s", uid, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def guide_of(db: Session, user_id: Any) -> dict[str, Any] | None:
    """给模板用：只有外部学员给引导（内部人员不弹），走完了 / 勾过「不再弹出」也不弹。"""
    if not is_student(user_id):
        return None
    if is_hidden(db, user_id):
        return None
    status = status_of(db, user_id)
    return None if status["finished"] else status
