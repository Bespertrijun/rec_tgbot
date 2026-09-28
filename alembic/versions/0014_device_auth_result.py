"""retain whether an approved device authorization reused a device"""

import sqlalchemy as sa

from alembic import op

revision = "0014_device_auth_result"
down_revision = "0013_device_task_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("device_actions", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("result_reused", sa.Boolean(), nullable=True))
        return

    op.add_column("device_actions", sa.Column("result_reused", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM device_actions WHERE result_reused IS NOT NULL LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade 0014_device_auth_result while reused results are recorded")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("device_actions", recreate="always") as batch_op:
            batch_op.drop_column("result_reused")
        return

    op.drop_column("device_actions", "result_reused")
