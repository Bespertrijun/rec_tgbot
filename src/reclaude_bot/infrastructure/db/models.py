from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from reclaude_bot.domain.enums import (
    BaselineStatus,
    BindingStatus,
    CycleStatus,
    GroupMembershipState,
    ManagedGroupStatus,
    QuotaRevocationStatus,
    UserStatus,
)

from .base import Base

MONEY = Numeric(18, 10)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
        UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        UniqueConstraint("reclaude_user_id", name="uq_users_reclaude_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    reclaude_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    binding_status: Mapped[str] = mapped_column(String(32), default=BindingStatus.BOUND.value, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=UserStatus.ACTIVE.value, nullable=False)
    baseline_status: Mapped[str] = mapped_column(String(32), default=BaselineStatus.UNKNOWN.value, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UpstreamMember(Base):
    __tablename__ = "upstream_members"
    __table_args__ = (
        UniqueConstraint("reclaude_user_id", name="uq_upstream_members_reclaude_user_id"),
        Index("ix_upstream_members_email_normalized", "email_normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_record_id: Mapped[str | None] = mapped_column(String(128))
    reclaude_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(128))
    total_usage_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuotaCycle(Base):
    __tablename__ = "quota_cycles"
    __table_args__ = (
        Index("ix_quota_cycles_status_reset", "status", "reset_at"),
        UniqueConstraint("reset_at", name="uq_quota_cycles_reset_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    weekly_percent: Mapped[Decimal | None] = mapped_column(MONEY)
    source_account_id: Mapped[int | None] = mapped_column(Integer)
    source_account_email_masked: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(32), default=CycleStatus.INITIALIZING.value, nullable=False)
    last_day_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_day_allow: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CycleBaseline(Base):
    __tablename__ = "member_cycle_baselines"
    __table_args__ = (UniqueConstraint("reclaude_user_id", "cycle_id", name="uq_member_cycle_baseline"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reclaude_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cycle_id: Mapped[int] = mapped_column(ForeignKey("quota_cycles.id"), nullable=False)
    baseline_total_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    baseline_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default=BaselineStatus.UNKNOWN.value, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))


class QuotaAdjustment(Base):
    __tablename__ = "quota_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("quota_cycles.id"), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuotaRevocation(Base):
    __tablename__ = "quota_revocations"
    __table_args__ = (
        UniqueConstraint("user_id", "cycle_id", name="uq_quota_revocation_user_cycle"),
        Index("ix_quota_revocations_state", "cycle_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("quota_cycles.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=QuotaRevocationStatus.PENDING_REVOKE.value, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), default="QUOTA", nullable=False)
    pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    quota_limit_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServiceState(Base):
    __tablename__ = "service_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    write_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), default="startup_recovery_required", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManagedGroup(Base):
    __tablename__ = "managed_groups"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_managed_groups_chat_id"),
        Index("ix_managed_groups_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=ManagedGroupStatus.PENDING.value, nullable=False)
    discovered_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    approved_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    disable_reason: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_user_id", name="uq_group_memberships_chat_user"),
        Index("ix_group_memberships_state_deadline", "state", "deadline"),
        Index("ix_group_memberships_user_state", "telegram_user_id", "state"),
        Index("ix_group_memberships_retry", "state", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("managed_groups.chat_id", ondelete="CASCADE"), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default=GroupMembershipState.RESTRICT_PENDING.value, nullable=False)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unmute_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unmuted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removal_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_token_hash: Mapped[str | None] = mapped_column(String(128))
    verification_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_action: Mapped[str | None] = mapped_column(String(32))
    action_attempt_id: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_alerted_retry_count: Mapped[int | None] = mapped_column(Integer)
    last_alerted_action: Mapped[str | None] = mapped_column(String(32))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
