"""durable notifications for automatic device-account cycle changes"""

import sqlalchemy as sa

from alembic import op

revision = "0016_account_notifications"
down_revision = "0015_device_usage_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_account_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("quota_tasks.id"), nullable=False),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("device_quota_cycles.id"), nullable=True),
        sa.Column("generation_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("recipient_type", sa.String(16), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.CheckConstraint(
            "kind IN ('ACCOUNT_RESET_SUCCESS', 'ACCOUNT_RESET_FAILURE')",
            name="ck_device_account_notifications_kind",
        ),
        sa.CheckConstraint(
            "recipient_type IN ('USER', 'ADMIN')",
            name="ck_device_account_notifications_recipient_type",
        ),
        sa.CheckConstraint("status IN ('PENDING', 'SENT', 'CANCELLED')", name="ck_device_account_notifications_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_device_account_notifications_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "(status = 'SENT' AND sent_at IS NOT NULL) OR "
            "(status <> 'SENT' AND sent_at IS NULL)",
            name="ck_device_account_notifications_sent_time",
        ),
    )
    op.create_index(
        "ix_device_account_notifications_pending",
        "device_account_notifications",
        ["status", "next_retry_at", "created_at"],
    )
    op.create_index(
        "uq_device_account_notifications_user_generation",
        "device_account_notifications",
        ["task_id", "generation_key", "kind", "recipient_type", "user_id"],
        unique=True,
        postgresql_where=sa.text("recipient_type = 'USER'"),
        sqlite_where=sa.text("recipient_type = 'USER'"),
    )
    op.create_index(
        "uq_device_account_notifications_admin_user_generation",
        "device_account_notifications",
        ["task_id", "generation_key", "kind", "recipient_type", "user_id", "recipient_id"],
        unique=True,
        postgresql_where=sa.text("recipient_type = 'ADMIN' AND user_id IS NOT NULL"),
        sqlite_where=sa.text("recipient_type = 'ADMIN' AND user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_device_account_notifications_failure_admin_generation",
        "device_account_notifications",
        ["task_id", "generation_key", "kind", "recipient_type", "recipient_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'ACCOUNT_RESET_FAILURE' AND recipient_type = 'ADMIN'"),
        sqlite_where=sa.text("kind = 'ACCOUNT_RESET_FAILURE' AND recipient_type = 'ADMIN'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM device_account_notifications LIMIT 1")).first() is not None:
        raise RuntimeError("refusing to drop non-empty device_account_notifications")
    op.drop_index("uq_device_account_notifications_failure_admin_generation", table_name="device_account_notifications")
    op.drop_index("uq_device_account_notifications_admin_user_generation", table_name="device_account_notifications")
    op.drop_index("uq_device_account_notifications_user_generation", table_name="device_account_notifications")
    op.drop_index("ix_device_account_notifications_pending", table_name="device_account_notifications")
    op.drop_table("device_account_notifications")
