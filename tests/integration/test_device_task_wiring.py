from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_task_members import DeviceTaskMemberService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceTaskMember, DeviceTaskScope, QuotaTask, QuotaTaskMember
from tests.fixtures.device_runtime import ready_cycle

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def service(factory, org_id=178):
    gateway = SimpleNamespace(members=AsyncMock(side_effect=AssertionError("legacy member API must not run")))
    return QuotaTaskService(factory, gateway, org_id=org_id), gateway


async def test_new_task_creates_explicit_stopped_org_scope(lifecycle_db):
    factory, _ = lifecycle_db
    task, gateway = service(factory, org_id=180)
    created = await task.create_task("device-new", Decimal("800"), 999)
    async with factory() as session:
        scope = await session.get(DeviceTaskScope, created.id)
        row = await session.get(QuotaTask, created.id)
        assert scope.org_id == 180 and scope.scope_mode == "ALL"
        assert row.status == "STOPPED" and row.limit_usd == Decimal("800")
    gateway.members.assert_not_called()


async def test_single_org_cannot_be_silently_reassigned_by_newtask(lifecycle_db):
    factory, _ = lifecycle_db
    task, _ = service(factory)
    with pytest.raises(EligibilityError):
        await task.create_task("another", Decimal("700"), 999)
    async with factory() as session:
        assert await session.scalar(select(QuotaTask).where(QuotaTask.name == "another")) is None
        assert (await session.get(DeviceTaskScope, 1)).org_id == 178


async def test_member_commands_modify_local_user_scope_without_rec_member_api(lifecycle_db):
    factory, _ = lifecycle_db
    task, gateway = service(factory)
    members = DeviceTaskMemberService(factory, 178)
    await members.add_members("task-1", [1, 2], 999)
    state = await task.snapshot("task-1")
    assert state.scope_mode == "ALLOWLIST" and {str(value) for value in state.member_ids} == {"1", "2"}
    await members.delete_members("task-1", [2], 999)
    async with factory() as session:
        assert (await session.scalars(select(DeviceTaskMember.user_id).where(DeviceTaskMember.task_id == 1))).all() == [1]
        assert (await session.scalars(select(QuotaTaskMember))).all() == []
    await members.reset_all("task-1", 999)
    assert (await task.snapshot("task-1")).scope_mode == "ALL"
    gateway.members.assert_not_called()


async def test_task_with_device_cycle_cannot_delete_accounting_history(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory, running=False)
    task, _ = service(factory)
    with pytest.raises(EligibilityError):
        await task.delete_task("task-1", 999)
    async with factory() as session:
        assert await session.get(QuotaTask, 1) is not None
        assert await session.get(DeviceTaskScope, 1) is not None


async def test_empty_stopped_task_can_be_deleted_without_affecting_other_org(lifecycle_db):
    factory, _ = lifecycle_db
    task, _ = service(factory)
    await task.delete_task("task-1", 999)
    async with factory() as session:
        assert await session.get(QuotaTask, 1) is None
        assert await session.get(DeviceTaskScope, 1) is None
        assert await session.get(DeviceTaskScope, 2) is not None


@pytest.mark.xfail(strict=True, reason="用户确认当前仅一个任务，暂不修跨组织默认任务解析")
async def test_omitted_task_name_uses_only_configured_org_tasks(lifecycle_db):
    factory, _ = lifecycle_db
    task, _ = service(factory)
    assert await task.resolve(None) == "task-1"
