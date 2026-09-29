"""Persist the organization usage refresh cooldown."""

import sqlalchemy as sa

from alembic import op

revision = "0017_account_usage_refresh"
down_revision = "0016_account_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_state", sa.Column("account_usage_refresh_attempted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("service_state", "account_usage_refresh_attempted_at")
