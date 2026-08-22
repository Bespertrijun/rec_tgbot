from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "0002_recovery_audit.py"


def _alembic_config(database: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    return config


def test_sqlite_upgrade_enforces_append_only_and_downgrade_cleans_up(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database = tmp_path / "migration.db"
    config = _alembic_config(database)

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO audit_logs
                        (actor_telegram_id, actor_type, action, target_type, target_id,
                         parameters_summary, result, created_at)
                    VALUES (NULL, 'SYSTEM', 'SYNC', 'USER', '1', '{}', 'SUCCESS',
                            '2026-08-19T00:00:00+00:00')
                    """
                )
            )

        with engine.connect() as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_logs'")
                )
            }
            index_names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'uq_quota_cycles_reset_at'")
                )
            }
        assert trigger_names == {"audit_logs_append_only_update", "audit_logs_append_only_delete"}
        assert index_names == {"uq_quota_cycles_reset_at"}

        with engine.begin() as connection:
            with pytest.raises(IntegrityError, match="audit_logs is append-only"):
                connection.execute(text("UPDATE audit_logs SET action = 'ALTERED' WHERE id = 1"))

        with engine.begin() as connection:
            with pytest.raises(IntegrityError, match="audit_logs is append-only"):
                connection.execute(text("DELETE FROM audit_logs WHERE id = 1"))

        assert inspect(engine).has_table("service_state")
    finally:
        engine.dispose()

    command.downgrade(config, "0001_initial")

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            remaining_triggers = connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_logs'")
            ).all()
            assert remaining_triggers == []
            remaining_indexes = connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'uq_quota_cycles_reset_at'")
            ).all()
            assert remaining_indexes == []
            assert not inspect(engine).has_table("service_state")
    finally:
        engine.dispose()


def test_postgresql_append_only_ddl_is_explicit_and_reversible() -> None:
    source = MIGRATION.read_text()

    assert "DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs" in source
    assert "CREATE TRIGGER audit_logs_append_only" in source
    assert "BEFORE UPDATE OR DELETE ON audit_logs" in source
    assert "EXECUTE FUNCTION audit_logs_append_only_guard()" in source
    assert "DROP FUNCTION IF EXISTS audit_logs_append_only_guard()" in source


def test_sqlite_group_onboarding_schema_is_reversible(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database = tmp_path / "group-onboarding.db"
    config = _alembic_config(database)

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    try:
        database_inspector = inspect(engine)
        assert database_inspector.has_table("managed_groups")
        assert database_inspector.has_table("group_memberships")

        managed_group_columns = {column["name"]: column for column in database_inspector.get_columns("managed_groups")}
        assert {
            "chat_id",
            "title",
            "status",
            "discovered_by_telegram_id",
            "approved_by_telegram_id",
            "bot_permissions",
            "disable_reason",
            "approved_at",
            "disabled_at",
            "created_at",
            "updated_at",
        } <= managed_group_columns.keys()
        assert "BIGINT" in str(managed_group_columns["chat_id"]["type"]).upper()

        membership_columns = {column["name"]: column for column in database_inspector.get_columns("group_memberships")}
        assert {
            "chat_id",
            "telegram_user_id",
            "generation",
            "joined_at",
            "deadline",
            "state",
            "bound_at",
            "unmute_requested_at",
            "unmuted_at",
            "removal_requested_at",
            "removed_at",
            "verification_token_hash",
            "pending_action",
            "action_attempt_id",
            "retry_count",
            "last_attempt_at",
            "next_retry_at",
            "last_error",
            "created_at",
            "updated_at",
        } <= membership_columns.keys()
        assert "BIGINT" in str(membership_columns["telegram_user_id"]["type"]).upper()

        managed_group_constraints = {
            constraint["name"] for constraint in database_inspector.get_unique_constraints("managed_groups")
        }
        membership_constraints = {
            constraint["name"] for constraint in database_inspector.get_unique_constraints("group_memberships")
        }
        assert "uq_managed_groups_chat_id" in managed_group_constraints
        assert "uq_group_memberships_chat_user" in membership_constraints

        index_names = {
            index["name"]
            for table in ("managed_groups", "group_memberships")
            for index in database_inspector.get_indexes(table)
        }
        assert {
            "ix_managed_groups_status",
            "ix_group_memberships_state_deadline",
            "ix_group_memberships_user_state",
            "ix_group_memberships_retry",
        } <= index_names

        foreign_keys = database_inspector.get_foreign_keys("group_memberships")
        assert any(
            foreign_key["referred_table"] == "managed_groups"
            and foreign_key["constrained_columns"] == ["chat_id"]
            and foreign_key["referred_columns"] == ["chat_id"]
            for foreign_key in foreign_keys
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO managed_groups
                        (chat_id, title, status, bot_permissions, created_at, updated_at)
                    VALUES (-100123, 'Test group', 'PENDING', '{}',
                            '2026-08-19T00:00:00+00:00', '2026-08-19T00:00:00+00:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO group_memberships
                        (chat_id, telegram_user_id, joined_at, deadline, created_at, updated_at)
                    VALUES (-100123, 9223372036854770000,
                            '2026-08-19T00:00:00+00:00', '2026-08-19T00:05:00+00:00',
                            '2026-08-19T00:00:00+00:00', '2026-08-19T00:00:00+00:00')
                    """
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, "0002_recovery_audit")

    engine = create_engine(f"sqlite:///{database}")
    try:
        assert not inspect(engine).has_table("group_memberships")
        assert not inspect(engine).has_table("managed_groups")
    finally:
        engine.dispose()
