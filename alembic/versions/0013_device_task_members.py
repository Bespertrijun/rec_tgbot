"""add local user scopes for device tasks"""

import sqlalchemy as sa

from alembic import op

revision = "0013_device_task_members"
down_revision = "0012_local_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("device_task_scopes", recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "scope_mode",
                    sa.String(length=16),
                    server_default=sa.text("'ALL'"),
                    nullable=False,
                )
            )
            batch_op.create_check_constraint(
                "ck_device_task_scopes_scope_mode",
                "scope_mode IN ('ALL', 'ALLOWLIST', 'EXCLUDE')",
            )
    else:
        op.add_column(
            "device_task_scopes",
            sa.Column("scope_mode", sa.String(length=16), server_default=sa.text("'ALL'"), nullable=False),
        )
        op.create_check_constraint(
            "ck_device_task_scopes_scope_mode",
            "device_task_scopes",
            "scope_mode IN ('ALL', 'ALLOWLIST', 'EXCLUDE')",
        )

    op.create_table(
        "device_task_members",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("added_by", sa.BigInteger()),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["device_task_scopes.task_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("task_id", "user_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM device_task_members LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade 0013_device_task_members while device task members exist")
    if (
        bind.execute(sa.text("SELECT 1 FROM device_task_scopes WHERE scope_mode IS NULL OR scope_mode <> 'ALL' LIMIT 1")).first()
        is not None
    ):
        raise RuntimeError("cannot downgrade 0013_device_task_members while a device task scope is not ALL")

    op.drop_table("device_task_members")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("device_task_scopes", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_device_task_scopes_scope_mode", type_="check")
            batch_op.drop_column("scope_mode")
    else:
        op.drop_constraint("ck_device_task_scopes_scope_mode", "device_task_scopes", type_="check")
        op.drop_column("device_task_scopes", "scope_mode")
