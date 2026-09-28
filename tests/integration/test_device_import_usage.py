"""Admin migration usage is spend, not quota credit or a device-reset loophole."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceAssociation, DeviceCycleLedger, DeviceQuotaAdjustment, DeviceQuotaCycle, DeviceUsageSegment
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_admin import record
from tests.integration.test_device_admin_commands import wired
from tests.integration.test_device_ledger import NOW, association, end_association, ledger_service, snapshot
from tests.integration.test_device_quota_actions import stack
from tests.integration.test_device_usage import usage

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def importer(factory, *, baseline="700"):
    gateway = SimpleNamespace(list_devices=AsyncMock(return_value=[record(44500)]),
                              device_usage=AsyncMock(return_value=usage(baseline)),
                              approve_device_auth=AsyncMock(), revoke_device=AsyncMock())
    quota = DeviceQuotaService(factory, 178, clock=lambda: NOW)
    service = DeviceAdminService(factory, gateway, 178, quota.auth_quota_check, clock=lambda: NOW)
    return service, gateway, quota


async def test_import_then_sampling_keeps_exact_spend_without_double_counting(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, gateway, quota = importer(factory)
    result = await admin.authuser(1, 44500, 999, used_usd=Decimal("500.1234567890"))
    status = await quota.status(1)
    assert status.used_usd == Decimal("500.1234567890")
    assert status.quality == "VERIFIED"
    gateway.device_usage.assert_awaited_once_with(44500, 178, range="all")
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()
    ledger, _ = ledger_service(factory)
    assert (await ledger.ensure(result.association_id, cycle_id)).quality == "VERIFIED"
    same = await ledger.apply(result.association_id, cycle_id, await snapshot(factory, "700"))
    assert same.confirmed_used_usd == Decimal("500.1234567890")
    next_id = await snapshot(factory, "725", sampled=NOW + timedelta(minutes=2))
    increment = await ledger.apply(result.association_id, cycle_id, next_id)
    assert increment.confirmed_used_usd == Decimal("525.1234567890")
    assert increment.quality == "VERIFIED"
    assert await ledger.apply(result.association_id, cycle_id, next_id) == increment
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceQuotaAdjustment)) == 0


async def test_import_is_retained_when_user_changes_device(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, _, _ = importer(factory)
    first = await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    await end_association(factory, first.association_id, at=NOW + timedelta(minutes=10))
    second = await association(factory, device_id=44501, started=NOW + timedelta(minutes=20))
    ledger, _ = ledger_service(factory)
    result = await ledger.apply(second, cycle_id, await snapshot(factory, "100", device_id=44501,
                                                               sampled=NOW + timedelta(minutes=30)))
    assert result.confirmed_used_usd == Decimal("600")
    assert result.quality == "VERIFIED"


async def test_repeated_import_cannot_replace_or_double_initial_spend(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, gateway, quota = importer(factory)
    first = await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    ledger, _ = ledger_service(factory)
    await ledger.apply(first.association_id, cycle_id, await snapshot(factory, "710", sampled=NOW + timedelta(minutes=1)))
    gateway.device_usage.return_value = usage("720")
    assert await admin.authuser(1, 44500, 999, used_usd=Decimal("500")) == first
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44500, 999, used_usd=Decimal("0"))
    assert (await quota.status(1)).used_usd == Decimal("510")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSegment)) == 1


@pytest.mark.parametrize("mode,revoked", [("normal", True), ("stopped", False), ("last_day", False)])
async def test_import_at_limit_uses_existing_enforcement_and_exemptions(lifecycle_db, mode, revoked):
    factory, _ = lifecycle_db
    await ready_cycle(factory, running=mode != "stopped",
                      reset=NOW + timedelta(hours=23) if mode == "last_day" else None,
                      allow_last_day=mode == "last_day")
    admin, _, _ = importer(factory)
    result = await admin.authuser(1, 44500, 999, used_usd=Decimal("700"))
    runtime = stack(factory)
    await runtime.actions.run_once()
    assert runtime.gateway.revoke_device.await_count == int(revoked)
    async with factory() as session:
        row = await session.get(DeviceAssociation, result.association_id)
        assert (row.state == "ENDED") == revoked


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "100000000", "0.00000000001"])
async def test_invalid_import_is_rejected_before_network_or_local_link(lifecycle_db, value):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    admin, gateway, _ = importer(factory)
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44500, 999, used_usd=Decimal(value))
    gateway.list_devices.assert_not_called()
    gateway.device_usage.assert_not_called()
    async with factory() as session:
        assert await session.scalar(select(DeviceAssociation)) is None


async def test_failed_baseline_does_not_half_import_and_retry_can_succeed(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    admin, gateway, quota = importer(factory)
    gateway.device_usage.side_effect = TimeoutError("no response")
    with pytest.raises((TimeoutError, EligibilityError)):
        await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    async with factory() as session:
        assert await session.scalar(select(DeviceAssociation)) is None
        assert await session.scalar(select(DeviceCycleLedger)) is None
    gateway.device_usage.side_effect = None
    await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    assert (await quota.status(1)).used_usd == Decimal("500")


async def test_existing_cycle_spend_cannot_be_reset_by_import(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    old, _ = await metered_user(factory, cycle_id, "300", device_id=44501)
    await end_association(factory, old, at=NOW)
    admin, _, quota = importer(factory)
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44500, 999, used_usd=Decimal("0"))
    assert (await quota.status(1)).used_usd == Decimal("300")


async def test_zero_import_is_an_explicit_verified_baseline(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    admin, _, quota = importer(factory)
    await admin.authuser(1, 44500, 999, used_usd=Decimal("0"))
    status = await quota.status(1)
    assert status.used_usd == Decimal("0") and status.quality == "VERIFIED"


async def test_cycle_change_during_baseline_query_rolls_back_association(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, gateway, _ = importer(factory)

    async def changed(*args, **kwargs):
        async with factory.begin() as session:
            row = await session.get(DeviceQuotaCycle, cycle_id)
            row.status = "EXPIRED"
        return usage("700")

    gateway.device_usage.side_effect = changed
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    async with factory() as session:
        assert await session.scalar(select(DeviceAssociation)) is None


async def test_concurrent_identical_import_creates_one_ledger(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    admin, _, quota = importer(factory)
    results = await asyncio.gather(*(admin.authuser(1, 44500, 999, used_usd=Decimal("500")) for _ in range(2)),
                                   return_exceptions=True)
    assert any(not isinstance(item, BaseException) for item in results)
    assert all(not isinstance(item, BaseException) or isinstance(item, EligibilityError) for item in results)
    assert (await quota.status(1)).used_usd == Decimal("500")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceCycleLedger)) == 1
        assert await session.scalar(select(func.count()).select_from(DeviceAssociation)) == 1


@pytest.mark.parametrize("task_arg", ["", "task-1 "])
async def test_command_forwards_import_and_preserves_optional_task(lifecycle_db, task_arg):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    callbacks, gateway, admin, _, message = wired(factory)
    gateway.device_usage = AsyncMock(return_value=usage("900"))
    await callbacks["authuser"](message, SimpleNamespace(args=f"1@example.invalid 44503 {task_arg}--used 500"), admin)
    assert (await DeviceQuotaService(factory, 178, clock=lambda: NOW).status(1)).used_usd == Decimal("500")
    assert "500" in message.answer.call_args.args[0]


@pytest.mark.parametrize("suffix", ["--used", "--used -1", "--used NaN", "--used 1 --used 2", "--wat 1"])
async def test_bad_command_never_associates(lifecycle_db, suffix):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    callbacks, gateway, admin, _, message = wired(factory)
    gateway.device_usage = AsyncMock(return_value=usage("700"))
    await callbacks["authuser"](message, SimpleNamespace(args=f"1@example.invalid 44503 {suffix}"), admin)
    async with factory() as session:
        assert await session.scalar(select(DeviceAssociation)) is None
    gateway.device_usage.assert_not_called()


async def test_ordinary_admin_association_does_not_query_or_import_history(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, gateway, _ = importer(factory)
    linked = await admin.authuser(1, 44500, 999)
    gateway.device_usage.assert_not_called()
    ledger, _ = ledger_service(factory)
    result = await ledger.apply(linked.association_id, cycle_id, await snapshot(factory, "700"))
    assert result.confirmed_used_usd == Decimal("0")
    assert result.quality == "NEEDS_REVIEW"
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))


async def test_usage_regression_cannot_erase_imported_spend(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, _, _ = importer(factory)
    linked = await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    ledger, _ = ledger_service(factory)
    result = await ledger.apply(linked.association_id, cycle_id, await snapshot(factory, "0"))
    assert result.confirmed_used_usd == Decimal("500")
    assert result.quality == "NEEDS_REVIEW"


async def test_import_is_not_reapplied_to_next_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    old_cycle = await ready_cycle(factory)
    admin, _, _ = importer(factory)
    linked = await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    later = NOW + timedelta(days=6, minutes=1)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, old_cycle)).status = "EXPIRED"
    from tests.integration.test_device_ledger import cycle
    new_cycle = await cycle(factory, started=NOW + timedelta(days=6), reset=NOW + timedelta(days=13))
    ledger, _ = ledger_service(factory, now=later)
    new = await ledger.apply(linked.association_id, new_cycle, await snapshot(factory, "725", sampled=later))
    assert new.confirmed_used_usd == Decimal("0")
    async with factory() as session:
        old = await session.scalar(select(DeviceCycleLedger).where(DeviceCycleLedger.cycle_id == old_cycle))
        assert old.confirmed_used_usd == Decimal("500")


async def test_import_works_with_realistically_advancing_clock(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    _, gateway, quota = importer(factory)
    from itertools import count
    ticks = count()
    admin = DeviceAdminService(factory, gateway, 178, quota.auth_quota_check,
                               clock=lambda: NOW + timedelta(microseconds=next(ticks)))
    await admin.authuser(1, 44500, 999, used_usd=Decimal("500"))
    assert (await quota.status(1)).used_usd == Decimal("500")
