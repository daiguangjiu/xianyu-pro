"""检测仓自己的角色权限。

**与渔枢系统的角色权限（``app.web.permissions``）没有任何关系：**
平台那套管的是「订单八个板块谁能看」，这里管的是「检测仓谁能建档 / 谁能改价 /
谁能审批核销」。平台管理员在检测仓自动拥有全部权限，其余账号必须在这里分配角色。

权限命名：``<域>.view`` 看、``<域>.manage`` 做。少数高风险动作单独一个权限码
（改价 ``wms.item.price``、盘点核销审批 ``wms.stocktake.approve``、人员授权
``wms.member.manage``），好让主管把它攥在自己手上。
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import WmsRole, WmsRolePerm, WmsUserRole

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------ 权限清单
# (分组, [(权限码, 说明)])
PERM_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "概览",
        (
            ("wms.overview.view", "查看检测仓总览"),
        ),
    ),
    (
        "批次与建档",
        (
            ("wms.batch.view", "查看批次与建档明细"),
            ("wms.batch.manage", "建批次 / 一机一码建档"),
        ),
    ),
    (
        "检测",
        (
            ("wms.inspect.view", "查看检测结果"),
            ("wms.inspect.manage", "提交检测 / 判定等级与出价"),
        ),
    ),
    (
        "标签",
        (
            ("wms.label.view", "查看打印队列"),
            ("wms.label.manage", "打印标签 / 补打 / 贴标复核"),
            # 云打印（2026-10-08）：改账号 / 加机器 / 改模板会把全场的标签都换掉，
            # 单独一把钥匙，别让谁都能动
            ("wms.print.view", "查看打印设置（云打印机与标签模板）"),
            ("wms.print.manage", "维护云打印机与标签模板（含测试打印、清空队列）"),
        ),
    ),
    (
        "库存",
        (
            ("wms.item.view", "查看库存台账"),
            ("wms.item.manage", "货品状态流转（入库 / 调拨 / 报损）"),
            ("wms.item.price", "改价（低于底价需审批）"),
            ("wms.ledger.view", "查看库存账本"),
            # 仓库 / 区 / 货架 / 层 / 位五级的维护（2026-10-08）：加一个仓、改个货架名
            # 都会影响全站下拉选什么，所以单独两把钥匙，别混在货品流转里
            ("wms.warehouse.view", "查看仓库与货架"),
            ("wms.warehouse.manage", "维护仓库 / 区 / 货架（增改删、启停、排序）"),
        ),
    ),
    (
        "出库",
        (
            ("wms.outbound.view", "查看出库单"),
            ("wms.outbound.manage", "建出库单 / 拣货 / 出库核销"),
        ),
    ),
    (
        "报修与拆件",
        (
            ("wms.repair.view", "查看报修工单"),
            ("wms.repair.manage", "开单 / 送修 / 修回 / 关单"),
            ("wms.part.view", "查看拆机件与残值"),
            ("wms.part.manage", "登记残值明细 / 标记变现"),
        ),
    ),
    (
        "盘点",
        (
            ("wms.stocktake.view", "查看盘点单"),
            ("wms.stocktake.manage", "开单 / 扫码 / 结束盘点"),
            ("wms.stocktake.approve", "差异核销审批（主管专属）"),
        ),
    ),
    (
        "规则与报表",
        (
            ("wms.rule.view", "查看分级规则与价格系数"),
            ("wms.rule.manage", "改分级规则与价格系数"),
            ("wms.report.view", "查看报表中心"),
        ),
    ),
    (
        "系统",
        (
            ("wms.member.manage", "人员授权（给账号分配检测仓角色）"),
        ),
    ),
)

ALL_PERMS: tuple[str, ...] = tuple(
    code for _group, items in PERM_GROUPS for code, _label in items
)


def all_perms() -> tuple[str, ...]:
    return ALL_PERMS


def perm_label(code: str) -> str:
    for _group, items in PERM_GROUPS:
        for perm, label in items:
            if perm == code:
                return label
    return code


# ------------------------------------------------------------------------ 默认角色
# code -> (角色名, 权限码集合标记, 说明)
# 集合用「全部 / 只读 / 组合」表达，避免手抄一遍权限码抄漏。
_VIEW_ONLY = tuple(code for code in ALL_PERMS if code.endswith(".view"))

_DEFAULT_ROLES: tuple[dict, ...] = (
    {
        "code": "admin",
        "name": "仓库主管",
        "remark": "检测仓最高权限：含差异核销审批、改价、规则配置与人员授权",
        "perms": ALL_PERMS,
    },
    {
        "code": "inspector",
        "name": "检测员",
        "remark": "扫码检测、定级出价、打印标签与贴标复核",
        "perms": _VIEW_ONLY
        + (
            "wms.inspect.manage",
            "wms.label.manage",
        ),
    },
    {
        "code": "keeper",
        "name": "仓管",
        "remark": "建档、库存流转、出库、盘点、报修送修（不含核销审批与改价）",
        "perms": _VIEW_ONLY
        + (
            "wms.batch.manage",
            "wms.label.manage",
            "wms.item.manage",
            # 建仓 / 加货架是仓管的日常活（2026-10-08 新增的仓库与货架维护）
            "wms.warehouse.manage",
            # 现场换打印机、换标签版式也是仓管说了算
            "wms.print.manage",
            "wms.outbound.manage",
            "wms.repair.manage",
            "wms.part.view",
            "wms.stocktake.manage",
        ),
    },
    {
        "code": "finance",
        "name": "财务",
        "remark": "看出入库与报价，管报价与残值变现",
        "perms": _VIEW_ONLY + ("wms.item.price", "wms.part.manage"),
    },
    {
        "code": "viewer",
        "name": "只读",
        "remark": "只能看，不能动任何货",
        "perms": _VIEW_ONLY,
    },
)


# ------------------------------------------------------------------------ 角色读写
def ensure_default_roles(db: Session) -> dict[str, WmsRole]:
    """内置角色兜底（幂等）：改内置角色的权限时**只补不删**，人工加的权限不丢。"""
    out: dict[str, WmsRole] = {}
    for idx, spec in enumerate(_DEFAULT_ROLES):
        row = db.scalar(select(WmsRole).where(WmsRole.code == spec["code"]))
        if row is None:
            row = WmsRole(
                code=spec["code"],
                name=spec["name"],
                remark=spec["remark"],
                builtin=True,
                sort=idx * 10,
            )
            db.add(row)
            db.flush()
        have = {
            p
            for p in db.scalars(
                select(WmsRolePerm.perm).where(WmsRolePerm.role_id == row.id)
            ).all()
        }
        for perm in dict.fromkeys(spec["perms"]):  # 去重：同一个权限码只插一次
            if perm not in have:
                have.add(perm)
                db.add(WmsRolePerm(role_id=row.id, perm=perm))
        out[spec["code"]] = row
    db.commit()
    return out


def roles(db: Session) -> list[WmsRole]:
    return list(
        db.scalars(select(WmsRole).order_by(WmsRole.sort.asc(), WmsRole.id.asc())).all()
    )


def perms_of_role(db: Session, role_id: int) -> set[str]:
    return set(
        db.scalars(select(WmsRolePerm.perm).where(WmsRolePerm.role_id == role_id)).all()
    )


def save_role(
    db: Session,
    *,
    role_id: int = 0,
    code: str = "",
    name: str = "",
    remark: str = "",
    perms: list[str] | set[str] = (),
) -> WmsRole:
    row = db.get(WmsRole, role_id) if role_id else None
    if row is None:
        code = code.strip()
        if not code:
            raise ValueError("角色标识不能为空")
        if db.scalar(select(WmsRole).where(WmsRole.code == code)):
            raise ValueError(f"角色标识 {code} 已存在")
        row = WmsRole(code=code, sort=len(roles(db)) * 10)
        db.add(row)
        db.flush()
    if name:
        row.name = name.strip()
    if remark or not row.remark:
        row.remark = remark.strip()
    if not row.code:
        row.code = code.strip()

    wanted = {p for p in perms if p in ALL_PERMS}
    if row.builtin and not wanted:
        # 内置角色不允许被清空（把主管清成没有任何权限，等于把检测仓锁死）
        raise ValueError("内置角色必须保留至少一个权限")
    have = perms_of_role(db, row.id)
    for perm in wanted - have:
        db.add(WmsRolePerm(role_id=row.id, perm=perm))
    for perm in have - wanted:
        db.execute(
            delete(WmsRolePerm).where(
                WmsRolePerm.role_id == row.id, WmsRolePerm.perm == perm
            )
        )
    db.commit()
    return row


def delete_role(db: Session, role_id: int) -> None:
    row = db.get(WmsRole, role_id)
    if row is None:
        return
    if row.builtin:
        raise ValueError("内置角色不能删除（可以改它的权限）")
    db.execute(delete(WmsRolePerm).where(WmsRolePerm.role_id == role_id))
    db.execute(delete(WmsUserRole).where(WmsUserRole.role_id == role_id))
    db.delete(row)
    db.commit()


# ------------------------------------------------------------------------ 人员授权
def perms_of(db: Session, user_id: int, is_admin: bool = False) -> set[str]:
    """某人当前的权限集合（多个角色取并集）。平台管理员 = 全部。

    2026-10-11 起还要并上**渔枢系统「角色权限」页**里勾的渔集权限：三套子系统的
    菜单级权限在那儿一块儿配，这里与检测仓自己的「人员授权」取并集（两边都能加）。
    """
    if is_admin:
        return set(ALL_PERMS)
    rows = db.scalars(
        select(WmsUserRole.role_id).where(WmsUserRole.user_id == user_id)
    ).all()
    out: set[str] = set()
    for role_id in rows:
        out |= perms_of_role(db, int(role_id))
    try:
        from .bridge import hub_role_perms  # noqa: PLC0415

        out |= hub_role_perms(int(user_id), "wms")
    except Exception as exc:  # noqa: BLE001  桥断了只降级，不把人锁在门外
        logger.debug("读取渔枢角色权限失败（user=%s）：%s", user_id, exc)
    return out


def role_names_of(db: Session, user_id: int) -> list[str]:
    rows = db.scalars(
        select(WmsUserRole.role_id).where(WmsUserRole.user_id == user_id)
    ).all()
    names: list[str] = []
    for role_id in rows:
        row = db.get(WmsRole, int(role_id))
        if row is not None:
            names.append(row.name)
    return names


def grant(
    db: Session,
    *,
    user_id: int,
    username: str = "",
    display_name: str = "",
    role_ids: list[int],
    actor: str = "",
) -> None:
    """给账号设置角色（全量覆盖该账号的检测仓角色）。"""
    db.execute(delete(WmsUserRole).where(WmsUserRole.user_id == user_id))
    for role_id in dict.fromkeys(int(r) for r in role_ids):
        if db.get(WmsRole, role_id) is None:
            continue
        db.add(
            WmsUserRole(
                user_id=int(user_id),
                username=username,
                display_name=display_name,
                role_id=role_id,
                granted_by=actor,
            )
        )
    db.commit()


def revoke(db: Session, user_id: int) -> None:
    db.execute(delete(WmsUserRole).where(WmsUserRole.user_id == user_id))
    db.commit()


def has(perms: set[str], code: str | None) -> bool:
    if not code:
        return True
    return code in perms
