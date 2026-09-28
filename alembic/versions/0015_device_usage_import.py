"""record explicitly imported device usage baselines"""

import sqlalchemy as sa

from alembic import op

revision = "0015_device_usage_import"
down_revision = "0014_device_auth_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("device_usage_segments", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("imported_used_usd", sa.Numeric(18, 10), nullable=True))
            batch_op.create_check_constraint(
                "ck_device_usage_segments_imported_used_range",
                "imported_used_usd IS NULL OR "
                "(imported_used_usd >= 0 AND imported_used_usd < 100000000)",
            )
        return

    op.add_column("device_usage_segments", sa.Column("imported_used_usd", sa.Numeric(18, 10), nullable=True))
    op.create_check_constraint(
        "ck_device_usage_segments_imported_used_range",
        "device_usage_segments",
        "imported_used_usd IS NULL OR "
        "(imported_used_usd >= 0 AND imported_used_usd < 100000000)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM device_usage_segments WHERE imported_used_usd IS NOT NULL LIMIT 1")
    ).first() is not None:
        raise RuntimeError("cannot downgrade 0015_device_usage_import while imported usage is recorded")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("device_usage_segments", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_device_usage_segments_imported_used_range", type_="check")
            batch_op.drop_column("imported_used_usd")
        return

    op.drop_constraint("ck_device_usage_segments_imported_used_range", "device_usage_segments", type_="check")
    op.drop_column("device_usage_segments", "imported_used_usd")
