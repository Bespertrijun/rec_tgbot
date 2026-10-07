"""Persist round estimates and the allocation made at each round boundary."""

import sqlalchemy as sa

from alembic import op

revision = "0018_round_quota"
down_revision = "0017_account_usage_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("device_quota_cycles") as batch:
        for name in ("estimated_total_usd", "estimate_used_usd", "estimate_percent", "opening_limit_usd", "quota_source_estimate_usd"):
            batch.add_column(sa.Column(name, sa.Numeric(18, 10), nullable=True))
        for name in ("estimate_snapshot_at", "estimate_recorded_at"):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("estimate_blocked", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("quota_source_cycle_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_cycle_quota_source", "device_quota_cycles", ["quota_source_cycle_id"], ["id"])
        batch.add_column(sa.Column("quota_reason", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_quota_cycles") as batch:
        batch.drop_constraint("fk_cycle_quota_source", type_="foreignkey")
        for name in ("quota_reason", "quota_source_cycle_id", "estimate_blocked", "estimate_recorded_at", "estimate_snapshot_at",
                     "quota_source_estimate_usd", "opening_limit_usd", "estimate_percent", "estimate_used_usd", "estimated_total_usd"):
            batch.drop_column(name)
