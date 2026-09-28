import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from reclaude_bot.application.device_task_members import DeviceTaskMemberService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceTaskMember, DeviceTaskScope, QuotaTask, QuotaTaskMember, User

NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def test_all_scope_transitions_preserve_empty_allowlist(lifecycle_db):
    factory, _ = lifecycle_db
    service = DeviceTaskMemberService(factory, 178)

    async def expect(mode, members, covered):
        snapshot = await service.snapshot()
        assert (snapshot.task_id, snapshot.name, snapshot.org_id) == (1, "task-1", 178)
        assert (snapshot.scope_mode, snapshot.member_ids, snapshot.covered_user_ids) == (mode, members, covered)
        with pytest.raises(FrozenInstanceError):
            snapshot.scope_mode = "ALL"

    await expect("ALL", (), (1, 2))
    assert await service.add_members(None, [2, 1, 2], 99) == (1, 2)
    await expect("ALLOWLIST", (1, 2), (1, 2))
    await service.add_members(None, [1], 99)
    await service.delete_members(None, [1, 2], 99)
    await expect("ALLOWLIST", (), ())
    await service.add_members(None, [2], 99)
    await expect("ALLOWLIST", (2,), (2,))
    await service.reset_all(None, 99)
    await expect("ALL", (), (1, 2))
    await service.delete_members(None, [2], 99)
    await expect("EXCLUDE", (2,), (1,))
    await service.delete_members(None, [1, 2], 99)
    await expect("EXCLUDE", (1, 2), ())
    await service.add_members(None, [2, 1], 99)
    await expect("EXCLUDE", (), (1, 2))
    await service.reset_all(None, 99)
    await expect("ALL", (), (1, 2))
    async with factory() as session:
        logs = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
        assert len(logs) == 9
        assert all(row.actor_telegram_id == 99 and row.target_id == "1" for row in logs)
        assert all(row.parameters_summary["org_id"] == 178 for row in logs)


@pytest.mark.parametrize("method", ["add_members", "delete_members"])
@pytest.mark.parametrize("ids", [[], [True], [0], [-1], ["legacy-1"], [1, 99999]])
async def test_invalid_or_unknown_users_reject_whole_operation(lifecycle_db, method, ids):
    factory, _ = lifecycle_db
    service = DeviceTaskMemberService(factory, 178)
    before = await service.snapshot()
    with pytest.raises(EligibilityError):
        await getattr(service, method)(None, ids, 99)
    assert await service.snapshot() == before
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_local_scope_is_separate_from_legacy_identity_and_permissions(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        task = await session.get(QuotaTask, 1)
        task.scope_mode = "EXCLUDE"
        session.add(QuotaTaskMember(task_id=1, reclaude_user_id="1", added_by=99, added_at=NOW))
        user = await session.get(User, 1)
        user.reclaude_user_id = "different-rec-identity"
        user.binding_status = "UNBOUND"
        user.status = "BANNED"
    service = DeviceTaskMemberService(factory, 178)
    seen = []

    def capture(_, __, statement, ___, ____, _____):
        seen.append(statement.lower())

    engine = factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await service.add_members(None, [1], 99)
        snapshot = await service.snapshot()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    # Scope membership never grants or changes auth eligibility.
    assert snapshot.covered_user_ids == (1,)
    assert not any("upstream_members" in sql or "quota_task_members" in sql for sql in seen)
    async with factory() as session:
        task = await session.get(QuotaTask, 1)
        assert (task.scope_mode, task.status, task.limit_usd) == ("EXCLUDE", "STOPPED", 700)
        assert await session.scalar(select(QuotaTaskMember.reclaude_user_id)) == "1"
        user = await session.get(User, 1)
        assert (user.binding_status, user.status, user.reclaude_user_id) == ("UNBOUND", "BANNED", "different-rec-identity")


async def test_org_isolation_and_scope_rechecked_after_resolve(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db
    service = DeviceTaskMemberService(factory, 178)
    with pytest.raises(EligibilityError):
        await service.add_members("task-2", [1], 99)
    await service.add_members(None, [1], 99)
    other = await DeviceTaskMemberService(factory, 179).snapshot()
    assert (other.scope_mode, other.member_ids) == ("ALL", ())
    original = service.task_service.resolve_task

    async def move_scope(name):
        context = await original(name)
        async with factory.begin() as session:
            (await session.get(DeviceTaskScope, 1)).org_id = 180
        return context

    monkeypatch.setattr(service.task_service, "resolve_task", move_scope)
    with pytest.raises(EligibilityError):
        await service.delete_members(None, [1], 99)
    async with factory() as session:
        assert await session.scalar(select(DeviceTaskMember.user_id).where(DeviceTaskMember.task_id == 1)) == 1


async def test_audit_failure_rolls_back_scope_and_members(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("reclaude_bot.application.device_task_members.audit", fail)
    service = DeviceTaskMemberService(factory, 178)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.add_members(None, [1], 99)
    snapshot = await service.snapshot()
    assert (snapshot.scope_mode, snapshot.member_ids) == ("ALL", ())


async def test_storage_rejects_orphans_duplicate_members_and_invalid_scope(lifecycle_db):
    factory, _ = lifecycle_db
    await DeviceTaskMemberService(factory, 178).add_members(None, [1], 99)
    for task_id, user_id in [(1, 1), (999, 1), (1, 999)]:
        with pytest.raises(IntegrityError):
            async with factory.begin() as session:
                session.add(DeviceTaskMember(task_id=task_id, user_id=user_id, added_at=NOW))
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            (await session.get(DeviceTaskScope, 1)).scope_mode = "INVALID"
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            await session.delete(await session.get(User, 1))


async def test_postgresql_concurrent_mutations_preserve_all_members(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies concurrent scope mutations")
    services = [DeviceTaskMemberService(factory, 178) for _ in range(2)]
    await asyncio.wait_for(asyncio.gather(*(service.add_members(None, [i + 1], 99) for i, service in enumerate(services))), 10)
    assert (await services[0].snapshot()).member_ids == (1, 2)
    await asyncio.wait_for(asyncio.gather(*(service.delete_members(None, [i + 1], 99) for i, service in enumerate(services))), 10)
    snapshot = await services[0].snapshot()
    assert (snapshot.scope_mode, snapshot.member_ids) == ("ALLOWLIST", ())


async def test_postgresql_snapshot_waits_for_scope_transaction(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies coherent scope snapshots under row locking")
    service = DeviceTaskMemberService(factory, 178)
    engine = factory.kw["bind"]
    reading_scope = asyncio.Event()

    def capture(_, __, statement, ___, ____, _____):
        if "FOR UPDATE" in statement and "device_task_scopes" in statement:
            reading_scope.set()

    async with factory.begin() as holder:
        scope = await holder.get(DeviceTaskScope, 1, with_for_update=True)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        pending = asyncio.create_task(service.snapshot())
        try:
            await asyncio.wait_for(reading_scope.wait(), 5)
            assert not pending.done()
            scope.scope_mode = "ALLOWLIST"
            holder.add(DeviceTaskMember(task_id=1, user_id=1, added_at=NOW))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
    result = await asyncio.wait_for(pending, 5)
    assert (result.scope_mode, result.member_ids, result.covered_user_ids) == ("ALLOWLIST", (1,), (1,))
