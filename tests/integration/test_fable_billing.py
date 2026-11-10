"""Subscription spend follows model deltas without charging pre-round history."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.infrastructure.db.models import DeviceCycleLedger, DeviceUsageSegment, DeviceUsageSnapshot
from reclaude_bot.infrastructure.reclaude.models import DeviceUsage
from tests.fixtures.device_runtime import ready_cycle
from tests.integration.test_device_account_usage import service as account_service
from tests.integration.test_device_account_usage import source_usage
from tests.integration.test_device_import_usage import importer
from tests.integration.test_device_ledger import NOW, association, cycle, end_association, ledger_service
from tests.integration.test_device_metering import job_row, ledger_rows, services
from tests.integration.test_device_reset import reset_task, runtime


def usage_with_fable(total: str, fable: str) -> DeviceUsage:
    return DeviceUsage.model_validate({
        "range": "all",
        "overview": {"total_usd": total},
        "models": {"breakdown": [
            {"model": "claude-fable-5-1", "total_usd": fable},
            {"model": "claude-opus-5-5", "total_usd": str(Decimal(total) - Decimal(fable))},
        ]},
    })


async def model_snapshot(factory, total: str, fable: str, *, sampled=None, omit_models=False, device_id=44500):
    payload = usage_with_fable(total, fable).model_dump(mode="json")
    if omit_models:
        payload.pop("models")
    async with factory.begin() as session:
        row = DeviceUsageSnapshot(
            org_id=178, device_id=device_id, sample_key=uuid4().hex, range="all",
            total_usd=Decimal(total), sampled_at=sampled or NOW + timedelta(minutes=1), payload=payload,
        )
        session.add(row)
        await session.flush()
        return row.id


@pytest.mark.parametrize("total,fable,expected", [("20", "20", "40"), ("50", "20", "70")])
async def test_new_device_charges_only_fable_twice_and_preserves_api_snapshot(lifecycle_db, total, fable, expected):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    sample_id = await model_snapshot(factory, total, fable)

    result = await service.apply(association_id, cycle_id, sample_id)

    assert result.confirmed_used_usd == Decimal(expected)
    assert result.quality == "VERIFIED"
    async with factory() as session:
        raw = await session.get(DeviceUsageSnapshot, sample_id)
        assert raw.total_usd == Decimal(total)
        assert raw.payload == usage_with_fable(total, fable).model_dump(mode="json")


async def test_replaying_weighted_sample_does_not_charge_fable_again(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    sample_id = await model_snapshot(factory, "100", "60")

    first = await service.apply(association_id, cycle_id, sample_id)
    repeated = await service.apply(association_id, cycle_id, sample_id)

    assert first.confirmed_used_usd == Decimal("160")
    assert repeated == first


async def test_existing_device_subtracts_fable_already_present_at_baseline(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory, reused=True)
    service, _ = ledger_service(factory)
    initial = await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))
    assert initial.confirmed_used_usd == 0

    result = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "130", "70", sampled=NOW + timedelta(minutes=2)),
    )

    assert result.confirmed_used_usd == Decimal("40")
    assert result.quality == "NEEDS_REVIEW"  # The reused device's original attribution gap remains.


async def test_fable_counter_regression_is_reviewed_even_when_api_total_increases(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))

    result = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "130", "50", sampled=NOW + timedelta(minutes=2)),
    )

    assert result.confirmed_used_usd == Decimal("160")
    assert result.quality == "NEEDS_REVIEW"


async def test_same_timestamp_with_different_model_costs_is_not_an_idempotent_sample(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))

    result = await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "70"))

    assert result.confirmed_used_usd == Decimal("160")
    assert result.quality == "NEEDS_REVIEW"


async def test_missing_model_detail_preserves_confirmed_spend_and_requires_review(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))

    result = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "130", "70", sampled=NOW + timedelta(minutes=2), omit_models=True),
    )

    assert result.confirmed_used_usd == Decimal("160")
    assert result.quality == "NEEDS_REVIEW"


async def test_reset_starts_at_zero_and_only_charges_new_weighted_spend(lifecycle_db):
    factory, _ = lifecycle_db
    old_cycle_id = await ready_cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    await service.apply(association_id, old_cycle_id, await model_snapshot(factory, "100", "60"))
    rt = runtime(factory)
    rt.gateway.device_usage.return_value = usage_with_fable("100", "60")

    reset = await reset_task(rt)
    status = await rt.quota.status(1, task_id=1)
    assert status.used_usd == 0 and status.quality == "VERIFIED"
    rt.clock[0] += timedelta(minutes=1)
    ledger = DeviceLedgerService(factory, 178, clock=lambda: rt.clock[0])
    result = await ledger.apply(
        association_id, reset.cycle_id,
        await model_snapshot(factory, "130", "70", sampled=rt.clock[0]),
    )

    assert result.confirmed_used_usd == Decimal("40")
    assert result.quality == "VERIFIED"


async def test_explicit_imported_spend_is_not_multiplied_with_fable_baseline(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    admin, gateway, quota = importer(factory)
    gateway.device_usage.return_value = usage_with_fable("100", "60")

    imported = await admin.authuser(1, 44500, 999, used_usd=Decimal("25"))
    assert (await quota.status(1)).used_usd == Decimal("25")
    service, _ = ledger_service(factory)
    result = await service.apply(imported.association_id, cycle_id, await model_snapshot(factory, "130", "70"))

    assert result.confirmed_used_usd == Decimal("65")
    assert result.quality == "VERIFIED"


async def test_account_capacity_estimate_uses_subscription_weighted_spend(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id = await association(factory)
    ledger, _ = ledger_service(factory)
    await ledger.apply(association_id, cycle_id, await model_snapshot(factory, "100", "40"))
    moment = NOW + timedelta(hours=1)
    account, _ = account_service(factory, source_usage(percent="10", sampled_at=moment), clock=lambda: moment)

    result = await account.get_account_usage(1)

    assert result.managed_used_usd == Decimal("140")
    assert result.estimated_total_usd == Decimal("1400")
    assert result.estimate_reason is None


@pytest.mark.parametrize("missing_endpoint", ["baseline_fable_usd", "latest_fable_usd"])
async def test_unconverted_model_endpoint_cannot_feed_an_account_capacity_estimate(lifecycle_db, missing_endpoint):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id = await association(factory, reused=True)
    ledger, _ = ledger_service(factory)
    await ledger.apply(association_id, cycle_id, await model_snapshot(factory, "100", "40"))
    billed = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "120", "50", sampled=NOW + timedelta(minutes=2)),
    )
    async with factory.begin() as session:
        segment = await session.scalar(select(DeviceUsageSegment).where(DeviceUsageSegment.id == billed.segment_id))
        setattr(segment, missing_endpoint, None)
    moment = NOW + timedelta(hours=1)
    account, _ = account_service(factory, source_usage(percent="10", sampled_at=moment), clock=lambda: moment)

    result = await account.get_account_usage(1)

    assert result.estimated_total_usd is None
    assert result.estimate_reason is not None
    assert not await account.record_estimate(1, expected_cycle_id=cycle_id)


@pytest.mark.parametrize("invalid_sample", ["missing_models", "fable_regression"])
async def test_invalid_samples_cannot_lower_the_trusted_fable_counter_before_recovery(lifecycle_db, invalid_sample):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, _ = ledger_service(factory)
    first = await service.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))

    invalid = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "120", "50", sampled=NOW + timedelta(minutes=2),
                             omit_models=invalid_sample == "missing_models"),
    )
    assert invalid.confirmed_used_usd == Decimal("160")
    assert invalid.quality == "NEEDS_REVIEW"
    still_regressed = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "200", "55", sampled=NOW + timedelta(minutes=3)),
    )
    assert still_regressed.confirmed_used_usd == Decimal("160")
    assert still_regressed.quality == "NEEDS_REVIEW"
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, first.segment_id)
        assert segment.latest_total_usd == Decimal("100")
        assert segment.latest_fable_usd == Decimal("60")
        assert segment.billing_valid is False

    recovered = await service.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "210", "65", sampled=NOW + timedelta(minutes=4)),
    )
    assert recovered.confirmed_used_usd == Decimal("275")
    assert recovered.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceUsageSegment, first.segment_id)).billing_valid is True


@pytest.mark.parametrize("invalid_sample", ["missing_models", "timestamp_conflict"])
async def test_invalid_billing_blocks_estimates_until_fresh_trustworthy_model_counters_arrive(lifecycle_db, invalid_sample):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id = await association(factory)
    ledger, _ = ledger_service(factory)
    first = await ledger.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))
    invalid_at = NOW + timedelta(minutes=2 if invalid_sample == "missing_models" else 1)
    invalid = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "100", "70", sampled=invalid_at, omit_models=invalid_sample == "missing_models"),
    )
    assert invalid.confirmed_used_usd == Decimal("160")
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, first.segment_id)
        assert segment.baseline_fable_usd is not None and segment.latest_fable_usd is not None
        assert segment.billing_valid is False
    moment = NOW + timedelta(hours=1)
    account, _ = account_service(factory, source_usage(percent="10", sampled_at=moment), clock=lambda: moment)
    assert (await account.get_account_usage(1)).estimated_total_usd is None
    assert not await account.record_estimate(1, expected_cycle_id=cycle_id)

    recovered = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "130", "70", sampled=NOW + timedelta(minutes=3)),
    )
    assert recovered.confirmed_used_usd == Decimal("200")
    assert recovered.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceUsageSegment, first.segment_id)).billing_valid is True
    assert (await account.get_account_usage(1)).estimated_total_usd == Decimal("2000")


@pytest.mark.parametrize("older_fable", ["0", "100"])
async def test_older_model_counters_cannot_replace_newer_weighted_spend(lifecycle_db, older_fable):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    ledger, _ = ledger_service(factory)
    current = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "100", "60", sampled=NOW + timedelta(minutes=3)),
    )

    replayed = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "100", older_fable, sampled=NOW + timedelta(minutes=2)),
    )

    assert replayed == current
    assert replayed.confirmed_used_usd == Decimal("160")
    assert replayed.quality == "VERIFIED"
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, current.segment_id)
        assert segment.latest_fable_usd == Decimal("60")
        assert segment.billing_valid is True


async def test_first_incomplete_sample_blocks_estimate_from_other_users_valid_spend(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    first_association = await association(factory)
    second_association = await association(factory, user_id=2, device_id=44501)
    ledger, _ = ledger_service(factory)
    await ledger.apply(first_association, cycle_id, await model_snapshot(factory, "100", "40"))
    pending = await ledger.ensure(second_association, cycle_id)
    assert pending.confirmed_used_usd is None and pending.quality == "UNKNOWN"
    moment = NOW + timedelta(hours=1)
    account, _ = account_service(factory, source_usage(percent="10", sampled_at=moment), clock=lambda: moment)
    assert (await account.get_account_usage(1)).estimated_total_usd == Decimal("1400")

    invalid = await ledger.apply(
        second_association, cycle_id,
        await model_snapshot(factory, "50", "20", device_id=44501, omit_models=True),
    )

    assert invalid.confirmed_used_usd is None
    assert invalid.quality == "NEEDS_REVIEW"
    result = await account.get_account_usage(1)
    assert result.estimated_total_usd is None
    assert result.estimate_reason is not None
    assert not await account.record_estimate(1, expected_cycle_id=cycle_id)


async def test_sample_older_than_latest_billing_failure_cannot_restore_valid_billing(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    ledger, _ = ledger_service(factory)
    first = await ledger.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))
    invalid = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "150", "70", sampled=NOW + timedelta(minutes=3), omit_models=True),
    )
    assert invalid.confirmed_used_usd == Decimal("160")

    late = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "120", "65", sampled=NOW + timedelta(minutes=2)),
    )

    assert late.confirmed_used_usd == Decimal("160")
    assert late.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceUsageSegment, first.segment_id)).billing_valid is False

    recovered = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "150", "70", sampled=NOW + timedelta(minutes=4)),
    )

    assert recovered.confirmed_used_usd == Decimal("220")
    assert recovered.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert (await session.get(DeviceUsageSegment, first.segment_id)).billing_valid is True


async def test_final_revoked_device_sample_retries_missing_models_before_completing(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    await end_association(factory, association_id, at=NOW + timedelta(minutes=1))
    metering, gateway, clock = services(factory, clock=[NOW + timedelta(minutes=2)])
    incomplete = usage_with_fable("100", "60")
    incomplete.models = None
    gateway.device_usage.return_value = incomplete
    job_id = await metering.enqueue(association_id, sequence=1)

    failed = await metering.run_job(job_id)

    assert failed.status == "PENDING" and failed.snapshot_id is None
    pending = await job_row(factory, job_id)
    assert pending.status == "PENDING" and pending.attempt_count == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSnapshot)) == 0

    clock[0] = pending.run_after
    gateway.device_usage.return_value = usage_with_fable("100", "60")
    completed = await metering.run_job(job_id)

    assert completed.status == "COMPLETED" and completed.snapshot_id is not None
    assert (await job_row(factory, job_id)).attempt_count == 2
    charged = await ledger_rows(factory)
    assert len(charged) == 1
    assert charged[0].confirmed_used_usd == Decimal("160")
    assert charged[0].quality == "VERIFIED"
    gateway.device_usage.assert_awaited_with(44500, 178, range="all")
    assert gateway.device_usage.await_count == 2
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


async def test_retained_ledger_high_water_mark_cannot_inflate_capacity_estimate(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id = await association(factory)
    ledger, _ = ledger_service(factory)
    first = await ledger.apply(association_id, cycle_id, await model_snapshot(factory, "100", "60"))
    async with factory.begin() as session:
        retained = await session.get(DeviceCycleLedger, first.ledger_id)
        retained.confirmed_used_usd = Decimal("200")
        retained.quality = "NEEDS_REVIEW"

    below_retained = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "110", "60", sampled=NOW + timedelta(minutes=2)),
    )

    assert below_retained.confirmed_used_usd == Decimal("200")
    assert below_retained.quality == "NEEDS_REVIEW"
    async with factory() as session:
        segment = await session.get(DeviceUsageSegment, first.segment_id)
        assert segment.confirmed_used_usd == Decimal("170")
        assert segment.billing_valid is True
        assert segment.baseline_fable_usd is not None and segment.latest_fable_usd is not None
    moment = NOW + timedelta(hours=1)
    account, _ = account_service(factory, source_usage(percent="10", sampled_at=moment), clock=lambda: moment)
    assert (await account.get_account_usage(1)).estimated_total_usd is None
    assert not await account.record_estimate(1, expected_cycle_id=cycle_id)

    recovered = await ledger.apply(
        association_id, cycle_id,
        await model_snapshot(factory, "160", "60", sampled=NOW + timedelta(minutes=3)),
    )

    assert recovered.confirmed_used_usd == Decimal("220")
    assert recovered.quality == "NEEDS_REVIEW"
    assert (await account.get_account_usage(1)).estimated_total_usd == Decimal("2200")
