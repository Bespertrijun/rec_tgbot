"""persist quota task lifecycle and member scope"""

import sqlalchemy as sa

from alembic import op

revision = "0005_quota_task_controls"
down_revision = "0004_selected_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_state",
        sa.Column("quota_task_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "service_state",
        sa.Column("quota_task_scope_mode", sa.String(length=16), nullable=False, server_default=sa.text("'ALL'")),
    )
    op.add_column("service_state", sa.Column("quota_task_updated_by", sa.BigInteger(), nullable=True))
    op.add_column("service_state", sa.Column("quota_task_updated_at", sa.DateTime(timezone=True), nullable=True))

    # Existing deployments used write_enabled as the durable operator decision.
    # Preserve that decision while new databases keep the explicit task switch stopped.
    op.execute(
        sa.text(
            "UPDATE service_state "
            "SET quota_task_enabled = write_enabled, "
            "quota_task_updated_at = updated_at "
            "WHERE quota_task_updated_at IS NULL"
        )
    )

    op.create_table(
        "quota_task_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reclaude_user_id", sa.String(length=128), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reclaude_user_id", name="uq_quota_task_members_reclaude_user_id"),
    )


def downgrade() -> None:
    op.drop_table("quota_task_members")
    op.drop_column("service_state", "quota_task_updated_at")
    op.drop_column("service_state", "quota_task_updated_by")
    op.drop_column("service_state", "quota_task_scope_mode")
    op.drop_column("service_state", "quota_task_enabled")
