"""persist device scopes, associations, and lifecycle actions"""

import sqlalchemy as sa

from alembic import op

revision = "0010_device_lifecycle"
down_revision = "0009_user_telegram_username"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_task_scopes",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("org_id > 0", name="ck_device_task_scopes_org_id_positive"),
        sa.ForeignKeyConstraint(["task_id"], ["quota_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("org_id", name="uq_device_task_scopes_org_id"),
        sa.UniqueConstraint("task_id", "org_id", name="uq_device_task_scopes_task_org"),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("first_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("org_id > 0", name="ck_devices_org_id_positive"),
        sa.CheckConstraint("device_id > 0", name="ck_devices_device_id_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "device_id", name="uq_devices_org_device"),
    )
    op.create_table(
        "device_associations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger()),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("end_reason", sa.String(length=64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING_AUTH', 'ACTIVE', 'PENDING_REVOKE', 'UNKNOWN', 'ENDED')",
            name="ck_device_associations_state",
        ),
        sa.CheckConstraint("device_id IS NULL OR device_id > 0", name="ck_device_associations_device_id_positive"),
        sa.CheckConstraint(
            "state NOT IN ('ACTIVE', 'PENDING_REVOKE') OR device_id IS NOT NULL",
            name="ck_device_associations_state_requires_device",
        ),
        sa.CheckConstraint(
            "(state = 'ENDED' AND ended_at IS NOT NULL) OR (state <> 'ENDED' AND ended_at IS NULL)",
            name="ck_device_associations_ended_state_time",
        ),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_device_associations_time_order"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["task_id", "org_id"],
            ["device_task_scopes.task_id", "device_task_scopes.org_id"],
            name="fk_device_associations_task_org_scope",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "device_id"],
            ["devices.org_id", "devices.device_id"],
            name="fk_device_associations_org_device",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_device_associations_open_user",
        "device_associations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "uq_device_associations_open_device",
        "device_associations",
        ["org_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL AND device_id IS NOT NULL"),
        sqlite_where=sa.text("ended_at IS NULL AND device_id IS NOT NULL"),
    )
    op.create_table(
        "device_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("association_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("target_device_id", sa.BigInteger()),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('AUTH', 'REVOKE')", name="ck_device_actions_kind"),
        sa.CheckConstraint("status IN ('PENDING', 'UNKNOWN', 'SUCCEEDED', 'FAILED')", name="ck_device_actions_status"),
        sa.CheckConstraint("target_device_id IS NULL OR target_device_id > 0", name="ck_device_actions_target_device_positive"),
        sa.CheckConstraint("kind <> 'REVOKE' OR target_device_id IS NOT NULL", name="ck_device_actions_revoke_requires_device"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_device_actions_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL) OR "
            "(status IN ('PENDING', 'UNKNOWN') AND completed_at IS NULL)",
            name="ck_device_actions_completion_time",
        ),
        sa.ForeignKeyConstraint(["association_id"], ["device_associations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_device_actions_idempotency_key"),
    )
    op.create_index(
        "uq_device_actions_pending_association",
        "device_actions",
        ["association_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'UNKNOWN')"),
        sqlite_where=sa.text("status IN ('PENDING', 'UNKNOWN')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = ("device_actions", "device_associations", "devices", "device_task_scopes")
    for table_name in table_names:
        table = sa.table(table_name)
        has_rows = bind.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first()
        if has_rows is not None:
            raise RuntimeError("cannot downgrade 0010_device_lifecycle while device lifecycle tables contain data")

    op.drop_table("device_actions")
    op.drop_table("device_associations")
    op.drop_table("devices")
    op.drop_table("device_task_scopes")
