"""渔货栈订单状态机（电商正向 + 逆向）。

业务口径（2026-10-02 定）：
    渔货栈自己接单、自己管订单；**推单是推给渔枢系统采购**，不是推 1688。
    渔枢系统采购完、发货之后，把物流单号与状态回写给渔货栈，渔货栈接着
    往下流转（已发货 → 已完成），退款也由渔枢系统回写。

正向：
    待确认 pending ──确认接单──▶ 待发货 confirmed ──推给渔枢系统采购──▶ 已推单 pushed
        ──渔枢系统回写物流单号──▶ 已发货 shipped ──买家签收──▶ 已完成 completed
    待发货也能直接发货（2026-10-14「我要录单」）：没上渔货栈的商品不用推单采购，
    确认接单后直接补个物流单号就发走。
逆向：
    已推单 / 已发货 / 已完成 ──申请退款──▶ 退款中 refunding ──退款完成──▶ 已退款 refunded
挂起与终止：
    待确认 / 待发货 ──取消订单──▶ 已关闭 cancelled
    待确认 / 待发货 / 已推单 ──标记异常──▶ 异常单 exception ──异常解除──▶ 待发货

规则只写在这一个文件里：路由、页面、渔枢系统回写都问它「下一步能做什么」，
不许在别处手写状态字符串，也不许跳过校验直接改 status。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------- 状态
S_PENDING = "pending"        # 待确认：单子下来了，等运营确认接单
S_CONFIRMED = "confirmed"    # 待发货：确认接单；录单的货直接发，店铺货可推单采购
S_PUSHED = "pushed"          # 已推单：已推给渔枢系统，等它采购发货
S_SHIPPED = "shipped"        # 已发货：回写了物流单号，包裹在途
S_COMPLETED = "completed"    # 已完成：买家签收，交易成功
S_REFUNDING = "refunding"    # 退款中：退单受理，钱还没退完
S_REFUNDED = "refunded"      # 已退款：退款完成
S_CANCELLED = "cancelled"    # 已关闭：发货前撤单（终态）
S_EXCEPTION = "exception"    # 异常单：卡住了，等人处理

STATUS_LABELS: dict[str, str] = {
    S_PENDING: "待确认",
    S_CONFIRMED: "待发货",
    S_PUSHED: "已推单",
    S_SHIPPED: "已发货",
    S_COMPLETED: "已完成",
    S_REFUNDING: "退款中",
    S_REFUNDED: "已退款",
    S_CANCELLED: "已关闭",
    S_EXCEPTION: "异常单",
}

# 列表页筛选顺序：正向在前，逆向与终态在后
STATUS_ORDER: tuple[str, ...] = (
    S_PENDING, S_CONFIRMED, S_PUSHED, S_SHIPPED, S_COMPLETED,
    S_REFUNDING, S_REFUNDED, S_CANCELLED, S_EXCEPTION,
)

# 终态：不再往下流转（撤单退款完成也算走完）
TERMINAL_STATUSES: frozenset[str] = frozenset({S_COMPLETED, S_REFUNDED, S_CANCELLED})

# 正向「走到哪一步了」，用于画进度条
FORWARD_FLOW: tuple[str, ...] = (S_PENDING, S_CONFIRMED, S_PUSHED, S_SHIPPED, S_COMPLETED)


def status_label(status: str | None) -> str:
    """状态中文名（认不出就原样返回，别让页面显示空白）。"""
    return STATUS_LABELS.get(status or "", status or STATUS_LABELS[S_PENDING])


# --------------------------------------------------------------------- 动作
@dataclass(frozen=True)
class Action:
    """一个状态动作。

    need：执行时必须带上哪些字段（缺了就拒绝，避免留下「已发货但没单号」这种脏数据）。
    """

    key: str
    label: str
    to_status: str
    need: tuple[str, ...] = ()
    # 页面上按钮的强调色：primary 主推 / danger 逆向 / outline 常规
    tone: str = "primary"
    hint: str = ""
    # 这个动作是不是渔枢系统回写触发的（页面按钮不显示，避免人工乱点）
    from_hub: bool = False


# 动作定义：key → Action
ACTIONS: dict[str, Action] = {
    "confirm": Action(
        "confirm", "确认接单", S_CONFIRMED, tone="primary",
        hint="确认这单接了，进入待发货",
    ),
    "push_hub": Action(
        "push_hub", "推给渔枢系统采购", S_PUSHED, tone="primary",
        hint="在渔枢系统建档，由渔枢系统采购并发货",
    ),
    "ship": Action(
        "ship", "标记发货", S_SHIPPED, need=("shipping_no",), tone="primary",
        hint="渔枢系统回写物流单号后自动流转；也能人工补录",
    ),
    "complete": Action(
        "complete", "确认收货", S_COMPLETED, tone="primary",
        hint="买家签收，交易成功",
    ),
    "refund_request": Action(
        "refund_request", "申请退款", S_REFUNDING, tone="danger",
        hint="退单受理，等渔枢系统退款",
    ),
    "refund_finish": Action(
        "refund_finish", "退款完成", S_REFUNDED, tone="danger",
        hint="钱已退给买家，订单收尾",
    ),
    "cancel": Action(
        "cancel", "取消订单", S_CANCELLED, tone="outline",
        hint="发货前取消，订单留档不参与后续",
    ),
    "mark_exception": Action(
        "mark_exception", "标记异常", S_EXCEPTION, tone="outline",
        hint="卡单了（缺货/地址错等），挂起等人处理",
    ),
    "resolve_exception": Action(
        "resolve_exception", "异常解除", S_CONFIRMED, tone="outline",
        hint="处理完了，回到待发货",
    ),
}

# 允许的流转：from_status → {action_key: to_status}
TRANSITIONS: dict[str, dict[str, str]] = {
    S_PENDING: {
        "confirm": S_CONFIRMED,
        "cancel": S_CANCELLED,
        "mark_exception": S_EXCEPTION,
    },
    S_CONFIRMED: {
        # 2026-10-14「我要录单」：没上渔货栈的商品不用推单，确认接单后直接发
        "ship": S_SHIPPED,
        "push_hub": S_PUSHED,
        "cancel": S_CANCELLED,
        "mark_exception": S_EXCEPTION,
    },
    S_PUSHED: {
        "ship": S_SHIPPED,
        "refund_request": S_REFUNDING,
        "mark_exception": S_EXCEPTION,
    },
    S_SHIPPED: {
        "complete": S_COMPLETED,
        "refund_request": S_REFUNDING,
    },
    S_COMPLETED: {
        "refund_request": S_REFUNDING,
    },
    S_REFUNDING: {
        "refund_finish": S_REFUNDED,
    },
    S_EXCEPTION: {
        "resolve_exception": S_CONFIRMED,
        "cancel": S_CANCELLED,
    },
    # 终态：cancelled / refunded 不再流转
    S_CANCELLED: {},
    S_REFUNDED: {},
}


def can(status: str, action: str) -> bool:
    """这个状态下能不能做这个动作。"""
    return action in TRANSITIONS.get(status or S_PENDING, {})


def target_status(status: str, action: str) -> str | None:
    """做完这个动作会落到哪个状态；不能做返回 None。"""
    return TRANSITIONS.get(status or S_PENDING, {}).get(action)


def available_actions(status: str) -> list[Action]:
    """这个状态下能做的动作（按定义顺序），页面拿它渲染按钮。"""
    return [ACTIONS[a] for a in TRANSITIONS.get(status or S_PENDING, {}) if a in ACTIONS]


@dataclass
class TransitionResult:
    """一次流转的结果，交给调用方（路由 / 服务）执行落库。"""

    ok: bool
    action: str = ""
    from_status: str = ""
    to_status: str = ""
    error: str = ""
    # 落库时要一起写的字段（物流单号 / 退款金额 / 各种时间戳）
    fields: dict = field(default_factory=dict)


def plan(status: str, action: str, **data) -> TransitionResult:
    """校验一次流转：能不能做、做了之后落什么状态、顺带写哪些字段。

    只做「判断」，不碰数据库 —— 落库在 src/order/flow.py::apply_action。
    """
    action_obj = ACTIONS.get(action)
    if action_obj is None:
        return TransitionResult(False, error=f"没有这个动作：{action}")
    cur = status or S_PENDING
    to = target_status(cur, action)
    if to is None:
        return TransitionResult(
            False, error=f"「{status_label(cur)}」不能做「{action_obj.label}」"
        )

    missing = [f for f in action_obj.need if not str(data.get(f) or "").strip()]
    if missing:
        names = {"shipping_no": "物流单号", "refund_amount": "退款金额"}
        return TransitionResult(
            False, error=f"「{action_obj.label}」缺：{'、'.join(names.get(m, m) for m in missing)}"
        )

    fields: dict = {}
    if action == "ship":
        no = str(data.get("shipping_no") or "").strip()
        fields.update(
            shipping_no=no,
            shipping_company=(str(data.get("shipping_company") or "").strip() or None),
            shipped_at=_now(),
        )
    elif action == "complete":
        fields["completed_at"] = _now()
    elif action == "confirm":
        fields["confirmed_at"] = _now()
    elif action == "push_hub":
        fields["pushed_at"] = _now()
        if data.get("hub_order_id"):
            fields["hub_order_id"] = int(data["hub_order_id"])
    elif action in ("refund_request", "refund_finish"):
        amount = data.get("refund_amount")
        if amount not in (None, ""):
            # 符号口径（2026-10-03）：与渔枢系统一致 —— 退给客户的钱存负值，
            # 页面 / 接口填的都是正数金额，这里统一转成 -abs，重复执行不会翻符号。
            try:
                fields["refund_amount"] = -abs(float(amount))
            except (TypeError, ValueError):
                fields["refund_amount"] = amount
        if action == "refund_finish":
            fields["refunded_at"] = _now()
    elif action == "cancel":
        fields["cancelled_at"] = _now()

    return TransitionResult(True, action, cur, to, fields=fields)


def _now():
    # 时间口径（2026-10-03）：库里存 UTC，跟渔枢系统同一个 clock，别用本地时间
    from app.timeutil import utc_now  # noqa: PLC0415  延迟导入：避免与 app 侧循环依赖

    return utc_now()
