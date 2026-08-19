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
from reclaude_bot.infrastructure.db.models import CycleBaseline, QuotaRevocation, ServiceState, UpstreamMember
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
async def test_recovery_health_checks_accounts_and_enables_gate(app_context):
    factory, gateway, settings = app_context
    gate = RecoveryGate(factory)
    await gate.ensure_disabled()
    quota = QuotaService(factory, gateway, settings)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.health_sync_reconcile_enable(1)
    assert gateway.me_calls == 1
    assert gateway.accounts_calls == 1
    assert gateway.members_calls == 1
    assert await gate.is_enabled()
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.write_enabled is True


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
