from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.application.task import ALL, ALLOWLIST, EXCLUDE, QuotaTaskService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, QuotaTaskMember, ServiceState
from reclaude_bot.jobs.scheduler import BackgroundJobs


@pytest.mark.asyncio
async def test_task_create_scope_defaults_all_and_is_idempotent(app_context):
    factory, gateway, settings = app_context
    task = QuotaTaskService(factory, gateway)

    created = await task.create_task("vip", None, 1)
    assert created.name == "vip"
    assert created.enabled is False
    assert created.scope_mode == ALL
    assert created.limit_usd == Decimal("700.00")
    assert created.member_ids == ()

    with pytest.raises(EligibilityError, match="已存在"):
        await task.create_task("VIP", None, 1)
    with pytest.raises(EligibilityError, match="保留字"):
        await task.create_task("all", None, 1)
    with pytest.raises(EligibilityError, match="非空白字符"):
        await task.create_task("has space", None, 1)
    with pytest.raises(EligibilityError, match="不能重复"):
        await task.add_members("vip", ["u-1", "u-1"], 1)
    with pytest.raises(EligibilityError, match="未知"):
        await task.add_members("vip", ["missing"], 1)

    assert await task.add_members("vip", ["u-1"], 1) == ("u-1",)
    assert await task.add_members("vip", ["u-1"], 1) == ("u-1",)
    scoped = await task.snapshot("vip")
    assert scoped.scope_mode == ALLOWLIST
    assert scoped.member_ids == ("u-1",)

    await task.delete_members("vip", ["u-1"], 1)
    empty = await task.snapshot("vip")
    assert empty.scope_mode == ALLOWLIST
    assert empty.member_ids == ()

    await task.add_members("vip", ["all"], 1)
    reset = await task.snapshot("vip")
    assert reset.scope_mode == ALL
    async with factory() as session:
        assert (await session.scalars(select(QuotaTaskMember))).all() == []
        actions = list((await session.scalars(select(AuditLog))).all())
        assert {row.action for row in actions} >= {"QUOTA_TASK_CREATED", "QUOTA_TASK_SCOPE_ADD", "QUOTA_TASK_SCOPE_DELETE", "QUOTA_TASK_SCOPE_RESET"}

    # Deleting from an ALL task excludes the member instead of erroring.
    assert await task.delete_members("vip", ["u-1"], 1) == ("u-1",)
    excluded = await task.snapshot("vip")
    assert excluded.scope_mode == EXCLUDE
    assert excluded.member_ids == ("u-1",)
    assert await task.delete_members("vip", ["u-1"], 1) == ("u-1",)
    assert (await task.snapshot("vip")).member_ids == ("u-1",)

    # Adding re-includes the excluded member; an empty exclusion list still covers everyone.
    assert await task.add_members("vip", ["u-1"], 1) == ("u-1",)
    reinstated = await task.snapshot("vip")
    assert reinstated.scope_mode == EXCLUDE
    assert reinstated.member_ids == ()

    with pytest.raises(EligibilityError, match="未知上游成员"):
        await task.delete_members("vip", ["ghost"], 1)


@pytest.mark.asyncio
async def test_task_name_resolution_requires_disambiguation(app_context):
    factory, gateway, settings = app_context
    task = QuotaTaskService(factory, gateway)

    with pytest.raises(EligibilityError, match="暂无限额任务"):
        await task.resolve(None)

    await task.create_task("vip", None, 1)
    assert await task.resolve(None) == "vip"
    assert await task.resolve("VIP") == "vip"
    with pytest.raises(EligibilityError, match="任务不存在"):
        await task.resolve("ghost")

    await task.create_task("base", None, 1)
    with pytest.raises(EligibilityError, match="多个任务"):
        await task.resolve(None)

    assert await task.resolve_members_args(["vip", "u-1"], usage="usage") == ("vip", ["u-1"])
    with pytest.raises(EligibilityError, match="多个任务"):
        await task.resolve_members_args(["u-1"], usage="usage")


@pytest.mark.asyncio
async def test_start_stop_persist_and_do_not_affect_group_worker(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    await recovery.validate_selected_account()
    await jobs.start_quota_task("default", 1)
    await asyncio.sleep(0.05)
    assert await task.any_enabled() is True
    assert await gate.is_enabled() is True
    assert jobs.last_tick_started is not None

    await jobs.stop_quota_task("default", 1)
    assert await task.any_enabled() is False
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.write_enabled is False
    assert (await task.snapshot("default")).enabled is False
    # The shared loop survives the stop: usage sync continues while writes stay closed.
    assert jobs.status()["loop_running"] is True
    await jobs.stop()
    assert jobs.status()["loop_running"] is False

    # A new BackgroundJobs instance still syncs members on tick but never writes.
    replacement = BackgroundJobs(quota, actions, task_service=task)
    members_calls = gateway.members_calls
    assert await replacement.run_tick(now=datetime(2026, 8, 18, tzinfo=UTC)) == 1
    assert gateway.members_calls == members_calls + 1
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []


@pytest.mark.asyncio
async def test_latch_stays_open_while_other_tasks_run(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    await task.create_task("vip", Decimal("50"), 1)

    await task.start("default", 1)
    await task.start("vip", 1)
    assert await gate.is_enabled() is True

    await task.stop("default", 1)
    assert await task.any_enabled() is True
    assert await gate.is_enabled() is True

    await task.stop("vip", 1)
    assert await task.any_enabled() is False
    assert await gate.is_enabled() is False

    # A stopped task starts again without recreating it.
    assert await task.start("vip", 1) is True
    assert (await task.snapshot("vip")).enabled is True


@pytest.mark.asyncio
async def test_stopped_task_still_syncs_members_without_writes(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    before_members = gateway.members_calls
    assert await jobs.run_tick(now=datetime(2026, 8, 18, tzinfo=UTC)) == 1
    assert gateway.members_calls == before_members + 1
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    assert await task.any_enabled() is False
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_running_task_is_force_stopped_when_start_validation_fails(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    await task.start("default", 1)
    assert await task.any_enabled() is True
    assert await gate.is_enabled() is True

    async def fail_accounts():
        raise EligibilityError("账号校验失败")

    gateway.accounts = fail_accounts
    with pytest.raises(EligibilityError, match="账号校验失败"):
        await recovery.validate_selected_account()

    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.write_enabled is False
        assert state.selected_account_id == "4949"
        assert state.reason == "quota_task_start_validation_failed"
    assert await task.any_enabled() is False
    assert (await task.snapshot("default")).enabled is False
    assert gateway.account_id is None


@pytest.mark.asyncio
async def test_delete_task_removes_scope_and_closes_latch(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("vip", None, 1)
    await task.add_members("vip", ["u-1"], 1)
    await task.start("vip", 1)
    assert await gate.is_enabled() is True

    await task.delete_task("vip", 1)

    assert await task.any_enabled() is False
    assert await gate.is_enabled() is False
    assert await task.list_tasks() == []
    async with factory() as session:
        assert (await session.scalars(select(QuotaTaskMember))).all() == []
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.action == "QUOTA_TASK_DELETED"))).all())
        assert len(actions) == 1


@pytest.mark.asyncio
async def test_usage_sync_switch_controls_loop_and_ticks(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    # The switch defaults to on: ticks sync members and the loop starts.
    assert await task.sync_enabled() is True
    assert await jobs.run_tick(now=datetime(2026, 8, 18, tzinfo=UTC)) == 1
    await jobs.start_quota_task("default", 1)
    await asyncio.sleep(0.05)
    assert jobs.status()["loop_running"] is True

    # Stopping stats cancels the loop and blocks ticks; the task and latch stay untouched.
    assert await jobs.stop_usage_sync(1) is True
    assert jobs.status()["loop_running"] is False
    assert await task.sync_enabled() is False
    assert await task.any_enabled() is True
    assert await gate.is_enabled() is True
    members_calls = gateway.members_calls
    assert await jobs.run_tick(now=datetime(2026, 8, 18, tzinfo=UTC)) == 0
    assert gateway.members_calls == members_calls

    # The stopped switch persists across instances, so a restart does not resume the loop.
    replacement = BackgroundJobs(quota, actions, task_service=task)
    assert await replacement.resume_quota_task() is False
    assert replacement.status()["loop_running"] is False

    # Starting stats again resumes ticks and the loop without reviving writes by itself.
    assert await replacement.start_usage_sync(1) is True
    assert replacement.status()["loop_running"] is True
    assert await task.sync_enabled() is True
    await replacement.stop()
    assert replacement.status()["loop_running"] is False
    members_calls = gateway.members_calls
    assert await replacement.run_tick(now=datetime(2026, 8, 18, tzinfo=UTC)) == 1
    assert gateway.members_calls == members_calls + 1
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    async with factory() as session:
        logged = {row.action for row in (await session.scalars(select(AuditLog))).all()}
        assert {"USAGE_SYNC_STOPPED", "USAGE_SYNC_STARTED"} <= logged


@pytest.mark.asyncio
async def test_starttask_while_sync_stopped_marks_running_without_loop(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.select_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    jobs = BackgroundJobs(quota, actions, task_service=task)

    # With stats stopped the task still flips to RUNNING and the latch opens, but no loop runs.
    assert await jobs.stop_usage_sync(1) is True
    assert await jobs.start_quota_task("default", 1) is True
    assert await task.any_enabled() is True
    assert await gate.is_enabled() is True
    assert jobs.status()["loop_running"] is False

    # Starting stats afterwards brings the loop up for the already-RUNNING task.
    await jobs.start_usage_sync(1)
    assert jobs.status()["loop_running"] is True
    await jobs.stop()
