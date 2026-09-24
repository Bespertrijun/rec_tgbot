from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.enums import BaselineStatus, BindingStatus, QuotaRevocationStatus, TaskStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, CycleBaseline, QuotaCycle, QuotaRevocation, QuotaTask, ServiceState, UpstreamMember, User
from reclaude_bot.infrastructure.reclaude.models import Member, MeResponse, SevenDay, WeeklyLimit
from reclaude_bot.jobs.usage_poll import poll_once


@pytest.mark.asyncio
async def test_cycle_boundary_builds_baseline_for_unbound_member(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    cycle = await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    async with factory() as session:
        member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == "u-1"))
        baseline = await session.scalar(select(CycleBaseline).where(CycleBaseline.reclaude_user_id == "u-1", CycleBaseline.cycle_id == cycle.id))
        assert member is not None
        assert baseline.status == BaselineStatus.VERIFIED.value
        assert baseline.user_id is None


@pytest.mark.asyncio
async def test_status_is_cache_only_and_used_uses_dynamic_limit(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    await BindingService(factory, gateway).bind(301, "one@example.com")
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="25")
    members_calls = gateway.members_calls
    await quota.sync_members(now=now + timedelta(minutes=1))
    value = await quota.get_status(301, now=now + timedelta(minutes=1))
    assert value["used_usd"] == Decimal("25")
    assert value["limit_usd"] == Decimal("700.00")
    assert gateway.members_calls == members_calls + 1
    calls_after_sync = gateway.members_calls
    await quota.get_status(301, now=now + timedelta(minutes=1))
    assert gateway.members_calls == calls_after_sync


@pytest.mark.asyncio
async def test_recovery_health_checks_accounts_without_enabling_task(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gateway.account_rows = [
        {
            "id": 7022,
            "account_email": "other@example.com",
            "account_id": 8123,
            "health": "degraded",
            "lifecycle": "bound",
            "org_id": 178,
        }
    ]
    gateway.me_response = gateway.me_response.model_copy(
        update={
            "current_account": gateway.me_response.current_account.model_copy(update={"email_masked": "different***@example.com"}),
        }
    )
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.health_sync_reconcile_enable(1)
    assert gateway.me_calls == 1
    assert gateway.accounts_calls == 1
    assert gateway.members_calls == 1
    assert gateway.account_id == 8123
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.write_enabled is False
        assert state.selected_account_id == "8123"
        audit_row = await session.scalar(select(AuditLog).where(AuditLog.action == "SELECT_ACCOUNT"))
        assert audit_row is not None
        assert audit_row.parameters_summary == {"account_id": "8123"}
        cycle = await session.scalar(select(QuotaCycle))
        assert cycle.source_account_id == 8123


@pytest.mark.asyncio
async def test_account_listing_allows_banned_current_account(app_context):
    factory, gateway, settings = app_context
    gateway.me_response = gateway.me_response.model_copy(
        update={"current_account": gateway.me_response.current_account.model_copy(update={"status": "banned"})}
    )
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    listing = await recovery.list_accounts()

    assert listing.me.current_account.status == "banned"
    assert listing.accounts.items[0].account_id == 4949
    assert gateway.accounts_calls == 1
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_select_account_rejects_banned_current_account_without_configuration_or_persistence(app_context):
    factory, gateway, settings = app_context
    gateway.me_response = gateway.me_response.model_copy(
        update={"current_account": gateway.me_response.current_account.model_copy(update={"status": "banned"})}
    )
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="当前账号状态异常：banned"):
        await recovery.select_account(4949, 1)

    assert gateway.accounts_calls == 0
    assert gateway.members_calls == 0
    assert gateway.account_id is None
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.selected_account_id is None


@pytest.mark.asyncio
async def test_select_account_persists_and_restores_after_restart(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    selected = await recovery.select_account("4949", 1)

    assert selected.account_id == 4949
    assert gateway.account_id == 4949
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "4949"
        assert state.write_enabled is False
        audit_row = await session.scalar(select(AuditLog).where(AuditLog.action == "SELECT_ACCOUNT"))
        assert audit_row is not None
        assert audit_row.parameters_summary == {"account_id": "4949"}

    await gate.ensure_disabled()
    gateway.account_id = None
    restored = await recovery.restore_persisted_account()

    assert restored == "4949"
    assert gateway.account_id == "4949"
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle", "health", "message"),
    [
        ("unbound", "healthy", "未绑定"),
        ("bound", "banned", "健康状态不可用"),
    ],
)
async def test_select_account_rejects_unusable_live_account(app_context, lifecycle, health, message):
    factory, gateway, settings = app_context
    gateway.account_rows = [{"id": 7022, "account_id": 4949, "health": health, "lifecycle": lifecycle, "org_id": 178}]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match=message):
        await recovery.select_account(4949, 1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_select_account_upstream_failure_leaves_gate_disabled(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()

    async def fail_members():
        raise ConnectionError("members unavailable")

    gateway.members = fail_members
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(ConnectionError, match="members unavailable"):
        await recovery.select_account(4949, 1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_select_account_final_audit_failure_rolls_back_activation(app_context, monkeypatch, fixed_clock):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("reclaude_bot.application.recovery.audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await recovery.select_account(4949, 1)

    assert gateway.members_calls == 1
    assert gateway.account_id is None
    assert await gate.is_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state is not None
        assert state.selected_account_id is None
        assert state.write_enabled is False
        assert (await session.scalars(select(AuditLog))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_id", ["not-an-id", "9999"])
async def test_select_account_rejects_invalid_or_missing_account(app_context, requested_id):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError):
        await recovery.select_account(requested_id, 1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_recovery_rejects_multiple_accounts_without_configuring_id(app_context):
    factory, gateway, settings = app_context
    gateway.account_rows = [
        {"id": 7022, "account_id": 8123, "health": "healthy", "lifecycle": "bound", "org_id": 178},
        {"id": 7023, "account_id": 8124, "health": "healthy", "lifecycle": "bound", "org_id": 178},
    ]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)

    with pytest.raises(EligibilityError, match="不唯一"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_recovery_rejects_zero_bound_accounts(app_context):
    factory, gateway, settings = app_context
    gateway.account_rows = [{"id": 7022, "account_id": 8123, "health": "healthy", "lifecycle": "unbound", "org_id": 178}]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="没有可用的已绑定账号"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["banned", " BANNED "])
async def test_recovery_rejects_banned_account_health(app_context, health):
    factory, gateway, settings = app_context
    gateway.account_rows = [{"id": 7022, "account_id": 8123, "health": health, "lifecycle": "bound", "org_id": 178}]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="健康状态不可用"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_recovery_rejects_banned_current_account_before_discovery(app_context):
    factory, gateway, settings = app_context
    gateway.me_response = gateway.me_response.model_copy(
        update={
            "current_account": gateway.me_response.current_account.model_copy(update={"status": "banned"}),
        }
    )
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="当前账号未绑定"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.accounts_calls == 0
    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [None, "", "   "])
async def test_recovery_rejects_missing_or_blank_account_health(app_context, health):
    factory, gateway, settings = app_context
    gateway.account_rows = [{"id": 7022, "account_id": 8123, "health": health, "lifecycle": "bound", "org_id": 178}]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="健康状态不可用"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_recovery_rejects_missing_account_id_even_when_record_id_exists(app_context):
    factory, gateway, settings = app_context
    gateway.account_rows = [{"id": 7022, "health": "healthy", "lifecycle": "bound", "org_id": 178}]
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)

    with pytest.raises(EligibilityError, match="account_id"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.account_id is None
    assert await gate.is_enabled() is False


@pytest.mark.asyncio
async def test_recovery_post_auth_failure_disables_previously_enabled_gate(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    await gate.persist_selected_account(4949, 1)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    await task.start("default", 1)
    assert await gate.is_enabled() is True

    async def fail_accounts():
        gateway.accounts_calls += 1
        raise EligibilityError("账号查询失败")

    gateway.accounts = fail_accounts
    recovery = RecoveryService(gate, QuotaService(factory, gateway, settings), gateway, settings)
    with pytest.raises(EligibilityError, match="账号查询失败"):
        await recovery.health_sync_reconcile_enable(1)

    assert gateway.me_calls == 1
    assert gateway.accounts_calls == 1
    assert await gate.is_enabled() is False
    assert await task.any_enabled() is False
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.reason == "reclaude_recovery_failed"


@pytest.mark.asyncio
async def test_write_disabled_gate_prevents_automatic_revoke(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    await gate.persist_selected_account(4949, 1)
    quota = QuotaService(factory, gateway, settings)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    await task.start("default", 1)
    # Close the internal latch while the task stays RUNNING.
    await gate.disable("maintenance")
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    await quota.sync_cycle_from_me(now=datetime(2026, 8, 18, tzinfo=UTC))
    await quota.sync_members(now=datetime(2026, 8, 18, tzinfo=UTC))
    await BindingService(factory, gateway).bind(302, "one@example.com")
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    members_before = gateway.members_calls
    await poll_once(quota, actions, now=datetime(2026, 8, 18, 0, 1, tzinfo=UTC))
    # The sync still runs; only the write path is blocked.
    assert gateway.members_calls == members_before + 1
    assert gateway.revoke_calls == []


@pytest.mark.asyncio
async def test_duplicate_quota_revocation_is_rejected(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    user = await BindingService(factory, gateway).bind(303, "one@example.com")
    async with factory() as session:
        cycle = await quota.current_cycle(session, now)
        session.add(QuotaRevocation(user_id=user.id, cycle_id=cycle.id, state=QuotaRevocationStatus.REVOKED.value, updated_at=now))
        await session.commit()
        duplicate = QuotaRevocation(user_id=user.id, cycle_id=cycle.id, state=QuotaRevocationStatus.REVOKED.value, updated_at=now)
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_setquota_without_cache_does_not_read_upstream(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    actions = QuotaActionService(factory, gateway, quota, settings)
    await AdminService(factory, quota, actions).set_quota(Decimal("650"), 1)
    assert gateway.members_calls == 0
    assert gateway.me_calls == 0


@pytest.mark.asyncio
async def test_get_account_usage_estimates_weekly_total_from_local_spend(app_context):
    factory, gateway, settings = app_context
    now = datetime(2026, 8, 18, tzinfo=UTC)
    quota = QuotaService(factory, gateway, settings)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="120")
    await quota.sync_members(now=now)
    me_calls_before = gateway.me_calls

    usage = await quota.get_account_usage(now=now)

    assert gateway.me_calls == me_calls_before + 1
    assert usage.email_masked == "owner***@example.com"
    assert usage.usage_updated_at == datetime(2026, 8, 18, tzinfo=UTC)
    # Local cycle spend $120 at 10% utilization → estimated weekly total $1200.
    assert usage.seven_day_utilization == Decimal("10")
    assert usage.seven_day_estimated_total == Decimal("1200")
    # The fixture has no five_hour window.
    assert usage.five_hour_utilization is None
    assert usage.five_hour_resets_at is None


@pytest.mark.asyncio
async def test_get_account_usage_reads_five_hour_window(app_context):
    factory, gateway, settings = app_context
    gateway.me_response = MeResponse.model_validate(
        {
            "current_account": {
                "status": "bound",
                "email_masked": "ma****@rekwa.com",
                "usage_updated_at": "2026-08-21T00:00:00Z",
                "usage_snapshot": {
                    "limits": [{"group": "weekly", "kind": "weekly_all", "scope": None, "percent": "6", "resets_at": "2026-08-25T00:00:00Z", "is_active": True}],
                    "seven_day": {"utilization": 6, "resets_at": "2026-08-25T00:00:00Z", "used_dollars": None, "limit_dollars": None},
                    "five_hour": {"utilization": 25, "resets_at": "2026-08-21T02:30:00Z", "used_dollars": None, "limit_dollars": None},
                },
            }
        }
    )
    quota = QuotaService(factory, gateway, settings)

    usage = await quota.get_account_usage(now=datetime(2026, 8, 21, tzinfo=UTC))

    assert usage.five_hour_utilization == Decimal("25")
    assert usage.five_hour_resets_at == datetime(2026, 8, 21, 2, 30, tzinfo=UTC)
    # No local cycle data → no weekly estimate.
    assert usage.seven_day_estimated_total is None


async def _seed_revoked_user(app_context):
    """Drive bound member u-1 into a confirmed REVOKED state under a RUNNING task."""
    factory, gateway, settings = app_context
    gateway.configure_account_id(4949)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd=Decimal("0"))
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    user = await BindingService(factory, gateway).bind(200, "one@example.com")
    actions = QuotaActionService(factory, gateway, quota, settings)
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    async with factory() as session:
        async with session.begin():
            session.add(ServiceState(id=1, write_enabled=False, reason="test", selected_account_id="4949", updated_at=now))
    await task.start("default", 1)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd=Decimal("800"))
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    return factory, gateway, settings, quota, actions, task, user, now


def _add_second_account(gateway) -> None:
    gateway.account_rows.append({"id": 7023, "account_email": "new@example.com", "account_id": 8123, "health": "healthy", "lifecycle": "bound", "org_id": 178})


def _set_weekly_reset(gateway, reset: datetime) -> None:
    snapshot = gateway.me_response.current_account.usage_snapshot
    gateway.me_response = gateway.me_response.model_copy(
        update={
            "current_account": gateway.me_response.current_account.model_copy(
                update={
                    "usage_snapshot": snapshot.model_copy(
                        update={
                            "limits": [WeeklyLimit(group="weekly", kind="weekly_all", scope=None, percent=Decimal("10"), resets_at=reset, is_active=True)],
                            "seven_day": SevenDay(utilization=Decimal("10"), resets_at=reset),
                        }
                    )
                }
            )
        }
    )


@pytest.mark.asyncio
async def test_select_account_carries_over_revocation_and_auto_restores(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, user, now = await _seed_revoked_user(app_context)
    async with factory() as session:
        old_cycle = await quota.current_cycle(session, now)
        old_revocation = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id, QuotaRevocation.cycle_id == old_cycle.id))
        assert old_revocation.state == QuotaRevocationStatus.REVOKED.value
    assert gateway.member_rows["u-1"].account_id is None

    _add_second_account(gateway)
    # The new account's week ends earlier than the old cycle, so it becomes current.
    new_reset = datetime(2026, 8, 20, tzinfo=UTC)
    _set_weekly_reset(gateway, new_reset)
    gate = RecoveryGate(factory)
    recovery = RecoveryService(gate, quota, gateway, settings)
    fixed_clock[0] = now + timedelta(minutes=3)

    account = await recovery.select_account(8123, 1)

    assert account.account_id == 8123
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "8123"
        # The previously RUNNING task resumes and re-opens the write latch.
        assert state.write_enabled is True
        task_row = await session.scalar(select(QuotaTask).where(QuotaTask.name_normalized == "default"))
        assert task_row.status == TaskStatus.RUNNING.value
        new_cycle = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at == new_reset))
        assert new_cycle is not None
        carried = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id, QuotaRevocation.cycle_id == new_cycle.id))
        assert carried is not None
        assert carried.state == QuotaRevocationStatus.REVOKED.value
        actions_logged = set((await session.scalars(select(AuditLog.action))).all())
        assert "QUOTA_REVOCATION_CARRY_OVER" in actions_logged
        assert "ACCOUNT_SWITCH_TASKS_RESUMED" in actions_logged

    # The next regular ticks re-assign the carried-over member and reconcile RESTORED.
    await poll_once(quota, actions, now=now + timedelta(minutes=4))
    assert gateway.assign_calls == ["u-1"]
    await poll_once(quota, actions, now=now + timedelta(minutes=5))
    async with factory() as session:
        carried = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id, QuotaRevocation.cycle_id == new_cycle.id))
        assert carried.state == QuotaRevocationStatus.RESTORED.value


@pytest.mark.asyncio
async def test_select_account_rebaselines_current_cycle_usage(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gateway.configure_account_id(4949)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd=Decimal("0"))
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    cycle = await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    # Usage climbs after the baseline: $300 already spent in the current cycle.
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd=Decimal("300"))
    await quota.sync_members(now=now + timedelta(minutes=1))
    async with factory() as session:
        baseline = await session.scalar(select(CycleBaseline).where(CycleBaseline.cycle_id == cycle.id))
        assert baseline.baseline_total_usd == Decimal("0")

    # Re-selecting an account on the same weekly reset keeps the cycle but zeroes usage.
    recovery = RecoveryService(RecoveryGate(factory), quota, gateway, settings)
    fixed_clock[0] = now + timedelta(minutes=2)
    account = await recovery.select_account(4949, 1)

    assert account.account_id == 4949
    async with factory() as session:
        baseline = await session.scalar(select(CycleBaseline).where(CycleBaseline.cycle_id == cycle.id))
        assert baseline.baseline_total_usd == Decimal("300")
        assert baseline.source == "account_switch"
        assert baseline.status == BaselineStatus.VERIFIED.value
        state = await session.get(ServiceState, 1)
        # Nothing was RUNNING before the switch, so the latch stays closed.
        assert state.selected_account_id == "4949"
        assert state.write_enabled is False


@pytest.mark.asyncio
async def test_carry_over_skips_restored_assigned_and_unbound_users(app_context, fixed_clock):
    factory, gateway, settings = app_context
    gateway.configure_account_id(4949)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd=Decimal("0"))
    gateway.member_rows["u-2"] = Member(user_id="u-2", email="two@example.com", account_id=4949, total_usage_usd=Decimal("0"))
    gateway.member_rows["u-3"] = Member(user_id="u-3", email="three@example.com", account_id=None, total_usage_usd=Decimal("0"))
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    cycle = await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    binding = BindingService(factory, gateway)
    restored_user = await binding.bind(200, "one@example.com")
    assigned_user = await binding.bind(201, "two@example.com")
    unbound_user = await binding.bind(202, "three@example.com")
    async with factory() as session:
        async with session.begin():
            session.add(QuotaRevocation(user_id=restored_user.id, cycle_id=cycle.id, state=QuotaRevocationStatus.RESTORED.value, reason="QUOTA", revoked_at=now, updated_at=now))
            session.add(QuotaRevocation(user_id=assigned_user.id, cycle_id=cycle.id, state=QuotaRevocationStatus.REVOKED.value, reason="QUOTA", revoked_at=now, updated_at=now))
            session.add(QuotaRevocation(user_id=unbound_user.id, cycle_id=cycle.id, state=QuotaRevocationStatus.REVOKED.value, reason="QUOTA", revoked_at=now, updated_at=now))
            (await session.get(User, unbound_user.id)).binding_status = BindingStatus.UNBOUND.value

    _add_second_account(gateway)
    new_reset = datetime(2026, 8, 20, tzinfo=UTC)
    _set_weekly_reset(gateway, new_reset)
    recovery = RecoveryService(RecoveryGate(factory), quota, gateway, settings)
    fixed_clock[0] = now + timedelta(minutes=1)

    await recovery.select_account(8123, 1)

    async with factory() as session:
        new_cycle = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at == new_reset))
        assert new_cycle is not None
        carried = (await session.scalars(select(QuotaRevocation).where(QuotaRevocation.cycle_id == new_cycle.id))).all()
        assert carried == []
