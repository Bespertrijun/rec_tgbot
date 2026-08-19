"""initial explicit schema"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

MONEY = sa.Numeric(18, 10)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("reclaude_user_id", sa.String(length=128), nullable=False),
        sa.Column("binding_status", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("baseline_status", sa.String(length=32), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        sa.UniqueConstraint("reclaude_user_id", name="uq_users_reclaude_user_id"),
    )
    op.create_table(
        "upstream_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_record_id", sa.String(length=128)),
        sa.Column("reclaude_user_id", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("account_id", sa.String(length=128)),
        sa.Column("total_usage_usd", MONEY, nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reclaude_user_id", name="uq_upstream_members_reclaude_user_id"),
    )
    op.create_index("ix_upstream_members_email_normalized", "upstream_members", ["email_normalized"])
    op.create_table(
        "quota_cycles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weekly_percent", MONEY),
        sa.Column("source_account_id", sa.Integer()),
        sa.Column("source_account_email_masked", sa.String(length=320)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_day_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_day_allow", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quota_cycles_status_reset", "quota_cycles", ["status", "reset_at"])
    op.create_table(
        "member_cycle_baselines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reclaude_user_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("quota_cycles.id"), nullable=False),
        sa.Column("baseline_total_usd", MONEY, nullable=False),
        sa.Column("baseline_captured_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64)),
        sa.UniqueConstraint("reclaude_user_id", "cycle_id", name="uq_member_cycle_baseline"),
    )
    op.create_table(
        "quota_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("quota_cycles.id"), nullable=False),
        sa.Column("amount_usd", MONEY, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "quota_revocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("quota_cycles.id"), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("pending_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("restored_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("user_id", "cycle_id", name="uq_quota_revocation_user_cycle"),
    )
    op.create_index("ix_quota_revocations_state", "quota_revocations", ["cycle_id", "state"])
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quota_limit_usd", MONEY, nullable=False),
        sa.Column("updated_by", sa.BigInteger()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_telegram_id", sa.BigInteger()),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("parameters_summary", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("runtime_settings")
    op.drop_index("ix_quota_revocations_state", table_name="quota_revocations")
    op.drop_table("quota_revocations")
    op.drop_table("quota_adjustments")
    op.drop_table("member_cycle_baselines")
    op.drop_index("ix_quota_cycles_status_reset", table_name="quota_cycles")
    op.drop_table("quota_cycles")
    op.drop_index("ix_upstream_members_email_normalized", table_name="upstream_members")
    op.drop_table("upstream_members")
    op.drop_table("users")
