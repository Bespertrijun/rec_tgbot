import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import DeviceUsageSegment, DeviceUsageSnapshot
from tests.integration.test_device_migration import config_for
from tests.integration.test_device_migration import migration_url as shared_migration_url

migration_url = shared_migration_url
REVISION = "0019_fable_billing"
PREVIOUS = "0018_round_quota"
ESTIMATE_FIELDS = ("estimated_total_usd", "estimate_used_usd", "estimate_percent", "estimate_snapshot_at", "estimate_recorded_at")


async def insert_old_row(connection, table, **values):
    # Textual INSERTs deliberately avoid new ORM defaults when creating the 0018 database.
    statement = sa.text(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join(':' + name for name in values)})")
    for name, value in values.items():
        if isinstance(value, datetime):
            statement = statement.bindparams(sa.bindparam(name, type_=sa.DateTime(timezone=True)))
        elif isinstance(value, Decimal):
            statement = statement.bindparams(sa.bindparam(name, type_=sa.Numeric(18, 10)))
        elif isinstance(value, dict):
            statement = statement.bindparams(sa.bindparam(name, type_=sa.JSON()))
    await connection.execute(statement, values)


async def seed_old_accounting(url, *, fault=None):
    engine = create_async_engine(url)
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=2)
    old_start = now - timedelta(days=9)
    baseline_at = now - timedelta(days=1)
    latest_at = now - timedelta(hours=2)
    next_at = now - timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            await insert_old_row(connection, "users", id=1, telegram_user_id=1001, email="one@example.invalid", email_normalized="one@example.invalid",
                                 binding_status="BOUND", status="ACTIVE", baseline_status="UNKNOWN", bound_at=old_start, updated_at=old_start)
            await connection.execute(sa.text("UPDATE quota_tasks SET limit_usd=700 WHERE id=1"))
            await insert_old_row(connection, "device_task_scopes", task_id=1, org_id=178, created_at=old_start, updated_at=old_start)
            for identifier, cycle_start, reset, status in [(1, start, now + timedelta(days=5), "VERIFIED"), (2, old_start, start, "EXPIRED")]:
                await insert_old_row(connection, "device_quota_cycles", id=identifier, task_id=1, started_at=cycle_start, reset_at=reset,
                                     created_at=cycle_start, status=status, account_id="7022", estimated_total_usd=Decimal("2400"),
                                     estimate_used_usd=Decimal("120"), estimate_percent=Decimal("5"), estimate_snapshot_at=latest_at,
                                     estimate_recorded_at=latest_at, estimate_blocked=True, opening_limit_usd=Decimal("700"))
                await insert_old_row(connection, "device_cycle_ledgers", id=identifier, user_id=1, task_id=1, cycle_id=identifier,
                                     confirmed_used_usd=Decimal("200" if fault == "ledger_high" and identifier == 1 else "85" if identifier == 1 else "99"),
                                     quality="VERIFIED", quota_locked_at=latest_at, created_at=cycle_start, updated_at=latest_at)
            for identifier, device_id, association_start, association_end in [(1, 44500, baseline_at, latest_at), (2, 44501, latest_at, None), (3, 44502, old_start, start)]:
                await insert_old_row(connection, "devices", id=identifier, org_id=178, device_id=device_id, name="test", first_synced_at=old_start, last_synced_at=now)
                await insert_old_row(connection, "device_associations", id=identifier, user_id=1, task_id=1, org_id=178, device_id=device_id,
                                     state="ENDED" if association_end else "ACTIVE", started_at=association_start, ended_at=association_end, updated_at=now)
            await insert_old_row(connection, "device_usage_segments", id=1, association_id=1, user_id=1, task_id=1, ledger_id=1,
                                 started_at=baseline_at, ended_at=latest_at, baseline_total_usd=Decimal("100"), baseline_captured_at=baseline_at,
                                 latest_total_usd=Decimal("150"), latest_sampled_at=latest_at, confirmed_used_usd=Decimal("55"), imported_used_usd=Decimal("5"), quality="VERIFIED")
            await insert_old_row(connection, "device_usage_segments", id=2, association_id=2, user_id=1, task_id=1, ledger_id=1,
                                 started_at=latest_at, baseline_total_usd=Decimal("0"), baseline_captured_at=latest_at,
                                 latest_total_usd=Decimal("30"), latest_sampled_at=next_at, confirmed_used_usd=Decimal("30"), quality="VERIFIED")
            await insert_old_row(connection, "device_usage_segments", id=3, association_id=3, user_id=1, task_id=1, ledger_id=2,
                                 started_at=old_start, ended_at=start, baseline_total_usd=Decimal("0"), baseline_captured_at=old_start,
                                 latest_total_usd=Decimal("99"), latest_sampled_at=start, confirmed_used_usd=Decimal("99"), quality="VERIFIED")
            await insert_old_row(connection, "device_quota_adjustments", id=1, ledger_id=1, amount_usd=Decimal("25"),
                                 reason="manual", operator_telegram_id=42, operation_key="preserved-adjustment", created_at=latest_at)

            async def snapshot(identifier, device_id, total, fable, at, *, missing_models=False):
                payload = {"range": "all", "overview": {"total_usd": str(total)}}
                if not missing_models:
                    payload["models"] = {"breakdown": [
                        {"model": "claude-fable-5-1", "total_usd": str(fable)},
                        {"model": "claude-opus-5-5", "total_usd": str(total - fable)},
                    ]}
                await insert_old_row(connection, "device_usage_snapshots", id=identifier, org_id=178, device_id=device_id,
                                     sample_key=f"migration-sample-{identifier}", range="all", total_usd=Decimal(total), sampled_at=at, payload=payload)

            if fault != "missing_baseline":
                await snapshot(1, 44500, 100, 80, baseline_at, missing_models=fault == "missing_models")
            await snapshot(2, 44500, 150, 70 if fault == "regression" else 110, latest_at, missing_models=fault == "missing_latest_models")
            await snapshot(3, 44501, 30, 20, next_at)
            await snapshot(4, 44502, 99, 90, start)
            if fault in {"conflicting_fable", "conflicting_raw", "identical_duplicate"}:
                await snapshot(5, 44500, 101 if fault == "conflicting_raw" else 100, 70 if fault == "conflicting_fable" else 80, baseline_at)
            if fault == "unmeasured":
                await connection.execute(sa.text(
                    "UPDATE device_usage_segments SET latest_total_usd=NULL, latest_sampled_at=NULL, confirmed_used_usd=NULL, quality='UNKNOWN' WHERE id=2"
                ))
                await connection.execute(sa.text("UPDATE device_cycle_ledgers SET confirmed_used_usd=55, quality='NEEDS_REVIEW' WHERE id=1"))
            if fault == "missing_latest_models":
                await snapshot(5, 44500, 170, 125, next_at)
    finally:
        await engine.dispose()


async def saved_accounting(url):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            metadata = sa.MetaData()
            await connection.run_sync(lambda conn: metadata.reflect(bind=conn))
            tables = ["quota_tasks", "device_quota_cycles", "device_cycle_ledgers", "device_usage_segments", "device_usage_snapshots", "device_quota_adjustments", "audit_logs"]
            result = {}
            for name in tables:
                table = metadata.tables[name]
                result[name] = [dict(row) for row in (await connection.execute(sa.select(table).order_by(table.c.id))).mappings()]
            result["segment_columns"] = set(metadata.tables["device_usage_segments"].c.keys())
            return result
    finally:
        await engine.dispose()


def upgrade_seeded(url, *, fault=None):
    config = config_for(url)
    command.upgrade(config, PREVIOUS)
    asyncio.run(seed_old_accounting(url, fault=fault))
    before = asyncio.run(saved_accounting(url))
    command.upgrade(config, REVISION)
    return config, before, asyncio.run(saved_accounting(url))


def test_repairs_current_active_and_ended_segments_without_recharging_the_baseline(migration_url):
    config, before, after = upgrade_seeded(migration_url)
    segments = after["device_usage_segments"]
    assert (segments[0]["baseline_fable_usd"], segments[0]["latest_fable_usd"], segments[0]["confirmed_used_usd"]) == (Decimal("80"), Decimal("110"), Decimal("85"))
    assert segments[0]["imported_used_usd"] == Decimal("5")
    assert segments[0]["ended_at"] is not None
    assert (segments[1]["baseline_fable_usd"], segments[1]["latest_fable_usd"], segments[1]["confirmed_used_usd"]) == (Decimal("0"), Decimal("20"), Decimal("50"))
    assert all(row["billing_valid"] for row in segments[:2])
    assert all(row["billing_invalid_at"] is None for row in segments[:2])
    assert after["device_cycle_ledgers"][0]["confirmed_used_usd"] == Decimal("135")
    assert after["device_cycle_ledgers"][0]["quality"] == "VERIFIED"
    assert after["device_cycle_ledgers"][0]["quota_locked_at"] == before["device_cycle_ledgers"][0]["quota_locked_at"]
    for table in ("quota_tasks", "device_quota_adjustments", "device_usage_snapshots"):
        assert after[table] == before[table]
    assert after["device_cycle_ledgers"][1] == before["device_cycle_ledgers"][1]
    for key, value in before["device_usage_segments"][2].items():
        assert segments[2][key] == value
    assert segments[2]["baseline_fable_usd"] is None and segments[2]["latest_fable_usd"] is None and not segments[2]["billing_valid"]
    for cycle in after["device_quota_cycles"]:
        assert all(cycle[name] is None for name in ESTIMATE_FIELDS)
        assert cycle["estimate_blocked"] and cycle["opening_limit_usd"] == Decimal("700")
    audit = after["audit_logs"][-1]
    assert audit["action"] == "DEVICE_FABLE_BILLING_MIGRATED"
    assert audit["parameters_summary"] == {
        "cycle_id": 1, "old_confirmed_used_usd": "85.0000000000", "new_confirmed_used_usd": "135.0000000000", "review_segment_ids": [],
    }
    command.upgrade(config, REVISION)
    assert asyncio.run(saved_accounting(migration_url)) == after


@pytest.mark.parametrize("fault", ["missing_baseline", "missing_models", "conflicting_fable", "conflicting_raw", "regression"])
def test_unrecoverable_endpoint_keeps_prior_usage_and_blocks_billing(migration_url, fault):
    _, _, after = upgrade_seeded(migration_url, fault=fault)
    segment = after["device_usage_segments"][0]
    assert segment["confirmed_used_usd"] == Decimal("55")
    assert segment["quality"] == "NEEDS_REVIEW" and not segment["billing_valid"]
    assert segment["billing_invalid_at"] is not None
    if fault != "regression":
        assert segment["baseline_fable_usd"] is None
    else:
        assert segment["baseline_fable_usd"] == Decimal("80") and segment["latest_fable_usd"] == Decimal("70")
    ledger = after["device_cycle_ledgers"][0]
    assert ledger["confirmed_used_usd"] == Decimal("105") and ledger["quality"] == "NEEDS_REVIEW"
    assert after["audit_logs"][-1]["parameters_summary"]["review_segment_ids"] == [1]


def test_conflicting_ledger_high_water_mark_is_preserved(migration_url):
    _, _, after = upgrade_seeded(migration_url, fault="ledger_high")
    assert after["device_cycle_ledgers"][0]["confirmed_used_usd"] == Decimal("200")
    assert after["device_cycle_ledgers"][0]["quality"] == "NEEDS_REVIEW"
    assert all(not segment["billing_valid"] for segment in after["device_usage_segments"][:2])
    assert all(segment["billing_invalid_at"] == segment["latest_sampled_at"] for segment in after["device_usage_segments"][:2])
    assert after["audit_logs"][-1]["parameters_summary"]["review_segment_ids"] == [1, 2]


def test_identical_duplicate_endpoint_evidence_is_accepted(migration_url):
    _, _, after = upgrade_seeded(migration_url, fault="identical_duplicate")
    assert after["device_cycle_ledgers"][0]["confirmed_used_usd"] == Decimal("135")
    assert after["device_cycle_ledgers"][0]["quality"] == "VERIFIED"


def test_pending_segment_does_not_invent_zero_consumption(migration_url):
    _, before, after = upgrade_seeded(migration_url, fault="unmeasured")
    segment = after["device_usage_segments"][1]
    assert segment["baseline_fable_usd"] == Decimal("0")
    assert segment["latest_fable_usd"] is None and segment["confirmed_used_usd"] is None
    assert not segment["billing_valid"] and segment["quality"] == "UNKNOWN"
    assert segment["billing_invalid_at"] is None
    assert after["device_cycle_ledgers"][0]["confirmed_used_usd"] == Decimal("85")
    assert after["device_cycle_ledgers"][0]["quality"] == "NEEDS_REVIEW"
    pending = DeviceUsageSegment(**segment)
    queued = DeviceUsageSnapshot(**before["device_usage_snapshots"][2])
    assert ensure_utc(queued.sampled_at) < ensure_utc(after["audit_logs"][-1]["created_at"])
    service = DeviceLedgerService(async_sessionmaker(), 178)
    assert service._apply_sample(pending, queued, zero_origin=True)
    assert pending.confirmed_used_usd == Decimal("50") and pending.billing_valid
    assert pending.billing_invalid_at is None


def test_queued_complete_sample_can_recover_bad_evidence_before_migration_time(migration_url):
    _, before, after = upgrade_seeded(migration_url, fault="missing_latest_models")
    segment = DeviceUsageSegment(**after["device_usage_segments"][0])
    queued = DeviceUsageSnapshot(**before["device_usage_snapshots"][-1])
    assert segment.baseline_fable_usd == Decimal("80") and segment.latest_fable_usd is None
    assert not segment.billing_valid and segment.confirmed_used_usd == Decimal("55")
    assert segment.billing_invalid_at == segment.latest_sampled_at
    assert ensure_utc(segment.billing_invalid_at) < ensure_utc(queued.sampled_at) < ensure_utc(after["audit_logs"][-1]["created_at"])
    service = DeviceLedgerService(async_sessionmaker(), 178)
    assert service._apply_sample(segment, queued, zero_origin=False)
    assert segment.confirmed_used_usd == Decimal("120") and segment.billing_valid
    assert segment.latest_fable_usd == Decimal("125") and segment.billing_invalid_at is None


def test_empty_migration_can_be_downgraded(migration_url):
    config = config_for(migration_url)
    command.upgrade(config, REVISION)
    command.downgrade(config, PREVIOUS)
    assert not {"baseline_fable_usd", "latest_fable_usd", "billing_valid", "billing_invalid_at"} & asyncio.run(saved_accounting(migration_url))["segment_columns"]


def test_downgrade_refuses_to_drop_corrected_accounting(migration_url):
    config, _, after = upgrade_seeded(migration_url)
    with pytest.raises(RuntimeError, match="subscription usage is recorded"):
        command.downgrade(config, PREVIOUS)
    assert asyncio.run(saved_accounting(migration_url)) == after
