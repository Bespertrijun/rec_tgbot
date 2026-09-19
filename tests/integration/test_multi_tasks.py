from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.task import ALL, QuotaTaskService
from reclaude_bot.infrastructure.db.models import AuditLog, ServiceState, UpstreamMember
from reclaude_bot.infrastructure.reclaude.models import Member


async def _prepare(app_context, member_usages: dict[str, str], *, account_id: int | None = 4949):
    """Sync upstream members and bind Telegram users 200+ to them in declaration order."""

    factory, gateway, settings = app_context
    gateway.configure_account_id(4949)
    for reclaude_user_id in member_usages:
        gateway.member_rows[reclaude_user_id] = Member(user_id=reclaude_user_id, email=f"{reclaude_user_id}@example.com", account_id=account_id, total_usage_usd=Decimal("0"))
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    binding = BindingService(factory, gateway)
    users = {}
    for index, reclaude_user_id in enumerate(member_usages):
        users[reclaude_user_id] = await binding.bind(200 + index, f"{reclaude_user_id}@example.com")
    async with factory() as session:
        async with session.begin():
            session.add(ServiceState(id=1, write_enabled=False, reason="test", selected_account_id="4949", updated_at=now))
    actions = QuotaActionService(factory, gateway, quota, settings)
    task = QuotaTaskService(factory, gateway)
    # Usage is applied after the baseline sync so cycle_used reflects it.
    for reclaude_user_id, total in member_usages.items():
        gateway.member_rows[reclaude_user_id] = Member(
            user_id=reclaude_user_id,
            email=f"{reclaude_user_id}@example.com",
            account_id=account_id,
            total_usage_usd=Decimal(total),
        )
    await quota.sync_members(now=now)
    return factory, gateway, settings, quota, actions, task, users, now


@pytest.mark.asyncio
async def test_overlapping_member_gets_strictest_running_limit(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "25", "u-2": "25"})
    await task.create_task("vip", Decimal("10"), 1)
    await task.add_members("vip", ["u-1"], 1)
    await task.create_task("base", Decimal("50"), 1)
    await task.start("base", 1)
    await task.start("vip", 1)

    executed = await actions.reconcile_cached(now=now + timedelta(minutes=1))

    # u-1 is covered by vip (10) and base (50): min 10 <= 25 usage → revoke.
    # u-2 is only covered by base (50): 25 < 50 → untouched.
    assert executed == 1
    assert gateway.revoke_calls == ["u-1"]

    # Stopping vip relaxes u-1 to base's 50: no further enforcement action.
    await task.stop("vip", 1)
    assert await actions.reconcile_cached(now=now + timedelta(minutes=2)) == 0
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_stopped_task_does_not_enforce_its_members(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "800"})
    await task.create_task("vip", Decimal("10"), 1)
    await task.add_members("vip", ["u-1"], 1)

    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 0
    assert gateway.revoke_calls == []

    await task.start("vip", 1)
    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 1
    assert gateway.revoke_calls == ["u-1"]

    await task.stop("vip", 1)
    assert await actions.reconcile_cached(now=now + timedelta(minutes=2)) == 0
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_empty_allowlist_running_task_runs_no_actions(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "800"})
    await task.create_task("vip", Decimal("1"), 1)
    await task.add_members("vip", ["u-1"], 1)
    await task.delete_members("vip", ["u-1"], 1)
    await task.start("vip", 1)

    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 0
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []


@pytest.mark.asyncio
async def test_set_task_quota_reconciles_immediately_with_new_limit(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "800"})
    await task.create_task("default", None, 1)
    await task.start("default", 1)

    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 1
    assert gateway.revoke_calls == ["u-1"]

    await quota.sync_members(now=now + timedelta(minutes=2))
    fixed_clock[0] = now + timedelta(minutes=2)
    admin = AdminService(factory, quota, actions, task)
    name, value = await admin.set_task_quota("default", Decimal("900"), 1)

    assert (name, value) == ("default", Decimal("900"))
    assert gateway.assign_calls == ["u-1"]


@pytest.mark.asyncio
async def test_list_task_usage_covers_bound_unbound_and_missing_members(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "25"})
    gateway.member_rows["u-2"] = Member(user_id="u-2", email="two@example.com", account_id=None, total_usage_usd="5")
    await quota.sync_members(now=now)
    await task.create_task("vip", Decimal("50"), 1)
    await task.add_members("vip", ["u-1", "u-2"], 1)
    async with factory() as session:
        async with session.begin():
            await session.execute(delete(UpstreamMember).where(UpstreamMember.reclaude_user_id == "u-2"))

    usage = await quota.list_task_usage(scope_mode="ALLOWLIST", member_ids=("u-1", "u-2"), limit_usd=Decimal("50"), now=now + timedelta(minutes=1))

    entries = usage["members"]
    assert len(entries) == 2
    bound = entries[0]
    assert bound["reclaude_user_id"] == "u-1"
    assert bound["used_usd"] == Decimal("25")
    assert bound["remaining_usd"] == Decimal("25")
    assert bound["telegram_user_id"] == 200
    assert bound["missing_upstream"] is False
    assert entries[1]["reclaude_user_id"] == "u-2"
    assert entries[1]["missing_upstream"] is True
    assert entries[1]["used_usd"] is None

    everything = await quota.list_task_usage(scope_mode=ALL, member_ids=(), limit_usd=Decimal("50"), now=now + timedelta(minutes=1))
    assert [entry["reclaude_user_id"] for entry in everything["members"]] == ["u-1"]


@pytest.mark.asyncio
async def test_task_status_controls_audit_per_task(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "0"})
    await task.create_task("vip", Decimal("10"), 1)
    await task.create_task("base", Decimal("50"), 1)
    await task.start("vip", 1)

    snapshots = {snapshot.name: snapshot for snapshot in await task.list_tasks()}
    assert snapshots["vip"].enabled is True
    assert snapshots["base"].enabled is False
    assert snapshots["vip"].limit_usd == Decimal("10")
    assert snapshots["base"].limit_usd == Decimal("50")

    async with factory() as session:
        rows = list((await session.scalars(select(AuditLog).where(AuditLog.action == "QUOTA_TASK_STARTED"))).all())
        assert len(rows) == 1
        assert rows[0].parameters_summary == {"name": "vip"}


@pytest.mark.asyncio
async def test_exclude_scope_covers_all_but_excluded_and_new_members(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "800", "u-2": "800"})
    await task.create_task("default", Decimal("10"), 1)
    assert await task.delete_members("default", ["u-1"], 1) == ("u-1",)
    assert (await task.snapshot("default")).scope_mode == "EXCLUDE"
    await task.start("default", 1)

    # u-1 is excluded; only u-2 is over the limit and revoked.
    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 1
    assert gateway.revoke_calls == ["u-2"]

    # A member joining later is covered automatically: baseline 0 first, then usage 800.
    gateway.member_rows["u-3"] = Member(user_id="u-3", email="three@example.com", account_id=4949, total_usage_usd=Decimal("0"))
    await quota.sync_members(now=now)
    binding = BindingService(factory, gateway)
    await binding.bind(203, "three@example.com")
    gateway.member_rows["u-3"] = Member(user_id="u-3", email="three@example.com", account_id=4949, total_usage_usd=Decimal("800"))
    await quota.sync_members(now=now)

    assert await actions.reconcile_cached(now=now + timedelta(minutes=1)) == 1
    assert gateway.revoke_calls == ["u-2", "u-3"]


@pytest.mark.asyncio
async def test_list_task_usage_exclude_scope_skips_excluded_members(app_context):
    factory, gateway, settings, quota, actions, task, users, now = await _prepare(app_context, {"u-1": "25", "u-2": "30"})

    usage = await quota.list_task_usage(scope_mode="EXCLUDE", member_ids=("u-1",), limit_usd=Decimal("50"), now=now + timedelta(minutes=1))

    assert [entry["reclaude_user_id"] for entry in usage["members"]] == ["u-2"]
