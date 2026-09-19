"""multi quota tasks with per-task limits and member scopes"""

import sqlalchemy as sa

from alembic import op

revision = "0006_multi_quota_tasks"
down_revision = "0005_quota_task_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("name_normalized", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="STOPPED"),
        sa.Column("scope_mode", sa.String(length=16), nullable=False, server_default="ALL"),
        sa.Column("limit_usd", sa.Numeric(18, 10), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name_normalized", name="uq_quota_tasks_name_normalized"),
    )

    # Carry the previous single-task switch and scope into a "default" task so a
    # persisted RUNNING decision survives the upgrade.
    op.execute(
        sa.text(
            "INSERT INTO quota_tasks (name, name_normalized, status, scope_mode, limit_usd, created_by, updated_by, created_at, updated_at) "
            "SELECT 'default', 'default', "
            "CASE WHEN quota_task_enabled THEN 'RUNNING' ELSE 'STOPPED' END, "
            "quota_task_scope_mode, "
            "COALESCE((SELECT quota_limit_usd FROM runtime_settings WHERE id = 1), 700), "
            "quota_task_updated_by, quota_task_updated_by, "
            "COALESCE(quota_task_updated_at, updated_at), COALESCE(quota_task_updated_at, updated_at) "
            "FROM service_state WHERE id = 1"
        )
    )
    # Databases that never ran the bot have no service_state row; give them the
    # same stopped default task a fresh single-task deployment started with.
    op.execute(
        sa.text(
            "INSERT INTO quota_tasks (name, name_normalized, status, scope_mode, limit_usd, created_by, updated_by, created_at, updated_at) "
            "SELECT 'default', 'default', 'STOPPED', 'ALL', "
            "COALESCE((SELECT quota_limit_usd FROM runtime_settings WHERE id = 1), 700), "
            "NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS (SELECT 1 FROM quota_tasks)"
        )
    )

    with op.batch_alter_table("quota_task_members") as batch:
        batch.add_column(sa.Column("task_id", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE quota_task_members SET task_id = (SELECT id FROM quota_tasks WHERE name_normalized = 'default')"))
    with op.batch_alter_table("quota_task_members") as batch:
        batch.alter_column("task_id", nullable=False)
        batch.create_foreign_key("fk_quota_task_members_task_id", "quota_tasks", ["task_id"], ["id"], ondelete="CASCADE")
        batch.drop_constraint("uq_quota_task_members_reclaude_user_id", type_="unique")
        batch.create_unique_constraint("uq_quota_task_members_task_member", ["task_id", "reclaude_user_id"])

    with op.batch_alter_table("service_state") as batch:
        batch.drop_column("quota_task_enabled")
        batch.drop_column("quota_task_scope_mode")
        batch.drop_column("quota_task_updated_by")
        batch.drop_column("quota_task_updated_at")


def downgrade() -> None:
    with op.batch_alter_table("service_state") as batch:
        batch.add_column(sa.Column("quota_task_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
        batch.add_column(sa.Column("quota_task_scope_mode", sa.String(length=16), nullable=False, server_default=sa.text("'ALL'")))
        batch.add_column(sa.Column("quota_task_updated_by", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("quota_task_updated_at", sa.DateTime(timezone=True), nullable=True))

    # Only the default task's decision and members survive the downgrade.
    op.execute(
        sa.text(
            "UPDATE service_state SET "
            "quota_task_enabled = COALESCE((SELECT status = 'RUNNING' FROM quota_tasks WHERE name_normalized = 'default'), false), "
            "quota_task_scope_mode = COALESCE((SELECT scope_mode FROM quota_tasks WHERE name_normalized = 'default'), 'ALL'), "
            "quota_task_updated_by = (SELECT updated_by FROM quota_tasks WHERE name_normalized = 'default'), "
            "quota_task_updated_at = (SELECT updated_at FROM quota_tasks WHERE name_normalized = 'default') "
            "WHERE id = 1"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM quota_task_members "
            "WHERE task_id <> COALESCE((SELECT id FROM quota_tasks WHERE name_normalized = 'default'), -1)"
        )
    )

    with op.batch_alter_table("quota_task_members") as batch:
        batch.drop_constraint("uq_quota_task_members_task_member", type_="unique")
        batch.drop_constraint("fk_quota_task_members_task_id", type_="foreignkey")
        batch.create_unique_constraint("uq_quota_task_members_reclaude_user_id", ["reclaude_user_id"])
        batch.drop_column("task_id")

    op.drop_table("quota_tasks")
