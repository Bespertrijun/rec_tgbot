"""persist device cycles, accounting evidence, and follow-up work"""

import sqlalchemy as sa

from alembic import op

revision = "0011_device_accounting"
down_revision = "0010_device_lifecycle"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(18, 10)


def upgrade() -> None:
    op.create_index(
        "uq_device_associations_id_user_task",
        "device_associations",
        ["id", "user_id", "task_id"],
        unique=True,
    )
    op.create_table(
        "device_quota_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=128)),
        sa.Column("weekly_percent", MONEY),
        sa.Column("last_day_allow", sa.Boolean()),
        sa.Column("last_day_checked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('INITIALIZING', 'VERIFIED', 'NEEDS_REVIEW', 'EXPIRED')",
            name="ck_device_quota_cycles_status",
        ),
        sa.CheckConstraint("reset_at > started_at", name="ck_device_quota_cycles_reset_after_start"),
        sa.CheckConstraint(
            "weekly_percent IS NULL OR (weekly_percent >= 0 AND weekly_percent < 100000000)",
            name="ck_device_quota_cycles_weekly_percent_range",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["device_task_scopes.task_id"],
            name="fk_device_quota_cycles_task_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "reset_at", name="uq_device_quota_cycles_task_reset"),
        sa.UniqueConstraint("id", "task_id", name="uq_device_quota_cycles_id_task"),
    )
    op.create_table(
        "device_cycle_ledgers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("cycle_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_used_usd", MONEY),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("quota_locked_at", sa.DateTime(timezone=True)),
        sa.Column("quota_unlocked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quality IN ('UNKNOWN', 'VERIFIED', 'NEEDS_REVIEW')",
            name="ck_device_cycle_ledgers_quality",
        ),
        sa.CheckConstraint(
            "confirmed_used_usd IS NULL OR "
            "(confirmed_used_usd >= 0 AND confirmed_used_usd < 100000000)",
            name="ck_device_cycle_ledgers_confirmed_used_range",
        ),
        sa.CheckConstraint(
            "quality <> 'VERIFIED' OR confirmed_used_usd IS NOT NULL",
            name="ck_device_cycle_ledgers_verified_has_usage",
        ),
        sa.CheckConstraint(
            "quota_unlocked_at IS NULL OR "
            "(quota_locked_at IS NOT NULL AND quota_unlocked_at >= quota_locked_at)",
            name="ck_device_cycle_ledgers_lock_time_order",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["cycle_id", "task_id"],
            ["device_quota_cycles.id", "device_quota_cycles.task_id"],
            name="fk_device_cycle_ledgers_cycle_task",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "cycle_id", name="uq_device_cycle_ledgers_user_cycle"),
        sa.UniqueConstraint("id", "user_id", "task_id", name="uq_device_cycle_ledgers_id_user_task"),
    )
    op.create_table(
        "device_usage_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("sample_key", sa.String(length=64), nullable=False),
        sa.Column("range", sa.String(length=8), nullable=False),
        sa.Column("total_usd", MONEY, nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint("range IN ('all', '7d')", name="ck_device_usage_snapshots_range"),
        sa.CheckConstraint(
            "total_usd >= 0 AND total_usd < 100000000",
            name="ck_device_usage_snapshots_total_range",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "device_id"],
            ["devices.org_id", "devices.device_id"],
            name="fk_device_usage_snapshots_org_device",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_key", name="uq_device_usage_snapshots_sample_key"),
    )
    op.create_table(
        "device_usage_segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("association_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("ledger_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("baseline_total_usd", MONEY),
        sa.Column("baseline_captured_at", sa.DateTime(timezone=True)),
        sa.Column("latest_total_usd", MONEY),
        sa.Column("latest_sampled_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_used_usd", MONEY),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "quality IN ('UNKNOWN', 'VERIFIED', 'NEEDS_REVIEW')",
            name="ck_device_usage_segments_quality",
        ),
        sa.CheckConstraint(
            "(baseline_total_usd IS NULL AND baseline_captured_at IS NULL) OR "
            "(baseline_total_usd IS NOT NULL AND baseline_captured_at IS NOT NULL)",
            name="ck_device_usage_segments_baseline_pair",
        ),
        sa.CheckConstraint(
            "(latest_total_usd IS NULL AND latest_sampled_at IS NULL) OR "
            "(latest_total_usd IS NOT NULL AND latest_sampled_at IS NOT NULL)",
            name="ck_device_usage_segments_latest_pair",
        ),
        sa.CheckConstraint(
            "baseline_total_usd IS NULL OR "
            "(baseline_total_usd >= 0 AND baseline_total_usd < 100000000)",
            name="ck_device_usage_segments_baseline_range",
        ),
        sa.CheckConstraint(
            "latest_total_usd IS NULL OR "
            "(latest_total_usd >= 0 AND latest_total_usd < 100000000)",
            name="ck_device_usage_segments_latest_range",
        ),
        sa.CheckConstraint(
            "confirmed_used_usd IS NULL OR "
            "(confirmed_used_usd >= 0 AND confirmed_used_usd < 100000000)",
            name="ck_device_usage_segments_confirmed_used_range",
        ),
        sa.CheckConstraint(
            "quality <> 'VERIFIED' OR "
            "(baseline_total_usd IS NOT NULL AND latest_total_usd IS NOT NULL AND confirmed_used_usd IS NOT NULL)",
            name="ck_device_usage_segments_verified_has_values",
        ),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_device_usage_segments_time_order"),
        sa.ForeignKeyConstraint(
            ["association_id", "user_id", "task_id"],
            ["device_associations.id", "device_associations.user_id", "device_associations.task_id"],
            name="fk_device_usage_segments_association_owner_task",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "user_id", "task_id"],
            ["device_cycle_ledgers.id", "device_cycle_ledgers.user_id", "device_cycle_ledgers.task_id"],
            name="fk_device_usage_segments_ledger_owner_task",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("association_id", "ledger_id", name="uq_device_usage_segments_association_ledger"),
    )
    op.create_table(
        "device_quota_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ledger_id", sa.Integer(), nullable=False),
        sa.Column("amount_usd", MONEY, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("transfer_key", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_usd > -100000000 AND amount_usd < 100000000",
            name="ck_device_quota_adjustments_amount_range",
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_device_quota_adjustments_reason_nonempty"),
        sa.ForeignKeyConstraint(["ledger_id"], ["device_cycle_ledgers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key", name="uq_device_quota_adjustments_operation_key"),
    )
    op.create_table(
        "device_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ledger_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(length=80)),
        sa.CheckConstraint(
            "event IN ('THRESHOLD_50', 'THRESHOLD_80', 'THRESHOLD_100', "
            "'AUTH_AVAILABLE_NEW_CYCLE', 'AUTH_AVAILABLE_LAST_DAY')",
            name="ck_device_notifications_event",
        ),
        sa.CheckConstraint("status IN ('PENDING', 'SENT', 'CANCELLED')", name="ck_device_notifications_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_device_notifications_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "(status = 'SENT' AND sent_at IS NOT NULL) OR "
            "(status <> 'SENT' AND sent_at IS NULL)",
            name="ck_device_notifications_sent_time",
        ),
        sa.ForeignKeyConstraint(["ledger_id"], ["device_cycle_ledgers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id", "event", name="uq_device_notifications_ledger_event"),
    )
    op.create_table(
        "device_resample_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("association_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("sequence >= 0", name="ck_device_resample_jobs_sequence_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'CANCELLED')",
            name="ck_device_resample_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_device_resample_jobs_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'CANCELLED') AND completed_at IS NOT NULL) OR "
            "(status = 'PENDING' AND completed_at IS NULL)",
            name="ck_device_resample_jobs_completed_time",
        ),
        sa.ForeignKeyConstraint(["association_id"], ["device_associations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("association_id", "sequence", name="uq_device_resample_jobs_association_sequence"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = (
        "device_resample_jobs",
        "device_notifications",
        "device_quota_adjustments",
        "device_usage_segments",
        "device_usage_snapshots",
        "device_cycle_ledgers",
        "device_quota_cycles",
    )
    for table_name in table_names:
        table = sa.table(table_name)
        has_rows = bind.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first()
        if has_rows is not None:
            raise RuntimeError("cannot downgrade 0011_device_accounting while device accounting tables contain data")

    op.drop_table("device_resample_jobs")
    op.drop_table("device_notifications")
    op.drop_table("device_quota_adjustments")
    op.drop_table("device_usage_segments")
    op.drop_table("device_usage_snapshots")
    op.drop_table("device_cycle_ledgers")
    op.drop_table("device_quota_cycles")
    op.drop_index("uq_device_associations_id_user_task", table_name="device_associations")
