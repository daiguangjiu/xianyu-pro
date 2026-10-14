"""渔货栈账号 ↔ 渔枢店铺的**关联台账**（首登自动关联 + 后续双向同步的落点）。

店铺档案本身只有一份，就是渔枢系统库里的 `shops` 表（渔货栈「店铺管理」直读直写，
见 `src/web/hub_shops.py`）—— 所以这里**不复制店铺数据**，只记两件事：

  · ``ShopUserLink``：哪个渔货栈账号看得到哪家店、是怎么看得到的（首登自动 / 手动添加）；
  · ``ShopSyncMark``：这个账号的首次同步时间、最近一次同步时间 —— **首登只做一次**，
    之后按节流增量补（渔枢那边新建的店铺自动跟上）。

两个外键都是**逻辑外键**（普通整数列 + 索引，不建 FK 约束）：渔货栈单独部署时
进程里没有渔枢那套模型，加了硬外键会让建表失败（同 `shops.owner_user_id` 的做法）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

# 关联来源：首登自动关联的会写明是按什么对上的，便于事后追问「为什么这家店在我这」
LINK_SOURCE_AUTO_PHONE = "auto_phone"      # 手机号对上（渔枢账号登录名 = 手机号）
LINK_SOURCE_AUTO_NAME = "auto_name"        # 姓名对上（实际运营人 / 闲鱼会员名）
LINK_SOURCE_AUTO_OWNER = "auto_owner"      # 渔枢端建档时提交人就是他
LINK_SOURCE_MANUAL = "manual"              # 自己在渔货栈填表添加 / 认领

LINK_SOURCE_TEXT = {
    LINK_SOURCE_AUTO_PHONE: "首登自动（手机号对上）",
    LINK_SOURCE_AUTO_NAME: "首登自动（姓名对上）",
    LINK_SOURCE_AUTO_OWNER: "渔枢提交人",
    LINK_SOURCE_MANUAL: "手动添加",
}


class ShopUserLink(Base):
    """渔货栈账号 ↔ 店铺的关联（一家店可以关联多个账号，一个账号可以有多家店）。"""

    __tablename__ = "shop_user_links"
    __table_args__ = (
        UniqueConstraint("user_id", "shop_id", name="uq_shop_user_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 渔枢统一账号 id（users.id，逻辑外键）
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    # 渔枢店铺档案 id（shops.id，逻辑外键）
    shop_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(24), default=LINK_SOURCE_MANUAL)
    # 关联当时店铺在渔枢的归属：0 = 无主（认领过来的），非 0 = 挂在别人名下（只关联不改归属）
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动解除关联的记时间（行留着，避免下次同步又给关联回来）
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def source_text(self) -> str:
        return LINK_SOURCE_TEXT.get(self.source, self.source or "")


class ShopSyncMark(Base):
    """每个渔货栈账号的同步标记：首登做过没、最近一次什么时候做的。"""

    __tablename__ = "shop_sync_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 渔枢统一账号 id（一个账号一行）
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    first_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 本次同步新关联上的店铺数（给页面提示「本次新增 N 家」）
    last_new_count: Mapped[int] = mapped_column(Integer, default=0)


def source_text(source: str) -> str:
    return LINK_SOURCE_TEXT.get(str(source or ""), str(source or ""))
