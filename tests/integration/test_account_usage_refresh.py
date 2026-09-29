from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.account_usage_refresh import AccountUsageRefreshService
from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.domain.errors import UpstreamError
from reclaude_bot.infrastructure.db.models import ServiceState
from tests.integration.test_device_account_usage import seeded, source_usage
from tests.integration.test_device_ledger import NOW as CYCLE_NOW

NOW = datetime(2026, 9, 29, tzinfo=UTC)


async def prepare(factory, *, enabled=True):
    async with factory() as session, session.begin():
        session.add(ServiceState(id=1, sync_enabled=enabled, updated_at=NOW))


async def test_three_hour_cooldown_survives_new_service_instance(app_context):
    factory, gateway, _ = app_context
    await prepare(factory)
    service = AccountUsageRefreshService(factory, gateway)
    assert await service.refresh_if_due(now=NOW)
    restarted = AccountUsageRefreshService(factory, gateway)
    assert not await restarted.refresh_if_due(now=NOW + timedelta(hours=3, microseconds=-1))
    assert await restarted.refresh_if_due(now=NOW + timedelta(hours=3))
    assert gateway.refresh_account_usage_calls == 2


async def test_failure_keeps_durable_cooldown(app_context):
    factory, gateway, _ = app_context
    await prepare(factory)

    async def limited():
        # The claim must already be committed before contacting REC.
        async with factory() as session:
            assert await session.scalar(select(ServiceState.account_usage_refresh_attempted_at)) is not None
        raise UpstreamError("Reclaude returned HTTP 429")

    gateway.refresh_account_usage = limited
    service = AccountUsageRefreshService(factory, gateway)
    with pytest.raises(UpstreamError, match="429"):
        await service.refresh_if_due(now=NOW)
    assert not await AccountUsageRefreshService(factory, gateway).refresh_if_due(now=NOW + timedelta(minutes=5))
    with pytest.raises(UpstreamError, match="429"):
        await service.refresh_if_due(now=NOW + timedelta(hours=3))


async def test_query_can_refresh_even_when_background_sync_is_paused(app_context):
    factory, gateway, _ = app_context
    await prepare(factory, enabled=False)
    assert await AccountUsageRefreshService(factory, gateway).refresh_if_due(now=NOW)
    assert gateway.refresh_account_usage_calls == 1


async def test_queries_refresh_before_read_only_when_three_hours_have_elapsed(app_context, lifecycle_db):
    factory, _ = lifecycle_db
    _, gateway, _ = app_context
    await seeded(factory)
    calls = []
    clock = [CYCLE_NOW]

    async def refresh():
        calls.append("refresh")

    async def read(org_id):
        calls.append("read")
        return source_usage()

    gateway.refresh_account_usage = refresh
    service = DeviceAccountUsageService(
        factory, SimpleNamespace(get_usage=AsyncMock(side_effect=read)), 178,
        clock=lambda: clock[0], refresh=AccountUsageRefreshService(factory, gateway),
    )
    await service.get_account_usage(1)
    assert calls == ["refresh", "read"]
    clock[0] += timedelta(hours=2)
    await service.get_account_usage(1)
    assert calls == ["refresh", "read", "read"]
    clock[0] += timedelta(hours=4)
    # Advancing time alone causes no refresh; only the next query does.
    assert calls.count("refresh") == 1
    await service.get_account_usage(1)
    assert calls == ["refresh", "read", "read", "refresh", "read"]
