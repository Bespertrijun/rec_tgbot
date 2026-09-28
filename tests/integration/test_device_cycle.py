import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_context import OrgAccountUsage
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceCycleLedger, DeviceQuotaCycle
from reclaude_bot.infrastructure.reclaude.models import CurrentAccount, MeResponse, SevenDay, UsageSnapshot, WeeklyLimit

# PostgreSQL is the supported runtime; user explicitly declined SQLite-only fixes.
pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
RESET = NOW + timedelta(days=6)


def snapshot(*, reset=RESET, sampled_at=NOW, percent="10", status="bound", account_id="7022", org_id=178, is_active=True):
    me = MeResponse(current_account=CurrentAccount(status=status, email_masked="owner***@example.invalid", usage_updated_at=sampled_at,
                    usage_snapshot=UsageSnapshot(limits=[WeeklyLimit(group="weekly", kind="weekly_all", scope=None, percent=percent,
                                                                    resets_at=reset, is_active=is_active)],
                                                 seven_day=SevenDay(utilization=percent, resets_at=reset))))
    return OrgAccountUsage(org_id=org_id, account_id=account_id, me=me)


def cycle_fixture(factory, value=None):
    source = SimpleNamespace(get_usage=AsyncMock(return_value=value or snapshot()))
    clock = [NOW]
    service = DeviceCycleService(factory, source, 178, clock=lambda: clock[0])
    return service, source, clock


async def cycles(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceQuotaCycle).order_by(DeviceQuotaCycle.reset_at))).all())


async def test_stopped_task_syncs_cycle_evidence_without_user_ledger_or_device_actions(lifecycle_db):
    factory, _ = lifecycle_db
    service, source, _ = cycle_fixture(factory)
    assert await service.current() is None
    created = await service.sync()
    assert created.task_id == 1 and created.status == "VERIFIED"
    assert ensure_utc(created.started_at) == RESET - timedelta(days=7)
    assert ensure_utc(created.reset_at) == RESET
    assert created.account_id == "7022" and created.weekly_percent == Decimal("10")
    assert created.last_day_allow is False
    assert (await service.current()).id == created.id
    assert (await service.sync()).id == created.id
    assert len(await cycles(factory)) == 1
    assert source.get_usage.await_args.args == (178,)
    async with factory() as session:
        assert await session.scalar(select(DeviceCycleLedger)) is None
        logs = list((await session.scalars(select(AuditLog))).all())
        assert logs and all(log.actor_type == "SYSTEM" for log in logs)


@pytest.mark.parametrize("until_reset,percent,allowed", [(timedelta(hours=24, seconds=1), "10", False),
                                                         (timedelta(hours=24), "10", True),
                                                         (timedelta(seconds=1), "99.99", True),
                                                         (timedelta(hours=12), "100", False),
                                                         (timedelta(hours=12), "101", False)])
async def test_last_day_boundary_and_weekly_capacity(lifecycle_db, until_reset, percent, allowed):
    factory, _ = lifecycle_db
    service, _, _ = cycle_fixture(factory, snapshot(reset=NOW + until_reset, percent=percent))
    result = await service.sync()
    assert result.status == "VERIFIED" and result.last_day_allow is allowed


@pytest.mark.parametrize("case", ["inactive", "unbound", "stale", "future", "unselected"])
async def test_invalid_account_evidence_cannot_grant_last_day_permission(lifecycle_db, case):
    factory, _ = lifecycle_db
    kwargs = {"reset": NOW + timedelta(hours=12)}
    if case == "inactive":
        kwargs["is_active"] = False
    elif case == "unbound":
        kwargs["status"] = "unbound"
    elif case == "stale":
        kwargs["sampled_at"] = NOW - timedelta(seconds=91)
    elif case == "future":
        kwargs["sampled_at"] = NOW + timedelta(seconds=1)
    else:
        kwargs["account_id"] = None
    service, _, _ = cycle_fixture(factory, snapshot(**kwargs))
    result = await service.sync()
    assert result.status == "NEEDS_REVIEW" and result.last_day_allow is False


async def test_last_day_evidence_refreshes_and_can_be_withdrawn(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=12)
    service, source, clock = cycle_fixture(factory, snapshot(reset=reset))
    first = await service.sync()
    assert first.last_day_allow is True
    clock[0] += timedelta(seconds=1)
    source.get_usage.return_value = snapshot(reset=reset, sampled_at=clock[0], percent="100")
    second = await service.sync()
    assert second.id == first.id and second.last_day_allow is False
    assert second.weekly_percent == Decimal("100")


@pytest.mark.parametrize("case", ["negative", "nan", "infinite", "too_large", "expired", "wrong_org", "no_weekly"])
async def test_malformed_cycle_source_never_creates_a_cycle(lifecycle_db, case):
    factory, _ = lifecycle_db
    kwargs = {}
    if case in {"negative", "too_large"}:
        kwargs["percent"] = {"negative": "-1", "too_large": "100000000"}[case]
    elif case == "expired":
        kwargs["reset"] = NOW
    elif case == "wrong_org":
        kwargs["org_id"] = 179
    value = snapshot(**kwargs)
    if case in {"nan", "infinite"}:
        # Pydantic rejects non-finite JSON input before the service. Mutate the
        # typed object to exercise the service's own defensive validation.
        value.me.current_account.usage_snapshot.limits[0].percent = Decimal("NaN" if case == "nan" else "Infinity")
    if case == "no_weekly":
        value.me.current_account.usage_snapshot.limits = []
    service, _, _ = cycle_fixture(factory, value)
    with pytest.raises(EligibilityError):
        await service.sync()
    assert await cycles(factory) == []


@pytest.mark.parametrize("change", ["account", "reset"])
async def test_source_change_within_current_cycle_does_not_reset_usage_period(lifecycle_db, change):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=12)
    service, source, clock = cycle_fixture(factory, snapshot(reset=reset))
    first = await service.sync()
    clock[0] += timedelta(seconds=1)
    source.get_usage.return_value = snapshot(reset=reset + timedelta(hours=1) if change == "reset" else reset,
                                             sampled_at=clock[0], account_id="8000" if change == "account" else "7022")
    current = await service.sync()
    assert current.id == first.id and len(await cycles(factory)) == 1
    assert ensure_utc(current.reset_at) == reset and current.account_id == "7022"
    assert current.status == "NEEDS_REVIEW" and current.last_day_allow is False


@pytest.mark.parametrize("gap_days", [0, 14])
async def test_new_cycle_keeps_history_and_handles_multiweek_downtime(lifecycle_db, gap_days):
    factory, _ = lifecycle_db
    service, source, clock = cycle_fixture(factory)
    old = await service.sync()
    clock[0] = RESET + timedelta(days=gap_days)
    assert await service.current() is None
    next_reset = clock[0] + timedelta(days=7)
    source.get_usage.return_value = snapshot(reset=next_reset, sampled_at=clock[0])
    current = await service.sync()
    assert current.id != old.id
    assert ensure_utc(current.started_at) == clock[0]
    rows = await cycles(factory)
    assert len(rows) == 2 and rows[0].status == "EXPIRED"
    assert ensure_utc(rows[0].reset_at) == RESET
    assert (await service.current()).id == current.id


async def test_network_failure_preserves_previous_period_and_evidence(lifecycle_db):
    factory, _ = lifecycle_db
    service, source, clock = cycle_fixture(factory)
    old = await service.sync()
    clock[0] += timedelta(seconds=20)
    source.get_usage.side_effect = RuntimeError("source unavailable")
    with pytest.raises((RuntimeError, EligibilityError)):
        await service.sync()
    rows = await cycles(factory)
    assert len(rows) == 1 and rows[0].id == old.id
    assert ensure_utc(rows[0].last_day_checked_at) == NOW


async def test_postgresql_late_response_cannot_overwrite_newer_capacity_evidence(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("requires independent PostgreSQL transactions")
    service, source, clock = cycle_fixture(factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    call_count = 0
    reset = NOW + timedelta(hours=12)

    async def get_usage(org_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            entered.set()
            await release.wait()
            return snapshot(reset=reset)
        return snapshot(reset=reset, sampled_at=clock[0], percent="100")

    source.get_usage.side_effect = get_usage
    old_request = asyncio.create_task(service.sync())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        clock[0] += timedelta(seconds=1)
        newer = await asyncio.wait_for(service.sync(), 10)
        assert newer.last_day_allow is False
    finally:
        release.set()
    late = await asyncio.wait_for(old_request, 10)
    assert late.id == newer.id and late.weekly_percent == Decimal("100")
    assert late.last_day_allow is False
    assert len(await cycles(factory)) == 1


async def test_postgresql_parallel_sync_creates_one_cycle(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    service, _, _ = cycle_fixture(factory)
    results = await asyncio.wait_for(asyncio.gather(*(service.sync() for _ in range(3))), 20)
    assert len({result.id for result in results}) == 1
    assert len(await cycles(factory)) == 1


async def test_reset_change_preserves_existing_user_consumption_and_quota_lock(lifecycle_db):
    factory, _ = lifecycle_db
    service, source, clock = cycle_fixture(factory)
    first = await service.sync()
    async with factory.begin() as session:
        session.add(DeviceCycleLedger(user_id=1, task_id=1, cycle_id=first.id, confirmed_used_usd=Decimal("700"),
                                      quality="VERIFIED", quota_locked_at=NOW, created_at=NOW, updated_at=NOW))
    clock[0] += timedelta(seconds=1)
    source.get_usage.return_value = snapshot(reset=RESET + timedelta(days=1), sampled_at=clock[0])
    assert (await service.sync()).id == first.id
    async with factory() as session:
        ledgers = list((await session.scalars(select(DeviceCycleLedger))).all())
        assert len(ledgers) == 1
        assert ledgers[0].confirmed_used_usd == Decimal("700")
        assert ensure_utc(ledgers[0].quota_locked_at) == NOW and ledgers[0].quota_unlocked_at is None


async def test_current_cycle_never_returns_another_orgs_period(lifecycle_db):
    factory, _ = lifecycle_db
    other_source = SimpleNamespace(get_usage=AsyncMock(return_value=snapshot(org_id=179)))
    other = DeviceCycleService(factory, other_source, 179, clock=lambda: NOW)
    other_cycle = await other.sync()
    service, source, _ = cycle_fixture(factory)
    assert await service.current() is None
    own_cycle = await service.sync()
    assert own_cycle.id != other_cycle.id and own_cycle.task_id == 1 and other_cycle.task_id == 2
    assert (await service.current()).id == own_cycle.id
    source.get_usage.assert_awaited_once_with(178)


async def test_invalid_refresh_does_not_replace_confirmed_period_with_empty_state(lifecycle_db):
    factory, _ = lifecycle_db
    service, source, clock = cycle_fixture(factory)
    old = await service.sync()
    clock[0] += timedelta(seconds=1)
    source.get_usage.return_value = snapshot(reset=NOW - timedelta(seconds=1), sampled_at=clock[0])
    with pytest.raises(EligibilityError):
        await service.sync()
    rows = await cycles(factory)
    assert len(rows) == 1 and rows[0].id == old.id
    assert ensure_utc(rows[0].reset_at) == RESET
    assert ensure_utc(rows[0].last_day_checked_at) == NOW
