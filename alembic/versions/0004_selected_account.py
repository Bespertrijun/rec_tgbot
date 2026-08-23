"""persist the operator-selected Reclaude account"""

import sqlalchemy as sa

from alembic import op

revision = "0004_selected_account"
down_revision = "0003_group_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_state", sa.Column("selected_account_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("service_state", "selected_account_id")
