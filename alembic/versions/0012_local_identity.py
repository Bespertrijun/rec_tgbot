"""allow local-only users while retaining legacy REC identities"""

import sqlalchemy as sa

from alembic import op

revision = "0012_local_identity"
down_revision = "0011_device_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("users", recreate="always") as batch_op:
            batch_op.alter_column(
                "reclaude_user_id",
                existing_type=sa.String(length=128),
                existing_nullable=False,
                nullable=True,
            )
        return

    op.alter_column(
        "users",
        "reclaude_user_id",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM users WHERE reclaude_user_id IS NULL LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade 0012_local_identity while users have no legacy REC identity")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("users", recreate="always") as batch_op:
            batch_op.alter_column(
                "reclaude_user_id",
                existing_type=sa.String(length=128),
                existing_nullable=True,
                nullable=False,
            )
        return

    op.alter_column(
        "users",
        "reclaude_user_id",
        existing_type=sa.String(length=128),
        existing_nullable=True,
        nullable=False,
    )
