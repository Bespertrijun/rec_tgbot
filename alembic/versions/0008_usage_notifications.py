"""per-user per-cycle usage threshold notifications"""

import sqlalchemy as sa

from alembic import op

revision = "0008_usage_notifications"
down_revision = "0007_usage_sync_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("quota_cycles.id"), nullable=False),
        sa.Column("threshold_percent", sa.Integer(), nullable=False),
        sa.Column("used_usd", sa.Numeric(18, 10), nullable=False),
        sa.Column("limit_usd", sa.Numeric(18, 10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "cycle_id", "threshold_percent", name="uq_usage_notifications_user_cycle_threshold"),
    )


def downgrade() -> None:
    op.drop_table("usage_notifications")
