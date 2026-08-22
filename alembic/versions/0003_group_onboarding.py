"""persist managed groups and current onboarding membership state"""

import sqlalchemy as sa

from alembic import op

revision = "0003_group_onboarding"
down_revision = "0002_recovery_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("discovered_by_telegram_id", sa.BigInteger()),
        sa.Column("approved_by_telegram_id", sa.BigInteger()),
        sa.Column("bot_permissions", sa.JSON(), nullable=False),
        sa.Column("disable_reason", sa.Text()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", name="uq_managed_groups_chat_id"),
    )
    op.create_index("ix_managed_groups_status", "managed_groups", ["status"])

    op.create_table(
        "group_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("managed_groups.chat_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'RESTRICT_PENDING'"),
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("unmute_requested_at", sa.DateTime(timezone=True)),
        sa.Column("unmuted_at", sa.DateTime(timezone=True)),
        sa.Column("removal_requested_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("verification_token_hash", sa.String(length=128)),
        sa.Column("verification_started_at", sa.DateTime(timezone=True)),
        sa.Column("pending_action", sa.String(length=32)),
        sa.Column("action_attempt_id", sa.String(length=64)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_alerted_retry_count", sa.Integer()),
        sa.Column("last_alerted_action", sa.String(length=32)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "telegram_user_id", name="uq_group_memberships_chat_user"),
    )
    op.create_index(
        "ix_group_memberships_state_deadline",
        "group_memberships",
        ["state", "deadline"],
    )
    op.create_index(
        "ix_group_memberships_user_state",
        "group_memberships",
        ["telegram_user_id", "state"],
    )
    op.create_index(
        "ix_group_memberships_retry",
        "group_memberships",
        ["state", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_group_memberships_retry", table_name="group_memberships")
    op.drop_index("ix_group_memberships_user_state", table_name="group_memberships")
    op.drop_index("ix_group_memberships_state_deadline", table_name="group_memberships")
    op.drop_table("group_memberships")
    op.drop_index("ix_managed_groups_status", table_name="managed_groups")
    op.drop_table("managed_groups")
