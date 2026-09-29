"""Exercise the explicit migration, not just ORM create_all, on both dialects."""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from reclaude_bot.config import validate_postgresql_database_url
from reclaude_bot.infrastructure.db.base import Base
from reclaude_bot.infrastructure.db.models import AuditLog, Device, DeviceAction, DeviceAssociation, DeviceTaskMember, ManagedGroup, QuotaAdjustment, QuotaCycle, User

ROOT = Path(__file__).parents[2]
PREVIOUS = "0009_user_telegram_username"
NEW_B1 = {"device_task_scopes", "devices", "device_associations", "device_actions"}
NEW_B2 = {"device_quota_cycles", "device_cycle_ledgers", "device_usage_snapshots", "device_usage_segments",
          "device_quota_adjustments", "device_notifications", "device_resample_jobs"}
NEW_C3 = {"device_task_members"}
NEW_RESET_NOTICES = {"device_account_notifications"}
NEW = NEW_B1 | NEW_B2 | NEW_C3 | NEW_RESET_NOTICES
NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def database_ddl(url, statement):
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_url(request, tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'devices.db'}"
    else:
        raw = os.getenv("TEST_DATABASE_URL")
        if not raw:
            pytest.skip("requires isolated TEST_DATABASE_URL")
        parent = make_url(validate_postgresql_database_url(raw))
        if not parent.database or not parent.database.endswith("_test"):
            pytest.fail("test database name must end with _test")
        name = "rec_migration_" + uuid4().hex + "_test"
        asyncio.run(database_ddl(parent, f'CREATE DATABASE "{name}"'))
        try:
            yield parent.set(database=name)
        finally:
            asyncio.run(database_ddl(parent, f'DROP DATABASE "{name}"'))


def config_for(url):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", (url.render_as_string(hide_password=False) if hasattr(url, "render_as_string") else str(url)).replace("%", "%%"))
    return config


async def inspect_database(url, *, seed=False, populate=None):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            if seed:
                await connection.execute(User.__table__.insert().values(
                    id=501, telegram_user_id=5001, email="old@example.invalid", email_normalized="old@example.invalid",
                    reclaude_user_id="legacy-501", bound_at=NOW, updated_at=NOW,
                ))
                await connection.execute(ManagedGroup.__table__.insert().values(
                    id=501, chat_id=-1000501, title="existing group", created_at=NOW, updated_at=NOW,
                ))
                await connection.execute(QuotaCycle.__table__.insert().values(id=501, started_at=NOW, reset_at=NOW, created_at=NOW))
                await connection.execute(QuotaAdjustment.__table__.insert().values(
                    id=501, user_id=501, cycle_id=501, amount_usd=Decimal("123.1234567890"),
                    reason="preserve existing balance", operator_telegram_id=1, created_at=NOW,
                ))
                await connection.execute(AuditLog.__table__.insert().values(
                    actor_type="SYSTEM", action="MIGRATION_TEST", target_type="USER", target_id="501",
                    parameters_summary={}, result="SUCCESS", created_at=NOW,
                ))
            if populate in {"scope", "association", "action"}:
                # Seed the historical schema without newer ORM column defaults.
                await connection.execute(text(
                    "INSERT INTO device_task_scopes (task_id, org_id, created_at, updated_at) VALUES (1, 178, :now, :now)"
                ), {"now": NOW})
            if populate in {"device", "association", "action"}:
                await connection.execute(Device.__table__.insert().values(
                    org_id=178, device_id=44500, name="test", first_synced_at=NOW, last_synced_at=NOW,
                ))
            if populate in {"association", "action"}:
                await connection.execute(DeviceAssociation.__table__.insert().values(
                    id=501, user_id=501, task_id=1, org_id=178, device_id=44500, state="ACTIVE", started_at=NOW, updated_at=NOW,
                ))
            if populate == "action":
                await connection.execute(DeviceAction.__table__.insert().values(
                    association_id=501, kind="REVOKE", status="UNKNOWN", idempotency_key="test-operation",
                    target_device_id=44500, attempt_count=1, created_at=NOW, updated_at=NOW,
                ))
            names = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
            rows = {}
            for name in sorted(names - {"alembic_version"}):
                rows[name] = (await connection.execute(text(f'SELECT * FROM "{name}" ORDER BY 1'))).fetchall()
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            return names, rows, version
    finally:
        await engine.dispose()


async def schema_diff(url, *, include_c3=True, include_d2=True, include_import=True, include_reset_notices=True):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            def compare(sync_connection):
                def include(obj, name, kind, reflected, compare_to):
                    if name in {"imported_used_usd", "ck_device_usage_segments_imported_used_range"} and (not include_import or not include_c3 or not include_d2):
                        return False
                    if kind == "table":
                        if name in NEW_RESET_NOTICES:
                            return include_reset_notices and include_c3 and include_d2 and include_import
                        return name in (NEW if include_c3 else NEW_B1 | NEW_B2)
                    if not include_c3 and kind == "column" and name == "scope_mode" and obj.table.name == "device_task_scopes":
                        return False
                    if not include_c3 and name == "ck_device_task_scopes_scope_mode":
                        return False
                    if (not include_c3 or not include_d2) and kind == "column" and name == "result_reused" and obj.table.name == "device_actions":
                        return False
                    return True

                context = MigrationContext.configure(sync_connection, opts={
                    "include_object": include,
                })
                return compare_metadata(context, Base.metadata)
            return await connection.run_sync(compare)
    finally:
        await engine.dispose()


def test_upgrade_preserves_legacy_rows_and_empty_downgrade_is_reversible(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, PREVIOUS)
    before_tables, before_rows, _ = asyncio.run(inspect_database(migration_url, seed=True))
    command.upgrade(config, "head")
    after_tables, after_rows, version = asyncio.run(inspect_database(migration_url))
    assert after_tables == before_tables | NEW
    assert version == ScriptDirectory.from_config(config).get_current_head()
    assert {name: after_rows[name] for name in before_rows} == before_rows
    assert all(after_rows[name] == [] for name in NEW)
    assert asyncio.run(schema_diff(migration_url)) == []
    command.downgrade(config, PREVIOUS)
    tables, rows, version = asyncio.run(inspect_database(migration_url))
    assert tables == before_tables
    assert rows == before_rows
    assert version == PREVIOUS


@pytest.mark.parametrize("populate", ["scope", "device", "association", "action"])
def test_downgrade_refuses_populated_device_tables_without_partial_deletion(migration_url, populate):
    config = config_for(migration_url)
    command.upgrade(config, "0010_device_lifecycle")
    before = asyncio.run(inspect_database(migration_url, seed=True, populate=populate))
    with pytest.raises(RuntimeError, match="contain data"):
        command.downgrade(config, PREVIOUS)
    assert asyncio.run(inspect_database(migration_url)) == before


async def assert_migrated_constraints(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            # SQLite does not enable FK checks by default; the PostgreSQL variant
            # checks them natively. Both must enforce the partial unique index.
            first = dict(user_id=501, task_id=1, org_id=178, state="PENDING_AUTH", started_at=NOW, updated_at=NOW)
            await connection.execute(DeviceAssociation.__table__.insert().values(**first))
            await connection.commit()
            with pytest.raises(IntegrityError):
                await connection.execute(DeviceAssociation.__table__.insert().values(**first))
                await connection.commit()
            await connection.rollback()
            assert await connection.scalar(select(func.count()).select_from(DeviceAssociation)) == 1
    finally:
        await engine.dispose()


def test_migration_installs_real_user_slot_constraint(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0010_device_lifecycle")
    asyncio.run(inspect_database(migration_url, seed=True, populate="scope"))
    asyncio.run(assert_migrated_constraints(migration_url))


async def populate_accounting(url, target):
    from datetime import timedelta

    from reclaude_bot.infrastructure.db.models import (
        DeviceCycleLedger,
        DeviceNotification,
        DeviceQuotaAdjustment,
        DeviceQuotaCycle,
        DeviceResampleJob,
        DeviceUsageSegment,
        DeviceUsageSnapshot,
    )

    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            if target not in {"snapshot", "resample"}:
                await connection.execute(DeviceQuotaCycle.__table__.insert().values(
                    id=501, task_id=1, started_at=NOW, reset_at=NOW + timedelta(days=7), created_at=NOW, status="INITIALIZING",
                ))
            if target in {"ledger", "segment", "adjustment", "notification"}:
                await connection.execute(DeviceCycleLedger.__table__.insert().values(
                    id=501, user_id=501, cycle_id=501, task_id=1, quality="UNKNOWN", created_at=NOW, updated_at=NOW,
                ))
            if target == "segment":
                await connection.execute(DeviceUsageSegment.__table__.insert().values(
                    association_id=501, user_id=501, task_id=1, ledger_id=501, started_at=NOW, quality="UNKNOWN",
                ))
            if target == "snapshot":
                await connection.execute(DeviceUsageSnapshot.__table__.insert().values(
                    org_id=178, device_id=44500, sample_key="sample", range="all", total_usd=Decimal("1.0000000001"), sampled_at=NOW, payload={},
                ))
            if target == "adjustment":
                await connection.execute(DeviceQuotaAdjustment.__table__.insert().values(
                    ledger_id=501, amount_usd=Decimal("1"), reason="test", operator_telegram_id=1, operation_key="adjustment", created_at=NOW,
                ))
            if target == "notification":
                await connection.execute(DeviceNotification.__table__.insert().values(
                    ledger_id=501, event="THRESHOLD_50", status="PENDING", attempt_count=0, created_at=NOW, updated_at=NOW, payload={},
                ))
            if target == "resample":
                await connection.execute(DeviceResampleJob.__table__.insert().values(
                    association_id=501, sequence=0, run_after=NOW, status="PENDING", attempt_count=0, created_at=NOW, updated_at=NOW,
                ))
    finally:
        await engine.dispose()


def test_accounting_upgrade_and_empty_downgrade_preserve_populated_lifecycle(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0010_device_lifecycle")
    before_tables, before_rows, _ = asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    command.upgrade(config, "0011_device_accounting")
    after_tables, after_rows, _ = asyncio.run(inspect_database(migration_url))
    assert after_tables == before_tables | NEW_B2
    assert {name: after_rows[name] for name in before_rows} == before_rows
    assert asyncio.run(schema_diff(migration_url, include_c3=False)) == []
    command.downgrade(config, "0010_device_lifecycle")
    tables, rows, version = asyncio.run(inspect_database(migration_url))
    assert tables == before_tables
    assert rows == before_rows
    assert version == "0010_device_lifecycle"


@pytest.mark.parametrize("target", ["cycle", "ledger", "snapshot", "segment", "adjustment", "notification", "resample"])
def test_accounting_downgrade_refuses_any_new_data_without_deleting_history(migration_url, target):
    config = config_for(migration_url)
    command.upgrade(config, "0011_device_accounting")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(populate_accounting(migration_url, target))
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError, match="contain data"):
        command.downgrade(config, "0010_device_lifecycle")
    assert asyncio.run(inspect_database(migration_url)) == before


async def nullable_legacy_identity(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(lambda c: inspect(c).get_columns("users"))
            return next(c["nullable"] for c in columns if c["name"] == "reclaude_user_id")
    finally:
        await engine.dispose()


async def add_local_users(url):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(User.__table__.insert(), [
                dict(id=i, telegram_user_id=5000+i, email=f"local-{i}@example.invalid", email_normalized=f"local-{i}@example.invalid",
                     reclaude_user_id=None, bound_at=NOW, updated_at=NOW)
                for i in (502, 503)
            ])
    finally:
        await engine.dispose()


def test_local_identity_migration_preserves_history_and_reverts_when_no_local_users(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0011_device_accounting")
    before = asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    assert asyncio.run(nullable_legacy_identity(migration_url)) is False
    command.upgrade(config, "0012_local_identity")
    after = asyncio.run(inspect_database(migration_url))
    assert after[:2] == before[:2]
    assert asyncio.run(nullable_legacy_identity(migration_url)) is True
    command.downgrade(config, "0011_device_accounting")
    assert asyncio.run(inspect_database(migration_url)) == before
    assert asyncio.run(nullable_legacy_identity(migration_url)) is False


def test_local_identity_downgrade_does_not_delete_users_without_rec_id(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0012_local_identity")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(add_local_users(migration_url))
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError):
        command.downgrade(config, "0011_device_accounting")
    assert asyncio.run(inspect_database(migration_url)) == before
    assert asyncio.run(nullable_legacy_identity(migration_url)) is True


async def set_migrated_scope(url, mode, *, member=False):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE device_task_scopes SET scope_mode=:mode WHERE task_id=1"), {"mode": mode})
            if member:
                await connection.execute(DeviceTaskMember.__table__.insert().values(task_id=1, user_id=501, added_by=99, added_at=NOW))
    finally:
        await engine.dispose()


def test_local_task_scope_upgrade_keeps_existing_device_history_and_can_revert(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0012_local_identity")
    before_tables, before_rows, before_version = asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    command.upgrade(config, "0013_device_task_members")
    after_tables, after_rows, _ = asyncio.run(inspect_database(migration_url))
    assert after_tables == before_tables | NEW_C3
    for name, rows in before_rows.items():
        if name != "device_task_scopes":
            assert after_rows[name] == rows
    assert after_rows["device_task_scopes"][0][-1] == "ALL"
    assert asyncio.run(schema_diff(migration_url, include_d2=False)) == []
    command.downgrade(config, "0012_local_identity")
    assert asyncio.run(inspect_database(migration_url)) == (before_tables, before_rows, before_version)


@pytest.mark.parametrize("mode,member", [("ALLOWLIST", False), ("EXCLUDE", False), ("ALL", True)])
def test_local_scope_downgrade_preserves_empty_allowlist_and_members(migration_url, mode, member):
    config = config_for(migration_url)
    command.upgrade(config, "0013_device_task_members")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(set_migrated_scope(migration_url, mode, member=member))
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError, match="cannot downgrade 0013"):
        command.downgrade(config, "0012_local_identity")
    assert asyncio.run(inspect_database(migration_url)) == before


async def assert_migrated_scope_constraints(url):
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine.sync_engine, "connect")
        def enable_fks(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    try:
        async with engine.begin() as connection:
            await connection.execute(DeviceTaskMember.__table__.insert().values(task_id=1, user_id=501, added_at=NOW))
        for task_id, user_id in [(1, 501), (1, 9999), (9999, 501)]:
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(DeviceTaskMember.__table__.insert().values(task_id=task_id, user_id=user_id, added_at=NOW))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE device_task_scopes SET scope_mode='INVALID'"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM users WHERE id=501"))
        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(DeviceTaskMember)) == 1
            if engine.dialect.name == "sqlite":
                assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()


def test_local_scope_migration_installs_constraints_on_existing_history(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0012_local_identity")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    command.upgrade(config, "head")
    asyncio.run(assert_migrated_scope_constraints(migration_url))


def test_auth_result_upgrade_preserves_existing_actions_and_empty_result_can_revert(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0013_device_task_members")
    tables, rows, version = asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    command.upgrade(config, "0014_device_auth_result")
    after_tables, after_rows, _ = asyncio.run(inspect_database(migration_url))
    assert after_tables == tables
    for name in rows:
        if name != "device_actions":
            assert after_rows[name] == rows[name]
    assert tuple(after_rows["device_actions"][0][:-1]) == tuple(rows["device_actions"][0])
    assert after_rows["device_actions"][0][-1] is None
    assert asyncio.run(schema_diff(migration_url, include_import=False)) == []
    command.downgrade(config, "0013_device_task_members")
    assert asyncio.run(inspect_database(migration_url)) == (tables, rows, version)


async def save_auth_result(url, reused):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(DeviceAction.__table__.update().values(result_reused=reused))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("reused", [True, False])
def test_auth_result_downgrade_never_discards_approval_evidence(migration_url, reused):
    config = config_for(migration_url)
    # Test the 0014 guard itself, not rollback of later revisions' DDL.
    command.upgrade(config, "0014_device_auth_result")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(save_auth_result(migration_url, reused))
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError, match="cannot downgrade 0014"):
        command.downgrade(config, "0013_device_task_members")
    assert asyncio.run(inspect_database(migration_url)) == before


@pytest.mark.parametrize("migration_url", ["postgresql"], indirect=True)
def test_import_column_upgrade_preserves_old_usage_and_bindings(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0014_device_auth_result")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(populate_accounting(migration_url, "segment"))
    tables, rows, version = asyncio.run(inspect_database(migration_url))
    command.upgrade(config, "0015_device_usage_import")
    new_tables, new_rows, _ = asyncio.run(inspect_database(migration_url))
    assert new_tables == tables
    for name, old_rows in rows.items():
        if name == "device_usage_segments":
            assert [tuple(row[:-1]) for row in new_rows[name]] == [tuple(row) for row in old_rows]
            assert all(row[-1] is None for row in new_rows[name])
        else:
            assert new_rows[name] == old_rows
    assert asyncio.run(schema_diff(migration_url, include_reset_notices=False)) == []
    command.downgrade(config, "0014_device_auth_result")
    assert asyncio.run(inspect_database(migration_url)) == (tables, rows, version)


@pytest.mark.parametrize("migration_url", ["postgresql"], indirect=True)
def test_import_evidence_cannot_be_lost_by_downgrade(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0015_device_usage_import")
    asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    asyncio.run(populate_accounting(migration_url, "segment"))

    async def imported():
        engine = create_async_engine(migration_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE device_usage_segments SET imported_used_usd = 0"))
        finally:
            await engine.dispose()

    asyncio.run(imported())
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError):
        command.downgrade(config, "0014_device_auth_result")
    assert asyncio.run(inspect_database(migration_url)) == before
