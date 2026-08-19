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
