import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import (
    AuditLog,
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
    User,
)

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def cycle(factory, *, started=None, reset=None, task_id=1, status="VERIFIED"):
    async with factory.begin() as session:
        row = DeviceQuotaCycle(task_id=task_id, started_at=started or NOW - timedelta(days=1),
                               reset_at=reset or NOW + timedelta(days=6), created_at=NOW,
                               status=status, account_id="7022")
        session.add(row)
        await session.flush()
        return row.id


async def association(factory, *, user_id=1, device_id=44500, reused=False, started=NOW, auth_status="SUCCEEDED"):
    async with factory.begin() as session:
        row = DeviceAssociation(user_id=user_id, task_id=1, org_id=178, device_id=device_id,
                                state="ACTIVE" if auth_status == "SUCCEEDED" else "UNKNOWN", started_at=started, updated_at=started)
        session.add(row)
        await session.flush()
        session.add(DeviceAction(association_id=row.id, kind="AUTH", status=auth_status, idempotency_key=uuid4().hex,
                                 target_device_id=device_id, result_reused=reused, attempt_count=0 if reused is None else 1,
                                 created_at=started, updated_at=started,
                                 completed_at=started if auth_status == "SUCCEEDED" else None))
        return row.id


async def end_association(factory, association_id, *, at, confirmed=True):
    async with factory.begin() as session:
        row = await session.get(DeviceAssociation, association_id)
        row.state = "ENDED"
        row.ended_at = at
        row.end_reason = "USER_DEAUTH"
        if confirmed:
            session.add(DeviceAction(association_id=row.id, kind="REVOKE", status="SUCCEEDED", idempotency_key=uuid4().hex,
                                     target_device_id=row.device_id, attempt_count=1, created_at=at, updated_at=at, completed_at=at))


async def snapshot(factory, total, *, sampled=NOW + timedelta(minutes=1), device_id=44500, org_id=178, range="all"):
    async with factory.begin() as session:
        row = DeviceUsageSnapshot(org_id=org_id, device_id=device_id, sample_key=uuid4().hex, range=range,
                                  total_usd=Decimal(total), sampled_at=sampled,
                                  payload={"range": range, "overview": {"total_usd": total}})
        session.add(row)
        await session.flush()
        return row.id


def ledger_service(factory, *, now=NOW + timedelta(hours=1)):
    clock = [now]
    return DeviceLedgerService(factory, 178, clock=lambda: clock[0]), clock


async def test_new_device_first_usage_is_fully_charged_and_replay_is_idempotent(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    pending = await service.ensure(association_id, cycle_id)
    assert pending.confirmed_used_usd is None and pending.quality == "UNKNOWN"
    snapshot_id = await snapshot(factory, "123.4567890123")
    result = await service.apply(association_id, cycle_id, snapshot_id)
    assert result.confirmed_used_usd == Decimal("123.4567890123") and result.quality == "VERIFIED"
    async with factory() as session:
        count_before = await session.scalar(select(func.count()).select_from(AuditLog))
        segment = await session.get(DeviceUsageSegment, result.segment_id)
        assert segment.baseline_total_usd == Decimal("0")
    assert await service.apply(association_id, cycle_id, snapshot_id) == result
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == count_before
        assert await session.scalar(select(func.count()).select_from(DeviceCycleLedger)) == 1
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSegment)) == 1


@pytest.mark.parametrize("reused", [True, None])
async def test_existing_device_uses_increment_anchor_but_discloses_initial_gap(lifecycle_db, reused):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory, reused=reused)
    service, _ = ledger_service(factory)
    first = await service.apply(association_id, cycle_id, await snapshot(factory, "1000"))
    assert first.confirmed_used_usd == Decimal("0") and first.quality == "NEEDS_REVIEW"
    second = await service.apply(association_id, cycle_id, await snapshot(factory, "1025", sampled=NOW + timedelta(minutes=2)))
    assert second.confirmed_used_usd == Decimal("25") and second.quality == "NEEDS_REVIEW"
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, second.segment_id)
        assert segment.baseline_total_usd == Decimal("1000")


async def test_no_successful_sample_never_becomes_zero_usage(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    first = await service.ensure(association_id, cycle_id)
    assert await service.ensure(association_id, cycle_id) == first
    assert first.confirmed_used_usd is None and first.quality == "UNKNOWN"
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, first.segment_id)
        assert segment.latest_total_usd is None and segment.confirmed_used_usd is None


async def test_same_cycle_replacement_keeps_old_usage_and_marks_partial_measurement(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    first_id = await association(factory)
    service, _ = ledger_service(factory)
    old = await service.apply(first_id, cycle_id, await snapshot(factory, "100"))
    await end_association(factory, first_id, at=NOW + timedelta(minutes=10))
    second_id = await association(factory, device_id=44501, started=NOW + timedelta(minutes=20))
    pending = await service.ensure(second_id, cycle_id)
    assert pending.ledger_id == old.ledger_id
    assert pending.confirmed_used_usd == Decimal("100") and pending.quality == "NEEDS_REVIEW"
    complete = await service.apply(second_id, cycle_id, await snapshot(factory, "25", device_id=44501, sampled=NOW + timedelta(minutes=30)))
    assert complete.confirmed_used_usd == Decimal("125") and complete.quality == "VERIFIED"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSegment)) == 2


async def test_regressions_do_not_clear_usage_lock_or_adjustments(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    first = await service.apply(association_id, cycle_id, await snapshot(factory, "100"))
    async with factory.begin() as session:
        ledger = await session.get(DeviceCycleLedger, first.ledger_id)
        ledger.quota_locked_at = NOW
        session.add(DeviceQuotaAdjustment(ledger_id=ledger.id, amount_usd=Decimal("50"), reason="manual adjustment",
                                          operator_telegram_id=999, operation_key=uuid4().hex, created_at=NOW))
    for minute, total, expected in [(2, "90", "100"), (3, "0", "100"), (4, "110", "110")]:
        result = await service.apply(association_id, cycle_id, await snapshot(factory, total, sampled=NOW + timedelta(minutes=minute)))
        assert result.confirmed_used_usd == Decimal(expected) and result.quality == "NEEDS_REVIEW"
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, first.ledger_id)
        assert ledger.quota_locked_at == NOW and ledger.quota_unlocked_at is None
        adjustment = await session.scalar(select(DeviceQuotaAdjustment))
        assert adjustment.amount_usd == Decimal("50")


async def test_older_sample_cannot_replace_latest_measurement(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    current = await service.apply(association_id, cycle_id, await snapshot(factory, "100", sampled=NOW + timedelta(minutes=10)))
    result = await service.apply(association_id, cycle_id, await snapshot(factory, "50", sampled=NOW + timedelta(minutes=5)))
    assert result == current
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, current.segment_id)
        assert segment.latest_total_usd == Decimal("100")
        assert segment.latest_sampled_at == NOW + timedelta(minutes=10)


@pytest.mark.parametrize("case", ["wrong_device", "wrong_org", "7d", "before_auth", "missing_snapshot", "unknown_auth", "wrong_task", "no_overlap"])
async def test_invalid_source_or_identity_rolls_back_without_creating_ledger(lifecycle_db, case):
    factory, _ = lifecycle_db
    cycle_kwargs = {}
    if case == "wrong_task":
        cycle_kwargs["task_id"] = 2
    elif case == "no_overlap":
        cycle_kwargs.update(started=NOW - timedelta(days=8), reset=NOW - timedelta(days=1), status="EXPIRED")
    cycle_id = await cycle(factory, **cycle_kwargs)
    association_id = await association(factory, auth_status="UNKNOWN" if case == "unknown_auth" else "SUCCEEDED")
    kwargs = {}
    if case == "wrong_device":
        kwargs["device_id"] = 44501
    elif case == "wrong_org":
        kwargs.update(org_id=179, device_id=44502)
    elif case == "7d":
        kwargs["range"] = "7d"
    elif case == "before_auth":
        kwargs["sampled"] = NOW - timedelta(seconds=1)
    snapshot_id = 999 if case == "missing_snapshot" else await snapshot(factory, "100", **kwargs)
    service, _ = ledger_service(factory)
    with pytest.raises(EligibilityError):
        await service.apply(association_id, cycle_id, snapshot_id)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceCycleLedger)) == 0
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSegment)) == 0


async def test_boundary_gap_is_not_assigned_wholly_to_old_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=1)
    old_cycle = await cycle(factory, reset=reset)
    association_id = await association(factory)
    service, clock = ledger_service(factory, now=NOW + timedelta(minutes=30))
    old = await service.apply(association_id, old_cycle, await snapshot(factory, "100"))
    clock[0] = reset + timedelta(minutes=2)
    boundary = await service.apply(association_id, old_cycle, await snapshot(factory, "150", sampled=reset - timedelta(seconds=1)))
    assert boundary.confirmed_used_usd == Decimal("100") and boundary.quality == "NEEDS_REVIEW"
    new_cycle = await cycle(factory, started=reset, reset=reset + timedelta(days=7))
    new = await service.apply(association_id, new_cycle, await snapshot(factory, "160", sampled=reset + timedelta(minutes=1)))
    assert new.ledger_id != old.ledger_id and new.confirmed_used_usd == Decimal("0") and new.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, old.ledger_id)).confirmed_used_usd == Decimal("100")


@pytest.mark.parametrize("confirmed", [True, False])
async def test_late_history_is_charged_to_old_cycle_only_after_confirmed_revoke(lifecycle_db, confirmed):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=1)
    cycle_id = await cycle(factory, reset=reset)
    association_id = await association(factory)
    service, clock = ledger_service(factory, now=NOW + timedelta(minutes=30))
    await service.apply(association_id, cycle_id, await snapshot(factory, "100"))
    await end_association(factory, association_id, at=NOW + timedelta(minutes=40), confirmed=confirmed)
    clock[0] = reset + timedelta(minutes=10)
    result = await service.apply(association_id, cycle_id, await snapshot(factory, "120", sampled=reset + timedelta(minutes=1)))
    assert result.confirmed_used_usd == Decimal("120" if confirmed else "100")
    assert result.quality == ("VERIFIED" if confirmed else "NEEDS_REVIEW")


async def test_late_snapshot_cannot_charge_usage_after_device_was_reassociated(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    old = await service.apply(association_id, cycle_id, await snapshot(factory, "100"))
    await end_association(factory, association_id, at=NOW + timedelta(minutes=10))
    await association(factory, user_id=2, started=NOW + timedelta(minutes=20), reused=True)
    late = await snapshot(factory, "150", sampled=NOW + timedelta(minutes=30))
    with pytest.raises(EligibilityError):
        await service.apply(association_id, cycle_id, late)
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, old.ledger_id)).confirmed_used_usd == Decimal("100")


async def test_concurrent_duplicate_snapshot_cannot_double_charge(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    snapshot_id = await snapshot(factory, "100")
    service, _ = ledger_service(factory)
    results = await asyncio.wait_for(asyncio.gather(*(service.apply(association_id, cycle_id, snapshot_id) for _ in range(3))), 20)
    assert all(result.confirmed_used_usd == Decimal("100") for result in results)
    assert len({result.ledger_id for result in results}) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSegment)) == 1


async def test_concurrent_different_device_segments_accumulate_without_lost_update(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    first_id = await association(factory)
    await end_association(factory, first_id, at=NOW + timedelta(minutes=10))
    second_id = await association(factory, device_id=44501, started=NOW + timedelta(minutes=20))
    first_sample = await snapshot(factory, "100")
    second_sample = await snapshot(factory, "25", device_id=44501, sampled=NOW + timedelta(minutes=30))
    service, _ = ledger_service(factory)
    results = await asyncio.wait_for(asyncio.gather(service.apply(first_id, cycle_id, first_sample), service.apply(second_id, cycle_id, second_sample)), 20)
    assert len({result.ledger_id for result in results}) == 1
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, results[0].ledger_id)
        assert ledger.confirmed_used_usd == Decimal("125") and ledger.quality == "VERIFIED"


async def test_banned_or_unbound_user_history_is_still_accounted(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    async with factory.begin() as session:
        user = await session.get(User, 1)
        user.status = "BANNED"
        user.binding_status = "UNBOUND"
    service, _ = ledger_service(factory)
    result = await service.apply(association_id, cycle_id, await snapshot(factory, "100"))
    assert result.confirmed_used_usd == Decimal("100")


async def test_unmeasured_previous_device_prevents_claiming_complete_current_cycle(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    old_id = await association(factory)
    # Simulate a restart before the old authorization got its first segment/sample.
    await end_association(factory, old_id, at=NOW + timedelta(minutes=10))
    new_id = await association(factory, device_id=44501, started=NOW + timedelta(minutes=20))
    service, _ = ledger_service(factory)
    result = await service.apply(new_id, cycle_id, await snapshot(factory, "25", device_id=44501, sampled=NOW + timedelta(minutes=30)))
    assert result.confirmed_used_usd == Decimal("25")
    assert result.quality == "NEEDS_REVIEW", "unsampled previous device must not disappear from cycle completeness"
    recovered = await service.apply(old_id, cycle_id, await snapshot(factory, "100"))
    assert recovered.ledger_id == result.ledger_id
    assert recovered.confirmed_used_usd == Decimal("125")
    assert recovered.quality == "VERIFIED"
    repeated = await service.ensure(new_id, cycle_id)
    assert repeated.ledger_id == recovered.ledger_id and repeated.segment_id == result.segment_id
    assert repeated.confirmed_used_usd == Decimal("125") and repeated.quality == "VERIFIED"


async def test_incomplete_segments_cannot_reduce_previously_confirmed_ledger(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    async with factory.begin() as session:
        session.add(DeviceCycleLedger(user_id=1, task_id=1, cycle_id=cycle_id, confirmed_used_usd=Decimal("500"),
                                      quality="VERIFIED", created_at=NOW, updated_at=NOW, quota_locked_at=NOW))
    service, _ = ledger_service(factory)
    result = await service.apply(association_id, cycle_id, await snapshot(factory, "20"))
    assert result.confirmed_used_usd == Decimal("500") and result.quality == "NEEDS_REVIEW"
    repeated = await service.ensure(association_id, cycle_id)
    assert repeated.confirmed_used_usd == Decimal("500") and repeated.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, result.ledger_id)).quota_locked_at == NOW


async def test_failed_transaction_keeps_previously_confirmed_consumption(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device_ledger

    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    old = await service.apply(association_id, cycle_id, await snapshot(factory, "100"))
    next_sample = await snapshot(factory, "150", sampled=NOW + timedelta(minutes=2))

    async def audit_failure(*args, **kwargs):
        raise RuntimeError("audit persistence failed")

    monkeypatch.setattr(device_ledger, "audit", audit_failure)
    with pytest.raises((RuntimeError, EligibilityError)):
        await service.apply(association_id, cycle_id, next_sample)
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, old.ledger_id)
        segment = await session.get(DeviceUsageSegment, old.segment_id)
        assert ledger.confirmed_used_usd == Decimal("100")
        assert segment.confirmed_used_usd == Decimal("100") and segment.latest_total_usd == Decimal("100")
        assert await session.get(DeviceUsageSnapshot, next_sample) is not None
