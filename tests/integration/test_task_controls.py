from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.application.task import ALL, ALLOWLIST, QuotaTaskService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, QuotaTaskMember, ServiceState
from reclaude_bot.jobs.scheduler import BackgroundJobs


@pytest.mark.asyncio
async def test_task_scope_defaults_all_and_is_idempotent(app_context):
    factory, gateway, settings = app_context
    task = QuotaTaskService(factory, gateway)

    initial = await task.snapshot()
    assert initial.enabled is False
    assert initial.scope_mode == ALL
    assert initial.member_ids == ()

    with pytest.raises(EligibilityError, match="不能重复"):
        await task.add_members(["u-1", "u-1"], 1)
    with pytest.raises(EligibilityError, match="未知"):
        await task.add_members(["missing"], 1)

    assert await task.add_members(["u-1"], 1) == ("u-1",)
    assert await task.add_members(["u-1"], 1) == ("u-1",)
    scoped = await task.snapshot()
    assert scoped.scope_mode == ALLOWLIST
    assert scoped.member_ids == ("u-1",)

    await task.delete_members(["u-1"], 1)
    empty = await task.snapshot()
    assert empty.scope_mode == ALLOWLIST
    assert empty.member_ids == ()

    await task.add_members(["all"], 1)
    reset = await task.snapshot()
    assert reset.scope_mode == ALL
    async with factory() as session:
        assert (await session.scalars(select(QuotaTaskMember))).all() == []
        actions = list((await session.scalars(select(AuditLog))).all())
        assert {row.action for row in actions} >= {"QUOTA_TASK_SCOPE_ADD", "QUOTA_TASK_SCOPE_DELETE", "QUOTA_TASK_SCOPE_RESET"}

    with pytest.raises(EligibilityError, match="范围为 ALL"):
        await task.delete_members(["u-1"], 1)


@pytest.mark.asyncio
async def test_start_stop_persist_and_do_not_affect_group_worker(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    await recovery.validate_selected_account()
    await jobs.start_quota_task(1)
    await asyncio.sleep(0.05)
    assert await task.is_enabled() is True
    assert await gate.is_enabled() is True
    assert jobs.last_tick_started is not None

    await jobs.stop_quota_task(1)
    assert await task.is_enabled() is False
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.quota_task_enabled is False
        assert state.write_enabled is False

    # A new BackgroundJobs instance observes the persisted stop and does not tick.
    replacement = BackgroundJobs(quota, actions, task_service=task)
    members_calls = gateway.members_calls
    assert await replacement.run_tick() == 0
    assert gateway.members_calls == members_calls


@pytest.mark.asyncio
async def test_stopped_task_never_refreshes_cycle_or_members(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    task = QuotaTaskService(factory, gateway)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    before_members = gateway.members_calls
    assert await jobs.run_tick() == 0
    assert gateway.members_calls == before_members
    async with factory() as session:
        assert (await session.scalars(select(ServiceState))).all()[0].quota_task_enabled is False


@pytest.mark.asyncio
async def test_running_task_is_force_stopped_when_start_validation_fails(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.start(1)
    assert await task.is_enabled() is True
    assert await gate.is_enabled() is True

    async def fail_accounts():
        raise EligibilityError("账号校验失败")

    gateway.accounts = fail_accounts
    with pytest.raises(EligibilityError, match="账号校验失败"):
        await recovery.validate_selected_account()

    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.quota_task_enabled is False
        assert state.write_enabled is False
        assert state.selected_account_id == "4949"
        assert state.reason == "quota_task_start_validation_failed"
    assert gateway.account_id is None
