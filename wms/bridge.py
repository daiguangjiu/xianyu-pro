"""检测仓 ↔ 平台的**互通桥**（唯一的数据 / 身份通道）。

规矩只有两条：

  1. 桥是**单向依赖**：检测仓可以试着读平台，平台不需要知道检测仓的存在；
  2. 桥必须**可断**：`import` 不到平台模块、表不存在、查询报错，一律返回空 /
     None，绝不把异常抛到页面上（合并部署失败时平台照常跑，检测仓降级提示）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("wms.bridge")


def standalone() -> bool:
    """是否独立进程部署（不由渔枢系统挂载）。

    独立部署时登录由网关负责：网关把账号放进 ``X-Forwarded-User`` 头带进来，
    ``WMS_STANDALONE_ADMIN`` 里写的账号名视为超级管理员（等价于平台 admin）。
    """
    return str(os.getenv("WMS_STANDALONE") or "").strip().lower() in ("1", "true", "yes")


def current_identity(request) -> dict | None:
    """当前登录身份（``None`` = 没登录）。平台账号就是这里的账号。

    合并部署：读平台自己的登录态（``app.web.auth``）；
    独立部署：读网关透传的 ``X-Forwarded-User``。
    """
    if standalone():
        name = str(request.headers.get("x-forwarded-user") or "").strip()
        if not name:
            return None
        raw_id = str(request.headers.get("x-forwarded-user-id") or "").strip()
        try:
            user_id = int(raw_id)
        except ValueError:
            user_id = 0
        admins = {
            s.strip()
            for s in str(os.getenv("WMS_STANDALONE_ADMIN") or "").split(",")
            if s.strip()
        }
        return {
            "id": user_id,
            "username": name,
            "display": name,
            "is_admin": name in admins,
        }

    try:
        from app.web.auth import ROLE_ADMIN, current_user
    except Exception:  # noqa: BLE001  平台不在同进程 → 独立部署逻辑走上面
        logger.debug("平台登录模块不可用（独立部署？）")
        return None
    try:
        user = current_user(request)
    except Exception:  # noqa: BLE001  登录态解析失败按未登录处理
        logger.debug("解析平台登录态失败", exc_info=True)
        return None
    if user is None:
        return None
    role = str(getattr(user, "role", "") or "")
    return {
        "id": int(getattr(user, "id", 0) or 0),
        "username": str(getattr(user, "username", "") or ""),
        "display": str(
            getattr(user, "display_name", "") or getattr(user, "username", "") or ""
        ),
        "is_admin": role == ROLE_ADMIN,
    }


def subsystem_options(request) -> list[dict]:
    """检测仓顶栏的「切换子系统」数据（渔枢系统 / 渔货栈 / 渔集）。

    2026-10-04 起检测仓是平台子系统之一，不再是平台侧边栏里的一个跳转入口：
    这里复用平台的子系统定义（``app.portals``）与账号授权，只看得到有权限的那些。
    桥断了（独立部署 / 平台模块不可用）返回空列表，顶栏退回一个「回平台」链接。
    """
    if standalone():
        return []
    try:
        from app.portals import switch_options
        from app.web.auth import current_user

        return switch_options(current_user(request), "/wms")
    except Exception:  # noqa: BLE001  拿不到就少一个入口，不该把页面弄挂
        logger.debug("读平台子系统定义失败", exc_info=True)
        return []


def hub_role_perms(user_id: int, system: str = "wms") -> set[str]:
    """渔枢系统「角色权限」页里给这个账号勾的某套子系统权限（2026-10-11）。

    三套子系统的菜单级权限收口在渔枢系统那页配：这里把它读回来，与检测仓自己的
    「人员授权」（wms_user_roles）**取并集** —— 两边都能加权限，不互相覆盖。
    桥断 / 读不到 → 空集（检测仓自己的角色照常生效）。
    """
    if standalone():
        return set()
    try:
        from app.web.permissions import perms_of_user  # noqa: PLC0415
    except Exception:  # noqa: BLE001  独立部署 / 平台没这套表
        logger.debug("读不到渔枢角色权限模块", exc_info=True)
        return set()
    try:
        return set(perms_of_user(int(user_id), system))
    except Exception:  # noqa: BLE001
        logger.debug("读取渔枢角色权限失败（user=%s）", user_id, exc_info=True)
        return set()


def hub_suppliers(db, keyword: str = "", limit: int = 500) -> list[dict]:
    """拉渔枢系统「供应商管理」的供应商档案（给批次建档挑来源方用）。

    检测仓**不自己维护供应商**：来源方以渔枢的供应商台账为准（只读不写）。
    桥断了 / 表里还没有供应商 → 返回空列表，页面提示去找渔枢管理员添加。
    """
    if standalone():
        return []
    try:
        from app.models import Supplier
        from sqlalchemy import or_, select
    except Exception:  # noqa: BLE001  独立部署 / 平台表不可读
        logger.debug("平台供应商表不可读", exc_info=True)
        return []
    stmt = select(Supplier).order_by(Supplier.name.asc())
    kw = keyword.strip()
    if kw:
        like = f"%{kw}%"
        stmt = stmt.where(or_(Supplier.name.like(like), Supplier.code.like(like)))
    try:
        rows = list(db.scalars(stmt.limit(limit)).all())
    except Exception:  # noqa: BLE001
        logger.debug("查询平台供应商失败", exc_info=True)
        return []
    return [
        {
            "id": int(getattr(row, "id", 0) or 0),
            "name": str(getattr(row, "name", "") or ""),
            "code": str(getattr(row, "code", "") or ""),
            "category": str(getattr(row, "category", "") or ""),
            "contact": str(getattr(row, "contact_name", "") or ""),
        }
        for row in rows
    ]


def hub_order_facts(order_no: str) -> dict:
    """读一张渔枢订单的事实（渔货栈自营单同步出库用，2026-10-15）。

    渔货栈的订单不走供应商推单，货从渔集发 —— 出库详情页要展示「这张单卖的是
    哪款货」：产品 / 规格 / 数量 / 收货人 + **渔货号**（商品档案的商家编码就是它，
    再兜一层来源消息里 YS 开头的串）。桥断 / 订单不存在 / 不是渔货栈的单 →
    返回空 dict，页面按普通出库单渲染。
    """
    order_no = str(order_no or "").strip()
    if standalone() or not order_no:
        return {}
    try:
        import re

        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Order, Product, RawMessage
    except Exception:  # noqa: BLE001  独立部署 / 平台表不可读
        logger.debug("平台订单表不可读", exc_info=True)
        return {}
    try:
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.order_no == order_no))
            if not order:
                return {}
            codes: list[str] = []
            seen: set[str] = set()
            product = s.get(Product, order.product_id) if order.product_id else None
            if product and (product.code or "").strip():
                codes.append(str(product.code).strip())
                seen.add(str(product.code).strip())
            msg = (
                s.scalar(
                    select(RawMessage).where(RawMessage.message_id == order.source_message_id)
                )
                if order.source_message_id
                else None
            )
            if msg and getattr(msg, "raw_text", ""):
                for m in re.findall(r"\bYS\d{8,12}\b", str(msg.raw_text)):
                    if m not in seen:
                        seen.add(m)
                        codes.append(m)
            return {
                "order_id": int(order.id),
                "order_no": order.order_no,
                "origin": str(order.origin or ""),
                "product_name": str(order.product_name or ""),
                "product_archive": str(getattr(product, "name", "") or ""),
                "spec": str(order.spec or ""),
                "quantity": int(order.quantity or 1),
                "buyer": str(order.buyer_nick or ""),
                "receiver_name": str(order.receiver_name or ""),
                "receiver_phone": str(order.receiver_phone or ""),
                "receiver_address": str(order.receiver_address or ""),
                "fish_codes": codes,
                "order_url": f"/orders/{order.id}",
            }
    except Exception:  # noqa: BLE001  桥断只降级
        logger.warning("读取渔枢订单事实失败（order_no=%s）", order_no, exc_info=True)
        return {}


def apply_hub_tracking(order_no: str, tracking_no: str, operator: str = "") -> dict:
    """渔集出库核销后把物流单号**回填渔枢订单**（2026-10-15）。

    单号会出现在渔枢「发货页（已推单记录）」的物流单号栏里，群通知 / 语音播报
    / 订单状态推进全部走平台同一套（app.services.push.apply_tracking_from_wms）。
    订单不存在（target_no 不是渔枢订单号）→ 返回空 dict，出库照常成功。
    """
    order_no = str(order_no or "").strip()
    tracking_no = str(tracking_no or "").strip()
    if standalone() or not order_no or not tracking_no:
        return {}
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Order
        from app.services import push as push_service
    except Exception:  # noqa: BLE001
        logger.debug("平台推单服务不可读", exc_info=True)
        return {}
    try:
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.order_no == order_no))
            if not order:
                return {}
            return push_service.apply_tracking_from_wms(s, order, tracking_no, operator=operator)
    except Exception:  # noqa: BLE001  桥断只降级，绝不能拖垮出库核销
        logger.warning("回填渔枢物流单号失败（order_no=%s）", order_no, exc_info=True)
        return {}


def to_fish_rows(rows: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """``[(条码, 变动台数)]`` → ``[(渔货号, 变动台数)]``（2026-10-14）。

    渔枢的商品档案自这次起认的是**渔货号**（商家编码存的就是它），所以推过去的
    也得是渔货号；同一款一口气出了好几台时，变动**合并成一条**再推 ——
    渔枢那边一个商家编码对上来的是一整款货，库存就是这个渔货号的合计台数。

    认不到渔货号的条码**原样留着**（老数据 / 还没补号的货），让它照旧走进
    ``missing`` 里在页面上报出来，别悄悄丢掉。
    """
    moves = [(str(code or "").strip().upper(), int(qty or 0)) for code, qty in (rows or [])]
    moves = [(code, qty) for code, qty in moves if code and qty]
    if not moves:
        return []
    fall_back = lambda: [(code, qty) for code, qty in moves]  # noqa: E731
    try:
        from sqlalchemy import select

        from .db import SessionLocal
        from .models import WmsItem
    except Exception:  # noqa: BLE001
        logger.info("渔集的表不可用（库存沿用条码推送）", exc_info=True)
        return fall_back()
    cache: dict[str, str] = {}
    merged: dict[str, int] = {}
    order: list[str] = []
    try:
        with SessionLocal() as db:
            for code, qty in moves:
                fish = cache.get(code)
                if fish is None:
                    item = db.scalar(select(WmsItem).where(WmsItem.barcode == code))
                    fish = str(item.fish_code or "").strip().upper() if item else ""
                    cache[code] = fish
                key = fish or code
                if key not in merged:
                    merged[key] = 0
                    order.append(key)
                merged[key] += qty
    except Exception:  # noqa: BLE001
        logger.warning("查渔货号失败（库存沿用条码推送）", exc_info=True)
        return fall_back()
    return [(key, merged[key]) for key in order if merged[key]]


def push_stock(rows: list[tuple[str, int]]) -> dict:
    """把渔集的库存变动推给渔枢的商品库存与渔货栈平台库存（2026-10-10）。

    ``rows = [(条码, 变动台数)]`` —— 负数 = 出库减、正数 = 退货召回加。渔集出库核销
    （货真卖了）与买家退货召回（货回来了）各调一次。

    **认货靠渔货号**（2026-10-14）：以前渔枢建档时「商家编码」直接复用渔集的条码，
    按 ``Product.code == 条码`` 认回来 —— 可是条码是「一台一个」，同一款货出两台就
    得推两次、且同款分散在好几个编码上，渔枢那边永远不知道这一款总共还剩多少。
    现在两边统一认渔货号：入口仍是条码（现场扫的就是它），出口换成渔货号并合并。

    这是桥里唯一一个「往平台写」的动作，边界照旧：
      · 独立部署（``WMS_STANDALONE``）直接不写；
      · 平台模块 / 表不在、查询报错 —— 一律返回空摘要并打日志，
        **绝不让渔集的出库动作因为同步失败而失败**（出库是现场动作，库存同步是账）。
    """
    empty: dict = {"updated": 0, "missing": [], "notes": [], "unchanged": []}
    moves = [(str(code or "").strip(), int(qty or 0)) for code, qty in (rows or [])]
    moves = [(code, qty) for code, qty in moves if code and qty]
    if not moves or standalone():
        return empty
    moves = to_fish_rows(moves)
    try:
        from app.services.wms_stock import apply_stock_deltas
    except Exception:  # noqa: BLE001  平台侧没这个模块（独立部署 / 老版本）
        logger.info("渔枢库存同步不可用（跳过）", exc_info=True)
        return empty
    try:
        return apply_stock_deltas(moves)
    except Exception:  # noqa: BLE001  同步失败不影响渔集出库
        logger.warning("渔枢 / 渔货栈库存同步失败（不影响渔集出库）", exc_info=True)
        return empty


def push_price_change(
    fish_code: str,
    *,
    name: str = "",
    selling_price: float = 0.0,
    suggest_price: float = 0.0,
    base_price: float = 0.0,
) -> dict:
    """渔集价格更新 → 渔枢商品档案 + 渔货栈货源 + 「商品价格变动」通知（2026-10-15）。

    渔集侧改价（检测定价 / 编辑 / 改价页）提交后各调一次。边界照 ``push_stock``：
    独立部署直接不推；桥断 / 同步失败只打日志，**绝不让渔集的改价动作失败**。
    """
    empty: dict = {"updated": False, "notice": False, "notes": []}
    code = str(fish_code or "").strip().upper()
    if standalone() or not code:
        return empty
    try:
        from app.services.wms_bridge import sync_price_from_wms
    except Exception:  # noqa: BLE001  平台侧没这个模块（独立部署 / 老版本）
        logger.info("渔枢价格同步不可用（跳过）", exc_info=True)
        return empty
    try:
        return sync_price_from_wms(
            code,
            name=str(name or ""),
            selling_price=float(selling_price or 0.0),
            suggest_price=float(suggest_price or 0.0),
            base_price=float(base_price or 0.0),
        )
    except Exception:  # noqa: BLE001  同步失败不影响渔集改价
        logger.warning("渔枢 / 渔货栈价格同步失败（不影响渔集改价）", exc_info=True)
        return empty


def push_profile_change(
    fish_code: str,
    *,
    brand: str = "",
    model: str = "",
    spec: str = "",
    order_time: str = "",
) -> dict:
    """渔集商品资料（品牌 / 型号 / 规格 / 下单时间）→ 渔枢商品档案 + 渔货栈货源（2026-10-16）。

    边界照 ``push_price_change``：独立部署直接不推；桥断 / 同步失败只打日志，
    **绝不让渔集这边的保存动作失败**。
    """
    empty: dict = {"updated": False, "notes": []}
    code = str(fish_code or "").strip().upper()
    if standalone() or not code:
        return empty
    try:
        from app.services.wms_bridge import sync_profile_from_wms
    except Exception:  # noqa: BLE001  平台侧没这个模块（独立部署 / 老版本）
        logger.info("渔枢商品资料同步不可用（跳过）", exc_info=True)
        return empty
    try:
        return sync_profile_from_wms(
            code, brand=str(brand or ""), model=str(model or ""), spec=str(spec or ""),
            order_time=str(order_time or ""),
        )
    except Exception:  # noqa: BLE001  同步失败不影响渔集保存
        logger.warning("渔枢 / 渔货栈商品资料同步失败（不影响渔集保存）", exc_info=True)
        return empty


def price_sync_note(report: dict | None) -> str:
    """把 ``push_price_change()`` 的摘要翻成一句人话（页面提示用）。没同步到就空串。"""
    if not report:
        return ""
    bits: list[str] = []
    if report.get("updated"):
        bits.append("渔枢 / 渔货栈价格已同步")
    if report.get("notice"):
        bits.append("价格变动通知已发到渔货栈")
    for note in list(report.get("notes") or [])[:2]:
        bits.append(str(note))
    return "；".join(bits)


def hub_sync_note(report: dict | None) -> str:
    """把 ``push_stock()`` 的摘要翻成一句人话（页面提示 / 审计都用它）。

    没有同步到任何商品时返回空串，调用方自己给兜底文案。
    """
    if not report:
        return ""
    bits: list[str] = []
    updated = int(report.get("updated") or 0)
    if updated:
        bits.append(f"渔枢 / 渔货栈库存已同步 {updated} 款")
    missing = [str(x) for x in (report.get("missing") or [])]
    if missing:
        bits.append("这些渔货号在渔枢没认到商品、库存没同步：" + "、".join(missing[:3])
                    + ("…" if len(missing) > 3 else ""))
    unchanged = [str(x) for x in (report.get("unchanged") or [])]
    if unchanged:
        bits.append("、".join(unchanged[:2]) + " 不管理库存，没动")
    return "；".join(bits)


def text_initials(text: str) -> str:
    """汉字 → 拼音首字母（给库位联想用：输 ``ks`` 就能找到「可售仓」）。

    字表在平台侧（``app.services.pinyin``，离线内置，不装 pypinyin）。桥断了 /
    模块不在就返回空串 —— 首字母只是「多一种认法」，没有它照样能按短码与名称找。
    """
    try:
        from app.services.pinyin import initials_of

        return initials_of(str(text or ""), keep_alnum=True)
    except Exception:  # noqa: BLE001  独立部署 / 平台模块不在
        logger.debug("拼音字表不可用（跳过首字母搜索）", exc_info=True)
        return ""


def platform_users(db, keyword: str = "", limit: int = 50) -> list[dict]:
    """拉平台账号列表（给「人员与授权」挑人用）。

    读的是平台的 ``users`` 表 —— **只读不写**：检测仓不维护人员档案，
    账号启用停用归平台管，这里只决定他能不能进检测仓、进来干什么。
    """
    try:
        from app.models import User
        from sqlalchemy import or_, select
    except Exception:  # noqa: BLE001  独立部署 / 平台表不在同一个库
        logger.debug("平台账号表不可读", exc_info=True)
        return []
    stmt = select(User).order_by(User.id.asc())
    kw = keyword.strip()
    if kw:
        like = f"%{kw}%"
        stmt = stmt.where(
            or_(User.username.like(like), User.display_name.like(like))
        )
    try:
        rows = list(db.scalars(stmt.limit(limit)).all())
    except Exception:  # noqa: BLE001
        logger.debug("查询平台账号失败", exc_info=True)
        return []
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "username": str(getattr(row, "username", "") or ""),
                "display_name": str(getattr(row, "display_name", "") or ""),
                "role": str(getattr(row, "role", "") or ""),
                "enabled": bool(getattr(row, "enabled", True)),
            }
        )
    return out
