import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.integration.test_device_migration import config_for
from tests.integration.test_device_migration import migration_url as shared_migration_url

migration_url = shared_migration_url


async def existing_round(url, *, seed=False):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            if seed:
                await connection.execute(text(
                    "INSERT INTO device_task_scopes (task_id, org_id, created_at, updated_at) "
                    "VALUES (1, 178, '2026-09-28 00:00:00+00:00', '2026-09-28 00:00:00+00:00')"
                ))
                await connection.execute(text(
                    "INSERT INTO device_quota_cycles (id, task_id, started_at, reset_at, created_at, status, account_id, weekly_percent) "
                    "VALUES (1, 1, '2026-09-28 00:00:00+00:00', '2026-10-05 00:00:00+00:00', "
                    "'2026-09-28 00:00:00+00:00', 'VERIFIED', '7022', 100)"
                ))
            return dict((await connection.execute(text("SELECT * FROM device_quota_cycles WHERE id = 1"))).mappings().one())
    finally:
        await engine.dispose()


def test_upgrade_preserves_existing_round_and_downgrade_restores_schema(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, "0017_account_usage_refresh")
    before = asyncio.run(existing_round(migration_url, seed=True))
    command.upgrade(config, "0018_round_quota")
    after = asyncio.run(existing_round(migration_url))
    assert not after.pop("estimate_blocked")
    for name in ("estimated_total_usd", "estimate_used_usd", "estimate_percent", "estimate_snapshot_at", "estimate_recorded_at",
                 "opening_limit_usd", "quota_source_cycle_id", "quota_source_estimate_usd", "quota_reason"):
        assert after.pop(name) is None
    assert after == before
    command.downgrade(config, "0017_account_usage_refresh")
    assert asyncio.run(existing_round(migration_url)) == before
