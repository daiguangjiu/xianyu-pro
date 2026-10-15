"""数据模型：原始消息、订单、订单截图、推单批次。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    and_,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .portals import PORTAL_HUB, PORTAL_KEYS, normalize_portals
from .timeutil import now as _now

# 撤回订单的展示文案（群里撤回模板消息 / 订单截图后打的标记）
RECALL_LABEL = "撤回订单"

# —— 订单状态（订单生命周期，2026-09 口径）——
# 新建：飞书群同步订单、刚归档出来的订单；
# 已推单：运营复制推单信息同步给上游（供应商）之后；
# 已发货：上游返回物流单号、运营回填到订单之后；
# 已撤单：还没发货前，卖家或运营把订单撤回 / 撤掉；
# 已退单：未发货时群里同步退单（已发货要求退单则不同意，让用户收到货后再退）。
ORDER_STATE_NEW = "new"
ORDER_STATE_PUSHED = "pushed"
ORDER_STATE_SHIPPED = "shipped"
# 已完成（正向终态，2026-10-03 补）：已发货 + 买家签收 / 超时自动确认收货之后落到这里。
# 有了它结算才能分「在途（已发货未签收）」与「可结（已完成）」—— 以前到「已发货」就停了。
ORDER_STATE_COMPLETED = "completed"
# —— 逆向退单：买家申请退单 -> 已同步供应商退单 -> 已退款 -> 关闭完成 ——
ORDER_STATE_REFUND_REQUEST = "refund_request"
ORDER_STATE_REFUND_SYNCED = "refund_synced"
ORDER_STATE_REFUNDED = "refunded"
# 关闭完成：逆向流程的终态 —— 该退的钱退完（已付款的：供应商已退款；未付款的：不再付款），
# 订单不再参与任何后续流程（推单 / 统计 / 结算）
ORDER_STATE_CLOSED = "closed"
ORDER_STATE_CANCELED = "canceled"

# —— 订单来源（Order.origin）——
# 绝大多数订单来自飞书群里的订单模板消息（origin 留空）；
# 渔货栈店铺出单的，由 services/dso_sync.py 在渔枢系统自动建档，标 "self"。
ORDER_ORIGIN_GROUP = ""
ORDER_ORIGIN_SELF = "self"
ORDER_ORIGIN_LABELS = {ORDER_ORIGIN_SELF: "渔货栈"}

ORDER_STATE_LABELS = {
    ORDER_STATE_NEW: "新建",
    ORDER_STATE_PUSHED: "已推单",
    ORDER_STATE_SHIPPED: "已发货",
    ORDER_STATE_COMPLETED: "已完成",
    ORDER_STATE_REFUND_REQUEST: "买家申请退单",
    ORDER_STATE_REFUND_SYNCED: "已同步供应商退单",
    ORDER_STATE_REFUNDED: "已退款",
    ORDER_STATE_CLOSED: "关闭完成",
    ORDER_STATE_CANCELED: "已撤单",
}
# 页面下拉 / 筛选的顺序
ORDER_STATE_CHOICES = (
    ORDER_STATE_NEW,
    ORDER_STATE_PUSHED,
    ORDER_STATE_SHIPPED,
    ORDER_STATE_COMPLETED,
    ORDER_STATE_REFUND_REQUEST,
    ORDER_STATE_REFUND_SYNCED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_CLOSED,
    ORDER_STATE_CANCELED,
)

# 退单处理状态（值 = 库里存的，不要随便改；文案在下面的 LABELS）
REFUND_STATUS_PENDING = "pending"  # 该拦截：已推单未发货（撤回发货）/ 已发货未收货（物流拦截）
REFUND_STATUS_INTERCEPTED = "intercepted"  # 已拦截
REFUND_STATUS_SHIPPED = "shipped"  # 已发货，拦不住了（人工判的，不按路径自动给）
REFUND_STATUS_RECEIVED = "received"  # 客户已收货：拦不了，等客户寄回
REFUND_STATUS_REFUNDED = "refunded"  # 已退款给客户
REFUND_STATUS_NOT_PUSHED = "not_pushed"  # 还没推给供应商：根本没有包裹可拦
REFUND_STATUS_CLOSED = "closed"  # 关闭完成（终态）

# 退单处理状态 -> 页面展示文案（"" 不在这里配：退单订单没记处理状态时，
# 展示按「推到哪一步了」派生 —— 见 Order.refund_status_label，不再一律说成待拦截）
REFUND_STATUS_LABELS = {
    REFUND_STATUS_PENDING: "待拦截",
    REFUND_STATUS_INTERCEPTED: "已拦截",
    REFUND_STATUS_SHIPPED: "已发货无法拦截",
    REFUND_STATUS_RECEIVED: "已收货，等客户寄回",
    REFUND_STATUS_REFUNDED: "已退款",
    REFUND_STATUS_NOT_PUSHED: "无需拦截（未推单）",
    REFUND_STATUS_CLOSED: "已关闭",
}
# 没记处理状态时的兜底文案（老数据：只落了 order_state，没落 refund_status）
REFUND_STATUS_FALLBACK = "待拦截"
# 退单处理状态的全部取值（下拉、筛选共用一份；"" 不在里面 —— 它表示「不是退单」）
REFUND_STATUS_CHOICES = (
    REFUND_STATUS_PENDING,
    REFUND_STATUS_INTERCEPTED,
    REFUND_STATUS_SHIPPED,
    REFUND_STATUS_RECEIVED,
    REFUND_STATUS_REFUNDED,
    REFUND_STATUS_NOT_PUSHED,
    REFUND_STATUS_CLOSED,
)

# —— 退单订单的统一口径（2026-09 整理）——
# 订单是不是退单，只看两件事：refund_status 记了处理状态，或 order_state 已进入退单流程。
# 页面筛选、统计、推单、资金结算都走这一个判定，不要再各写各的。
# 「关闭完成」是退单 / 撤单走完之后的终态，同样算逆向订单（不参与正向统计）。
REFUND_ORDER_STATES = (
    ORDER_STATE_REFUND_REQUEST,
    ORDER_STATE_REFUND_SYNCED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_CLOSED,
)
# 待拦截 = 退单订单里还没拦截/退款完的（refund_status 为 pending，或老数据没记处理状态）
REFUND_PENDING_STATUSES = ("", "pending")
# 钱已经退给买家的处理状态（退款流水据此自动核销，见 services/finance.sync_refund）
REFUND_SETTLED_STATUSES = ("refunded", "closed")

# —— 资金结算（公司自营应收 / 公司自营应付 / 退款给买家）——
SETTLEMENT_INITIATOR = "initiator"
SETTLEMENT_SUPPLIER = "supplier"
# 退款（2026-10-01 加）：退单受理后要退给买家的钱。方向与应付一样是流出，
# 但对象是买家不是供应商，混在一起就没法对账（「这笔钱到底给谁了」），所以单独一类。
SETTLEMENT_REFUND = "refund"
SETTLEMENT_KINDS = (SETTLEMENT_INITIATOR, SETTLEMENT_SUPPLIER, SETTLEMENT_REFUND)
SETTLEMENT_KIND_LABELS = {
    SETTLEMENT_INITIATOR: "公司自营应收",
    SETTLEMENT_SUPPLIER: "公司自营应付",
    SETTLEMENT_REFUND: "退款 · 退买家",
}
# 资金方向：in 应收（钱进公司）| out 应付（钱付给上游供应商）/ 退款（钱退给买家）
SETTLEMENT_DIRECTIONS = {
    SETTLEMENT_INITIATOR: "in",
    SETTLEMENT_SUPPLIER: "out",
    SETTLEMENT_REFUND: "out",
}

SETTLEMENT_PENDING = "pending"
SETTLEMENT_SETTLED = "settled"
SETTLEMENT_VOID = "void"
SETTLEMENT_STATUSES = (SETTLEMENT_PENDING, SETTLEMENT_SETTLED, SETTLEMENT_VOID)
SETTLEMENT_STATUS_LABELS = {
    SETTLEMENT_PENDING: "待结算",
    SETTLEMENT_SETTLED: "已结算",
    SETTLEMENT_VOID: "已作废",
}

# 聚合方式：按笔 / 按天 / 按月
SETTLEMENT_GRANULARITIES = ("order", "day", "month")
SETTLEMENT_GRANULARITY_LABELS = {"order": "按笔", "day": "按天", "month": "按月"}


class RawMessage(Base):
    """从飞书群同步到的原始消息。"""

    __tablename__ = "raw_messages"
    __table_args__ = (Index("ix_raw_chat_time", "chat_id", "msg_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    chat_name: Mapped[str] = mapped_column(String(128), default="")
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    msg_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sender_id: Mapped[str] = mapped_column(String(64), default="")
    # 发送人姓名（飞书通讯录查得到就存名字，查不到留空，页面退回显示 open_id 短串）
    sender_name: Mapped[str] = mapped_column(String(128), default="")
    sender_type: Mapped[str] = mapped_column(String(32), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    content_json: Mapped[str] = mapped_column(Text, default="{}")

    # pending | parsed | ignored | failed | filtered | duplicate（内容与已归档订单一致）
    parse_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    parse_error: Mapped[str] = mapped_column(Text, default="")
    # 被过滤 / 忽略的人话原因（消息页直接展示，便于一眼看出为什么没归档）
    ignore_reason: Mapped[str] = mapped_column(Text, default="")
    # 该消息挂靠到的文本消息（截图归属）
    parent_message_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    # 消息类别：template(模板消息) | screenshot(订单截图) | other(其他，已过滤)
    kind: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 归档出的订单，便于消息页直达
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 回执通知：""(未通知) | sent | failed | skipped
    notify_status: Mapped[str] = mapped_column(String(16), default="")
    notify_error: Mapped[str] = mapped_column(Text, default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 回执类型：receipt 归档 | refund 退单 | cancel 撤单 | duplicate 重复 | intent 意图提示
    # 挂起的回执补发时要按原类型重新渲染（否则退单回执会被写成「订单已收到」）
    notify_kind: Mapped[str] = mapped_column(String(16), default="")

    # 模板消息缺少配套订单截图时的回群提醒（一条模板只提醒一次）
    missing_image_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 该消息在群里已被撤回：对应订单只留档，不参与统计与后续流程
    is_recalled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 意图识别（不看固定格式，按「动作意图」判断这条消息要干什么）——
    # ""（无意图）| refund 退单 | cancel 撤单
    intent: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 意图判定的说明：命中的词、关联到的订单与置信度（消息页直接展示，便于人工核对）
    intent_note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Order(Base):
    """归档后的订单。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("platform", "order_no", name="uq_order_platform_no"),
        Index("ix_order_supplier_status", "supplier_status"),
        Index("ix_order_time", "ordered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), default="xianyu", index=True)
    # 订单主键：有订单截图取截图里的订单编号；没有截图时先落临时编号（TMP-…），
    # 后续补到截图再改写成真实编号（原临时编号保留在 temp_order_no 里备查）
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    temp_order_no: Mapped[str] = mapped_column(String(64), default="")

    # 来源
    source_message_id: Mapped[str] = mapped_column(String(64), default="")
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    chat_name: Mapped[str] = mapped_column(String(128), default="")
    # 上行消息维度：谁在哪个群、什么时候发的（补全到订单字段里，便于列表与详情查阅）
    sender_id: Mapped[str] = mapped_column(String(64), default="")
    sender_name: Mapped[str] = mapped_column(String(128), default="")
    msg_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 交易主体
    buyer_nick: Mapped[str] = mapped_column(String(128), default="")
    receiver_name: Mapped[str] = mapped_column(String(128), default="")
    receiver_phone: Mapped[str] = mapped_column(String(64), default="")
    receiver_address: Mapped[str] = mapped_column(Text, default="")

    # 商品
    product_name: Mapped[str] = mapped_column(String(255), default="")
    spec: Mapped[str] = mapped_column(String(255), default="")
    # 从订单截图里裁出的商品缩略图（相对媒体目录的路径）
    product_image_path: Mapped[str] = mapped_column(String(512), default="")
    # 挂到商品档案（按商品名 + 规格匹配，见 services/product.py）；没配上商品的为空
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    # 金额（单位：元）
    sale_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 成交价（截图：成交价（在支付宝担保账户中））
    deal_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 商品总价（截图：商品总价（已优惠…））
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)
    platform_fee: Mapped[float] = mapped_column(Float, default=0.0)
    # 平台技术服务费是不是人工填的（2026-10-03）：人工填的改售价时不重算，
    # 系统按费率自动算的一律跟着售价重算 —— 否则改了价、手续费还停在老数上。
    platform_fee_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    profit_amount: Mapped[float] = mapped_column(Float, default=0.0)

    # 供应链与资金归属
    supplier_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 出货的闲鱼账号
    xianyu_account: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 回款支付宝归属人（资金归属维度）
    payee_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    supplier_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | pushed | done | cancelled
    # 推给供应商的时间（单笔推单：复制推单信息即标记已推单）
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ——「今日第 x 单」：供应商维度、按订单发送时间排队的号 ——
    # 推单时报给供应商的号（0 = 还没推过）。撤回后单号会重排，靠它对照出变化，便于对账
    push_index: Mapped[int] = mapped_column(Integer, default=0)
    # 撤回 / 撤销撤回后自动修正到的号（0 = 与推单时报的一致）
    push_index_fixed: Mapped[int] = mapped_column(Integer, default=0)
    # 是否转账：paid（已转账）| unpaid（未转账），推单信息里会带上
    transfer_status: Mapped[str] = mapped_column(String(16), default="unpaid")
    # 标记「已转账」的时间：钱付给供应商的时刻，资金结算里「应付」流水的结算时间取它
    # （改回未转账会清空；在资金页手工点的核销以实际点击时间为准）
    transfer_paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 物流单号：供应链发货页维护，更新后自动在群里同步给订单发送人
    tracking_no: Mapped[str] = mapped_column(String(128), default="")
    # 最近一次把物流单号同步到群里的时间
    tracking_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 物流单号被改过几次：**首次填写（空 -> 有值）不算**，之后只允许改 1 次就锁定。
    # 单号一改就会在飞书群里 @ 发送人，来回改会把群里刷屏，也给对账留下多个版本。
    tracking_edit_count: Mapped[int] = mapped_column(Integer, default=0)
    # 物流公司：填单号时按单号规则自动认（顺丰速运 / 圆通速递 …），认不出留空
    tracking_company: Mapped[str] = mapped_column(String(64), default="")

    # —— 老状态字段（2026-09 起只作兼容保留，页面 / 统计 / 结算一律不读它）——
    # 值由 services/order_state.py 单向镜像：退单 = "refund"，其余 = "new"，
    # 这样老备份、老脚本看到的还是「这单是不是退单」，但真口径只有 order_state + refund_status。
    order_status: Mapped[str] = mapped_column(String(16), default="new", index=True)

    # 订单状态（生命周期）：new 新建 | pushed 已推单 | shipped 已发货 | completed 已完成（正向终态）
    #                    | canceled 已撤单 | refunded 已退单
    order_state: Mapped[str] = mapped_column(String(16), default="new", index=True)
    # 已发货时间（上游返回物流单号、运营回填到订单的时间）
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 已完成时间（买家签收 / 超时自动确认收货）：正向终态 completed 的落库时间。
    # 有了它，结算才能把「已发货没签收」标成在途、「已完成」标成可结。
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 订单来源 ——
    # ""：飞书群里的订单模板消息解析归档（绝大多数单都走这条路）；
    # "self"：渔货栈店铺出单，由 services/dso_sync.py 自动建档（不依赖群里那条消息）。
    # 列表「来源」一列按它显示「渔货栈」，免得运营看着一条没有发送人的订单不知道哪来的。
    origin: Mapped[str] = mapped_column(String(16), default="", index=True)

    # —— 重复订单去重 ——
    # 业务指纹：产品 + 收货人 + 手机 + 地址 + 金额 + 供应链 + 闲鱼号 + 回款人 的哈希。
    # 群里重复发同一笔订单时靠它认出「同一单」，内容没变就不重复归档。
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)

    # —— 逆向退单（群里同步「此单退 / 退单」，需及时拦截）——
    # ""（正常订单）| pending 待拦截 | intercepted 已拦截 | shipped 已发货无法拦截 | refunded 已退款
    refund_status: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 识别到退单的时间 / 命中的关键词 / 退单消息
    refund_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refund_keyword: Mapped[str] = mapped_column(String(32), default="")
    refund_source_message_id: Mapped[str] = mapped_column(String(64), default="")
    # 拦截处理：备注与处理人、处理时间
    refund_note: Mapped[str] = mapped_column(Text, default="")
    refund_by: Mapped[str] = mapped_column(String(64), default="")
    refund_handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 退单理由：群里同步退单时必须写清楚为什么退；没写的会回群提醒补充，运营也能手工补
    refund_reason: Mapped[str] = mapped_column(Text, default="")
    # 缺退单理由时回群提醒的时间（只提醒一次，不刷屏）
    refund_reason_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 是否退款：paid（已退款）| unpaid（未退款）—— 与「是否转账」同一套口径，退单流程里由运营维护
    refund_paid: Mapped[str] = mapped_column(String(16), default="unpaid")
    refund_paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 运营把退单同步给供应商的时间（状态落到「已同步供应商退单」）
    refund_supplier_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 逆向流程新增字段（2026-09-29 推单 / 退单口径）——
    # 退款金额：退给用户的钱，默认取成交价（deal_amount，截图里的「成交价（在支付宝担保账户中）」），
    # 运营可按实际退款额手工改（例如只退了一部分）。
    # **符号口径（2026-10-03）：存负值** —— 给出去的钱记负，与「供应商退款」的正值相反；
    # 页面展示取绝对值（标签已经说明方向），毛利公式里直接加这个负值。
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 供应商退款金额：供应商把钱退给我们的金额。可能少于我们退给客户的
    # （比如只肯退成本价、扣掉已发运费），差额就是这一单自己承担的亏损。
    supplier_refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 退单物流单号：用户已收到货、把货寄回来时的退回快递单号（运营维护）
    return_tracking_no: Mapped[str] = mapped_column(String(128), default="")
    # 是否已收货：区分「已发货但没收到（退单要拦截快递）」与「已收到货（退单要退回快递单号）」。
    # 没有这个标记时，系统没法判断该拦截还是该等退回件。
    is_received: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 供应商已退款：paid（供应商已把钱退回来）| unpaid（还没退）。
    # 只有「已付款给供应商」的退单才需要这一步：运营线下核对完手动标记，
    # 标记后订单自动落到「关闭完成」。未付款的单不用等供应商退款（也不许再付款），直接关闭完成。
    supplier_refunded: Mapped[str] = mapped_column(String(16), default="unpaid")
    supplier_refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 关闭完成的时间（逆向流程走完：钱该退的退完、该不付的没付）
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 退单步骤的「事实时间」（2026-10 阶段二）——
    # 已叫停：不推给供应商 / 撤回发货 / 物流拦截 三选一（stopped_action）。
    # 以前靠 refund_status + 订单状态推断，说不清「到底做了没做、什么时候做的」；
    # 现在单独记时间与动作，物流轨迹查到拦截成功时也能自动补上。
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_action: Mapped[str] = mapped_column(String(16), default="")
    # 谁叫停的：运营用户名，或自动来源「物流轨迹」
    stopped_by: Mapped[str] = mapped_column(String(32), default="")
    # 已收到退回件：客户把货寄回来、我们签收的时间。
    # 有了它就不用拿「填了退回单号」近似代替（填单号只说明寄了，不代表收到）。
    return_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 物流轨迹状态（预留：接入物流查询后由接口回写，页面与步骤条直接读）——
    # 正向件：""（未查）| transit 运输中 | signed 已签收 | returning 退回中
    #       | returned 已退回 | intercepted 拦截成功 | failed 异常
    tracking_status: Mapped[str] = mapped_column(String(16), default="")
    tracking_status_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 退回件（客户寄回的那单）："" | transit 运输中 | signed 已签收（= 我们收到退回件）
    return_tracking_status: Mapped[str] = mapped_column(String(16), default="")
    return_tracking_status_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # —— 消息撤回 / 撤单 ——
    # 群里的订单模板消息 / 订单截图被撤回：订单只留档，不参与任何统计与后续流程
    is_recalled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 被撤回的那条消息（模板消息 / 订单截图）与撤回人
    recalled_message_id: Mapped[str] = mapped_column(String(64), default="")
    recalled_by: Mapped[str] = mapped_column(String(64), default="")
    # 撤单来源说明：群里撤回消息 / 群里发消息撤单（命中词）/ 运营手工标记
    recall_reason: Mapped[str] = mapped_column(String(128), default="")

    ordered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 解析元信息
    raw_text: Mapped[str] = mapped_column(Text, default="")
    parse_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 需要人工确认的具体原因（解析存疑点，例如「产品名称」里看着像手机号）
    review_reason: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    images: Mapped[list["OrderImage"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    batch_items: Mapped[list["PushBatchItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def net_profit(self) -> float:
        """毛利（2026-10-03 定稿口径）：

            毛利 = 销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费

        · 成本价 = 自营飞书群同步的成本价（cost_amount）；
        · 订单上「退款给客户」存负值（钱出去）、「供应商退款」存正值（钱进来），
          所以公式里直接相加；非退单订单两项都是 0，自动退化成「销售价 − 运费 − 成本价 − 手续费」；
        · 唯一实现在 ``finance.order_amounts``（毛利只在那儿算一次，页面不会出现两个毛利）；
        · 不再优先取群消息解析的 ``profit_amount`` —— 那是另一个来源的数，口径不统一。
        """
        from .services.finance import order_amounts  # 局部导入：finance 依赖 models，避免循环

        return order_amounts(self)["profit"]

    @property
    def refund_amount_abs(self) -> float:
        """退款金额的**绝对值**：页面展示 / 表单回填一律用它。

        库里按符号口径存负值（钱出去），但页面上「退款给客户 ¥50.00」读起来才顺，
        比较（部分退款、少退多少）也用绝对值，别的地方（毛利公式）才用带符号的原值。
        """
        return round(abs(float(self.refund_amount or 0)), 2)

    @property
    def is_refund(self) -> bool:
        """是否为退单（逆向订单）：见 REFUND_ORDER_STATES 的统一口径。"""
        return bool((self.refund_status or "").strip()) or (
            (self.order_state or "") in REFUND_ORDER_STATES
        )

    @property
    def order_state_label(self) -> str:
        """订单状态展示文案（新建 / 已推单 / 已发货 / 已撤单 / 已退单）。"""
        value = self.order_state or ORDER_STATE_NEW
        return ORDER_STATE_LABELS.get(value, value)

    @property
    def is_shipped(self) -> bool:
        """是否已发货：已回填物流单号即视为已发货。"""
        return self.order_state == ORDER_STATE_SHIPPED or bool(
            (self.tracking_no or "").strip()
        )

    @property
    def is_closed(self) -> bool:
        """是否「关闭完成」（逆向流程终态：钱该退的退完、该不付的没付）。"""
        return (self.order_state or "") == ORDER_STATE_CLOSED

    @property
    def refund_status_label(self) -> str:
        if not self.is_refund:
            return ""
        value = (self.refund_status or "").strip()
        if value in REFUND_STATUS_LABELS:
            return REFUND_STATUS_LABELS[value]
        # 老数据没记处理状态：按「推到哪一步了」派生，别一律说成待拦截 ——
        # 还没推给供应商的单，压根没有包裹可拦（口径同 services/refund.refund_path）
        if self.is_received:
            return REFUND_STATUS_LABELS[REFUND_STATUS_RECEIVED]
        if (
            self.is_shipped
            or self.pushed_at is not None
            or (self.supplier_status or "") in ("pushed", "done")
        ):
            return REFUND_STATUS_FALLBACK
        return REFUND_STATUS_LABELS[REFUND_STATUS_NOT_PUSHED]

    @property
    def recalled_label(self) -> str:
        """撤回标记文案（""= 正常订单）。"""
        return RECALL_LABEL if self.is_recalled else ""


def not_recalled():
    """筛选条件：排除群消息已撤回的订单（撤回订单只留档，不参与统计与后续流程）。"""
    return Order.is_recalled.is_(False)


def is_refund_order():
    """筛选条件：全是退单订单 —— is_refund（Python 版）的 SQL 版，两处口径必须一致。"""
    return or_(Order.refund_status != "", Order.order_state.in_(REFUND_ORDER_STATES))


def is_refund_pending():
    """筛选条件：待拦截的退单 —— 退单里还没拦截/退款完的（含老数据没记处理状态）。"""
    return and_(is_refund_order(), Order.refund_status.in_(REFUND_PENDING_STATUSES))


def not_refund_order():
    """筛选条件：排除退单订单（统计「正常订单」时用，等价于 not order.is_refund）。"""
    return ~is_refund_order()


def refund_in_progress():
    """筛选条件：**还在走退单流程**的单（退单受理 → 拦截 / 退款中 → 已退款，未到关闭完成）。

    跟 is_refund_order() 的区别：那个把「关闭完成」也算作退单（关闭是逆向流程的终态），
    结果正常签收完成的单也会被当成退单。判断「这单还要不要发货 / 要不要催回单」时必须用
    这条：供应链发货页排退单就是用它，否则已关闭完成的单会被一起排掉，正常单也会误伤。
    """
    return and_(
        Order.order_state != ORDER_STATE_CLOSED,
        or_(
            and_(Order.refund_status != "", Order.refund_status != "closed"),
            Order.order_state.in_(REFUND_ORDER_STATES),
        ),
    )


def not_refund_in_progress():
    """筛选条件：排除正在走退单流程的单（已关闭完成的终态保留）。"""
    return ~refund_in_progress()


def pushed_order():
    """筛选条件：已经推给供应商的订单（有推单时间，或推单状态已推进）。"""
    return or_(Order.pushed_at.is_not(None), Order.supplier_status.in_(("pushed", "done")))


def pending_push_order():
    """筛选条件：待推单 —— 还没推给供应商、**且不是撤单 / 退单**的正常订单。

    撤单 / 退单的订单不会再推给供应商：它们的推单状态已经按事实派生为
    「用户撤单，无需推单」「用户退单，无需推单」这类话术（见 push.refund_push_status），
    所以不该再出现在「待推单」里 —— 否则页面上写着「无需推单」、却还挂在待推单列表，
    运营会以为漏了一单没推。

    首页「待推单」、订单页 ?supplier_status=pending、菜单徽标、供应商详情都走这一条口径。
    """
    return and_(
        not_recalled(),
        Order.supplier_status == "pending",
        not_refund_order(),
        or_(Order.order_state.is_(None), Order.order_state != ORDER_STATE_CANCELED),
    )


def awaiting_tracking():
    """筛选条件：待填回单 —— 已推给供应商、但供应商还没回物流单号。

    首页「待供应商回单」、「供应链推单」页顶部提醒、菜单徽标都走这一条口径，
    免得同一个数字在三处各算各的。退单 / 撤回的订单不算（它们不发货了）。
    """
    return and_(pushed_order(), Order.tracking_no == "", not_refund_order(), not_recalled())


class OrderImage(Base):
    """订单截图（飞书图片资源归档 + OCR 文本）。"""

    __tablename__ = "order_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_id: Mapped[str] = mapped_column(String(64), default="")
    file_key: Mapped[str] = mapped_column(String(128), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    local_path: Mapped[str] = mapped_column(String(512), default="")
    mime: Mapped[str] = mapped_column(String(64), default="image/png")
    size: Mapped[int] = mapped_column(Integer, default=0)

    # pending | done | failed | skipped
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    # 疑似订单号：整图数字识别到了长数字、但没在「订单编号」标签旁确认，只提示人工确认
    order_no_hint: Mapped[str] = mapped_column(String(32), default="")
    # 重新识别次数（截图 OCR 没读出订单号时会定时重试，避免无限重试）
    ocr_attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    order: Mapped["Order | None"] = relationship(back_populates="images")


class PushBatch(Base):
    """推单批次：一次推给某个供应商的订单集合。"""

    __tablename__ = "push_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    supplier_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    # draft | pushed | done | cancelled
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    total_sale: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["PushBatchItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class PushBatchItem(Base):
    __tablename__ = "push_batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "order_id", name="uq_batch_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("push_batches.id", ondelete="CASCADE"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    # 供应商反馈的单号
    supplier_order_no: Mapped[str] = mapped_column(String(128), default="")
    tracking_no: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    batch: Mapped["PushBatch"] = relationship(back_populates="items")
    order: Mapped["Order"] = relationship(back_populates="batch_items")


class Supplier(Base):
    """供应商档案：后台维护的基础信息与对接方式（按供应商名称关联订单）。"""

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("name", name="uq_supplier_name"),
        Index("ix_supplier_city", "city"),
        Index("ix_supplier_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 与订单的 supplier_name 对应，用于统计该供应商的历史订单
    name: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 供应商编码：后台自己维护的编号（如 GYS001），用于对账/沟通时快速指认
    code: Mapped[str] = mapped_column(String(64), default="", index=True)
    city: Mapped[str] = mapped_column(String(64), default="")
    # 主营类目，如「数码配件」「家居家纺」
    category: Mapped[str] = mapped_column(String(128), default="")
    contact_name: Mapped[str] = mapped_column(String(64), default="")
    # 联系方式：手机号 / 微信 / 其它
    contact_info: Mapped[str] = mapped_column(String(128), default="")
    # 是否有对接微信群；有群时群名填在 group_name
    has_group: Mapped[bool] = mapped_column(Boolean, default=False)
    group_name: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


SHOP_STATUS_RUNNING = "正常运营"
SHOP_STATUS_CHOICES = (SHOP_STATUS_RUNNING, "暂停运营", "封禁/异常", "已注销")

# 店铺审核状态：渔枢系统端自己建的档案默认「已通过」（台账本来就有人维护）；
# 渔货栈账号自己添加的店铺先是「待审核」，渔枢系统端审核通过后才在渔货栈生效（见 app/services/shop.py）
SHOP_AUDIT_PENDING = "pending"
SHOP_AUDIT_APPROVED = "approved"
SHOP_AUDIT_REJECTED = "rejected"
SHOP_AUDIT_CHOICES = (SHOP_AUDIT_PENDING, SHOP_AUDIT_APPROVED, SHOP_AUDIT_REJECTED)
SHOP_AUDIT_TEXT = {
    SHOP_AUDIT_PENDING: "待审核",
    SHOP_AUDIT_APPROVED: "已通过",
    SHOP_AUDIT_REJECTED: "已驳回",
}

# 店铺类型（闲鱼账号的身份）：三个固定值，编辑页从下拉里选，不许手填别的
SHOP_TYPE_PERSONAL = "个人闲置店铺"
SHOP_TYPE_PERSONAL_SHOP = "个人认证鱼小铺"
SHOP_TYPE_ENTERPRISE_SHOP = "企业认证鱼小铺"
SHOP_TYPE_CHOICES = (SHOP_TYPE_PERSONAL, SHOP_TYPE_PERSONAL_SHOP, SHOP_TYPE_ENTERPRISE_SHOP)
# 默认值：历史店铺（台账导入 / 群里自动补建的）一律按「个人闲置店铺」记
SHOP_TYPE_DEFAULT = SHOP_TYPE_PERSONAL

# 店铺性质（2026-10-11 引入学员）：与上面的「店铺类型」（闲鱼认证身份）是两个维度，
# 各管各的 —— 类型回答「这个号是什么身份」，性质回答「这个店是公司的还是学员的」。
#   自营店铺：公司自己运营的账号（历史店铺、台账导入、普通群模板出单的都算这个）
#   学员店铺：学员自己的店（群里「学员姓名 / 学员店铺名称 / 带教」这套模板出的单）
SHOP_NATURE_SELF_RUN = "自营店铺"
SHOP_NATURE_STUDENT = "学员店铺"
SHOP_NATURE_CHOICES = (SHOP_NATURE_SELF_RUN, SHOP_NATURE_STUDENT)
# 默认值：补列时带 DEFAULT，线上老店铺自动落「自营店铺」，不用再跑迁移脚本
SHOP_NATURE_DEFAULT = SHOP_NATURE_SELF_RUN

# 平台技术服务费：平台结算给公司时按成交额扣的比例，单位 %（0.6 = 0.6%）。
# 挂在店铺上（Shop.service_fee_rate），不同店铺类型可能不一样；店铺没单独设就用这个。
SERVICE_FEE_RATE_DEFAULT = 0.6
# 费率的上下限：填成负数或离谱的数一律回落默认，别把毛利算穿
SERVICE_FEE_RATE_MIN = 0.0
SERVICE_FEE_RATE_MAX = 100.0

# 渔枢系统后台的账号组（权限角色名，见 roles 表）：
# 这个组下的账号是公司自营运营，名下的店铺档案可以直接同步给渔货栈学员（见 app/services/shop.py）
SELF_RUN_ROLE_NAME = "自营账号组"


class Shop(Base):
    """店铺档案（闲鱼自营账号）：台账维护的基础信息 + 历史订单补充的经营信息。

    与订单的关联：``name``（账号名称 = 闲鱼昵称）对应订单的 ``xianyu_account``，
    ``operator``（账号运营人）对应订单的飞书发送人 ``sender_name`` ——
    群里出单、登记表导入时遇到没建档的账号会自动补一条店铺记录。
    """

    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint("name", name="uq_shop_name"),
        Index("ix_shop_operator", "operator"),
        Index("ix_shop_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 账号名称（闲鱼昵称），对应订单的出货闲鱼号
    name: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 账号唯一编号（如 XYSK0001）
    code: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 账号运营人：对应飞书里的发送人（订单 sender_name）
    operator: Mapped[str] = mapped_column(String(64), default="")
    # 账号所属人（挂靠人）
    owner: Mapped[str] = mapped_column(String(64), default="")
    # 绑定手机设备
    device: Mapped[str] = mapped_column(String(64), default="")
    # 经营细分类目（鞋类 / 家电 / 数码…）
    category: Mapped[str] = mapped_column(String(64), default="")
    # 店铺类型：见 SHOP_TYPE_*；历史数据默认「个人闲置店铺」（补列时带默认值，老店铺自动落这个）
    shop_type: Mapped[str] = mapped_column(String(32), default=SHOP_TYPE_DEFAULT, index=True)
    # 店铺性质：见 SHOP_NATURE_*（自营店铺 / 学员店铺）；老店铺补列时自动落「自营店铺」
    shop_nature: Mapped[str] = mapped_column(String(32), default=SHOP_NATURE_DEFAULT, index=True)
    # 所属带教：只有学员店铺才有（带这个学员的老师），从学员模板的「带教：」带过来
    coach: Mapped[str] = mapped_column(String(64), default="")
    # 平台技术服务费率（%，如 0.6 = 0.6%）：平台结算时按成交额扣这笔，
    # 订单的「平台技术服务费」金额 = 成交价 × 这个费率（见 services/finance.py）
    service_fee_rate: Mapped[float] = mapped_column(
        Float, default=SERVICE_FEE_RATE_DEFAULT
    )
    # 绑定手机号
    phone: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default=SHOP_STATUS_RUNNING)
    # 类目 / 供应链（如「京东家电类」「莆田椰子鞋」）
    supply: Mapped[str] = mapped_column(String(128), default="")
    # 支付宝绑定账号
    alipay: Mapped[str] = mapped_column(String(128), default="")
    credit_score: Mapped[str] = mapped_column(String(32), default="")
    # 实名认证人
    real_name: Mapped[str] = mapped_column(String(64), default="")
    # 闲气值 / 鱼力值
    xianqi: Mapped[str] = mapped_column(String(32), default="")
    yuli: Mapped[str] = mapped_column(String(32), default="")
    violation: Mapped[str] = mapped_column(String(32), default="")
    wechat: Mapped[str] = mapped_column(String(128), default="")
    wechat_password: Mapped[str] = mapped_column(String(128), default="")
    wechat_pay_password: Mapped[str] = mapped_column(String(128), default="")
    # 新老账号区分（老账号 / 新账号）
    account_age: Mapped[str] = mapped_column(String(32), default="")
    # 回款支付宝归属人：按历史订单补（订单里的 payee_name）
    payee_name: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    # 会员名：闲鱼「账号与安全」里的会员名（学员在渔货栈建档时填，与账号名称不一定相同）
    member_name: Mapped[str] = mapped_column(String(64), default="")
    # 联系电话（联系运营人本人用）：区别于「绑定手机号」（闲鱼账号绑的那个号）
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    # 实名认证是否完成：学员自助填报的口径（渔枢系统端可在审核时核对）
    real_name_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # 提交人（渔枢系统用户 id）：渔货栈账号自己加的店铺记在名下；0 = 渔枢系统端直接建档，不归属任何渔货栈账号
    owner_user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # 渔货栈店铺已关联：渔枢系统里已有的店铺档案被某个渔货栈账号「带入」时记下关联账号与时间。
    # 与 owner_user_id 的分工：认领渔枢端无主档案时两个都记（归属也转过来）；
    # 只同步不改归属（如自营账号组运营人名下的店）时只记这里，档案归属仍归渔枢系统那边的账号。
    self_linked_user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    self_linked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 审核状态：见 SHOP_AUDIT_*；只有「已通过」的店铺才在渔货栈生效
    audit_status: Mapped[str] = mapped_column(String(32), default=SHOP_AUDIT_APPROVED, index=True)
    # 审核备注（驳回原因等）
    audit_note: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def is_running(self) -> bool:
        return (self.status or "").strip() in ("", SHOP_STATUS_RUNNING)

    @property
    def audit_text(self) -> str:
        return SHOP_AUDIT_TEXT.get(self.audit_status or "", self.audit_status or "")

    @property
    def is_approved(self) -> bool:
        """审核通过 = 在渔货栈生效（渔货栈只认这一种）。"""
        return (self.audit_status or "") == SHOP_AUDIT_APPROVED

    @property
    def service_fee_rate_value(self) -> float:
        """平台技术服务费率（%）：没填 / 填了离谱的数都按默认 0.6 算。"""
        try:
            rate = float(self.service_fee_rate or 0)
        except (TypeError, ValueError):
            return SERVICE_FEE_RATE_DEFAULT
        if rate < SERVICE_FEE_RATE_MIN or rate > SERVICE_FEE_RATE_MAX:
            return SERVICE_FEE_RATE_DEFAULT
        return rate

    @property
    def shop_type_label(self) -> str:
        """店铺类型（展示用）：没记 / 记了不在选项里的老数据，一律按默认「个人闲置店铺」显示。"""
        value = (self.shop_type or "").strip()
        return value if value in SHOP_TYPE_CHOICES else SHOP_TYPE_DEFAULT

    @property
    def shop_nature_label(self) -> str:
        """店铺性质（展示用）：没记 / 记了不在选项里的老数据，一律按默认「自营店铺」显示。"""
        value = (self.shop_nature or "").strip()
        return value if value in SHOP_NATURE_CHOICES else SHOP_NATURE_DEFAULT

    @property
    def is_student_shop(self) -> bool:
        """学员店铺：只有学员店铺才有「所属带教」，页面按这个决定要不要显示带教那一栏。"""
        return self.shop_nature_label == SHOP_NATURE_STUDENT

    @property
    def is_self_submitted(self) -> bool:
        """是否由渔货栈账号自己提交（渔枢系统端台账建档的没有提交人）。"""
        return bool(self.owner_user_id)

    @property
    def is_self_linked(self) -> bool:
        """渔货栈店铺已关联：已有档案被某个渔货栈账号带入 / 同步过（不一定改了归属）。"""
        return bool(self.self_linked_user_id)

    @property
    def self_linked_only(self) -> bool:
        """只关联没改归属：渔货栈能看到、能维护，但档案仍挂在渔枢系统那边的账号名下。"""
        return bool(self.self_linked_user_id) and not self.owner_user_id

    @property
    def real_name_verified_text(self) -> str:
        return "已完成" if self.real_name_verified else "未完成"


class Settlement(Base):
    """资金结算流水：公司自营应收 <-> 订单发起人、公司自营应付 <-> 上游供应商。

    每个订单会生成两条流水（应收 / 应付），金额跟着订单走（订单金额被修正后自动同步）：
      - 公司自营应收：销售金额（买家实付）− 平台技术服务费（0.6%，平台结算时扣除）；
      - 公司自营应付：采购成本 + 运费，需结算给上游供应商。
    撤单 / 已退单的订单不再结算：对应流水置为「已作废」，只留档。
    """

    __tablename__ = "settlements"
    __table_args__ = (
        UniqueConstraint("order_id", "kind", name="uq_settlement_order_kind"),
        Index("ix_settlement_day", "biz_day"),
        Index("ix_settlement_status", "status"),
        Index("ix_settlement_counterparty", "counterparty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # initiator 公司自营应收 | supplier 公司自营应付
    kind: Mapped[str] = mapped_column(String(16), default=SETTLEMENT_INITIATOR, index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 结算对象：订单发起人姓名（应收）或供应商名称（应付）
    counterparty: Mapped[str] = mapped_column(String(128), default="", index=True)
    # in：应收（钱进公司）| out：应付（钱付给上游供应商）
    direction: Mapped[str] = mapped_column(String(8), default="in")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 业务日 / 业务月（按订单出单时间，聚合统计用）
    biz_day: Mapped[str] = mapped_column(String(10), default="", index=True)
    biz_month: Mapped[str] = mapped_column(String(7), default="", index=True)
    # pending 待结算 | settled 已结算 | void 已作废（撤单 / 退单不再结算）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_by: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    order: Mapped["Order | None"] = relationship()

    @property
    def kind_label(self) -> str:
        return SETTLEMENT_KIND_LABELS.get(self.kind, self.kind)

    @property
    def status_label(self) -> str:
        return SETTLEMENT_STATUS_LABELS.get(self.status, self.status)

    @property
    def amount_label(self) -> str:
        """带方向展示：应收 +¥x / 应付 -¥x。"""
        sign = "+" if self.direction == "in" else "-"
        return f"{sign}¥{self.amount:.2f}"


class Role(Base):
    """角色：一个账号能进哪些菜单（2026-09-29 起）。

    粒度定在**菜单**上 —— 菜单就是运营实际感知的「能进哪些页面」，
    也正是 nav.py 里每个 NavItem 的 key（如 orders / products / users）。

    数据级权限先留字段（`data_scope`）不生效：真要按供应商 / 闲鱼号过滤时，
    在 permissions.menu_keys_for 旁边加一层过滤即可，不用再动表结构。
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(String(255), default="")
    # 可见菜单：nav.py 里 NavItem 的 key 列表，如 ["orders", "products", "suppliers"]
    menus: Mapped[list] = mapped_column(JSON, default=list)
    # 页面内区块（细到「同一页面里能看哪些内容」）：permissions.BLOCKS 里的 key，
    # 如 ["order_push"] —— 不勾就看不见订单详情的推单信息。
    # None = 这个角色没单独配过区块（沿用默认：都能看，与加这套权限之前一致）；
    # [] = 配过但一个都不给。两者靠 None / [] 区分，见 web/permissions.py。
    blocks: Mapped[list | None] = mapped_column(JSON, default=None, nullable=True)
    # 预留（数据级）：{"suppliers": [], "shops": [], "own_only": false}，当前只存不生效
    data_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    # 内置角色（管理员 / 子账号默认）不允许删除
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ---------------------------------------------------------------- 账号身份
# 2026-10-11：账号分「内部人员」与「外部学员」—— 外部学员是来学做闲鱼的（带教老师带的
# 学员），第一次进渔货栈要按「建店铺 → 选品铺货 → 下素材 → 出第一单」走一遍引导。
# 系统里已有的账号一律算内部人员（建列时的默认值，老数据不用补）。
IDENTITY_INTERNAL = "internal"   # 内部人员
IDENTITY_STUDENT = "student"     # 外部学员
IDENTITY_CHOICES: tuple[tuple[str, str], ...] = (
    (IDENTITY_INTERNAL, "内部人员"),
    (IDENTITY_STUDENT, "外部学员"),
)


class User(Base):
    """后台登录账号：admin 为管理员，其余为管理员创建的子账号。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 手机号：渔货栈的老账号是按手机号注册的，两端合并成一套账号后，
    # 登录时「用户名 / 手机号都认」（见 app/web/auth.py）。
    # 可空（渔枢系统自己的账号不一定有手机号）—— Postgres 里 NULL 不参与唯一约束，
    # 多个没填手机号的账号也不会互相打架。
    phone: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(64), default="")
    # pbkdf2_sha256$<iterations>$<salt>$<hex>
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    # admin（管理员，可见解析规则/系统设置/日志）| user（子账号）
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    # 角色（可空）：空了就按上面的 role 走老逻辑 —— admin 全菜单、user 默认菜单。
    # 挂上角色后按角色勾选的菜单来（见 app/web/permissions.py）。
    role_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("roles.id"), nullable=True, index=True
    )
    # active | disabled（停用后不能登录）
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 可用端：hub（渔枢系统）/ self（渔货栈），逗号分隔，见 app/portals.py。
    # 管理员不看这个值 —— 一律全端；子账号由管理员在「用户管理」里勾选。
    portals: Mapped[str] = mapped_column(String(64), default=PORTAL_HUB)
    # 身份：internal 内部人员 / student 外部学员（见上面 IDENTITY_CHOICES）。
    # 外部学员第一次进渔货栈会先走「开店 → 选品 → 素材 → 第一单」的引导流程。
    identity: Mapped[str] = mapped_column(String(16), default=IDENTITY_INTERNAL)
    # 从渔枢系统迁过来的账号自带的旧密码哈希（bcrypt）：第一次用老密码登录成功后
    # 会换成这套系统的哈希并清空，老用户不用改密码（见 web/auth.py）
    legacy_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def identity_label(self) -> str:
        """身份的中文名（老账号没值时按内部人员算）。"""
        return dict(IDENTITY_CHOICES).get(
            (self.identity or IDENTITY_INTERNAL), "内部人员"
        )

    @property
    def is_student(self) -> bool:
        """是不是外部学员（渔货栈给他走引导流程）。"""
        return (self.identity or IDENTITY_INTERNAL) == IDENTITY_STUDENT

    @property
    def portal_keys(self) -> tuple[str, ...]:
        """这个账号能进哪些子系统。

        管理员一律全端（不因为漏配就进不去某个端）；子账号按库里勾的端，
        老账号没有这个值时按「渔枢系统」处理。
        """
        if self.is_admin:
            return PORTAL_KEYS
        return normalize_portals(self.portals)

    def has_portal(self, key: str) -> bool:
        """能不能进某个端（端切换、/hub 下的页面都用它拦一道）。"""
        return key in self.portal_keys


class OrderLog(Base):
    """订单处理日志：人工标记 / 改动订单状态的动作，逐单留痕。

    与账号维度的 OperationLog 的分工：OperationLog 记「谁在后台做了什么」，
    这里记「订单上发生过什么」—— 撤单、推单、转账、标记退单 / 拦截等，
    订单详情页「推单信息」下面按时间倒序列出来，查订单问题时一眼看到处理过程。
    """

    __tablename__ = "order_logs"
    __table_args__ = (Index("ix_order_log_order", "order_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 动作：标记撤回 / 标记退单 / 标记拦截 / 标记已推单 / 修改是否转账 …
    action: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 说明：具体变化（如「未转账 → 已转账」「已推单 → 已发货」）
    detail: Mapped[str] = mapped_column(String(500), default="")
    # 操作人：后台账号（群里自动触发的记「系统」或留空）
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ParseFeedback(Base):
    """人工标记：运营在飞书消息页对一条消息下的判断（不是订单 / 其实是订单）。

    人工判断是最准的口径，所以既用来改这条消息对应的订单，也用来「学策略」：
    从消息里提炼出的话术写进 ParsePhrase，下一条同类通告进来就不用再人工标了。
    指纹相同的消息（重复同步 / 被转发）直接按人工结论处理。
    """

    __tablename__ = "parse_feedbacks"
    __table_args__ = (Index("ix_parse_feedback_fp", "fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 内容指纹：同一条消息再进来（重复同步 / 转发）时按人工结论处理
    fingerprint: Mapped[str] = mapped_column(String(40), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    # not_order 人工确认不是订单 | order 人工确认其实是订单
    label: Mapped[str] = mapped_column(String(16), default="", index=True)
    sender_id: Mapped[str] = mapped_column(String(64), default="")
    sender_name: Mapped[str] = mapped_column(String(128), default="")
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    # 标记时关联的订单（误判归档的那笔，标记后已撤单留档）
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 这次学到的话术（JSON 数组，页面上能回看「学到了什么」）
    phrases: Mapped[str] = mapped_column(Text, default="[]")
    operator: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ParsePhrase(Base):
    """解析策略里的话术：人工标记学到 / 手工添加的排除词与订单特征词。

    kind = exclude：命中即判「不是订单」（通告、闲聊的固定说法）；
    kind = force：命中即判「是订单」（人工确认过、规则漏掉的模板写法）。
    排除词只在消息本身不像订单模板（标签行不足）时生效，避免误伤正常订单的备注。
    """

    __tablename__ = "parse_phrases"
    __table_args__ = (UniqueConstraint("kind", "phrase", name="uq_parse_phrase_kind_text"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phrase: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(16), default="", index=True)
    # learn 人工标记学到的 | manual 运营手工添加的
    source: Mapped[str] = mapped_column(String(16), default="learn")
    operator: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ErrorLog(Base):
    """系统异常日志（2026-10-09）：线上出 500 / 接口报错时自动落一条。

    为什么要有：以前报错只有容器日志里那行 ``GET /xxx 500``，滚几屏就没了 ——
    用户在页面上只看到「上传失败」，截图里那句 ``Unexpected token '<'`` 谁都看不懂
    （那是后端 500 返回了 HTML 错误页、前端却按 JSON 解析）。这张表把**谁在什么时候
    点了什么、服务端到底抛了什么、堆栈长什么样**一起存下来，管理员在「异常日志」
    页直接查。三个子系统（渔枢 / 检测仓 / 渔货栈）共用这一张表，靠 ``scope`` 区分。
    """

    __tablename__ = "error_logs"
    __table_args__ = (
        Index("ix_errlog_created", "created_at"),
        Index("ix_errlog_scope", "scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 哪个子系统：app 渔枢 / wms 检测仓 / hub 渔货栈
    scope: Mapped[str] = mapped_column(String(16), default="app", index=True)
    # server 服务端未捕获异常 / client 前端上报（拿到非 JSON 响应、脚本报错）
    kind: Mapped[str] = mapped_column(String(16), default="server", index=True)
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(255), default="", index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=500)
    exc_type: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    traceback: Mapped[str] = mapped_column(Text, default="")
    # 现场上下文：表单字段名 / 查询串 / 前端拿到的报文摘要（都截断，不存文件内容）
    detail: Mapped[str] = mapped_column(Text, default="")
    # 账号快照：账号删了也能追溯
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    ua: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class OperationLog(Base):
    """操作日志：记录后台账号做了什么，便于事后追查问题。"""

    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("ix_oplog_created", "created_at"),
        Index("ix_oplog_action", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 账号快照：账号被删除后日志仍可追溯
    username: Mapped[str] = mapped_column(String(32), default="", index=True)
    role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class VoiceEvent(Base):
    """语音播报事件：后台产生、浏览器端朗读。

    kind：order（群里同步到新订单）| tracking（供应商回单物流更新）
    浏览器登录后轮询 /api/voice/pending 取未播报的事件朗读，
    历史事件由 voice.prune() 定期清理，不影响订单数据。
    """

    __tablename__ = "voice_events"
    __table_args__ = (Index("ix_voice_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="order", index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_no: Mapped[str] = mapped_column(String(64), default="")
    # 最终朗读的文本（生成时按当时的配置渲染好，改文案不影响历史事件）
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class SyncState(Base):
    """每个群的同步水位，避免重复拉取。"""

    __tablename__ = "sync_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chat_name: Mapped[str] = mapped_column(String(128), default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DailySummaryLog(Base):
    """每日出单汇总推送记录：同一天只推一次，重建容器 / 重启服务都不丢。

    按「人」汇总当天出单店铺数、出单数、出单金额、退单数、退单金额后推到群里，
    推送成功按统计日（YYYY-MM-DD）记一条，定时任务据此跳过已推送的日子。
    """

    __tablename__ = "daily_summary_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 统计日（YYYY-MM-DD），唯一 -> 同一天不会被重复推送
    day: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    # 推送到的群（逗号分隔）
    chat_ids: Mapped[str] = mapped_column(Text, default="")
    people: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    refunds: Mapped[int] = mapped_column(Integer, default=0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)


class ProductMerge(Base):
    """统计用的商品合并规则：把取名不一致的同一个商品归到一起看数字。

    只在统计（热销 TOP 榜单）里生效，**不改动订单原始商品名**；删掉规则就恢复原样。
    """

    __tablename__ = "product_merges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 合并后统一显示的商品名
    canonical: Mapped[str] = mapped_column(String(255), default="", index=True)
    # 被合并进去的原始商品名（订单里解析出来的名字）
    member: Mapped[str] = mapped_column(String(255), default="", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Product(Base):
    """商品档案：把订单里的商品名归一到这里，并承载闲鱼上架需要的电商属性。

    与订单的关联方式（见 services/product.py）：
      · name —— 规范商品名，订单里的 product_name 与它相等就直接认上；
      · 订单归档后自动挂到商品上（Order.product_id），商品页就能看这款卖了多少单、赚了多少。
    商品属性按闲鱼的商品维度来留：类目 / 成色 / 库存 / 发货方式 / 售价与划线价 / 商品 ID 与链接。
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("name", name="uq_product_name"),
        Index("ix_product_category", "category"),
        Index("ix_product_status", "status"),
        Index("ix_product_supplier", "supplier_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 规范商品名：订单里的商品名与它一致时直接关联
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    # 商品编码：后台自己维护的编号（如 SPD0001），对账 / 沟通时快速指认
    code: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 商品标签（一行一个）：发布商品时勾选的固定标签，如「源头工厂」「现货速发」。
    # 可选清单维护在 product_tags 表（渔枢系统「商品 · 商品标签」），随上架同步到渔货栈。
    # 与渔货栈那边按数据实时算出来的动态标签（今日上新 / 直降 x 元 / 高毛利 x%）分开：
    # 这里存的是人工认下来的事实，动态标签不落库。
    tags: Mapped[str] = mapped_column(Text, default="")
    # ---------------- 闲鱼电商属性 ----------------
    # 类目，如「数码配件」「家居家纺」
    category: Mapped[str] = mapped_column(String(128), default="")
    brand: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    # 规格（2026-10-15）：与渔集建档的「规格」同一个字段（如 1.75L / 黑色），
    # 渔集那边是商品档案字段、不是规格维度；扫渔集条码建档时一并带过来
    spec: Mapped[str] = mapped_column(String(128), default="")
    # 成色：见 services/product.py 的 CONDITION_CHOICES
    condition: Mapped[str] = mapped_column(String(32), default="")
    # 闲鱼商品 ID 与商品链接（上架后用，便于与平台对上）
    listing_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    listing_url: Mapped[str] = mapped_column(String(512), default="")
    # 售价与划线价（原价）
    price: Mapped[float] = mapped_column(Float, default=0.0)
    original_price: Mapped[float] = mapped_column(Float, default=0.0)
    # 供货成本（采购价），与订单的 cost_amount 口径一致
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 渔货价：渔货栈对外的成本价 / 进货价（我们向渔货栈拿货的价格，区别于供货成本）
    fish_price: Mapped[float] = mapped_column(Float, default=0.0)
    # 库存：-1 表示不管理库存（按单采购）
    stock: Mapped[int] = mapped_column(Integer, default=-1)
    # 库存管理：默认启用（按数量卖）。发布页不再单独给这个开关，
    # 老商品上原有的值原样保留（表单不再提交这个字段）。
    stock_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 发货方式与默认运费：见 SHIPPING_CHOICES
    shipping_mode: Mapped[str] = mapped_column(String(32), default="")
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    # ---------------- 货源与在售账号 ----------------
    # 所属供应链：对应订单的 supplier_name / 供应商档案
    supplier_name: Mapped[str] = mapped_column(String(128), default="")
    # 供应链档案 id：商品属性选「供应链商品」时指定是哪家供应链（与 supplier_name 同步）
    supplier_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # 在售闲鱼号：对应订单的 xianyu_account / 店铺档案
    shop_name: Mapped[str] = mapped_column(String(128), default="")
    image_path: Mapped[str] = mapped_column(String(512), default="")
    # 状态：在售 / 已下架 / 草稿（draft 多为「订单里出现过但还没完善资料」）
    status: Mapped[str] = mapped_column(String(32), default="draft")
    # ---------------- 来源与审核（对齐自营商品的口径）----------------
    # self_operated 自营（自己供货）/ third_party 三方（代发）
    source_type: Mapped[str] = mapped_column(String(32), default="self_operated", index=True)
    # 审核：draft 草稿 / pending 待审核 / approved 已通过 / rejected 已驳回
    audit_status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    audit_note: Mapped[str] = mapped_column(String(255), default="")
    # ---------------- 素材与详情 ----------------
    # 图集（JSON 数组，主图之外的其它图）
    images: Mapped[str] = mapped_column(Text, default="[]")
    # 卖点（一行一个）
    selling_points: Mapped[str] = mapped_column(Text, default="")
    # 图文详情（可直接贴到闲鱼的描述）：多组文案拼在一起的结果，给闲鱼发布包与渔货栈同步用
    detail_html: Mapped[str] = mapped_column(Text, default="")
    # 多组详情文案（JSON：[{"title": "文案一", "content": "…"}]，最多 20 组，按顺序即排序）
    detail_blocks: Mapped[str] = mapped_column(Text, default="[]")
    # 供应商友情提示：渔货栈特色信息（写在图文详情下面），给自营团队选品 / 经营做参考
    supplier_tip: Mapped[str] = mapped_column(Text, default="")
    # ---------------- 履约 ----------------
    freight_template: Mapped[str] = mapped_column(String(64), default="")
    delivery_days: Mapped[str] = mapped_column(String(32), default="")
    after_sale: Mapped[str] = mapped_column(String(255), default="")
    listed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 规格定义（JSON：[{"name": "颜色", "values": ["黑", "白"]}]），SKU 由它生成组合
    spec_defs: Mapped[str] = mapped_column(Text, default="[]")
    # 卖点 / 备注（可直接作为闲鱼商品描述草稿）
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    @property
    def tag_list(self) -> list[str]:
        """商品标签列表（一行一个，去重保序），模板里直接遍历显示。"""
        out: list[str] = []
        for line in (self.tags or "").splitlines():
            text = line.strip()
            if text and text not in out:
                out.append(text)
        return out

    @property
    def image_list(self) -> list[str]:
        """图集（JSON 数组），坏数据按空处理，不让页面挂掉。"""
        import json

        try:
            data = json.loads(self.images or "[]")
        except (TypeError, ValueError):
            return []
        return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []

    @property
    def spec_def_list(self) -> list[dict]:
        """规格定义：[{"name": "颜色", "values": ["黑", "白"]}]，SKU 矩阵的输入。"""
        import json

        try:
            data = json.loads(self.spec_defs or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            values = [str(v).strip() for v in (item.get("values") or []) if str(v).strip()]
            if name and values:
                out.append({"name": name, "values": values})
        return out

    @property
    def selling_point_list(self) -> list[str]:
        return [line.strip() for line in (self.selling_points or "").splitlines() if line.strip()]


class ProductTag(Base):
    """商品标签字典：发布商品时能勾选的那些固定标签（源头工厂 / 现货速发 / 工厂直供 …）。

    与渔货栈那边实时算出来的动态标签（今日上新 / 直降 x 元 / 高毛利 x%）分工：
      · 本表 = 人工认定的事实标签，由运营在「商品 · 商品标签」页维护，发布 / 编辑页勾选；
      · 动态标签不入库，渔货栈按商品数据当场算（见 src/web/router.py 的货源卡片构造）。

    kind 只影响前端展示配色（brand / success / info / danger），语义仍是一个标签。
    """

    __tablename__ = "product_tags"
    __table_args__ = (UniqueConstraint("name", name="uq_product_tag_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 展示配色：brand 金 / success 绿 / info 蓝 / danger 红（见 style.css 的 .ftag）
    kind: Mapped[str] = mapped_column(String(16), default="info")
    # 说明：鼠标悬停时给运营看的一句话（为什么用它 / 什么时候用）
    hint: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 种子标签（系统播种的），删掉了下次启动还会补回来；运营自己加的可以随便删
    seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PurchaseNode(Base):
    """采购节点：货源广场顶部「采购节点」运营位那份活动日历。

    为什么落表（2026-10-04 起）：以前节点写死在 src/goods/catalog.py 的 PURCHASE_NODES
    常量里，改个日期要发版。现在运营在「货源 · 采购节点」页维护日期窗口、图标、关键词、
    启停与排序，渔货栈货源广场每 60 秒读一次，改完立刻生效。

    节点下的商品不写死绑定：靠 keywords 在货源标题里实时匹配（空 = 不限关键词）。
    """

    __tablename__ = "purchase_nodes"
    __table_args__ = (UniqueConstraint("node_key", name="uq_purchase_node_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 稳定标识（mid_autumn / double11 …）：渔货栈的活动链接参数用它，建好别再改
    node_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # Bootstrap 图标名（如 bi-moon-stars）
    icon: Mapped[str] = mapped_column(String(32), default="bi-calendar-event")
    # 窗口 MM-DD（end < start 视为跨年节点，如年货节 12-25 ~ 01-20）
    start_md: Mapped[str] = mapped_column(String(5), default="")
    end_md: Mapped[str] = mapped_column(String(5), default="")
    # 关键词（一行一个，也认逗号 / 顿号）：商品标题命中任一即归入该节点；空 = 通吃全品类
    keywords: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 种子节点：删掉了下次启动会补回来；运营自己加的不受影响
    seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PurchaseNodeItem(Base):
    """采购节点下**手动配置**的商品（2026-10-11 用户拍板）：这个活动就推这几款。

    与关键词匹配的关系（两套并行、不互相替代）：
      · 关键词是**系统规则**：商品标题命中任一关键词即归入该节点（留空 = 通吃），规则不变；
      · 手动配置是**运营加塞 / 置顶**：渔货栈展示时手动配置的排在关键词匹配结果的前面
        （按这里的 sort_order），两组合起来去重后就是这个节点下能看到的货。

    存的是渔货栈货源编号（supplier_items.item_id，如 hub-prd-14 / M1-xxx），不是商品档案 ——
    货源广场卖的就是货源，标题改名 / 下架都在货源那边，这里只留一份标题快照便于认人。
    """

    __tablename__ = "purchase_node_items"
    __table_args__ = (UniqueConstraint("node_id", "item_id", name="uq_node_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_nodes.id", ondelete="CASCADE"), index=True
    )
    # 渔货栈货源编号（supplier_items.item_id）
    item_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 标题快照：货源改名 / 下架后，页面上还能看出当初配的是哪一款
    title: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProductCategory(Base):
    """商品类目（三级）：发布商品时一级 → 二级 → 三级级联选择的那份数据。

    为什么要独立成表（2026-09-28 起）：
      · 以前类目是写死在 services/product.py 里的二级常量，改一个词要发版；
      · 渔货栈货源广场那套三级类目（10 一级 / 64 二级 / 375 三级）是业务
        真实在用的口径，发布商品要和它对齐，所以在这里留一份可维护的原始数据；
      · 首次打开「货源 · 类目管理」时按渔货栈那套常量自动播种（见 services/category.py），
        之后就在这里增删改，两端看的是同一份口径。

    商品上的 Product.category 仍是文本（"一级 / 二级 / 三级"），列表 / 统计按文本走，
    不因为类目层级加深而改动历史数据。
    """

    __tablename__ = "product_categories"
    __table_args__ = (
        Index("ix_product_category_parent", "parent_id"),
        Index("ix_product_category_level", "level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 父类目 id：一级类目为空
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 层级：1 一级 / 2 二级 / 3 三级
    level: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64), default="")
    # 同级排序（越小越前；管理页可以上移 / 下移）
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 停用后不再出现在发布页的级联里（历史商品不受影响）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 渔货栈那套类目的原始 id（如 ap_women / ap_women_l3_0）：同步时按它认领，
    # 手工新增的类目为空
    source_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 类目特性规则（主要维护在一级类目上）：不影响类目显示，只作为上架商品与后续经营的
    # 经验规则 —— 系统用它生成商品标题（品牌词 / 产品词 / 修饰词 / 热搜词 / 长尾词）、
    # 推荐别名，并在上架时把经营经验提示给运营。格式见 services/category.py 的 parse_rules()
    rules: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ShippingRule(Base):
    """运费模板 / 闲鱼发货方式（2026-09-29 起）。

    两类规则共用一张表（`kind` 区分），因为它们的管理逻辑完全一样：
      · `freight` 运费模板：上架商品时默认套用的运费口径（全国包邮 / 固定运费 ¥6 …）；
      · `method` 闲鱼发货方式：邮寄-包邮 / 不包邮-按距离付费 / 不包邮-固定收费 / 无需邮寄-买家自提。

    命中优先级（`services/shipping.py::pick_rule`）：
      供应商 + 类目 > 类目 > 供应商 > 全局默认（`is_default`）
    只关联了供应商的规则对所有类目生效，只关联类目的规则对所有供应商生效；
    什么都没关联的规则等于全局规则，其中可以指定一条 `is_default`（每个 kind 一条）。
    """

    __tablename__ = "shipping_rules"
    __table_args__ = (
        Index("ix_shipping_rule_kind", "kind"),
        Index("ix_shipping_rule_supplier", "supplier_id"),
        Index("ix_shipping_rule_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # freight 运费模板 / method 发货方式
    kind: Mapped[str] = mapped_column(String(16), default="freight")
    # 模板名（商品上存的就是这个名字，所以改名要连带更新已有商品）
    name: Mapped[str] = mapped_column(String(64), default="")
    # 运费金额（kind=freight 时用；包邮为 0）
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    # 发货方式的取值：free / distance / fixed / self_pickup（见 product.SHIPPING_CHOICES）
    mode: Mapped[str] = mapped_column(String(32), default="")
    # 全局默认：每个 kind 只允许一条
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 关联供应商（供应商档案 id，空 = 不限）
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 关联类目（类目 id，空 = 不限；存文本快照便于展示，类目改名后按 id 仍能对上）
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_text: Mapped[str] = mapped_column(String(255), default="")
    # 排序（小的优先），同类命中时用它决断
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# 地址用途：退货地址（买家寄回）/ 发货地址（我们寄出）
ADDR_KIND_RETURN = "return"
ADDR_KIND_SHIP = "ship"


class ReturnAddress(Base):
    """地址：退货地址（买家寄回）/ 发货地址（我们寄出），2026-09-29 起共用这一张表。

    管理与运费模板（`ShippingRule`）同一套：一条全局默认 + 按供应商 / 类目细分，
    优先级 供应商 + 类目 > 类目 > 供应商 > 全局默认。

    多一层：还能**直接关联指定商品**（见 `ReturnAddressProduct`）——
    商品是最具体的维度，命中优先级最高（比「供应商 + 类目」还高）。

    类目口径与运费模板一致：**只关联到一级类目**（类目太细，维护成本高，
    而退货仓 / 发货仓通常按品类分，一级够用）。

    两种用途字段与命中规则完全一样，只有 `kind` 不同，所以共用一张表
    （与 `ShippingRule` 用 kind 区分运费模板 / 发货方式是同一套做法）。
    「全局默认」是**按 kind 各一条**，互不影响。
    """

    __tablename__ = "return_addresses"
    __table_args__ = (
        Index("ix_return_address_kind", "kind"),
        Index("ix_return_address_supplier", "supplier_id"),
        Index("ix_return_address_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 用途：return = 买家退货寄到哪儿；ship = 我们的货从哪儿发出
    kind: Mapped[str] = mapped_column(String(16), default=ADDR_KIND_RETURN)
    # 地址名（给运营看的，如「广州白云退货仓」），商品上展示的就是它
    name: Mapped[str] = mapped_column(String(64), default="")
    contact: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    # 省 / 市 / 区（一段文本，省得拆三列）
    region: Mapped[str] = mapped_column(String(128), default="")
    # 详细地址（街道门牌）
    address: Mapped[str] = mapped_column(String(255), default="")
    # 全局默认：全站只允许一条
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 关联供应商（空 = 不限）
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 关联一级类目（空 = 不限；category_text 是快照，类目改名后按 id 仍能对上）
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_text: Mapped[str] = mapped_column(String(255), default="")
    # 排序（小的优先），同维度命中时用它决断
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ReturnAddressProduct(Base):
    """退货地址 ↔ 商品的关联：一条地址可以指定生效在哪些商品上。

    商品维度最具体，命中时压过供应商 / 类目规则（见 `services/return_addr.pick`）。
    """

    __tablename__ = "return_address_products"
    __table_args__ = (
        UniqueConstraint("address_id", "product_id", name="uq_return_address_product"),
        Index("ix_return_address_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    product_id: Mapped[int] = mapped_column(Integer, default=0)
    # 商品名快照：列表页直接显示，不用再 join 一次
    product_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProductSku(Base):
    """商品规格 / SKU：同一款商品的不同规格（颜色、容量、尺寸…），价格与库存各算各的。

    订单里的 spec 字段与这里的 spec 对应，用于把订单精确落到某个规格上。
    """

    __tablename__ = "product_skus"
    __table_args__ = (Index("ix_product_sku_product", "product_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # 规格名，如「深空黑 128G」
    spec: Mapped[str] = mapped_column(String(255), default="")
    # 规格编码 / 闲鱼 SKU ID（由商家编码派生：XYSK…-01）
    sku_code: Mapped[str] = mapped_column(String(64), default="")
    # 建议售价（渔货栈 / 闲鱼发布用；渔货价与建议售价是必填口径）
    price: Mapped[float] = mapped_column(Float, default=0.0)
    # 成本价（对供应链的采购价，内部对账用，可留空）
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 渔货价（渔货栈看到的成本价 / 进货价）与市场价（划线价）：可留空，空则页面不显示
    fish_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_price: Mapped[float] = mapped_column(Float, default=0.0)
    stock: Mapped[int] = mapped_column(Integer, default=-1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SystemFlag(Base):
    """一次性动作的标记：演示数据播种这类「只做一次」的事靠它记住。

    记在库里而不是靠「表里有没有数据」判断 —— 否则运营把演示数据删掉后，
    下次重启又会被当成新库重新塞一遍。
    """

    __tablename__ = "system_flags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ParseCorrection(Base):
    """纠错样本：运营在订单页把某个字段改对时留的档。

    人工改一次往往意味着解析规则有疏漏，以前改完就散了、同样的错还会再犯。
    现在把三件事存下来：机器原来取成什么、人改成了什么、原文里那一行长什么样。
    新订单解析完按「字段 + 原文那一行」查这里，命中就直接采用人工结论
    （见 services/learn.py），同样的写法下次不用再人改。
    """

    __tablename__ = "parse_corrections"
    __table_args__ = (Index("ix_parse_correction_lookup", "field_key", "source_line"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_no: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")
    field_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    field_label: Mapped[str] = mapped_column(String(32), default="")
    # 机器解析出来的值 / 人工改成的值（都按字符串存，金额也能直接比）
    machine_value: Mapped[str] = mapped_column(String(255), default="")
    human_value: Mapped[str] = mapped_column(String(255), default="")
    # 原文里带这个值的那一行：下次遇到同样的写法靠它认出来
    source_line: Mapped[str] = mapped_column(String(255), default="", index=True)
    # 当时消息的原文片段，给人回看上下文
    raw_snippet: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---------------------------------------------------------------- 公司资源 · 手机号
# 手机号是公司的一类资源：一个号开出来的闲鱼 / 支付宝 / 微信 / 小红书账号是能直接干活的
# 资产，谁实名、什么性质、主卡还是副卡、套餐怎么付，都要能查得到 —— 以前这些散在
# 个人手里（Excel / 聊天记录），号停用、换实名之后对不上账。
# 口径与取值见 app/services/phone.py（choices 与文案都在那里，模型只存值）。
PHONE_NATURE_PERSONAL = "personal"
PHONE_NATURE_ENTERPRISE = "enterprise"
PHONE_NATURE_CHOICES = (PHONE_NATURE_PERSONAL, PHONE_NATURE_ENTERPRISE)
PHONE_NATURE_LABELS = {PHONE_NATURE_PERSONAL: "个人号", PHONE_NATURE_ENTERPRISE: "企业主体号"}

PHONE_CARD_MAIN = "main"
PHONE_CARD_SUB = "sub"
PHONE_CARD_CHOICES = (PHONE_CARD_MAIN, PHONE_CARD_SUB)
PHONE_CARD_LABELS = {PHONE_CARD_MAIN: "主卡号码", PHONE_CARD_SUB: "副卡号码"}

# 套餐付费方式：按月后付费（先使用后出账）/ 预充值（先充钱再扣）
PHONE_PLAN_POSTPAID = "postpaid"
PHONE_PLAN_PREPAID = "prepaid"
PHONE_PLAN_CHOICES = (PHONE_PLAN_POSTPAID, PHONE_PLAN_PREPAID)
PHONE_PLAN_LABELS = {PHONE_PLAN_POSTPAID: "按月后付费", PHONE_PLAN_PREPAID: "预充值"}

PHONE_CARRIER_MOBILE = "mobile"
PHONE_CARRIER_UNICOM = "unicom"
PHONE_CARRIER_TELECOM = "telecom"
PHONE_CARRIER_BROADNET = "broadnet"
PHONE_CARRIER_VIRTUAL = "virtual"
PHONE_CARRIER_OTHER = "other"
PHONE_CARRIER_CHOICES = (
    PHONE_CARRIER_MOBILE,
    PHONE_CARRIER_UNICOM,
    PHONE_CARRIER_TELECOM,
    PHONE_CARRIER_BROADNET,
    PHONE_CARRIER_VIRTUAL,
    PHONE_CARRIER_OTHER,
)
PHONE_CARRIER_LABELS = {
    PHONE_CARRIER_MOBILE: "移动",
    PHONE_CARRIER_UNICOM: "联通",
    PHONE_CARRIER_TELECOM: "电信",
    PHONE_CARRIER_BROADNET: "广电",
    PHONE_CARRIER_VIRTUAL: "虚拟运营商",
    PHONE_CARRIER_OTHER: "其他",
}

# 一个手机号能注册哪些平台账号（字段名 / 标签 / 图标，页面四格展示与统计都按它循环）
PHONE_PLATFORMS: tuple[tuple[str, str, str], ...] = (
    ("xianyu", "闲鱼", "bi-shop"),
    ("alipay", "支付宝", "bi-wallet2"),
    ("wechat", "微信", "bi-chat-dots"),
    ("xiaohongshu", "小红书", "bi-camera"),
)
PHONE_PLATFORM_FIELDS = tuple(key for key, _label, _icon in PHONE_PLATFORMS)
PHONE_PLATFORM_LABELS = {key: label for key, label, _icon in PHONE_PLATFORMS}


class PhoneNumber(Base):
    """公司资源 · 手机号（2026-10-06 起）。

    一条 = 一个手机号资源，记四件事：
      · **号本身**：号码、运营商、主卡 / 副卡（副卡填挂在哪个主卡下）；
      · **归属**：账号性质（个人号 / 企业主体号）+ 所属人（实名人）—— 实名是谁，
        出了事找谁、账号能不能过户都看它；
      · **套餐**：按月后付费 / 预充值，月租多少、预充值还剩多少；
      · **开出来的平台账号**：闲鱼 / 支付宝 / 微信 / 小红书各有没有注册（勾选即可）。

    停用（enabled=False）= 号还在但不再使用（停机保号 / 已回收），不删记录：
    它注册过的账号可能还在跑，删了就查不到是谁开的。
    """

    __tablename__ = "phone_numbers"
    __table_args__ = (
        UniqueConstraint("number", name="uq_phone_number"),
        Index("ix_phone_owner", "owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 手机号：11 位（存归一化后的纯数字，展示时再分段）
    number: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 运营商（可留空：号拿过来时未必知道，后面补）
    carrier: Mapped[str] = mapped_column(String(16), default="")
    # 账号性质：个人号 / 企业主体号
    nature: Mapped[str] = mapped_column(String(16), default=PHONE_NATURE_PERSONAL)
    # 所属人（实名人）：个人号写本人，企业主体号写经办人 / 对接人
    owner_name: Mapped[str] = mapped_column(String(64), default="")
    # 账号类型：主卡号码 / 副卡号码
    card_type: Mapped[str] = mapped_column(String(16), default=PHONE_CARD_MAIN)
    # 副卡挂在哪个主卡号下（副卡才有意义，主卡留空）
    parent_number: Mapped[str] = mapped_column(String(32), default="")
    # 套餐付费方式：按月后付费 / 预充值
    plan: Mapped[str] = mapped_column(String(16), default=PHONE_PLAN_POSTPAID)
    # 月租 / 套餐金额（元）：后付费看每个月固定花多少，预充值看充值面额
    monthly_fee: Mapped[float] = mapped_column(Float, default=0.0)
    # 预充值余额（元）：只有预充值的号才填，后付费留 0
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    # 各平台账号有没有注册（勾选即有；没注册的就是还没开出来的资产）
    xianyu: Mapped[bool] = mapped_column(Boolean, default=False)
    alipay: Mapped[bool] = mapped_column(Boolean, default=False)
    wechat: Mapped[bool] = mapped_column(Boolean, default=False)
    xiaohongshu: Mapped[bool] = mapped_column(Boolean, default=False)
    # 备注：这个号在干什么（绑了哪台设备 / 开过哪些号 / 为什么停用）
    note: Mapped[str] = mapped_column(Text, default="")
    # 停用 = 不再使用（停机保号 / 已回收），不删记录
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def nature_text(self) -> str:
        return PHONE_NATURE_LABELS.get(self.nature, self.nature or "—")

    @property
    def card_text(self) -> str:
        return PHONE_CARD_LABELS.get(self.card_type, self.card_type or "—")

    @property
    def plan_text(self) -> str:
        return PHONE_PLAN_LABELS.get(self.plan, self.plan or "—")

    @property
    def carrier_text(self) -> str:
        return PHONE_CARRIER_LABELS.get(self.carrier, "—") if self.carrier else "—"

    def registered(self, key: str) -> bool:
        """这个平台账号有没有注册（key 见 PHONE_PLATFORM_FIELDS）。"""
        return bool(getattr(self, key, False))

    @property
    def registered_platforms(self) -> list[str]:
        """已注册的平台名列表（页面上「已开账号」那一行直接用）。"""
        return [PHONE_PLATFORM_LABELS[key] for key in PHONE_PLATFORM_FIELDS if self.registered(key)]


class ShareCard(Base):
    """引流名片：一份带 OG 标签的 H5 长链接。

    抖音（以及微信、小红书这类）发链接时，后台爬虫会去抓页面 ``<head>`` 里的
    OG 标签，抓到就把链接渲染成一张卡片（封面图 + 标题 + 简介）。**爬虫不执行 JS**，
    所以 OG 必须写在服务端返回的 HTML 源码里 —— 这也正是这个模块存在的理由：
    在这里填一次，系统自动生成那个静态页（``/c/<slug>``），复制长链接就能直接贴。

    卡片渲染函数在 ``services/share_card.render_html``。
    """

    __tablename__ = "share_cards"
    __table_args__ = (UniqueConstraint("slug", name="uq_share_card_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 长链接里的短名：/c/<slug>，中文名自动转拼音首字母（同名自动补序号）
    slug: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 内部备注：自己认这张卡片是给谁 / 哪个活动用的，不出现在卡片上
    name: Mapped[str] = mapped_column(String(64), default="")
    # ------- OG：爬虫读的就是下面这四个 -------
    og_title: Mapped[str] = mapped_column(String(120), default="")
    og_desc: Mapped[str] = mapped_column(String(255), default="")
    og_image: Mapped[str] = mapped_column(String(500), default="")
    # ------- 落地页：用户点开卡片后看到的内容 -------
    # 落地页大标题（留空时沿用 og_title）
    body_title: Mapped[str] = mapped_column(String(120), default="")
    # 落地页正文：按换行分段
    body_text: Mapped[str] = mapped_column(Text, default="")
    # 二维码图片地址（选填）
    qr_image: Mapped[str] = mapped_column(String(500), default="")
    # 打开页面几秒后自动跳到这个地址（选填；用 JS 跳，不影响爬虫抓 OG）
    jump_url: Mapped[str] = mapped_column(String(500), default="")
    jump_seconds: Mapped[int] = mapped_column(Integer, default=2)
    # 是否被打开过（爬虫来抓也算，所以这个数只看趋势，别当真实访客）
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SourcingDisplayConfig(Base):
    """渔货栈货源展示基数（2026-10-13 用户拍板）：给「X 家上架 / 已售」垫一个基数。

    运营视角：一款货明明只被 1 家店铺铺过，卡片上就写「1 家上架」，看着冷清、不像
    有人在做。所以允许在渔枢后台配一个**展示基数**，渔货栈卡片显示「基数 + 真实数量」。

    规矩（用户原话「实际的上架数量仍然要准确的」）：
      · **只影响展示** —— 真实值照旧单独算：排序、热销榜计数、商品详情页「在售闲鱼号」
        一律用真实值，基数一个都不加；
      · 全站一份配置（这张表只存第 1 行），不按商品、不按人配。
    """

    __tablename__ = "sourcing_display_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 上架家数基数：卡片「X 家上架」= 它 + 真实铺过这款货的店铺数
    listed_shops_base: Mapped[int] = mapped_column(Integer, default=0)
    # 已售基数：卡片「已售 N」= 它 + 真实成交件数
    sold_base: Mapped[int] = mapped_column(Integer, default=0)
    # 备注：为什么配这个数（换季活动 / 新品冷启动…），只给内部看
    note: Mapped[str] = mapped_column(String(255), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LogisticsTrace(Base):
    """物流轨迹本地存档（2026-10-13）：三端共用的「查一次、留一份」。

    渔货栈用户查订单、渔枢订单页查物流、渔集出库查物流，调的都是同一个服务
    （``services/logistics_trace``）。菜鸟按次计费/限流，同一单号被三个端各查一遍
    纯属浪费 —— 所以每次查询在这里留一份：

      · 在途单：``next_refresh_at`` 之前再来查，直接给存档，不打菜鸟；
      · 终态单（签收 / 退回 / 拦截）：轨迹不会再变，永不再查（除非页面点「刷新」）；
      · 查不到的单（刚下单还没揽收）：按空结果短缓存，别把额度刷在无效单上。

    ``query_count`` 记真实调菜鸟的次数，``hit_count`` 记走存档的次数 ——
    两个数的差就是这套存档省下来的接口调用。
    """

    __tablename__ = "logistics_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 运单号（唯一：同一单号全系统只存一份，三端共享）
    mail_no: Mapped[str] = mapped_column(String(64), default="", index=True, unique=True)
    # 快递公司编码（SF / ZTO …）；空表示当时没认出来
    cp_code: Mapped[str] = mapped_column(String(32), default="")
    # 快递公司显示名（中通快递 …），页面直接显示
    cp_name: Mapped[str] = mapped_column(String(64), default="")
    # 收件人手机号后 4 位（隐私号查询用）；只存后四位，不存完整号码
    phone_tail: Mapped[str] = mapped_column(String(8), default="")
    # 末次轨迹状态（signed / delivering / transit …，见 services.logistics.STATUS_MAP）
    status: Mapped[str] = mapped_column(String(32), default="")
    # 是否终态：签收 / 退回 / 拦截 / 失败 —— 是的话不再自动刷新
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    # 轨迹明细：[{time, desc, station, status}]，最新在前
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    # 菜鸟末次返回的原文摘要（排查 S02 / S18 这类回执用，截断存）
    last_msg: Mapped[str] = mapped_column(String(500), default="")
    # 末次查询失败原因（成功时为空）
    last_error: Mapped[str] = mapped_column(String(500), default="")
    # 真实调菜鸟的次数 / 走存档的次数
    query_count: Mapped[int] = mapped_column(Integer, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    first_queried_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_queried_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 什么时候之后允许再查一次（终态单为 None）
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LogisticsNotify(Base):
    """物流状态通知记录（2026-10-13）：状态变了通知订单发送人，一单一状态只发一次。

    定时任务（独立的物流轨迹服务）刷到轨迹状态变化时发飞书给**订单发送人**
    （``Order.sender_id`` —— 就是当初在飞书群里报这单的那个人）。

    ``(mail_no, to_status)`` 上建了唯一约束：**同一单号的同一个状态只通知一次**，
    刷新一百遍也不会把同一条「已签收」发一百遍。发送失败的记录会被下一次覆盖
    （先删旧的失败记录再记新的），所以接口临时抽风也能自己补上。
    """

    __tablename__ = "logistics_notifies"
    __table_args__ = (UniqueConstraint("mail_no", "to_status", name="uq_logistics_notify"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mail_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 关联的订单号（订单详情 / 通知文案里要用）
    order_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 状态变化：从什么变成什么（首次查询 from 为空）
    from_status: Mapped[str] = mapped_column(String(32), default="")
    to_status: Mapped[str] = mapped_column(String(32), default="")
    # 收通知的人（飞书 open_id）与姓名
    receiver_id: Mapped[str] = mapped_column(String(64), default="")
    receiver_name: Mapped[str] = mapped_column(String(128), default="")
    # 是否异常分支（派送失败 / 退回中 / 已退回）
    abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    # 发送失败的原因（没发送人 / 飞书没启用 / 接口报错）
    error: Mapped[str] = mapped_column(String(500), default="")
    # 实际发出去的文案（页面上能回看发了什么）
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
