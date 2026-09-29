import asyncio
import inspect
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_context import SingleOrgAccountSource, SingleOrgTaskService
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceUsageSegment,
    QuotaTask,
    ServiceState,
    User,
)
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_cycle import snapshot as account_snapshot
from tests.integration.test_device_ledger import NOW, snapshot
from tests.integration.test_device_usage import usage
from tests.unit.test_device_command_wiring import command_callbacks
from tests.unit.test_device_context import accounts

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def runtime(factory, *, account_id=8123, now=None, reset=None):
    from reclaude_bot.application.device_account_reconcile import DeviceAccountReconcileService

    clock = [now or NOW + timedelta(hours=1)]
    gateway = SimpleNamespace(
        account_id="7022", org_id=178, accounts=AsyncMock(return_value=accounts(account_id)),
        me=AsyncMock(return_value=account_snapshot(sampled_at=clock[0], reset=reset or NOW + timedelta(days=6)).me),
        device_usage=AsyncMock(return_value=usage("1000")), assign=AsyncMock(), revoke=AsyncMock(),
    )
    gateway.configure_account_id = lambda value: setattr(gateway, "account_id", value)
    gateway.authenticate = gateway.me
    source = SingleOrgAccountSource(gateway, 178)
    cycle = DeviceCycleService(factory, source, 178, clock=lambda: clock[0])
    reset_service = DeviceTaskResetService(factory, gateway, cycle, 178, clock=lambda: clock[0])
    reconciler = DeviceAccountReconcileService(factory, gateway, source, cycle, reset_service, 178, clock=lambda: clock[0])
    return SimpleNamespace(reconcile=reconciler.reconcile, reconciler=reconciler, source=source, cycle=cycle, reset=reset_service,
                           gateway=gateway, clock=clock, quota=DeviceQuotaService(factory, 178, clock=lambda: clock[0]))


async def cycle_count(factory):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(DeviceQuotaCycle).where(DeviceQuotaCycle.task_id == 1))


async def latest_cycle(factory):
    async with factory() as session:
        return await session.scalar(select(DeviceQuotaCycle).where(DeviceQuotaCycle.task_id == 1).order_by(DeviceQuotaCycle.id.desc()))


async def test_first_discovery_needs_no_manual_selection(lifecycle_db):
    factory, _ = lifecycle_db
    rt = runtime(factory, account_id=8123)
    await rt.reconcile()
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "8123"
        assert not state.write_enabled
    rt.gateway.device_usage.assert_not_called()
    rt.gateway.assign.assert_not_called()
    rt.gateway.revoke.assert_not_called()


async def test_same_account_never_clears_existing_spend(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, old, "123")
    rt = runtime(factory, account_id=7022)
    await rt.reconcile()
    await rt.reconcile()
    assert await cycle_count(factory) == 1
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 123
    rt.gateway.device_usage.assert_not_called()


async def test_account_display_is_read_only_even_after_upstream_rotation(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)
    app = DeviceAccountUsageService(factory, rt.source, 178, clock=lambda: rt.clock[0])
    result = await app.get_account_usage(1)
    assert result.account_id == "8123" and result.estimated_total_usd is None
    assert await cycle_count(factory) == 1
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and state.write_enabled
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 20
    rt.gateway.device_usage.assert_not_called()


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("reset_days", [5, 6, 8])
async def test_account_change_resets_whole_task_and_counts_only_new_growth(lifecycle_db, running, reset_days):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory, running=running)
    aid, ledger_id = await metered_user(factory, old, "750", locked=True)
    await metered_user(factory, old, "30", user_id=2, device_id=44501)
    async with factory.begin() as session:
        session.add(DeviceQuotaAdjustment(ledger_id=ledger_id, amount_usd=Decimal("90"), reason="old bonus", operator_telegram_id=1,
                                          operation_key="old-bonus", created_at=NOW))
    rt = runtime(factory, account_id=8123, reset=NOW + timedelta(days=reset_days))
    await rt.reconcile()
    new = await latest_cycle(factory)
    assert new.id != old and new.account_id == "8123"
    assert new.reset_at == NOW + timedelta(days=reset_days)
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "8123" and state.write_enabled is running
        assert (await session.get(QuotaTask, 1)).status == ("RUNNING" if running else "STOPPED")
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 750
        assert (await session.get(DeviceAssociation, aid)).state == "ACTIVE"
        segments = list((await session.scalars(select(DeviceUsageSegment).join(DeviceCycleLedger).where(DeviceCycleLedger.cycle_id == new.id))).all())
        assert len(segments) == 2
        assert all(s.baseline_total_usd == 1000 and s.confirmed_used_usd == 0 for s in segments)
    status = await rt.quota.status(1, task_id=1)
    assert status.used_usd == 0 and status.effective_limit_usd == 700 and not status.quota_locked
    rt.clock[0] += timedelta(minutes=5)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    result = await ledger.apply(aid, new.id, await snapshot(factory, "1003", sampled=rt.clock[0]))
    assert result.confirmed_used_usd == 3
    summary = await DeviceAccountUsageService(factory, rt.source, 178, clock=lambda: rt.clock[0]).get_account_usage(1)
    assert summary.managed_used_usd == 3 and summary.estimated_total_usd == 30
    rt.gateway.assign.assert_not_called()
    rt.gateway.revoke.assert_not_called()


async def test_restart_and_repeat_detection_do_not_reset_again(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "50")
    first = runtime(factory, account_id=8123)
    await first.reconcile()
    new = (await latest_cycle(factory)).id
    second = runtime(factory, account_id=8123)
    await second.reconcile()
    await second.reconcile()
    assert (await latest_cycle(factory)).id == new and await cycle_count(factory) == 2
    second.gateway.device_usage.assert_not_called()


async def test_returning_to_previous_account_creates_another_generation(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "50")
    rt = runtime(factory, account_id=8123)
    await rt.reconcile()
    second = (await latest_cycle(factory)).id
    rt.clock[0] += timedelta(minutes=1)
    rt.gateway.accounts.return_value = accounts(7022)
    await rt.reconcile()
    third = await latest_cycle(factory)
    assert third.id != second and third.account_id == "7022" and await cycle_count(factory) == 3


async def test_baseline_failure_keeps_old_identity_and_history_and_retries_after_restart(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, old, "20")
    await metered_user(factory, old, "30", user_id=2, device_id=44501)
    rt = runtime(factory, account_id=8123)
    rt.gateway.device_usage.side_effect = [usage("1000"), TimeoutError("baseline unavailable")]
    with pytest.raises((TimeoutError, EligibilityError)):
        await rt.reconcile()
    assert await cycle_count(factory) == 1
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and not state.write_enabled
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 20
    recovered = runtime(factory, account_id=8123)
    await recovered.reconcile()
    assert await cycle_count(factory) == 2
    async with factory() as session:
        assert (await session.get(ServiceState, 1)).write_enabled


@pytest.mark.parametrize("ids", [(), (8123, 9000)])
async def test_missing_or_ambiguous_inventory_does_not_reset(lifecycle_db, ids):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, old, "20")
    rt = runtime(factory)
    rt.gateway.accounts.return_value = accounts(*ids)
    with pytest.raises(EligibilityError):
        await rt.reconcile()
    assert await cycle_count(factory) == 1
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and not state.write_enabled
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 20
    rt.gateway.device_usage.assert_not_called()


async def test_unresolved_auth_blocks_reset_without_partial_clear(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    aid, _ = await metered_user(factory, old, "20")
    async with factory.begin() as session:
        action = await session.scalar(select(DeviceAction).where(DeviceAction.association_id == aid))
        action.status = "UNKNOWN"
        action.completed_at = None
    rt = runtime(factory, account_id=8123)
    with pytest.raises(EligibilityError):
        await rt.reconcile()
    assert await cycle_count(factory) == 1


async def test_another_account_change_during_baseline_cannot_commit_mixed_identity(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)

    async def rotate_again(*args, **kwargs):
        rt.gateway.accounts.return_value = accounts(9000)
        return usage("1000")

    rt.gateway.device_usage.side_effect = rotate_again
    with pytest.raises(EligibilityError):
        await rt.reconcile()
    assert await cycle_count(factory) == 1
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and not state.write_enabled


async def test_reset_accepts_subsecond_variation_in_same_upstream_period(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory)
    before = rt.gateway.me.return_value
    after = account_snapshot(sampled_at=rt.clock[0], reset=NOW + timedelta(days=6, microseconds=999)).me
    rt.gateway.me.side_effect = [before, before, after]
    await rt.reconcile()
    assert await cycle_count(factory) == 2


async def test_expired_previous_cycle_still_permits_account_reset(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    later = NOW + timedelta(days=8)
    rt = runtime(factory, account_id=8123, now=later, reset=later + timedelta(days=6))
    await rt.reconcile()
    assert await cycle_count(factory) == 2
    assert (await latest_cycle(factory)).account_id == "8123"


async def test_concurrent_detection_creates_only_one_reset(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    one, two = runtime(factory, account_id=8123), runtime(factory, account_id=8123)
    outcomes = await asyncio.wait_for(asyncio.gather(one.reconcile(), two.reconcile(), return_exceptions=True), 20)
    assert any(not isinstance(item, Exception) for item in outcomes)
    assert all(not isinstance(item, Exception) or isinstance(item, EligibilityError) for item in outcomes)
    assert await cycle_count(factory) == 2


async def test_new_manual_stop_is_not_overwritten_by_auto_resume(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)

    async def stop_during_reset(*args, **kwargs):
        await QuotaTaskService(factory, org_id=178).stop("task-1", 999)
        return usage("1000")

    rt.gateway.device_usage.side_effect = stop_during_reset
    try:
        await rt.reconcile()
    except EligibilityError:
        pass
    rt.gateway.device_usage.side_effect = None
    await rt.reconcile()
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).status == "STOPPED"
        assert not (await session.get(ServiceState, 1)).write_enabled


async def test_pending_reset_blocks_fresh_auth_and_generic_latch_resume(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, old)).last_day_checked_at = rt.clock[0]

    async def try_interleaved_writes(*args, **kwargs):
        tasks = QuotaTaskService(factory, org_id=178)
        try:
            await tasks.enable_latch()
        except EligibilityError:
            pass
        async with factory() as session:
            assert not (await session.get(ServiceState, 1)).write_enabled
        context = await SingleOrgTaskService(factory, 178).resolve_task("task-1")
        async with factory.begin() as session:
            user = await session.get(User, 1)
            with pytest.raises(EligibilityError):
                await rt.quota.auth_quota_check(session, user, context, rt.clock[0])
        return usage("1000")

    rt.gateway.device_usage.side_effect = try_interleaved_writes
    await rt.reconcile()
    assert await cycle_count(factory) == 2


async def test_recovery_command_keeps_its_explicit_stop_behavior(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=7022)
    handler = command_callbacks(build_admin_router(Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[999],
    )))["recovery_enable"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    await handler(message, SimpleNamespace(command="recovery_enable", args=None), rt.reconciler)
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).status == "STOPPED"
        assert not (await session.get(ServiceState, 1)).write_enabled


async def test_manual_sync_cannot_hide_a_rotation_after_old_cycle_expired(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    later = NOW + timedelta(days=8)
    rt = runtime(factory, account_id=8123, now=later, reset=later + timedelta(days=6))
    handler = command_callbacks(build_admin_router(Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[999],
    )))["sync"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    provided = dict(message=message, device_cycle=rt.cycle, device_sampling=SimpleNamespace(tick=AsyncMock(return_value=())),
                    device_account_reconcile=rt.reconciler)
    await handler(**{name: provided[name] for name in inspect.signature(handler).parameters})
    # A later normal detection must not mistake a cycle minted by /sync for a
    # completed account transition and permanently skip device baselines.
    await rt.reconcile()
    rt.gateway.device_usage.assert_awaited_once()
    current = await latest_cycle(factory)
    assert current.id != old and current.account_id == "8123"
    assert current.started_at == later


async def test_cycle_change_during_reset_rejects_old_period_baselines(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)
    before = rt.gateway.me.return_value
    after = account_snapshot(sampled_at=rt.clock[0], reset=NOW + timedelta(days=7)).me
    # Discovery, pre-baseline evidence, post-baseline evidence. The account
    # remains B but the upstream period changes while the reset is in flight.
    rt.gateway.me.side_effect = [before, before, after]
    with pytest.raises(EligibilityError):
        await rt.reconcile()
    assert await cycle_count(factory) == 1
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and not state.write_enabled
    # A later retry obtains a complete new-period baseline and can commit.
    rt.gateway.me.side_effect = None
    rt.gateway.me.return_value = after
    await rt.reconcile()
    assert await cycle_count(factory) == 2
    assert (await latest_cycle(factory)).reset_at == NOW + timedelta(days=7)


async def test_pending_reset_cannot_be_reopened_by_task_start(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = runtime(factory, account_id=8123)

    async def start_during_reset(*args, **kwargs):
        tasks = QuotaTaskService(factory, org_id=178)
        try:
            await tasks.start("task-1", 999)
        except EligibilityError:
            pass
        async with factory() as session:
            assert not (await session.get(ServiceState, 1)).write_enabled
        return usage("1000")

    rt.gateway.device_usage.side_effect = start_during_reset
    try:
        await rt.reconcile()
    except EligibilityError:
        # Starting changes the task version; aborting the reset is correct as
        # long as the interleaved start never opened writes on old evidence.
        async with factory() as session:
            state = await session.get(ServiceState, 1)
            assert not state.write_enabled and state.selected_account_id == "7022"


async def test_explicit_recovery_authenticates_before_inventory_lookup(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory, running=False)
    rt = runtime(factory, account_id=7022)
    authenticated = False

    async def authenticate():
        nonlocal authenticated
        authenticated = True
        return rt.gateway.me.return_value

    async def inventory():
        if not authenticated:
            raise AuthenticationCircuitOpen("session unavailable; explicit recovery required")
        return accounts(7022)

    rt.gateway.authenticate = AsyncMock(side_effect=authenticate)
    rt.gateway.accounts.side_effect = inventory
    handler = command_callbacks(build_admin_router(Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[999],
    )))["recovery_enable"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    await handler(message, SimpleNamespace(command="recovery_enable", args=None), rt.reconciler)
    rt.gateway.authenticate.assert_awaited_once()
    assert "STOPPED" in message.answer.await_args.args[0]
