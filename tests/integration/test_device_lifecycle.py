"""Device lifecycle storage contracts on SQLite and isolated PostgreSQL schemas."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from reclaude_bot.infrastructure.db.models import Device, DeviceAction, DeviceAssociation, DeviceTaskScope, QuotaTask, User

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def association(**overrides):
    values = dict(user_id=1, task_id=1, org_id=178, device_id=44500, state="ACTIVE", started_at=NOW, updated_at=NOW)
    values.update(overrides)
    return DeviceAssociation(**values)


def action(association_id, **overrides):
    values = dict(association_id=association_id, kind="AUTH", status="PENDING", idempotency_key=uuid4().hex,
                  attempt_count=0, created_at=NOW, updated_at=NOW)
    values.update(overrides)
    return DeviceAction(**values)


async def save(factory, row):
    async with factory.begin() as session:
        session.add(row)
    return row


@pytest.mark.parametrize("state", ["PENDING_AUTH", "ACTIVE", "PENDING_REVOKE", "UNKNOWN"])
async def test_user_slot_is_global_and_covers_unconfirmed_operations(lifecycle_db, state):
    factory, _ = lifecycle_db
    device_id = None if state in {"PENDING_AUTH", "UNKNOWN"} else 44500
    await save(factory, association(state=state, device_id=device_id))
    with pytest.raises(IntegrityError):
        await save(factory, association(task_id=2, org_id=179, device_id=44502))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceAssociation)) == 1


async def test_device_cannot_belong_to_two_users(lifecycle_db):
    factory, _ = lifecycle_db
    await save(factory, association())
    with pytest.raises(IntegrityError):
        await save(factory, association(user_id=2))


async def test_confirmed_end_releases_slot_and_preserves_history(lifecycle_db):
    factory, _ = lifecycle_db
    old = await save(factory, association())
    async with factory.begin() as session:
        row = await session.get(DeviceAssociation, old.id)
        row.state = "ENDED"
        row.ended_at = NOW + timedelta(minutes=1)
        row.end_reason = "MANUAL"
    new = await save(factory, association(started_at=NOW + timedelta(minutes=2)))
    assert new.id != old.id
    async with factory() as session:
        rows = (await session.scalars(select(DeviceAssociation).order_by(DeviceAssociation.id))).all()
        assert [(r.state, r.device_id) for r in rows] == [("ENDED", 44500), ("ACTIVE", 44500)]


@pytest.mark.parametrize("changes", [
    {"task_id": 2}, {"org_id": 179}, {"device_id": 44502}, {"user_id": 999},
    {"task_id": 999}, {"device_id": 999}, {"state": "INVALID"},
    {"state": "ACTIVE", "device_id": None}, {"state": "PENDING_REVOKE", "device_id": None},
    {"state": "ENDED"}, {"ended_at": NOW},
    {"state": "ENDED", "ended_at": NOW - timedelta(seconds=1)},
])
async def test_invalid_association_is_rejected_by_database(lifecycle_db, changes):
    factory, _ = lifecycle_db
    with pytest.raises(IntegrityError):
        await save(factory, association(**changes))


async def test_distinct_users_can_reserve_slots_without_device_ids(lifecycle_db):
    factory, _ = lifecycle_db
    await save(factory, association(state="PENDING_AUTH", device_id=None))
    await save(factory, association(user_id=2, state="PENDING_AUTH", device_id=None))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceAssociation)) == 2


async def test_device_bigint_and_unconfigured_org_can_be_cached(lifecycle_db):
    factory, _ = lifecycle_db
    await save(factory, Device(org_id=999, device_id=2**40, name="unassigned", first_synced_at=NOW, last_synced_at=NOW))
    async with factory() as session:
        assert await session.scalar(select(Device.device_id).where(Device.org_id == 999)) == 2**40


@pytest.mark.parametrize("target", ["user", "task", "device", "scope", "association"])
async def test_history_is_not_cascade_deleted(lifecycle_db, target):
    factory, _ = lifecycle_db
    linked = await save(factory, association())
    await save(factory, action(linked.id))
    statements = {
        "user": delete(User).where(User.id == 1),
        "task": delete(QuotaTask).where(QuotaTask.id == 1),
        "device": delete(Device).where(Device.device_id == 44500),
        "scope": delete(DeviceTaskScope).where(DeviceTaskScope.task_id == 1),
        "association": delete(DeviceAssociation).where(DeviceAssociation.id == linked.id),
    }
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            await session.execute(statements[target])
    async with factory() as session:
        assert await session.get(DeviceAssociation, linked.id) is not None
        assert await session.scalar(select(func.count()).select_from(DeviceAction)) == 1


@pytest.mark.parametrize("changes", [
    {"kind": "REVOKE"}, {"kind": "DELETE"}, {"status": "INVALID"}, {"attempt_count": -1},
    {"status": "SUCCEEDED"}, {"status": "FAILED"}, {"completed_at": NOW},
    {"kind": "REVOKE", "target_device_id": 0},
])
async def test_invalid_action_is_rejected_by_database(lifecycle_db, changes):
    factory, _ = lifecycle_db
    linked = await save(factory, association())
    with pytest.raises(IntegrityError):
        await save(factory, action(linked.id, **changes))


async def test_unknown_action_prevents_duplicate_work_and_keeps_retry_state(lifecycle_db):
    factory, _ = lifecycle_db
    linked = await save(factory, association())
    pending = await save(factory, action(linked.id, status="UNKNOWN", attempt_count=1, next_retry_at=NOW + timedelta(minutes=1)))
    with pytest.raises(IntegrityError):
        await save(factory, action(linked.id))
    async with factory.begin() as session:
        loaded = await session.get(DeviceAction, pending.id)
        assert loaded.status == "UNKNOWN"
        assert loaded.attempt_count == 1
        assert loaded.next_retry_at is not None
        loaded.status = "SUCCEEDED"
        loaded.completed_at = NOW + timedelta(minutes=1)
    await save(factory, action(linked.id, kind="REVOKE", target_device_id=44500))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceAction)) == 2


async def test_action_idempotency_is_global_even_after_completion(lifecycle_db):
    factory, _ = lifecycle_db
    first = await save(factory, association())
    second = await save(factory, association(user_id=2, device_id=44501))
    await save(factory, action(first.id, idempotency_key="same-operation", status="SUCCEEDED", completed_at=NOW))
    with pytest.raises(IntegrityError):
        await save(factory, action(second.id, idempotency_key="same-operation"))


@pytest.mark.parametrize("collision", ["user", "device"])
async def test_postgresql_concurrent_transactions_have_exactly_one_winner(lifecycle_db, collision):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("transaction competition is verified on PostgreSQL")
    ready = asyncio.Event()
    arrived = 0

    async def reserve(row):
        nonlocal arrived
        async with factory() as session:
            try:
                async with session.begin():
                    # Check out independent connections before releasing both writers.
                    await session.execute(text("SELECT 1"))
                    arrived += 1
                    if arrived == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 5)
                    session.add(row)
                    await session.flush()
                return "committed"
            except IntegrityError:
                return "conflict"

    if collision == "user":
        left = association(state="PENDING_AUTH", device_id=None)
        right = association(task_id=2, org_id=179, state="PENDING_AUTH", device_id=None)
    else:
        left, right = association(), association(user_id=2)
    results = await asyncio.wait_for(asyncio.gather(reserve(left), reserve(right)), 10)
    assert sorted(results) == ["committed", "conflict"]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceAssociation)) == 1
