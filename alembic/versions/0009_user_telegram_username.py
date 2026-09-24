"""cache telegram usernames on users for /send mention resolution"""

import sqlalchemy as sa

from alembic import op

revision = "0009_user_telegram_username"
down_revision = "0008_usage_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_username", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "telegram_username")
