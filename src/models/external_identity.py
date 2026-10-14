"""Trusted identities synchronized from integrated external systems."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from app.timeutil import now as _now


class ExternalIdentity(Base):
    """External system user record; it is not itself a platform login account."""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("source_system", "external_user_id", name="uq_external_identity_source_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_role: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
