"""货品生命周期状态机 —— 唯一定义处。

**任何地方都不许手写状态串**：新增流转只能改这里的 ``TRANSITIONS``，
跳步（PENDING 直接 SOLD）由它挡住。所有流转都会写库存流水 + 审计日志（在调用方写）。

10 个状态 + 召回（RECALL）。终态（SOLD / HAZARD）不可逆，且终态之间互不流转。
"""
from __future__ import annotations

from .models import (
    GRADE_LABELS,
    GRADE_ROUTE,
    GRADE_S,
    GRADE_A,
    NOT_SELLABLE_GRADES,
    SELLABLE_GRADES,
    STATUS_CHOICES,
    STATUS_GRADED,
    STATUS_HAZARD,
    STATUS_LABELS,
    STATUS_LOCKED,
    STATUS_OUT_REPAIR,
    STATUS_PARTS,
    STATUS_PENDING,
    STATUS_RECALL,
    STATUS_REPAIR,
    STATUS_REWORK,
    STATUS_SELLABLE,
    STATUS_SOLD,
    NO_OUTBOUND_STATUSES,
    TERMINAL_STATUSES,
    WH_HAZARD,
)

# 源状态 -> 允许的目标状态（白名单，不在表里的跳转一律拒绝）
TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_PENDING: (STATUS_GRADED, STATUS_HAZARD),
    STATUS_GRADED: (STATUS_SELLABLE, STATUS_REPAIR, STATUS_OUT_REPAIR, STATUS_PARTS, STATUS_HAZARD),
    STATUS_SELLABLE: (STATUS_LOCKED, STATUS_REWORK, STATUS_REPAIR, STATUS_OUT_REPAIR,
                      STATUS_PARTS, STATUS_HAZARD),
    STATUS_LOCKED: (STATUS_SELLABLE, STATUS_SOLD, STATUS_REWORK),
    STATUS_SOLD: (STATUS_RECALL,),
    # 修回 / 撤单回仓：一律先回复检池重新检测定级，**不得沿用送修前等级**
    STATUS_REPAIR: (STATUS_GRADED, STATUS_REWORK, STATUS_PARTS, STATUS_HAZARD),
    STATUS_OUT_REPAIR: (STATUS_GRADED, STATUS_REWORK, STATUS_PARTS, STATUS_HAZARD),
    STATUS_REWORK: (STATUS_PENDING, STATUS_GRADED, STATUS_PARTS, STATUS_HAZARD),
    STATUS_RECALL: (STATUS_PENDING, STATUS_PARTS, STATUS_HAZARD),
    # 终态：不可流出
    STATUS_PARTS: (),
    STATUS_HAZARD: (),
}

# 触发动作：页面按钮与审计日志按它翻译（key -> 文案）
ACTIONS: dict[tuple[str, str], str] = {
    (STATUS_PENDING, STATUS_GRADED): "提交检测并定级",
    (STATUS_PENDING, STATUS_HAZARD): "命中安全红线转危废",
    (STATUS_GRADED, STATUS_SELLABLE): "贴标复核通过入库",
    (STATUS_GRADED, STATUS_REPAIR): "B 档开维修工单",
    (STATUS_GRADED, STATUS_OUT_REPAIR): "C 档发起报修",
    (STATUS_GRADED, STATUS_PARTS): "D 档判报废",
    (STATUS_GRADED, STATUS_HAZARD): "判危废",
    (STATUS_SELLABLE, STATUS_LOCKED): "订单锁货",
    (STATUS_SELLABLE, STATUS_REWORK): "退回复检池",
    (STATUS_SELLABLE, STATUS_REPAIR): "转厂修",
    (STATUS_SELLABLE, STATUS_OUT_REPAIR): "转报修",
    (STATUS_SELLABLE, STATUS_PARTS): "转拆机件",
    (STATUS_SELLABLE, STATUS_HAZARD): "判危废",
    (STATUS_LOCKED, STATUS_SELLABLE): "取消订单释放",
    (STATUS_LOCKED, STATUS_SOLD): "出库核销",
    (STATUS_LOCKED, STATUS_REWORK): "退回复检池",
    (STATUS_SOLD, STATUS_RECALL): "买家退货召回",
    (STATUS_REPAIR, STATUS_GRADED): "修好复检定级",
    (STATUS_REPAIR, STATUS_REWORK): "修回待重检",
    (STATUS_REPAIR, STATUS_PARTS): "修不好转拆件",
    (STATUS_REPAIR, STATUS_HAZARD): "转危废",
    (STATUS_OUT_REPAIR, STATUS_GRADED): "官方修回复检",
    (STATUS_OUT_REPAIR, STATUS_REWORK): "修回 / 撤单回仓待重检",
    (STATUS_OUT_REPAIR, STATUS_PARTS): "官方判定不可修",
    (STATUS_OUT_REPAIR, STATUS_HAZARD): "撤单转危废",
    (STATUS_REWORK, STATUS_PENDING): "退回重检",
    (STATUS_REWORK, STATUS_GRADED): "复检定级",
    (STATUS_REWORK, STATUS_PARTS): "转拆件",
    (STATUS_REWORK, STATUS_HAZARD): "转危废",
    (STATUS_RECALL, STATUS_PENDING): "召回后重检",
    (STATUS_RECALL, STATUS_PARTS): "召回转拆件",
    (STATUS_RECALL, STATUS_HAZARD): "召回转危废",
}


def can_transition(src: str, dst: str) -> bool:
    """能不能从 src 流到 dst（终态不可逆 —— 只有买家退货召回这一条售后路径例外）。"""
    src = str(src or "").upper()
    dst = str(dst or "").upper()
    if src == dst:
        return True
    if src in TERMINAL_STATUSES:
        # 终态不可逆，但**买家退货召回**是例外（2026-10-10）：
        # ``TRANSITIONS`` 里本来就写着 SOLD → RECALL，这里以前一刀切拦掉了 —— 结果
        # 「买家退货」这个动作根本登记不了，退回来的货既进不了召回池、库存也没法加回去。
        return src == STATUS_SOLD and dst == STATUS_RECALL
    return dst in TRANSITIONS.get(src, ())


def action_label(src: str, dst: str) -> str:
    """流转动作的中文名（审计日志 / 提示语用）。"""
    return ACTIONS.get((str(src or "").upper(), str(dst or "").upper()), f"{src} → {dst}")


def allowed_targets(src: str) -> tuple[str, ...]:
    """这个状态下一步能去哪（页面渲染按钮用）。"""
    return TRANSITIONS.get(str(src or "").upper(), ())


def transition_error(src: str, dst: str) -> str:
    """跳步时给检测员看的可执行提示（不通过就抛 ValueError）。"""
    src = str(src or "").upper()
    dst = str(dst or "").upper()
    if src in TERMINAL_STATUSES:
        if src == STATUS_SOLD:
            return "「已售」是终态，只有买家退货召回（已售 → 召回）能出这一档"
        return f"「{STATUS_LABELS.get(src, src)}」是终态，不能再流转"
    allowed = TRANSITIONS.get(src, ())
    names = " / ".join(STATUS_LABELS.get(s, s) for s in allowed) or "（无）"
    return f"不允许「{STATUS_LABELS.get(src, src)}」直接到「{STATUS_LABELS.get(dst, dst)}」，下一步只能是：{names}"


def assert_transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise ValueError(transition_error(src, dst))


def route_of(grade: str) -> tuple[str, str]:
    """等级 → 默认（仓库, 状态）。定级后按这条路由入仓。"""
    return GRADE_ROUTE.get(str(grade or "").upper(), (WH_HAZARD, STATUS_HAZARD))


def can_outbound(status: str, warehouse_can_out: bool = True) -> tuple[bool, str]:
    """能不能出库：待检 / 返工 / 危废 / 召回 / 已售一律拒，仓本身禁出也拒。

    这是风控清单里最容易出事的一条 —— 销售出库只校验「能不能卖」不校验状态，
    A 档当 S 档发出去就是几百块差价 + 必然纠纷。
    """
    status = str(status or "").upper()
    if status in NO_OUTBOUND_STATUSES:
        return False, f"「{STATUS_LABELS.get(status, status)}」状态的货品禁止出库"
    if status not in (STATUS_SELLABLE, STATUS_LOCKED, STATUS_PARTS, STATUS_OUT_REPAIR):
        return False, f"「{STATUS_LABELS.get(status, status)}」状态不能直接出库"
    if not warehouse_can_out:
        return False, "所在仓不允许出库（危废区 / 待检仓等）"
    return True, ""


def can_sell(grade: str, status: str) -> tuple[bool, str]:
    """销售出库专用：可售档（N / S / A / B）的可售或已锁货能卖。"""
    grade = str(grade or "").upper()
    status = str(status or "").upper()
    if grade not in SELLABLE_GRADES:
        return False, f"{grade} 档不可销售（销售出库只认 N / S / A / B 档）"
    if status not in (STATUS_SELLABLE, STATUS_LOCKED):
        return False, f"「{STATUS_LABELS.get(status, status)}」状态不能销售出库"
    return True, ""


def can_enter_sale(grade: str) -> tuple[bool, str]:
    """能不能进可售仓 / 置成「可售」状态：**报修（C）与报废（D）不行**。

    2026-10-13 用户拍板：报修 / 报废的货不许进可售卖仓。以前移库与改状态两处都没
    校验 —— 一台报修中的货能被手工搬进可售仓，渔枢那边一扫条码就当成能卖的货建了档。

    只拦已经判成 C / D 的货：W（未定级）那些还轮不到它卖，但入仓动线里允许先
    摆到可售仓的货架上等检测，别把人的活路堵了。
    """
    grade = str(grade or "").upper()
    if grade in NOT_SELLABLE_GRADES:
        return False, (f"{GRADE_LABELS.get(grade, grade)} 档的货不能进可售仓"
                       f"（报修 / 报废的只能待在报修仓、拆机件仓）")
    return True, ""


def is_terminal(status: str) -> bool:
    return str(status or "").upper() in TERMINAL_STATUSES


def choices() -> tuple[str, ...]:
    return STATUS_CHOICES
