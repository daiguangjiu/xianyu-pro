"""商家（供货商）入驻 + 资金结算模型。

对应需求：「供货商入驻 → 商家端后台（上传商品 / 商品管理 / 订单管理 / 发货 / 资金结算）」。

设计要点：
- **主体与账号分离**：一个 User（登录账号）可通过 MerchantUser 关联多个 Merchant（总账号 / 子账号）。
- **入驻审核留痕**：申请快照放 `MerchantApply`，审核通过后才正式建 `Merchant`；
  驳回可重提（同一账号保留历史申请记录，便于演示"驳回 → 修改 → 再审"）。
- **资金走平台代收代付**：买家付款进平台账 → 按账期生成 `SettlementBill`
  （货款 - 佣金 - 退款 = 实结）→ 结算款入 `MerchantWallet` → 商家申请 `WithdrawApply` 提现。

本文件只定义模型；注册见 `src/models/__init__.py` 底部的 import。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


# ====== 主体类型 / 状态常量（与前端表单、模板展示共用） ======
SUBJECT_TYPES = {
    "enterprise": "企业",
    "individual": "个体工商户",
    "personal": "个人",
}

MERCHANT_STATUS = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "frozen": "已冻结",
}

APPLY_STATUS = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
}

USER_ROLES = {
    "platform_admin": "平台管理员",
    "merchant": "商家",
    "reseller": "分销商",
}

SETTLE_STATUS = {
    "pending": "待结算",
    "settled": "已结算",
    "cancelled": "已取消",
}

WITHDRAW_STATUS = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "paid": "已打款",
}


# ====== 1. User：登录账号 ======
# 账号已与渔枢系统合并（2026-09-30 起：一套系统、一套库、一套账号）——
# 两端共用 users 表，模型定义在 app/models.py（渔枢系统侧），带手机号与端权限。
# 这里只做转发，渔货栈 `from src.models.merchant import User` 的写法一行都不用改；
# 角色口径也换成渔枢系统的 admin / user（渔货栈老角色 platform_admin 已映射成 admin）。
from app.models import User  # noqa: E402,F401


# ====== 2. Merchant：商家主体（入驻成功后生成） ======
class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))  # 企业全称 / 店铺名
    short_name: Mapped[str | None] = mapped_column(String(64))

    # enterprise / individual / personal
    subject_type: Mapped[str] = mapped_column(String(16), default="enterprise", index=True)

    # ---- 资质 ----
    license_no: Mapped[str | None] = mapped_column(String(64))  # 营业执照号 / 身份证号
    legal_person: Mapped[str | None] = mapped_column(String(64))  # 法人 / 经营者

    # ---- 联系 ----
    contact_name: Mapped[str | None] = mapped_column(String(64))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    contact_email: Mapped[str | None] = mapped_column(String(128))

    # ---- 经营 ----
    categories: Mapped[list] = mapped_column(JSON, default=list)  # 经营类目
    province: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)

    # ---- 结算账户 ----
    settle_bank_name: Mapped[str | None] = mapped_column(String(128))
    settle_account_name: Mapped[str | None] = mapped_column(String(128))
    settle_bank_account: Mapped[str | None] = mapped_column(String(64))

    # ---- 平台规则 ----
    # 平台佣金率（平台代收代付模式下从每笔货款中扣除）
    commission_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=Decimal("0.0500"))

    # pending / approved / rejected / frozen
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 主账号（申请人）
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), index=True
    )

    @property
    def status_text(self) -> str:
        return MERCHANT_STATUS.get(self.status, self.status)

    @property
    def subject_type_text(self) -> str:
        return SUBJECT_TYPES.get(self.subject_type, self.subject_type)


# ====== 3. MerchantUser：账号 ↔ 商家关联（总账号 / 子账号） ======
class MerchantUser(Base):
    __tablename__ = "merchant_users"
    __table_args__ = (UniqueConstraint("merchant_id", "user_id", name="uq_merchant_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    # owner / staff
    role: Mapped[str] = mapped_column(String(16), default="staff")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ====== 4. MerchantApply：入驻申请（资质快照 + 审核留痕） ======
class MerchantApply(Base):
    __tablename__ = "merchant_applies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    # 审核通过后回填
    merchant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )

    # ---- 申请资料快照（与 Merchant 字段同构；审核时不改 Merchant，只改这份）----
    name: Mapped[str] = mapped_column(String(128))
    short_name: Mapped[str | None] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(16), default="enterprise")
    license_no: Mapped[str | None] = mapped_column(String(64))
    legal_person: Mapped[str | None] = mapped_column(String(64))
    contact_name: Mapped[str | None] = mapped_column(String(64))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    contact_email: Mapped[str | None] = mapped_column(String(128))
    categories: Mapped[list] = mapped_column(JSON, default=list)
    province: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    settle_bank_name: Mapped[str | None] = mapped_column(String(128))
    settle_account_name: Mapped[str | None] = mapped_column(String(128))
    settle_bank_account: Mapped[str | None] = mapped_column(String(64))

    # 预留：资质图片 / 补充材料 / 审核过程记录
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def status_text(self) -> str:
        return APPLY_STATUS.get(self.status, self.status)

    @property
    def subject_type_text(self) -> str:
        return SUBJECT_TYPES.get(self.subject_type, self.subject_type)


# ====== 5. SettlementBill：结算单（平台代收代付 + 抽佣） ======
class SettlementBill(Base):
    __tablename__ = "settlement_bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )

    # 账期
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order_count: Mapped[int] = mapped_column(Integer, default=0)
    goods_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # 实结 = goods_amount - commission - refund_amount
    settle_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    # pending / settled / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    @property
    def status_text(self) -> str:
        return SETTLE_STATUS.get(self.status, self.status)


# ====== 6. MerchantWallet：商家钱包 ======
class MerchantWallet(Base):
    __tablename__ = "merchant_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("merchants.id"), unique=True, index=True
    )

    available_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0")
    )  # 可提现
    pending_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0")
    )  # 待结算（已售未到账期）
    withdrawn_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0")
    )  # 累计已提现
    frozen_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0")
    )  # 冻结（提现审核中 / 争议）


# ====== 7. WithdrawApply：提现申请 ======
class WithdrawApply(Base):
    __tablename__ = "withdraw_applies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )
    apply_no: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    bank_name: Mapped[str | None] = mapped_column(String(128))
    account_name: Mapped[str | None] = mapped_column(String(128))
    bank_account: Mapped[str | None] = mapped_column(String(64))

    # pending / approved / rejected / paid
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def status_text(self) -> str:
        return WITHDRAW_STATUS.get(self.status, self.status)


__all__ = [
    "User",
    "Merchant",
    "MerchantUser",
    "MerchantApply",
    "SettlementBill",
    "MerchantWallet",
    "WithdrawApply",
    "SUBJECT_TYPES",
    "MERCHANT_STATUS",
    "APPLY_STATUS",
    "USER_ROLES",
    "SETTLE_STATUS",
    "WITHDRAW_STATUS",
]
