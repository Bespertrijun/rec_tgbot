import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from reclaude_bot.application.device_usage import DeviceUsageCollector
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import DeviceAction, DeviceAssociation, DeviceTaskScope, DeviceUsageSnapshot, User
from reclaude_bot.infrastructure.reclaude.models import DeviceUsage, DeviceUsageOverview

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def usage(total="12.3456789012", *, range="all"):
    return DeviceUsage(range=range, overview=DeviceUsageOverview(total_usd=total, messages=42,
                       heatmap=[{"date": "2026-09-28", "total_usd": total}]), models={"breakdown": []})


async def associated(factory, *, user_id=1, device_id=44500, org_id=178, task_id=1, state="ACTIVE", auth_status="SUCCEEDED"):
    async with factory.begin() as session:
        association = DeviceAssociation(user_id=user_id, task_id=task_id, org_id=org_id, device_id=device_id,
                                        state=state, started_at=NOW - timedelta(hours=1), updated_at=NOW,
                                        ended_at=NOW if state == "ENDED" else None,
                                        end_reason="USER_DEAUTH" if state == "ENDED" else None)
        session.add(association)
        await session.flush()
        if auth_status is not None:
            session.add(DeviceAction(association_id=association.id, kind="AUTH", status=auth_status,
                                     idempotency_key=f"auth-{association.id}", target_device_id=device_id,
                                     result_reused=False if auth_status == "SUCCEEDED" else None, attempt_count=1,
                                     created_at=NOW - timedelta(hours=1), updated_at=NOW,
                                     completed_at=NOW if auth_status == "SUCCEEDED" else None))
        return association.id


def collector_fixture(factory, *, data=None, org_id=178):
    gateway = SimpleNamespace(account_id=7022, device_usage=AsyncMock(return_value=data if data is not None else usage()),
                              approve_device_auth=AsyncMock(), revoke_device=AsyncMock())
    clock = [NOW]
    service = DeviceUsageCollector(factory, gateway, org_id, clock=lambda: clock[0])
    return service, gateway, clock


async def snapshots(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceUsageSnapshot).order_by(DeviceUsageSnapshot.id))).all())


async def test_collects_exact_all_snapshot_and_reuses_idempotency_key_without_get(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, clock = collector_fixture(factory)
    first = await service.collect(association_id, sample_key="sample-once")
    assert first.status == "SUCCEEDED" and first.error_code is None
    assert (first.association_id, first.device_id, first.sample_key) == (association_id, 44500, "sample-once")
    clock[0] += timedelta(minutes=1)
    assert await service.collect(association_id, sample_key="sample-once") == first
    gateway.device_usage.assert_awaited_once_with(44500, 178, range="all")
    rows = await snapshots(factory)
    assert len(rows) == 1 and rows[0].id == first.snapshot_id
    assert rows[0].total_usd == Decimal("12.3456789012") and rows[0].range == "all"
    assert ensure_utc(rows[0].sampled_at) == NOW
    assert rows[0].payload == usage().model_dump(mode="json")
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("state", ["ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"])
async def test_confirmed_device_can_be_sampled_after_revocation_or_while_unresolved(lifecycle_db, state):
    factory, _ = lifecycle_db
    association_id = await associated(factory, state=state)
    service, gateway, _ = collector_fixture(factory)
    result = await service.collect(association_id)
    assert result.status == "SUCCEEDED" and 0 < len(result.sample_key) <= 64
    async with factory() as session:
        row = await session.get(DeviceAssociation, association_id)
        assert row.state == state
        assert (row.ended_at is not None) == (state == "ENDED")
    gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "invalid_id", "unknown_auth", "no_auth", "other_org", "invalid_key"])
async def test_unconfirmed_ownership_and_invalid_inputs_never_request_usage(lifecycle_db, case):
    factory, _ = lifecycle_db
    kwargs = {}
    if case == "missing":
        association_id = 999
    elif case == "invalid_id":
        association_id = True
    elif case == "unknown_auth":
        association_id = await associated(factory, state="UNKNOWN", auth_status="UNKNOWN")
    elif case == "no_auth":
        association_id = await associated(factory, auth_status=None)
    elif case == "other_org":
        association_id = await associated(factory, org_id=179, task_id=2, device_id=44502)
    else:
        association_id = await associated(factory)
        kwargs["sample_key"] = "x" * 65
    service, gateway, _ = collector_fixture(factory)
    with pytest.raises(EligibilityError):
        await service.collect(association_id, **kwargs)
    gateway.device_usage.assert_not_called()
    assert await snapshots(factory) == []


@pytest.mark.parametrize("case", ["timeout", "bad_response", "wrong_range", "nan", "negative", "overflow", "precision", "account_changed", "circuit"])
async def test_failed_collection_never_inserts_zero_or_changes_authorization(lifecycle_db, case):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, _ = collector_fixture(factory)
    if case == "timeout":
        gateway.device_usage.side_effect = httpx.ReadTimeout("private session")
    elif case == "bad_response":
        gateway.device_usage.return_value = {"overview": {"total_usd": "0"}}
    elif case == "wrong_range":
        gateway.device_usage.return_value = usage(range="7d")
    elif case in {"nan", "negative", "overflow", "precision"}:
        data = usage()
        data.overview.total_usd = Decimal({"nan": "NaN", "negative": "-1", "overflow": "100000000", "precision": "0.12345678901"}[case])
        gateway.device_usage.return_value = data
    elif case == "account_changed":
        async def switched(*args, **kwargs):
            gateway.account_id = 9000
            return usage()
        gateway.device_usage.side_effect = switched
    else:
        gateway.device_usage.side_effect = AuthenticationCircuitOpen("private session")
    if case == "circuit":
        with pytest.raises(AuthenticationCircuitOpen):
            await service.collect(association_id)
    else:
        result = await service.collect(association_id)
        assert result.status == "FAILED" and result.snapshot_id is None and result.error_code
        assert "private" not in repr(result)
    assert await snapshots(factory) == []
    async with factory() as session:
        assert (await session.get(DeviceAssociation, association_id)).state == "ACTIVE"
        assert (await session.get(User, 1)).binding_status == "BOUND"
    gateway.revoke_device.assert_not_called()


async def test_raw_regression_and_zero_are_stored_as_facts_without_overwriting_history(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, clock = collector_fixture(factory, data=usage("50"))
    first = await service.collect(association_id)
    clock[0] += timedelta(seconds=1)
    gateway.device_usage.return_value = usage("0")
    second = await service.collect(association_id)
    assert first.snapshot_id != second.snapshot_id and second.status == "SUCCEEDED"
    assert [row.total_usd for row in await snapshots(factory)] == [Decimal("50"), Decimal("0")]


async def test_same_sample_key_cannot_be_reassigned_to_another_device(lifecycle_db):
    factory, _ = lifecycle_db
    first_id = await associated(factory)
    second_id = await associated(factory, user_id=2, device_id=44501)
    service, gateway, _ = collector_fixture(factory)
    await service.collect(first_id, sample_key="reserved-sample")
    with pytest.raises(EligibilityError):
        await service.collect(second_id, sample_key="reserved-sample")
    gateway.device_usage.assert_awaited_once()
    assert len(await snapshots(factory)) == 1


async def test_network_does_not_hold_database_locks_and_deauth_during_get_preserves_sample(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, _ = collector_fixture(factory)

    async def stop_while_sampling(*args, **kwargs):
        async with factory.begin() as session:
            await session.scalar(select(DeviceTaskScope).where(DeviceTaskScope.task_id == 1).with_for_update(nowait=True))
            row = await session.get(DeviceAssociation, association_id, with_for_update={"nowait": True})
            row.state = "ENDED"
            row.ended_at = NOW
            row.end_reason = "USER_DEAUTH"
        return usage()

    gateway.device_usage.side_effect = stop_while_sampling
    assert (await service.collect(association_id)).status == "SUCCEEDED"
    assert len(await snapshots(factory)) == 1
    async with factory() as session:
        assert (await session.get(DeviceAssociation, association_id)).state == "ENDED"


@pytest.mark.parametrize("changed", ["device", "owner"])
async def test_changed_target_during_get_is_not_reported_as_success(lifecycle_db, changed):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, _ = collector_fixture(factory)

    async def change_target(*args, **kwargs):
        async with factory.begin() as session:
            row = await session.get(DeviceAssociation, association_id)
            if changed == "device":
                row.device_id = 44501
            else:
                row.user_id = 2
        return usage()

    gateway.device_usage.side_effect = change_target
    try:
        result = await service.collect(association_id)
    except EligibilityError:
        pass
    else:
        assert result.status == "FAILED" and result.snapshot_id is None
    assert await snapshots(factory) == []


async def test_concurrent_same_key_creates_one_snapshot(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, _ = collector_fixture(factory)
    entered = 0
    both = asyncio.Event()

    async def fetch(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return usage()

    gateway.device_usage.side_effect = fetch
    results = await asyncio.wait_for(asyncio.gather(*(service.collect(association_id, sample_key="shared-read") for _ in range(2))), 20)
    assert all(result.status == "SUCCEEDED" for result in results)
    assert results[0].snapshot_id == results[1].snapshot_id
    assert len(await snapshots(factory)) == 1


async def test_database_commit_failure_is_not_reported_as_a_saved_sample(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await associated(factory)
    service, gateway, _ = collector_fixture(factory)
    fail_next = [False]

    async def received(*args, **kwargs):
        fail_next[0] = True
        return usage()

    def reject_commit(session):
        if fail_next[0]:
            fail_next[0] = False
            raise RuntimeError("sample commit failed")

    gateway.device_usage.side_effect = received
    event.listen(Session, "before_commit", reject_commit)
    try:
        result = await service.collect(association_id, sample_key="retry-after-save-failure")
    finally:
        event.remove(Session, "before_commit", reject_commit)
    assert result.status == "FAILED" and result.snapshot_id is None
    assert await snapshots(factory) == []
    gateway.device_usage.side_effect = None
    assert (await service.collect(association_id, sample_key="retry-after-save-failure")).status == "SUCCEEDED"
    assert len(await snapshots(factory)) == 1
