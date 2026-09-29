import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from reclaude_bot.application.device_context import SingleOrgAccountSource
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import (
    AuditLog,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
    QuotaTask,
    ServiceState,
)
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_cycle import snapshot as account_snapshot
from tests.integration.test_device_ledger import NOW, association, end_association, snapshot
from tests.integration.test_device_quota_actions import stack as action_stack
from tests.integration.test_device_usage import usage
from tests.unit.test_device_context import accounts

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
RESET_MOMENT = NOW + timedelta(hours=1)


def runtime(factory, *, reset=None):
    # Match production: reads must not depend on implicit ORM autoflush.
    production_factory = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, autoflush=False)
    clock = [RESET_MOMENT]
    gateway = SimpleNamespace(
        account_id="7022",
        accounts=AsyncMock(return_value=accounts(7022)),
        me=AsyncMock(return_value=account_snapshot(sampled_at=RESET_MOMENT, reset=reset or NOW + timedelta(days=6)).me),
        device_usage=AsyncMock(return_value=usage("900")),
        approve_device_auth=AsyncMock(),
        revoke_device=AsyncMock(),
    )
    cycle = DeviceCycleService(production_factory, SingleOrgAccountSource(gateway, 178), 178, clock=lambda: clock[0])
    service = DeviceTaskResetService(production_factory, gateway, cycle, 178, clock=lambda: clock[0])
    return SimpleNamespace(service=service, gateway=gateway, cycle=cycle, clock=clock,
                           quota=DeviceQuotaService(production_factory, 178, clock=lambda: clock[0]))


async def reset_task(rt, key="reset-1"):
    return await rt.service.reset("task-1", 999, operation_key=key)


async def database_state(factory):
    models = (DeviceQuotaCycle, DeviceCycleLedger, DeviceQuotaAdjustment, DeviceUsageSegment,
              DeviceUsageSnapshot, AuditLog, DeviceAssociation, QuotaTask, ServiceState)
    async with factory() as session:
        return {
            model.__tablename__: [tuple(getattr(row, c.name) for c in model.__table__.columns)
                                  for row in (await session.scalars(select(model).order_by(model.id))).all()]
            for model in models
        }


@pytest.mark.parametrize("running", [False, True])
async def test_reset_preserves_history_binding_task_and_gate_but_clears_new_usage_and_adjustments(lifecycle_db, running):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory, running=running)
    association_id, ledger_id = await metered_user(factory, old_id, "800", locked=True)
    old_quota = DeviceQuotaService(factory, 178, clock=lambda: NOW)
    await old_quota.adjust(1, old_id, Decimal("50"), "old adjustment", 999, operation_key="old-adjust")
    rt = runtime(factory)
    result = await reset_task(rt)
    assert result.cycle_id != old_id
    status = await rt.quota.status(1, task_id=1)
    assert status.used_usd == 0 and status.effective_limit_usd == 700 and not status.quota_locked
    assert status.quality == "VERIFIED"
    async with factory() as session:
        old = await session.get(DeviceCycleLedger, ledger_id)
        assert old.confirmed_used_usd == Decimal("800") and old.quota_locked_at == NOW
        assert (await session.get(DeviceQuotaCycle, old_id)).status == "EXPIRED"
        assert len((await session.scalars(select(DeviceQuotaAdjustment))).all()) == 1
        assert (await session.get(QuotaTask, 1)).status == ("RUNNING" if running else "STOPPED")
        assert (await session.get(ServiceState, 1)).write_enabled is running
        assert (await session.get(DeviceAssociation, association_id)).state == "ACTIVE"
        segment = await session.scalar(select(DeviceUsageSegment).join(DeviceCycleLedger).where(DeviceCycleLedger.cycle_id == result.cycle_id))
        assert segment.baseline_total_usd == Decimal("900") and segment.confirmed_used_usd == 0
    rt.gateway.approve_device_auth.assert_not_called()
    rt.gateway.revoke_device.assert_not_called()
    rt.clock[0] += timedelta(minutes=5)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    applied = await ledger.apply(association_id, result.cycle_id,
                                await snapshot(factory, "920", sampled=rt.clock[0]))
    assert applied.confirmed_used_usd == Decimal("20") and applied.quality == "VERIFIED"


async def test_repeated_command_is_idempotent_but_new_command_can_reset_same_rec_week_again(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    await metered_user(factory, 1, "500")
    rt = runtime(factory)
    first = await reset_task(rt)
    before = await database_state(factory)
    rt.clock[0] += timedelta(minutes=1)
    repeated = await reset_task(rt)
    assert repeated.cycle_id == first.cycle_id
    assert await database_state(factory) == before
    second = await reset_task(rt, "reset-2")
    assert second.cycle_id != first.cycle_id
    assert second.reset_at == first.reset_at
    latest_state = await database_state(factory)
    assert (await reset_task(rt)).cycle_id == first.cycle_id
    assert await database_state(factory) == latest_state


async def test_changed_rec_reset_is_accepted_and_next_sync_keeps_new_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, old_id)).status = "NEEDS_REVIEW"
    rt = runtime(factory, reset=NOW + timedelta(days=5))
    result = await reset_task(rt)
    assert result.reset_at == NOW + timedelta(days=5)
    assert (await rt.cycle.sync("task-1")).id == result.cycle_id


async def test_second_device_query_failure_leaves_entire_task_unchanged(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "500")
    await metered_user(factory, cycle_id, "600", user_id=2, device_id=44501)
    rt = runtime(factory)
    before = await database_state(factory)
    rt.gateway.device_usage.side_effect = [usage("900"), httpx.ReadTimeout("unavailable")]
    with pytest.raises((httpx.ReadTimeout, EligibilityError)):
        await reset_task(rt)
    assert await database_state(factory) == before


async def test_account_fetch_failure_leaves_task_unchanged(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    rt = runtime(factory)
    before = await database_state(factory)
    rt.gateway.me.side_effect = httpx.ReadTimeout("unavailable")
    with pytest.raises((httpx.ReadTimeout, EligibilityError)):
        await reset_task(rt)
    assert await database_state(factory) == before
    rt.gateway.device_usage.assert_not_called()


@pytest.mark.parametrize("case", ["unbound", "inactive", "account_changed", "expired"])
async def test_invalid_account_or_cycle_does_not_reset(lifecycle_db, case):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    rt = runtime(factory)
    if case == "account_changed":
        rt.gateway.accounts.return_value = accounts(9999)
    else:
        kwargs: dict[str, Any] = {"sampled_at": RESET_MOMENT}
        if case == "unbound":
            kwargs["status"] = "unbound"
        elif case == "inactive":
            kwargs["is_active"] = False
        else:
            kwargs["reset"] = RESET_MOMENT
        rt.gateway.me.return_value = account_snapshot(**kwargs).me
    before = await database_state(factory)
    with pytest.raises(EligibilityError):
        await reset_task(rt)
    assert await database_state(factory) == before


async def test_reset_network_reads_hold_no_database_locks_and_detect_device_change(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, "800")
    rt = runtime(factory)

    async def consume(*args, **kwargs):
        async with factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            await session.execute(text("SELECT task_id FROM device_task_scopes FOR UPDATE"))
            linked = await session.get(DeviceAssociation, association_id, with_for_update=True)
            linked.state = "UNKNOWN"
        return usage("900")

    rt.gateway.device_usage.side_effect = consume
    with pytest.raises(EligibilityError):
        await reset_task(rt)
    async with factory() as session:
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 1
        assert (await session.get(DeviceQuotaCycle, cycle_id)).status == "VERIFIED"


async def test_quota_revoked_user_is_not_reauthorized_but_gets_one_eligibility_notice(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, old_id, "800", locked=True)
    await end_association(factory, association_id, at=NOW + timedelta(minutes=10))
    async with factory.begin() as session:
        (await session.get(DeviceAssociation, association_id)).end_reason = "QUOTA"
    rt = runtime(factory)
    result = await reset_task(rt)
    status = await rt.quota.status(1, task_id=1)
    assert status.cycle_id == result.cycle_id and status.used_usd == 0 and not status.quota_locked
    actions = action_stack(factory, now=RESET_MOMENT)
    await actions.actions.run_once()
    await actions.actions.run_once()
    assert actions.notify.await_count == 1
    assert "/auth" in actions.notify.await_args.args[1]
    rt.gateway.device_usage.assert_not_called()
    rt.gateway.approve_device_auth.assert_not_called()


@pytest.mark.parametrize("state", ["PENDING_AUTH", "UNKNOWN", "PENDING_REVOKE"])
async def test_unresolved_device_operation_prevents_partial_reset(lifecycle_db, state):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, old_id, "500")
    async with factory.begin() as session:
        (await session.get(DeviceAssociation, association_id)).state = state
    rt = runtime(factory)
    before = await database_state(factory)
    with pytest.raises(EligibilityError):
        await reset_task(rt)
    assert await database_state(factory) == before


async def test_switching_device_after_reset_retains_new_cycle_consumption(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    first_id, _ = await metered_user(factory, old_id, "800")
    rt = runtime(factory)
    result = await reset_task(rt)
    rt.clock[0] += timedelta(minutes=5)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    await ledger.apply(first_id, result.cycle_id, await snapshot(factory, "920", sampled=rt.clock[0]))
    await end_association(factory, first_id, at=rt.clock[0])
    rt.clock[0] += timedelta(minutes=1)
    second_id = await association(factory, device_id=44501, started=rt.clock[0])
    rt.clock[0] += timedelta(minutes=1)
    applied = await ledger.apply(second_id, result.cycle_id,
                                await snapshot(factory, "30", sampled=rt.clock[0], device_id=44501))
    assert applied.confirmed_used_usd == Decimal("50") and applied.quality == "VERIFIED"


async def test_old_revoked_device_followup_cannot_charge_new_reset_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, old_id, "800", locked=True)
    await end_association(factory, association_id, at=NOW + timedelta(minutes=10))
    rt = runtime(factory)
    result = await reset_task(rt)
    rt.clock[0] += timedelta(minutes=5)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    await ledger.apply(association_id, old_id, await snapshot(factory, "810", sampled=rt.clock[0]))
    status = await rt.quota.status(1, task_id=1)
    assert status.cycle_id == result.cycle_id and status.used_usd == 0


async def test_other_org_task_is_not_reset(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    rt = runtime(factory)
    before = await database_state(factory)
    with pytest.raises(EligibilityError):
        await rt.service.reset("task-2", 999, operation_key="wrong-org")
    assert await database_state(factory) == before
    rt.gateway.me.assert_not_called()


async def test_reset_discards_transfer_effects_for_both_users_but_retains_transfer_history(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    await metered_user(factory, old_id, "100")
    await metered_user(factory, old_id, "200", user_id=2, device_id=44501)
    quota = DeviceQuotaService(factory, 178, clock=lambda: NOW)
    await quota.transfer(1, 2, old_id, Decimal("50"), operation_key="old-transfer")
    assert (await quota.status(1)).effective_limit_usd == 650
    assert (await quota.status(2)).effective_limit_usd == 750
    rt = runtime(factory)
    await reset_task(rt)
    for user_id in (1, 2):
        status = await rt.quota.status(user_id, task_id=1)
        assert status.used_usd == 0 and status.effective_limit_usd == 700
    async with factory() as session:
        assert len((await session.scalars(select(DeviceQuotaAdjustment))).all()) == 2


async def test_concurrent_duplicate_reset_creates_only_one_new_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    rt = runtime(factory)
    results = await asyncio.wait_for(asyncio.gather(reset_task(rt), reset_task(rt)), 15)
    assert results[0].cycle_id == results[1].cycle_id
    async with factory() as session:
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 2


async def test_manual_reset_keeps_closed_write_latch_closed_even_for_running_task(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    async with factory.begin() as session:
        state = await session.get(ServiceState, 1)
        state.write_enabled = False
        state.reason = "quota_task_start_validation"
    rt = runtime(factory)
    await reset_task(rt)
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).status == "RUNNING"
        state = await session.get(ServiceState, 1)
        assert state.write_enabled is False and state.reason == "quota_task_start_validation"


async def test_regular_metering_after_reset_counts_only_new_growth(lifecycle_db):
    from tests.integration.test_device_metering import services

    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, old_id, "800")
    rt = runtime(factory)
    result = await reset_task(rt)
    rt.clock[0] += timedelta(minutes=5)
    rt.gateway.device_usage.return_value = usage("925")
    metering, _, _ = services(factory, clock=rt.clock, gateway=rt.gateway)
    job_id = await metering.enqueue(association_id, sequence=8, run_after=rt.clock[0])
    assert (await metering.run_job(job_id)).status == "COMPLETED"
    current = await rt.quota.status(1, task_id=1)
    assert current.cycle_id == result.cycle_id and current.used_usd == Decimal("25")
    assert current.quality == "VERIFIED"


async def test_commit_phase_failure_rolls_back_cycle_and_all_balances(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    await metered_user(factory, old_id, "800", locked=True)
    rt = runtime(factory)
    before = await database_state(factory)
    monkeypatch.setattr("reclaude_bot.application.device_reset.audit", AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError):
        await reset_task(rt)
    assert await database_state(factory) == before


async def test_threshold_notifications_can_fire_again_in_new_local_period(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, old_id, "400")
    actions = action_stack(factory)
    await actions.actions.run_once()
    assert actions.notify.await_count == 1
    rt = runtime(factory)
    result = await reset_task(rt)
    rt.clock[0] += timedelta(seconds=1)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    await ledger.apply(association_id, result.cycle_id, await snapshot(factory, "1250", sampled=rt.clock[0]))
    actions.clock[0] = rt.clock[0]
    await actions.actions.run_once()
    assert actions.notify.await_count == 2
    assert "50%" in actions.notify.await_args.args[1]


async def test_reset_handles_time_passing_between_device_baseline_requests(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    await metered_user(factory, old_id, "100")
    await metered_user(factory, old_id, "200", user_id=2, device_id=44501)
    rt = runtime(factory)

    async def baseline(*args, **kwargs):
        rt.clock[0] += timedelta(seconds=10)
        return usage("900")

    rt.gateway.device_usage.side_effect = baseline
    result = await reset_task(rt)
    for user_id in (1, 2):
        status = await rt.quota.status(user_id, task_id=1)
        assert status.cycle_id == result.cycle_id and status.used_usd == 0 and status.quality == "VERIFIED"


async def test_reset_refuses_rolling_usage_as_cumulative_baseline(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await ready_cycle(factory)
    await metered_user(factory, old_id, "100")
    rt = runtime(factory)
    rt.gateway.device_usage.return_value = usage("900", range="7d")
    before = await database_state(factory)
    with pytest.raises(EligibilityError):
        await reset_task(rt)
    assert await database_state(factory) == before
