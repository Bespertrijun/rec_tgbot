"""recovery gate and append-only audit enforcement"""

import sqlalchemy as sa

from alembic import op

revision = "0002_recovery_audit"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("service_state"):
        op.create_table(
            "service_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("write_enabled", sa.Boolean(), nullable=False),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if bind.dialect.name == "postgresql":
        inspector = sa.inspect(bind)
        existing_constraints = {item["name"] for item in inspector.get_unique_constraints("quota_cycles")}
        existing_indexes = {item["name"] for item in inspector.get_indexes("quota_cycles")}
        if "uq_quota_cycles_reset_at" not in existing_constraints | existing_indexes:
            op.create_unique_constraint("uq_quota_cycles_reset_at", "quota_cycles", ["reset_at"])
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs"))
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION audit_logs_append_only_guard()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_logs is append-only';
                END;
                $$;
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER audit_logs_append_only
                BEFORE UPDATE OR DELETE ON audit_logs
                FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only_guard()
                """
            )
        )
    else:
        op.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS uq_quota_cycles_reset_at ON quota_cycles (reset_at)"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only_delete"))
        op.execute(
            sa.text(
                """
                CREATE TRIGGER audit_logs_append_only_update
                BEFORE UPDATE ON audit_logs
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs is append-only');
                END
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER audit_logs_append_only_delete
                BEFORE DELETE ON audit_logs
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs is append-only');
                END
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS audit_logs_append_only_guard()"))
        inspector = sa.inspect(bind)
        existing_constraints = {item["name"] for item in inspector.get_unique_constraints("quota_cycles")}
        existing_indexes = {item["name"] for item in inspector.get_indexes("quota_cycles")}
        if "uq_quota_cycles_reset_at" in existing_constraints:
            op.drop_constraint("uq_quota_cycles_reset_at", "quota_cycles", type_="unique")
        elif "uq_quota_cycles_reset_at" in existing_indexes:
            op.drop_index("uq_quota_cycles_reset_at", table_name="quota_cycles")
    else:
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_logs_append_only_delete"))
        op.execute(sa.text("DROP INDEX IF EXISTS uq_quota_cycles_reset_at"))
    if sa.inspect(bind).has_table("service_state"):
        op.drop_table("service_state")
