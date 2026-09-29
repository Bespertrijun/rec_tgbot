"""Check the durable refresh timestamp against an existing service-state row."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.integration.test_device_migration import config_for
from tests.integration.test_device_migration import migration_url as shared_migration_url

migration_url = shared_migration_url


async def service_state(url, *, seed=False, attempted=False):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            if seed:
                await connection.execute(text(
                    "INSERT INTO service_state (id, write_enabled, sync_enabled, reason, selected_account_id, updated_at) "
                    "VALUES (1, true, true, 'existing', '7022', '2026-09-29 00:00:00+00:00')"
                ))
            if attempted:
                await connection.execute(text(
                    "UPDATE service_state SET account_usage_refresh_attempted_at = '2026-09-29 01:00:00+00:00' WHERE id = 1"
                ))
            return dict((await connection.execute(text("SELECT * FROM service_state WHERE id = 1"))).mappings().one())
    finally:
        await engine.dispose()


def test_refresh_migration_preserves_existing_state_and_is_reversible(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0016_account_notifications")
    before = asyncio.run(service_state(migration_url, seed=True))
    command.upgrade(config, "0017_account_usage_refresh")
    after = asyncio.run(service_state(migration_url))
    assert after.pop("account_usage_refresh_attempted_at") is None
    assert after == before
    assert asyncio.run(service_state(migration_url, attempted=True))["account_usage_refresh_attempted_at"] is not None
    command.downgrade(config, "0016_account_notifications")
    assert asyncio.run(service_state(migration_url)) == before
