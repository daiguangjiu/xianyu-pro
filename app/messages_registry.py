"""飞书消息一览：系统对外发出的**每一条消息**都在这里登记。

这里只放数据、不碰配置与飞书，所以 config 和 services 都能安全引用。
每条消息的 `default_channels` 记的是**改造前的老行为** —— .env 里没有为它单独配过发送方式时
就沿用这套默认值，所以这张表上线前后，系统实际发出去的消息一模一样。
"""

from __future__ import annotations

from dataclasses import dataclass

# 可选的发送渠道
CH_GROUP = "group"  # 群里回复那条消息，并把该 @ 的人 @ 上
CH_PM_SENDER = "pm_sender"  # 私聊同步一条给这条消息的发送人本人
CH_PM_SYNC = "pm_sync"  # 私聊发给通知同步人（默认牛磊）

CHANNELS: list[tuple[str, str]] = [
    (CH_GROUP, "群里回复（@ 到人）"),
    (CH_PM_SENDER, "私信同步给发送人"),
    (CH_PM_SYNC, "私信发给通知同步人"),
]


@dataclass(frozen=True)
class MessageKind:
    key: str
    name: str
    trigger: str  # 什么时候会发
    rules: str  # 现在受哪些规则管着
    default_channels: tuple[str, ...]  # 默认发送方式 = 改造前的行为
    template_key: str = ""  # 文案模板在 .env 里的键（空=文案写在代码里）
    receiver: str = ""  # 默认会 @ / 发给谁

    @property
    def env_key(self) -> str:
        return f"MSG_ROUTE_{self.key.upper()}"

    @property
    def default_route(self) -> str:
        return ",".join(self.default_channels)


KINDS: list[MessageKind] = [
    MessageKind(
        key="receipt",
        name="订单归档回执",
        trigger="群里发了订单模板消息或订单截图，解析归档成功",
        receiver="@ 发送人 + 回执名单里的人（牛磊等）",
        rules=(
            "同一条消息只回执一次；同一订单 10 分钟内不重复回执；"
            "消息超过 60 分钟不再补发；等订单截图补全正式单号（最多 60 秒）后再发；"
            "静默时段先挂起、恢复后合并补发；受每小时下发上限约束"
        ),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_RECEIPT_TEMPLATE",
    ),
    MessageKind(
        key="missing_image",
        name="缺订单截图提醒",
        trigger="收到订单模板消息，但一直没配上订单截图",
        receiver="@ 发送人",
        rules=("同一条消息只提醒一次；已经配上截图就不再提醒；不受最小间隔与静默期限制"),
        default_channels=(CH_GROUP,),
        template_key="NOTIFY_MISSING_IMAGE_TEMPLATE",
    ),
    MessageKind(
        key="duplicate",
        name="重复订单提醒",
        trigger="群里重复发了同一笔订单（内容没变化）",
        receiver="@ 发送人",
        rules=("不受 10 分钟最小间隔限制：重复发了就该明确告知「已收到过」"),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_DUPLICATE_TEMPLATE",
    ),
    MessageKind(
        key="refund",
        name="退单回执",
        trigger="识别到退单（「此单退」「不要了」等）",
        receiver="@ 发送人 + @ 供应链对接人（牛磊）",
        rules=(
            "不受最小间隔、静默期挂起、等截图这三条限制 —— "
            "退单要马上去拦截，压到第二天早上就耽误事了"
        ),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_REFUND_DETAIL_TEMPLATE",
    ),
    MessageKind(
        key="cancel",
        name="撤单回执",
        trigger="识别到撤单（订单还没发货就取消）",
        receiver="@ 发送人",
        rules=("与退单回执同级：不受最小间隔、静默期、等截图限制"),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_CANCEL_TEMPLATE",
    ),
    MessageKind(
        key="intent",
        name="意图命中提示",
        trigger="听出像退单 / 撤单，但对不上具体订单、或需要人工确认",
        receiver="@ 发送人",
        rules=("按发送人做 10 分钟限流（避免同一人反复刷屏）；消息超过 60 分钟不再补发"),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_INTENT_TEMPLATE",
    ),
    MessageKind(
        key="digest",
        name="静默期合并补发",
        trigger="静默时段攒下的回执，到可发送时段后一次性发出",
        receiver="@ 发送人",
        rules=("同一会话的多条会合并成一条摘要；超过 20 条只列前 20 条"),
        default_channels=(CH_GROUP, CH_PM_SENDER),
        template_key="NOTIFY_DIGEST_TEMPLATE",
    ),
    MessageKind(
        key="tracking",
        name="物流单号同步",
        trigger="后台回填 / 更新了物流单号后触发同步",
        receiver="@ 该订单的发送人 + @ 通知同步人",
        rules=("静默时段不拦截（这是运维主动同步）；只在「关闭下发」时不发"),
        default_channels=(CH_GROUP, CH_PM_SENDER),
    ),
    MessageKind(
        key="refund_reason",
        name="催补充退单理由",
        trigger="退单消息里没写退单理由",
        receiver="@ 发送人",
        rules=("同一订单只催一次"),
        default_channels=(CH_GROUP,),
    ),
    MessageKind(
        key="daily_summary",
        name="每日出单汇总",
        trigger="每天定时推送（可在下方汇总设置里改时间）",
        receiver="@ 所有人（汇总群全体）",
        rules=("固定发到汇总群；当天没有出单与退单时自动跳过；不受回执那套限流约束"),
        default_channels=(CH_GROUP,),
        template_key="DAILY_SUMMARY_TEMPLATE",
    ),
]

KEYS: list[str] = [m.key for m in KINDS]
BY_KEY: dict[str, MessageKind] = {m.key: m for m in KINDS}
DEFAULT_ROUTES: dict[str, str] = {m.key: m.default_route for m in KINDS}
