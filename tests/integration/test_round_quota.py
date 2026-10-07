import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.infrastructure.db.models import DeviceCycleLedger, DeviceQuotaCycle, QuotaTask
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_account_usage import service, source_usage
from tests.integration.test_device_cycle import snapshot as account_snapshot
from tests.integration.test_device_ledger import NOW, snapshot
from tests.integration.test_device_reset import database_state, reset_task, runtime

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


async def saved_round(factory, total="2400", percent="100"):
    cycle_id = await ready_cycle(factory)
    association_id, ledger_id = await metered_user(factory, cycle_id, total)
    app, _ = service(factory, source_usage(percent=percent))
    assert await app.record_estimate(1, expected_cycle_id=cycle_id)
    return cycle_id, association_id, ledger_id


@pytest.mark.parametrize("next_boundary", ["natural", "reset"])
async def test_2400_reset_575_then_2700_next_round_650(lifecycle_db, next_boundary):
    factory, _ = lifecycle_db
    old_id, aid, _ = await saved_round(factory)
    await DeviceQuotaService(factory, 178, clock=lambda: NOW).adjust(1, old_id, Decimal("50"), "bonus", 999, operation_key="bonus")
    rt = runtime(factory)
    first = await reset_task(rt)
    assert first.opening_limit_usd == 575 and first.quota_source_estimate_usd == 2400
    assert first.reset_at == NOW + timedelta(days=6)
    assert (await rt.quota.status(1, task_id=1)).effective_limit_usd == 575
    rt.clock[0] += timedelta(minutes=5)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    await ledger.apply(aid, first.cycle_id, await snapshot(factory, "3600", sampled=rt.clock[0]))
    rt.gateway.me.return_value = account_snapshot(percent="100", sampled_at=rt.clock[0]).me
    await rt.cycle.sync()
    # A fresh service instance proves the estimate does not depend on in-memory state.
    app = DeviceAccountUsageService(rt.service.session_factory, rt.cycle.source, 178, clock=lambda: rt.clock[0])
    assert await app.record_estimate(1, expected_cycle_id=first.cycle_id)
    async with factory() as session:
        assert (await session.get(DeviceQuotaCycle, first.cycle_id)).estimated_total_usd == 2700
        assert (await session.get(QuotaTask, 1)).limit_usd == 575
        assert (await session.get(DeviceQuotaCycle, old_id)).estimated_total_usd == 2400
    if next_boundary == "natural":
        rt.clock[0] = first.reset_at
        rt.gateway.me.return_value = account_snapshot(percent="0", sampled_at=rt.clock[0], reset=rt.clock[0] + timedelta(days=7)).me
        last = await rt.cycle.sync()
    else:
        rt.clock[0] += timedelta(minutes=1)
        last = await reset_task(rt, "reset-2")
    assert last.opening_limit_usd == 650 and last.quota_source_estimate_usd == 2700
    assert (await rt.quota.status(1, task_id=1)).effective_limit_usd == 650
    # Old command replay must retain its original result, without resetting the new quota.
    assert (await reset_task(rt)).opening_limit_usd == 575
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).limit_usd == 650


async def test_drop_blocks_old_estimate_even_when_sync_later_recovers(lifecycle_db):
    factory, _ = lifecycle_db
    old_id, _, _ = await saved_round(factory)
    rt = runtime(factory)
    rt.gateway.me.return_value = account_snapshot(percent="0", sampled_at=rt.clock[0]).me
    # Simulate last sync having observed 100% before the reset card was used.
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, old_id)).weekly_percent = Decimal("100")
    await rt.cycle.sync()
    rt.clock[0] += timedelta(seconds=1)
    rt.gateway.me.return_value = account_snapshot(percent="20", sampled_at=rt.clock[0]).me
    await rt.cycle.sync()
    app = DeviceAccountUsageService(factory, rt.cycle.source, 178, clock=lambda: rt.clock[0])
    assert not await app.record_estimate(1, expected_cycle_id=old_id)
    assert (await app.get_account_usage(1)).estimated_total_usd is None
    async with factory() as session:
        old = await session.get(DeviceQuotaCycle, old_id)
        assert old.estimate_blocked and old.estimated_total_usd == 2400
    assert (await reset_task(rt)).opening_limit_usd == 575


@pytest.mark.parametrize("case", ["same_snapshot", "older_snapshot", "pre_round_snapshot", "wrong_cycle", "zero_percent", "wrong_account"])
async def test_invalid_or_repeated_evidence_does_not_replace_saved_estimate(lifecycle_db, case):
    factory, _ = lifecycle_db
    old_id, _, ledger_id = await saved_round(factory)
    async with factory.begin() as session:
        (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd = Decimal("2700")
    clock = NOW + timedelta(seconds=10)
    evidence = source_usage(percent="100")
    expected_id = old_id
    if case == "older_snapshot":
        evidence = source_usage(percent="100", sampled_at=NOW - timedelta(seconds=1))
    elif case == "pre_round_snapshot":
        evidence = source_usage(percent="100", sampled_at=NOW - timedelta(days=2))
    elif case == "wrong_cycle":
        expected_id += 1
    elif case == "zero_percent":
        evidence = source_usage(percent="0", sampled_at=clock)
    elif case == "wrong_account":
        evidence = source_usage(percent="100", sampled_at=clock, account_id="other")
    app, _ = service(factory, evidence, clock=lambda: clock)
    assert not await app.record_estimate(1, expected_cycle_id=expected_id)
    async with factory() as session:
        assert (await session.get(DeviceQuotaCycle, old_id)).estimated_total_usd == 2400


async def test_reset_rolls_quota_back_if_baseline_commit_fails(lifecycle_db):
    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = runtime(factory)
    before = await database_state(factory)
    rt.service.ledger.initialize_reset_baseline = AsyncMock(side_effect=RuntimeError("baseline failed"))
    with pytest.raises(RuntimeError, match="baseline failed"):
        await reset_task(rt)
    assert await database_state(factory) == before


async def test_empty_round_retains_limit_and_does_not_reapply_formula(lifecycle_db):
    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = runtime(factory)
    first = await reset_task(rt)
    rt.clock[0] += timedelta(minutes=1)
    second = await reset_task(rt, "reset-2")
    assert first.opening_limit_usd == second.opening_limit_usd == 575
    assert second.quota_source_estimate_usd is None and second.quota_reason == "NO_VALID_ESTIMATE"


async def test_partial_utilization_uses_same_estimation_formula(lifecycle_db):
    factory, _ = lifecycle_db
    await saved_round(factory, total="1200", percent="50")
    assert (await reset_task(runtime(factory))).opening_limit_usd == 575


async def test_late_estimate_cannot_write_into_reset_round(lifecycle_db):
    factory, _ = lifecycle_db
    old_id, _, _ = await saved_round(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    app, source = service(factory, clock=lambda: NOW + timedelta(seconds=10))

    async def fetch(_):
        entered.set()
        await release.wait()
        return source_usage(percent="100", sampled_at=NOW + timedelta(seconds=10))

    source.get_usage.side_effect = fetch
    pending = asyncio.create_task(app.record_estimate(1, expected_cycle_id=old_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await reset_task(runtime(factory))
    finally:
        release.set()
    assert not await asyncio.wait_for(pending, 5)
    async with factory() as session:
        assert (await session.get(DeviceQuotaCycle, result.cycle_id)).estimated_total_usd is None
        assert (await session.get(DeviceQuotaCycle, old_id)).estimated_total_usd == 2400


async def test_parallel_natural_sync_allocates_only_once(lifecycle_db):
    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = runtime(factory)
    rt.clock[0] = NOW + timedelta(days=6)
    rt.gateway.me.return_value = account_snapshot(percent="0", sampled_at=rt.clock[0], reset=rt.clock[0] + timedelta(days=7)).me
    rows = await asyncio.wait_for(asyncio.gather(*(rt.cycle.sync() for _ in range(3))), 10)
    assert len({row.id for row in rows}) == 1
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).limit_usd == 575
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 2


async def test_account_change_notice_uses_new_allocated_limit(lifecycle_db):
    from tests.integration.test_account_reset_notifications import notification_runtime

    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = notification_runtime(factory)
    await rt.reconcile()
    await rt.notices.deliver_pending()
    messages = [call.args[1] for call in rt.notify.await_args_list]
    assert messages and all("$575.00" in message for message in messages)
    assert all("$700.00" not in message for message in messages)


async def test_observed_drop_without_cycle_sync_is_also_sticky(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, _ = await saved_round(factory)
    moment = NOW + timedelta(seconds=10)
    app, source = service(factory, source_usage(percent="20", sampled_at=moment), clock=lambda: moment)
    assert not await app.record_estimate(1, expected_cycle_id=cycle_id)
    source.get_usage.return_value = source_usage(percent="100", sampled_at=moment)
    assert not await app.record_estimate(1, expected_cycle_id=cycle_id)
    async with factory() as session:
        row = await session.get(DeviceQuotaCycle, cycle_id)
        assert row.estimate_blocked and row.estimated_total_usd == 2400


async def test_manual_quota_override_lasts_only_until_next_round(lifecycle_db):
    from reclaude_bot.application.task import QuotaTaskService

    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = runtime(factory)
    task_service = QuotaTaskService(factory, rt.gateway, org_id=178)
    await task_service.set_limit("task-1", Decimal("999"), 999)
    assert (await rt.quota.status(1, task_id=1)).effective_limit_usd == 999
    assert (await reset_task(rt)).opening_limit_usd == 575


async def test_parallel_same_reset_message_returns_one_allocation(lifecycle_db):
    factory, _ = lifecycle_db
    await saved_round(factory)
    rt = runtime(factory)
    first, second = await asyncio.wait_for(asyncio.gather(reset_task(rt), reset_task(rt)), 10)
    assert first == second and first.opening_limit_usd == 575
    async with factory() as session:
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 2
        assert (await session.get(QuotaTask, 1)).limit_usd == 575


async def test_cycle_sync_response_started_before_reset_cannot_reallocate(lifecycle_db):
    factory, _ = lifecycle_db
    old_id, _, _ = await saved_round(factory)
    app, source = service(factory)
    from reclaude_bot.application.device_cycle import DeviceCycleService

    cycle_service = DeviceCycleService(factory, source, 178, clock=lambda: NOW)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        entered.set()
        await release.wait()
        return source_usage(percent="100")

    source.get_usage.side_effect = fetch
    pending = asyncio.create_task(cycle_service.sync())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await reset_task(runtime(factory))
    finally:
        release.set()
    late = await asyncio.wait_for(pending, 5)
    assert late.id == result.cycle_id != old_id
    assert late.opening_limit_usd == 575
    async with factory() as session:
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 2


async def test_same_source_snapshot_after_restart_cannot_inflate_estimate(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, ledger_id = await saved_round(factory)
    async with factory.begin() as session:
        (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd = Decimal("5000")
    app, _ = service(factory, source_usage(percent="100"), clock=lambda: NOW + timedelta(hours=1))
    assert not await app.record_estimate(1, expected_cycle_id=cycle_id)
    assert (await reset_task(runtime(factory))).opening_limit_usd == 575
