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
from reclaude_bot.domain.enums import BaselineStatus, QuotaRevocationStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, CycleBaseline, QuotaCycle, QuotaRevocation, ServiceState, UpstreamMember
from reclaude_bot.infrastructure.reclaude.models import Member
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
        assert state.quota_task_enabled is False
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
        assert state.quota_task_enabled is False
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
    await gate.activate_task(1)
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
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.reason == "reclaude_recovery_failed"


@pytest.mark.asyncio
async def test_write_disabled_gate_prevents_automatic_revoke(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    actions = QuotaActionService(factory, gateway, quota, settings, gate=gate)
    await quota.sync_cycle_from_me(now=datetime(2026, 8, 18, tzinfo=UTC))
    await quota.sync_members(now=datetime(2026, 8, 18, tzinfo=UTC))
    await BindingService(factory, gateway).bind(302, "one@example.com")
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=datetime(2026, 8, 18, 0, 1, tzinfo=UTC))
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
