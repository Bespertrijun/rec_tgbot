"""operator switch for the usage-sync statistics loop"""

import sqlalchemy as sa

from alembic import op

revision = "0007_usage_sync_switch"
down_revision = "0006_multi_quota_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_state", sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("service_state", "sync_enabled")
