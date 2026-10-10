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


async def test_explicit_refresh_bypasses_and_restarts_query_cooldown(app_context):
    factory, gateway, _ = app_context
    await prepare(factory)
    service = AccountUsageRefreshService(factory, gateway)
    assert await service.refresh_if_due(now=NOW)
    reset_at = NOW + timedelta(hours=1)
    assert await service.refresh_if_due(now=reset_at, force=True)
    assert not await service.refresh_if_due(now=NOW + timedelta(hours=3))
    assert gateway.refresh_account_usage_calls == 2


@pytest.mark.parametrize("outcome", ["fresh", "fresh_skew", "stale", "failed"])
async def test_reset_refreshes_cached_snapshot_before_building_new_round(lifecycle_db, outcome):
    from reclaude_bot.domain.errors import EligibilityError
    from tests.integration.test_device_reset import database_state, reset_task, runtime

    factory, _ = lifecycle_db
    await seeded(factory)
    rt = runtime(factory)
    rt.gateway.me.return_value = source_usage(sampled_at=CYCLE_NOW).me
    before = await database_state(factory)

    async def refresh():
        rt.gateway.me.assert_not_awaited()
        rt.clock[0] += timedelta(seconds=1)
        if outcome == "failed":
            raise UpstreamError("refresh failed")
        if outcome in {"fresh", "fresh_skew"}:
            skew = timedelta(seconds=2.620 if outcome == "fresh_skew" else 0)
            rt.gateway.me.return_value = source_usage(percent="1", sampled_at=rt.clock[0] + skew).me
        # Network latency must not put the new snapshot before the new round.
        rt.clock[0] += timedelta(seconds=1)

    rt.gateway.refresh_account_usage = AsyncMock(side_effect=refresh)
    rt.service.refresh = AccountUsageRefreshService(factory, rt.gateway)
    # Ordinary query cooldown is active when the explicit reset arrives.
    async with factory() as session, session.begin():
        state = await session.get(ServiceState, 1)
        state.account_usage_refresh_attempted_at = rt.clock[0] - timedelta(minutes=5)

    if outcome not in {"fresh", "fresh_skew"}:
        with pytest.raises((EligibilityError, UpstreamError), match="快照仍未更新|refresh failed"):
            await reset_task(rt)
        after = await database_state(factory)
        # Only the durable refresh attempt is permitted to change on failure.
        before.pop("service_state")
        after.pop("service_state")
        assert after == before
        rt.gateway.device_usage.assert_not_awaited()
    else:
        result = await reset_task(rt)
        summary = await DeviceAccountUsageService(
            factory, rt.cycle.source, 178, clock=lambda: rt.clock[0], refresh=rt.service.refresh,
        ).get_account_usage(1)
        assert summary.cycle_id == result.cycle_id
        assert summary.seven_day_utilization == 1
        assert summary.usage_updated_at >= summary.cycle_started_at
        assert summary.estimate_reason == "本周期托管设备消费为零"
        assert (await reset_task(rt)).cycle_id == result.cycle_id
    rt.gateway.refresh_account_usage.assert_awaited_once()


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


@pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
async def test_expired_cycle_retry_is_atomic_and_survives_restart(lifecycle_db):
    import asyncio

    factory, _ = lifecycle_db
    await prepare(factory)
    gateway = SimpleNamespace(refresh_account_usage=AsyncMock())
    first = AccountUsageRefreshService(factory, gateway)
    second = AccountUsageRefreshService(factory, gateway)
    assert sorted(await asyncio.gather(
        first.refresh_if_due(now=NOW, expired_cycle=True),
        second.refresh_if_due(now=NOW, expired_cycle=True),
    )) == [False, True]
    restarted = AccountUsageRefreshService(factory, gateway)
    assert not await restarted.refresh_if_due(now=NOW + timedelta(minutes=5, microseconds=-1), expired_cycle=True)
    assert await restarted.refresh_if_due(now=NOW + timedelta(minutes=5), expired_cycle=True)
    assert not await restarted.refresh_if_due(now=NOW + timedelta(hours=1))
    assert gateway.refresh_account_usage.await_count == 2


@pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("seconds_left,expected", [(61, False), (60, True), (1, True), (0, False), (-1, False)])
async def test_final_minute_refresh_bypasses_query_cooldown_once(lifecycle_db, seconds_left, expected):
    factory, _ = lifecycle_db
    await prepare(factory)
    gateway = SimpleNamespace(refresh_account_usage=AsyncMock())
    service = AccountUsageRefreshService(factory, gateway)
    reset = NOW + timedelta(minutes=10)
    assert await service.refresh_if_due(now=NOW)
    moment = reset - timedelta(seconds=seconds_left)
    assert await service.refresh_if_due(now=moment, before_reset_at=reset) is expected
    restarted = AccountUsageRefreshService(factory, gateway)
    assert not await restarted.refresh_if_due(now=moment, before_reset_at=reset)
    assert gateway.refresh_account_usage.await_count == 1 + expected


@pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
@pytest.mark.parametrize("failed", [False, True])
async def test_final_minute_refresh_claim_survives_concurrency_and_failure(lifecycle_db, failed):
    import asyncio

    factory, _ = lifecycle_db
    await prepare(factory)
    async with factory() as session, session.begin():
        (await session.get(ServiceState, 1)).account_usage_refresh_attempted_at = NOW - timedelta(minutes=2)
    gateway = SimpleNamespace(refresh_account_usage=AsyncMock(side_effect=TimeoutError("unavailable") if failed else None))
    reset = NOW + timedelta(minutes=1)
    results = await asyncio.gather(*(
        AccountUsageRefreshService(factory, gateway).refresh_if_due(now=NOW, before_reset_at=reset)
        for _ in range(2)
    ), return_exceptions=True)
    assert results.count(False) == 1
    assert sum(isinstance(result, TimeoutError) for result in results) == int(failed)
    assert results.count(True) == int(not failed)
    assert not await AccountUsageRefreshService(factory, gateway).refresh_if_due(now=NOW + timedelta(seconds=10), before_reset_at=reset)
    gateway.refresh_account_usage.assert_awaited_once()
