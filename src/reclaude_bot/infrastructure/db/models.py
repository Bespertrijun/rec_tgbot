from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from reclaude_bot.domain.enums import (
    BaselineStatus,
    BindingStatus,
    CycleStatus,
    GroupMembershipState,
    ManagedGroupStatus,
    QuotaRevocationStatus,
    TaskStatus,
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
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    reclaude_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
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


class UsageNotification(Base):
    __tablename__ = "usage_notifications"
    __table_args__ = (UniqueConstraint("user_id", "cycle_id", "threshold_percent", name="uq_usage_notifications_user_cycle_threshold"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("quota_cycles.id"), nullable=False)
    threshold_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    used_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(128), default="startup_recovery_required", nullable=False)
    selected_account_id: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuotaTask(Base):
    __tablename__ = "quota_tasks"
    __table_args__ = (UniqueConstraint("name_normalized", name="uq_quota_tasks_name_normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.STOPPED.value, nullable=False)
    scope_mode: Mapped[str] = mapped_column(String(16), default="ALL", nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuotaTaskMember(Base):
    __tablename__ = "quota_task_members"
    __table_args__ = (UniqueConstraint("task_id", "reclaude_user_id", name="uq_quota_task_members_task_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("quota_tasks.id", ondelete="CASCADE"), nullable=False)
    reclaude_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class DeviceTaskScope(Base):
    __tablename__ = "device_task_scopes"
    __table_args__ = (
        CheckConstraint("org_id > 0", name="ck_device_task_scopes_org_id_positive"),
        CheckConstraint("scope_mode IN ('ALL', 'ALLOWLIST', 'EXCLUDE')", name="ck_device_task_scopes_scope_mode"),
        UniqueConstraint("org_id", name="uq_device_task_scopes_org_id"),
        UniqueConstraint("task_id", "org_id", name="uq_device_task_scopes_task_org"),
    )

    task_id: Mapped[int] = mapped_column(ForeignKey("quota_tasks.id"), primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope_mode: Mapped[str] = mapped_column(String(16), default="ALL", server_default=text("'ALL'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceTaskMember(Base):
    __tablename__ = "device_task_members"

    task_id: Mapped[int] = mapped_column(ForeignKey("device_task_scopes.task_id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("org_id > 0", name="ck_devices_org_id_positive"),
        CheckConstraint("device_id > 0", name="ck_devices_device_id_positive"),
        UniqueConstraint("org_id", "device_id", name="uq_devices_org_device"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceAssociation(Base):
    __tablename__ = "device_associations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "org_id"],
            ["device_task_scopes.task_id", "device_task_scopes.org_id"],
            name="fk_device_associations_task_org_scope",
        ),
        ForeignKeyConstraint(
            ["org_id", "device_id"],
            ["devices.org_id", "devices.device_id"],
            name="fk_device_associations_org_device",
        ),
        CheckConstraint(
            "state IN ('PENDING_AUTH', 'ACTIVE', 'PENDING_REVOKE', 'UNKNOWN', 'ENDED')",
            name="ck_device_associations_state",
        ),
        CheckConstraint("device_id IS NULL OR device_id > 0", name="ck_device_associations_device_id_positive"),
        CheckConstraint(
            "state NOT IN ('ACTIVE', 'PENDING_REVOKE') OR device_id IS NOT NULL",
            name="ck_device_associations_state_requires_device",
        ),
        CheckConstraint(
            "(state = 'ENDED' AND ended_at IS NOT NULL) OR (state <> 'ENDED' AND ended_at IS NULL)",
            name="ck_device_associations_ended_state_time",
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_device_associations_time_order"),
        Index(
            "uq_device_associations_open_user",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
        Index(
            "uq_device_associations_open_device",
            "org_id",
            "device_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL AND device_id IS NOT NULL"),
            sqlite_where=text("ended_at IS NULL AND device_id IS NOT NULL"),
        ),
        Index("uq_device_associations_id_user_task", "id", "user_id", "task_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceAction(Base):
    __tablename__ = "device_actions"
    __table_args__ = (
        CheckConstraint("kind IN ('AUTH', 'REVOKE')", name="ck_device_actions_kind"),
        CheckConstraint("status IN ('PENDING', 'UNKNOWN', 'SUCCEEDED', 'FAILED')", name="ck_device_actions_status"),
        CheckConstraint("target_device_id IS NULL OR target_device_id > 0", name="ck_device_actions_target_device_positive"),
        CheckConstraint("kind <> 'REVOKE' OR target_device_id IS NOT NULL", name="ck_device_actions_revoke_requires_device"),
        CheckConstraint("attempt_count >= 0", name="ck_device_actions_attempt_count_nonnegative"),
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL) OR "
            "(status IN ('PENDING', 'UNKNOWN') AND completed_at IS NULL)",
            name="ck_device_actions_completion_time",
        ),
        UniqueConstraint("idempotency_key", name="uq_device_actions_idempotency_key"),
        Index(
            "uq_device_actions_pending_association",
            "association_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'UNKNOWN')"),
            sqlite_where=text("status IN ('PENDING', 'UNKNOWN')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("device_associations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_device_id: Mapped[int | None] = mapped_column(BigInteger)
    result_reused: Mapped[bool | None] = mapped_column(Boolean)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceQuotaCycle(Base):
    __tablename__ = "device_quota_cycles"
    __table_args__ = (
        ForeignKeyConstraint(["task_id"], ["device_task_scopes.task_id"], name="fk_device_quota_cycles_task_scope"),
        CheckConstraint(
            "status IN ('INITIALIZING', 'VERIFIED', 'NEEDS_REVIEW', 'EXPIRED')",
            name="ck_device_quota_cycles_status",
        ),
        CheckConstraint("reset_at > started_at", name="ck_device_quota_cycles_reset_after_start"),
        CheckConstraint(
            "weekly_percent IS NULL OR (weekly_percent >= 0 AND weekly_percent < 100000000)",
            name="ck_device_quota_cycles_weekly_percent_range",
        ),
        UniqueConstraint("task_id", "reset_at", name="uq_device_quota_cycles_task_reset"),
        UniqueConstraint("id", "task_id", name="uq_device_quota_cycles_id_task"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(128))
    weekly_percent: Mapped[Decimal | None] = mapped_column(MONEY)
    last_day_allow: Mapped[bool | None] = mapped_column(Boolean)
    last_day_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceCycleLedger(Base):
    __tablename__ = "device_cycle_ledgers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id", "task_id"],
            ["device_quota_cycles.id", "device_quota_cycles.task_id"],
            name="fk_device_cycle_ledgers_cycle_task",
        ),
        CheckConstraint(
            "quality IN ('UNKNOWN', 'VERIFIED', 'NEEDS_REVIEW')",
            name="ck_device_cycle_ledgers_quality",
        ),
        CheckConstraint(
            "confirmed_used_usd IS NULL OR "
            "(confirmed_used_usd >= 0 AND confirmed_used_usd < 100000000)",
            name="ck_device_cycle_ledgers_confirmed_used_range",
        ),
        CheckConstraint(
            "quality <> 'VERIFIED' OR confirmed_used_usd IS NOT NULL",
            name="ck_device_cycle_ledgers_verified_has_usage",
        ),
        CheckConstraint(
            "quota_unlocked_at IS NULL OR "
            "(quota_locked_at IS NOT NULL AND quota_unlocked_at >= quota_locked_at)",
            name="ck_device_cycle_ledgers_lock_time_order",
        ),
        UniqueConstraint("user_id", "cycle_id", name="uq_device_cycle_ledgers_user_cycle"),
        UniqueConstraint("id", "user_id", "task_id", name="uq_device_cycle_ledgers_id_user_task"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    cycle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_used_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    quota_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceUsageSnapshot(Base):
    __tablename__ = "device_usage_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "device_id"],
            ["devices.org_id", "devices.device_id"],
            name="fk_device_usage_snapshots_org_device",
        ),
        CheckConstraint("range IN ('all', '7d')", name="ck_device_usage_snapshots_range"),
        CheckConstraint(
            "total_usd >= 0 AND total_usd < 100000000",
            name="ck_device_usage_snapshots_total_range",
        ),
        UniqueConstraint("sample_key", name="uq_device_usage_snapshots_sample_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_key: Mapped[str] = mapped_column(String(64), nullable=False)
    range: Mapped[str] = mapped_column(String(8), nullable=False)
    total_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class DeviceUsageSegment(Base):
    __tablename__ = "device_usage_segments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["association_id", "user_id", "task_id"],
            ["device_associations.id", "device_associations.user_id", "device_associations.task_id"],
            name="fk_device_usage_segments_association_owner_task",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "user_id", "task_id"],
            ["device_cycle_ledgers.id", "device_cycle_ledgers.user_id", "device_cycle_ledgers.task_id"],
            name="fk_device_usage_segments_ledger_owner_task",
        ),
        CheckConstraint(
            "quality IN ('UNKNOWN', 'VERIFIED', 'NEEDS_REVIEW')",
            name="ck_device_usage_segments_quality",
        ),
        CheckConstraint(
            "(baseline_total_usd IS NULL AND baseline_captured_at IS NULL) OR "
            "(baseline_total_usd IS NOT NULL AND baseline_captured_at IS NOT NULL)",
            name="ck_device_usage_segments_baseline_pair",
        ),
        CheckConstraint(
            "(latest_total_usd IS NULL AND latest_sampled_at IS NULL) OR "
            "(latest_total_usd IS NOT NULL AND latest_sampled_at IS NOT NULL)",
            name="ck_device_usage_segments_latest_pair",
        ),
        CheckConstraint(
            "baseline_total_usd IS NULL OR "
            "(baseline_total_usd >= 0 AND baseline_total_usd < 100000000)",
            name="ck_device_usage_segments_baseline_range",
        ),
        CheckConstraint(
            "latest_total_usd IS NULL OR "
            "(latest_total_usd >= 0 AND latest_total_usd < 100000000)",
            name="ck_device_usage_segments_latest_range",
        ),
        CheckConstraint(
            "confirmed_used_usd IS NULL OR "
            "(confirmed_used_usd >= 0 AND confirmed_used_usd < 100000000)",
            name="ck_device_usage_segments_confirmed_used_range",
        ),
        CheckConstraint(
            "quality <> 'VERIFIED' OR "
            "(baseline_total_usd IS NOT NULL AND latest_total_usd IS NOT NULL AND confirmed_used_usd IS NOT NULL)",
            name="ck_device_usage_segments_verified_has_values",
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_device_usage_segments_time_order"),
        UniqueConstraint("association_id", "ledger_id", name="uq_device_usage_segments_association_ledger"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    association_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline_total_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    baseline_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_total_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    latest_sampled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_used_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)


class DeviceQuotaAdjustment(Base):
    __tablename__ = "device_quota_adjustments"
    __table_args__ = (
        CheckConstraint(
            "amount_usd > -100000000 AND amount_usd < 100000000",
            name="ck_device_quota_adjustments_amount_range",
        ),
        CheckConstraint("length(trim(reason)) > 0", name="ck_device_quota_adjustments_reason_nonempty"),
        UniqueConstraint("operation_key", name="uq_device_quota_adjustments_operation_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("device_cycle_ledgers.id"), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceNotification(Base):
    __tablename__ = "device_notifications"
    __table_args__ = (
        CheckConstraint(
            "event IN ('THRESHOLD_50', 'THRESHOLD_80', 'THRESHOLD_100', "
            "'AUTH_AVAILABLE_NEW_CYCLE', 'AUTH_AVAILABLE_LAST_DAY')",
            name="ck_device_notifications_event",
        ),
        CheckConstraint("status IN ('PENDING', 'SENT', 'CANCELLED')", name="ck_device_notifications_status"),
        CheckConstraint("attempt_count >= 0", name="ck_device_notifications_attempt_count_nonnegative"),
        CheckConstraint(
            "(status = 'SENT' AND sent_at IS NOT NULL) OR "
            "(status <> 'SENT' AND sent_at IS NULL)",
            name="ck_device_notifications_sent_time",
        ),
        UniqueConstraint("ledger_id", "event", name="uq_device_notifications_ledger_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("device_cycle_ledgers.id"), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class DeviceResampleJob(Base):
    __tablename__ = "device_resample_jobs"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="ck_device_resample_jobs_sequence_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'CANCELLED')",
            name="ck_device_resample_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_device_resample_jobs_attempt_count_nonnegative"),
        CheckConstraint(
            "(status IN ('COMPLETED', 'CANCELLED') AND completed_at IS NOT NULL) OR "
            "(status = 'PENDING' AND completed_at IS NULL)",
            name="ck_device_resample_jobs_completed_time",
        ),
        UniqueConstraint("association_id", "sequence", name="uq_device_resample_jobs_association_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("device_associations.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
