"""Upgrade existing ledgers and guard durable notification history on downgrade."""
import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from reclaude_bot.infrastructure.db.models import DeviceAccountNotification
from tests.integration.test_device_migration import (
    NOW,
    config_for,
    inspect_database,
    schema_diff,
)
from tests.integration.test_device_migration import (
    migration_url as shared_migration_url,
)

migration_url = shared_migration_url


async def insert_notice(url, **overrides):
    engine = create_async_engine(url)
    values = dict(task_id=1, generation_key="reset:501:user:501", kind="ACCOUNT_RESET_SUCCESS",
                  recipient_type="USER", recipient_id=5001, user_id=501, status="PENDING",
                  attempt_count=0, created_at=NOW, updated_at=NOW, payload={"text": "reset complete"})
    values.update(overrides)
    try:
        async with engine.begin() as connection:
            await connection.execute(DeviceAccountNotification.__table__.insert().values(**values))
    finally:
        await engine.dispose()


def test_notification_upgrade_preserves_history_and_empty_downgrade(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0015_device_usage_import")
    before_tables, before_rows, before_version = asyncio.run(inspect_database(migration_url, seed=True, populate="action"))
    command.upgrade(config, "head")
    tables, rows, _ = asyncio.run(inspect_database(migration_url))
    assert tables == before_tables | {"device_account_notifications"}
    assert {name: rows[name] for name in before_rows} == before_rows
    assert rows["device_account_notifications"] == []
    assert asyncio.run(schema_diff(migration_url)) == []
    command.downgrade(config, "0015_device_usage_import")
    assert asyncio.run(inspect_database(migration_url)) == (before_tables, before_rows, before_version)


def test_notification_migration_enforces_dedupe_and_preserves_pending_delivery(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "head")
    asyncio.run(inspect_database(migration_url, seed=True))
    asyncio.run(insert_notice(migration_url))
    with pytest.raises(IntegrityError):
        asyncio.run(insert_notice(migration_url))
    # Same text mirrored to an admin is an independent delivery.
    asyncio.run(insert_notice(migration_url, recipient_type="ADMIN", recipient_id=999))
    with pytest.raises(IntegrityError):
        asyncio.run(insert_notice(migration_url, recipient_id=5002, status="SENT"))
    before = asyncio.run(inspect_database(migration_url))
    with pytest.raises(RuntimeError):
        command.downgrade(config, "0015_device_usage_import")
    assert asyncio.run(inspect_database(migration_url)) == before

    async def clear_notices():
        engine = create_async_engine(migration_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM device_account_notifications"))
        finally:
            await engine.dispose()

    asyncio.run(clear_notices())
    command.downgrade(config, "0015_device_usage_import")
