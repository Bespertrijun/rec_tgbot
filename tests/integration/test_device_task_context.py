import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from reclaude_bot.application.device_context import SingleOrgTaskService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceTaskScope, QuotaTask, ServiceState

NOW = datetime(2026, 9, 28, tzinfo=UTC)


@pytest_asyncio.fixture
async def device_tasks(lifecycle_db):
    factory, backend = lifecycle_db
    async with factory.begin() as session:
        session.add_all([
            QuotaTask(id=i, name=f"task-{i}", name_normalized=f"task-{i}", status="STOPPED", limit_usd=Decimal("123.4567890001"), created_at=NOW, updated_at=NOW)
            for i in (3, 4)
        ])
        session.add(ServiceState(id=1, write_enabled=False, sync_enabled=False, reason="test", updated_at=NOW))
    return factory, backend


async def test_resolve_uses_configured_org_scope_not_arbitrary_existing_task(device_tasks):
    factory, _ = device_tasks
    service = SingleOrgTaskService(factory, 178)
    context = await service.resolve_task()
    assert (context.task_id, context.org_id, context.name) == (1, 178, "task-1")
    assert context.limit_usd == Decimal("700")
    assert context.enabled is False
    with pytest.raises(FrozenInstanceError):
        context.name = "changed"
    with pytest.raises(EligibilityError):
        await service.resolve_task("task-2")
    with pytest.raises(EligibilityError):
        await service.resolve_task("task-3")
    with pytest.raises(EligibilityError):
        await service.resolve_task("missing")
    with pytest.raises(EligibilityError):
        await SingleOrgTaskService(factory, 180).resolve_task()


async def test_configure_preserves_task_and_gate_and_audits_only_once(device_tasks):
    factory, _ = device_tasks
    service = SingleOrgTaskService(factory, 180)
    first = await service.configure_task(" TASK-3 ", 1)
    again = await service.configure_task("task-3", 1)
    assert first == again
    assert first.limit_usd == Decimal("123.4567890001")
    assert first.enabled is False
    assert (first.task_id, first.org_id) == (3, 180)
    async with factory() as session:
        scope = await session.get(DeviceTaskScope, 3)
        assert scope.org_id == 180
        task = await session.get(QuotaTask, 3)
        assert (task.status, task.limit_usd) == ("STOPPED", Decimal("123.4567890001"))
        gate = await session.get(ServiceState, 1)
        assert (gate.write_enabled, gate.sync_enabled, gate.reason) == (False, False, "test")
        notices = (await session.scalars(select(AuditLog).where(AuditLog.action == "DEVICE_TASK_CONFIGURED"))).all()
        assert len(notices) == 1
        assert notices[0].actor_telegram_id == 1
    assert await service.resolve_task() == first


@pytest.mark.parametrize("org_id,task_name", [(178, "task-3"), (180, "task-1"), (180, "missing")])
async def test_configure_cannot_steal_org_or_reassign_task_or_create_implicitly(device_tasks, org_id, task_name):
    factory, _ = device_tasks
    with pytest.raises(EligibilityError):
        await SingleOrgTaskService(factory, org_id).configure_task(task_name, 1)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceTaskScope)) == 2
        assert await session.scalar(select(func.count()).select_from(QuotaTask)) == 4
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_first_configure_requires_stopped_task_but_existing_scope_is_idempotent(device_tasks):
    factory, _ = device_tasks
    async with factory.begin() as session:
        (await session.get(QuotaTask, 3)).status = "RUNNING"
        (await session.get(QuotaTask, 1)).status = "RUNNING"
    with pytest.raises(EligibilityError):
        await SingleOrgTaskService(factory, 180).configure_task("task-3", 1)
    existing = await SingleOrgTaskService(factory, 178).configure_task("task-1", 1)
    assert existing.enabled is True
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_context_reads_current_task_limit_and_status(device_tasks):
    factory, _ = device_tasks
    service = SingleOrgTaskService(factory, 178)
    async with factory.begin() as session:
        task = await session.get(QuotaTask, 1)
        task.limit_usd = Decimal("900")
        task.status = "RUNNING"
    result = await service.resolve_task()
    assert (result.limit_usd, result.enabled) == (Decimal("900"), True)


async def test_postgresql_two_tasks_competing_for_one_org_return_one_domain_failure(device_tasks):
    factory, backend = device_tasks
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies competing configuration transactions")
    results = await asyncio.wait_for(asyncio.gather(
        SingleOrgTaskService(factory, 180).configure_task("task-3", 1),
        SingleOrgTaskService(factory, 180).configure_task("task-4", 1),
        return_exceptions=True,
    ), 10)
    assert sum(isinstance(item, EligibilityError) for item in results) == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceTaskScope).where(DeviceTaskScope.org_id == 180)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "DEVICE_TASK_CONFIGURED")) == 1


async def test_postgresql_same_task_concurrent_configuration_is_idempotent(device_tasks):
    factory, backend = device_tasks
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies row locking across service instances")
    results = await asyncio.wait_for(asyncio.gather(
        SingleOrgTaskService(factory, 180).configure_task("task-3", 1),
        SingleOrgTaskService(factory, 180).configure_task("task-3", 1),
    ), 10)
    assert results[0] == results[1]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceTaskScope).where(DeviceTaskScope.org_id == 180)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "DEVICE_TASK_CONFIGURED")) == 1
