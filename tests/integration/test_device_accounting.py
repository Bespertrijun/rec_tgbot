"""Persistence invariants for accounting; quota algorithms belong to later batches."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reclaude_bot.infrastructure.db.models import (
    Device,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceNotification,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceResampleJob,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
RESET = NOW + timedelta(days=7)


async def save(factory, row):
    async with factory.begin() as session:
        session.add(row)
    return row


@pytest_asyncio.fixture
async def accounting(lifecycle_db):
    factory, backend = lifecycle_db
    async with factory.begin() as session:
        session.add_all([
            DeviceQuotaCycle(id=i, task_id=i, started_at=NOW, reset_at=RESET, created_at=NOW, status="INITIALIZING")
            for i in (1, 2)
        ])
        session.add(DeviceAssociation(id=1, user_id=1, task_id=1, org_id=178, device_id=44500, state="ACTIVE", started_at=NOW, updated_at=NOW))
        await session.flush()
        session.add_all([
            DeviceCycleLedger(id=i, user_id=user, cycle_id=cycle, task_id=task, quality="UNKNOWN", created_at=NOW, updated_at=NOW)
            for i, user, cycle, task in ((1, 1, 1, 1), (2, 2, 1, 1), (3, 1, 2, 2))
        ])
    return factory, backend


def segment(**changes):
    values = dict(association_id=1, user_id=1, task_id=1, ledger_id=1, started_at=NOW, quality="UNKNOWN")
    values.update(changes)
    return DeviceUsageSegment(**values)


def snapshot(**changes):
    values = dict(org_id=178, device_id=44500, sample_key=uuid4().hex, range="all", total_usd=Decimal("5839.0072946000"), sampled_at=NOW, payload={"range": "all"})
    values.update(changes)
    return DeviceUsageSnapshot(**values)


def notification(**changes):
    values = dict(ledger_id=1, event="THRESHOLD_100", status="PENDING", attempt_count=0, created_at=NOW, updated_at=NOW, payload={"used_usd": "700"})
    values.update(changes)
    return DeviceNotification(**values)


def resample(**changes):
    values = dict(association_id=1, sequence=0, run_after=NOW + timedelta(minutes=1), status="PENDING", attempt_count=0, created_at=NOW, updated_at=NOW)
    values.update(changes)
    return DeviceResampleJob(**values)


def adjustment(**changes):
    values = dict(ledger_id=1, amount_usd=Decimal("50.1234567890"), reason="test adjustment", operator_telegram_id=1, operation_key=uuid4().hex, created_at=NOW)
    values.update(changes)
    return DeviceQuotaAdjustment(**values)


async def test_same_reset_is_isolated_by_task_and_duplicates_are_rejected(accounting):
    factory, _ = accounting
    async with factory() as session:
        rows = (await session.scalars(select(DeviceQuotaCycle).order_by(DeviceQuotaCycle.id))).all()
        assert [row.task_id for row in rows] == [1, 2]
        assert rows[0].reset_at == rows[1].reset_at
    with pytest.raises(IntegrityError):
        await save(factory, DeviceQuotaCycle(task_id=1, started_at=NOW, reset_at=RESET, status="INITIALIZING", created_at=NOW))


async def test_unknown_ledger_and_segment_do_not_create_zero_consumption(accounting):
    factory, _ = accounting
    row = await save(factory, segment())
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, 1)
        part = await session.get(DeviceUsageSegment, row.id)
        assert ledger.confirmed_used_usd is None
        assert ledger.quality == "UNKNOWN"
        assert part.baseline_total_usd is None
        assert part.latest_total_usd is None
        assert part.confirmed_used_usd is None


@pytest.mark.parametrize("changes", [
    {"ledger_id": 2}, {"ledger_id": 2, "user_id": 2}, {"ledger_id": 3}, {"ledger_id": 3, "task_id": 2},
    {"quality": "VERIFIED"}, {"quality": "INVALID"}, {"baseline_total_usd": Decimal("1")},
    {"baseline_captured_at": NOW}, {"latest_total_usd": Decimal("1")}, {"latest_sampled_at": NOW},
    {"ended_at": NOW - timedelta(seconds=1)}, {"confirmed_used_usd": Decimal("-1")},
])
async def test_bad_segment_cannot_mix_identity_or_claim_complete_measurement(accounting, changes):
    factory, _ = accounting
    with pytest.raises(IntegrityError):
        await save(factory, segment(**changes))


async def test_valid_segment_preserves_precision_and_a_regression_as_review_evidence(accounting):
    factory, _ = accounting
    row = await save(factory, segment(
        baseline_total_usd=Decimal("100.1234567890"), baseline_captured_at=NOW,
        latest_total_usd=Decimal("101.1234567891"), latest_sampled_at=NOW + timedelta(seconds=1),
        confirmed_used_usd=Decimal("1.0000000001"), quality="VERIFIED",
    ))
    with pytest.raises(IntegrityError):
        await save(factory, segment())
    async with factory.begin() as session:
        part = await session.get(DeviceUsageSegment, row.id)
        assert part.confirmed_used_usd == Decimal("1.0000000001")
        part.latest_total_usd = Decimal("0")
        part.quality = "NEEDS_REVIEW"
    async with factory() as session:
        part = await session.get(DeviceUsageSegment, row.id)
        assert part.confirmed_used_usd == Decimal("1.0000000001")
        assert part.latest_total_usd == Decimal("0")
        assert part.quality == "NEEDS_REVIEW"


@pytest.mark.parametrize("bad_money", [Decimal("-0.0000000001"), Decimal("NaN")])
async def test_snapshot_invalid_money_is_rejected(accounting, bad_money):
    factory, _ = accounting
    with pytest.raises(IntegrityError):
        await save(factory, snapshot(total_usd=bad_money))


async def test_snapshot_deduplication_is_by_operation_not_repeated_total(accounting):
    factory, _ = accounting
    first = await save(factory, snapshot(sample_key="sample-operation"))
    with pytest.raises(IntegrityError):
        await save(factory, snapshot(sample_key="sample-operation"))
    second = await save(factory, snapshot(sampled_at=NOW + timedelta(minutes=1)))
    async with factory() as session:
        a = await session.get(DeviceUsageSnapshot, first.id)
        b = await session.get(DeviceUsageSnapshot, second.id)
        assert a.total_usd == b.total_usd == Decimal("5839.0072946000")
    with pytest.raises(IntegrityError):
        await save(factory, snapshot(org_id=179))


async def test_adjustments_are_separate_from_usage_and_keep_operation_identity(accounting):
    factory, _ = accounting
    await save(factory, adjustment(operation_key="transfer-out", amount_usd=Decimal("-50.1234567890"), transfer_key="transfer"))
    await save(factory, adjustment(ledger_id=2, operation_key="transfer-in", transfer_key="transfer"))
    with pytest.raises(IntegrityError):
        await save(factory, adjustment(operation_key="transfer-in"))
    async with factory() as session:
        rows = (await session.scalars(select(DeviceQuotaAdjustment))).all()
        assert sum((r.amount_usd for r in rows), Decimal("0")) == 0
        assert (await session.get(DeviceCycleLedger, 1)).confirmed_used_usd is None


@pytest.mark.parametrize("changes", [
    {"event": "INVALID"}, {"status": "SENT"}, {"sent_at": NOW}, {"attempt_count": -1},
])
async def test_notification_invalid_state_is_rejected(accounting, changes):
    factory, _ = accounting
    with pytest.raises(IntegrityError):
        await save(factory, notification(**changes))


async def test_notification_identity_survives_delivery_and_different_events_remain_distinct(accounting):
    factory, _ = accounting
    row = await save(factory, notification())
    async with factory.begin() as session:
        notice = await session.get(DeviceNotification, row.id)
        notice.status = "SENT"
        notice.sent_at = NOW
    with pytest.raises(IntegrityError):
        await save(factory, notification())
    await save(factory, notification(event="AUTH_AVAILABLE_LAST_DAY"))
    await save(factory, notification(event="AUTH_AVAILABLE_NEW_CYCLE"))


@pytest.mark.parametrize("changes", [
    {"sequence": -1}, {"attempt_count": -1}, {"status": "INVALID"}, {"status": "COMPLETED"},
    {"status": "CANCELLED"}, {"completed_at": NOW},
])
async def test_resample_job_invalid_state_is_rejected(accounting, changes):
    factory, _ = accounting
    with pytest.raises(IntegrityError):
        await save(factory, resample(**changes))


async def test_revoked_history_locks_notifications_and_resampling_survive_new_session(accounting):
    factory, _ = accounting
    sample = await save(factory, snapshot())
    await save(factory, segment())
    await save(factory, notification(event="AUTH_AVAILABLE_NEW_CYCLE", next_retry_at=NOW + timedelta(minutes=2)))
    await save(factory, resample())
    with pytest.raises(IntegrityError):
        await save(factory, resample())
    async with factory.begin() as session:
        assoc = await session.get(DeviceAssociation, 1)
        assoc.state, assoc.ended_at, assoc.end_reason = "ENDED", NOW, "QUOTA"
        ledger = await session.get(DeviceCycleLedger, 1)
        ledger.confirmed_used_usd, ledger.quality, ledger.quota_locked_at = Decimal("700"), "VERIFIED", NOW
        device = await session.scalar(select(Device).where(Device.org_id == 178, Device.device_id == 44500))
        device.revoked_at = NOW
    async with factory() as session:
        assert (await session.get(DeviceAssociation, 1)).state == "ENDED"
        assert (await session.get(DeviceUsageSnapshot, sample.id)).total_usd == Decimal("5839.0072946000")
        assert (await session.get(DeviceCycleLedger, 1)).quota_locked_at is not None
        assert (await session.scalar(select(DeviceNotification))).status == "PENDING"
        assert (await session.scalar(select(DeviceResampleJob))).status == "PENDING"


@pytest.mark.parametrize("changes", [
    {"confirmed_used_usd": Decimal("-1")}, {"quality": "VERIFIED"}, {"quality": "INVALID"},
    {"quota_unlocked_at": NOW}, {"quota_locked_at": NOW, "quota_unlocked_at": NOW - timedelta(seconds=1)},
    {"task_id": 2},
])
async def test_ledger_cannot_claim_false_knowledge_or_wrong_scope(accounting, changes):
    factory, _ = accounting
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            ledger = await session.get(DeviceCycleLedger, 1)
            for field, value in changes.items():
                setattr(ledger, field, value)
