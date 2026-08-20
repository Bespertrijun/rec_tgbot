from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.domain.enums import QuotaRevocationStatus
from reclaude_bot.infrastructure.db.models import AuditLog, QuotaRevocation
from reclaude_bot.infrastructure.reclaude.models import Member
from reclaude_bot.jobs.usage_poll import poll_once


async def seed(app_context, *, account_id: int | None = 4949, total: str = "0"):
    factory, gateway, settings = app_context
    gateway.configure_account_id(4949)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=account_id, total_usage_usd=Decimal(total))
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    binding = BindingService(factory, gateway)
    user = await binding.bind(200, "one@example.com")
    actions = QuotaActionService(factory, gateway, quota, settings)
    return factory, gateway, settings, quota, actions, user, now


@pytest.mark.asyncio
async def test_normal_tick_has_one_members_call_and_reconciles_quota_revoke(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    before = gateway.members_calls
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    assert gateway.members_calls == before + 1
    assert gateway.me_calls == 1
    assert gateway.revoke_calls == ["u-1"]

    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    async with factory() as session:
        row = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id))
        assert row.state == QuotaRevocationStatus.REVOKED.value


@pytest.mark.asyncio
async def test_setquota_uses_local_cache_and_increased_limit_restores(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    await quota.sync_members(now=datetime.now(UTC))
    members_calls = gateway.members_calls
    admin = AdminService(factory, quota, actions)
    value = await admin.set_quota(Decimal("900"), 1)
    assert value == Decimal("900")
    assert gateway.members_calls == members_calls
    assert gateway.assign_calls == ["u-1"]

    await poll_once(quota, actions, now=now + timedelta(minutes=3))
    async with factory() as session:
        row = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id))
        assert row.state == QuotaRevocationStatus.RESTORED.value


@pytest.mark.asyncio
async def test_lowering_limit_revoke_uses_cached_member_without_read(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=datetime.now(UTC))
    members_calls = gateway.members_calls
    admin = AdminService(factory, quota, actions)
    await admin.set_quota(Decimal("700"), 1)
    assert gateway.members_calls == members_calls
    assert gateway.revoke_calls == ["u-1"]
    async with factory() as session:
        audit_rows = list((await session.scalars(select(AuditLog).where(AuditLog.action == "SET_QUOTA"))).all())
        assert audit_rows[0].parameters_summary == {"old_limit_usd": "700.00", "new_limit_usd": "700"}


@pytest.mark.asyncio
async def test_setquota_ignores_stale_member_snapshot_without_reads_or_writes(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=now + timedelta(minutes=1))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    value = await AdminService(factory, quota, actions).set_quota(Decimal("700"), 1)

    assert value == Decimal("700")
    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    async with factory() as session:
        assert await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id)) is None


@pytest.mark.asyncio
async def test_next_members_tick_executes_after_stale_setquota_snapshot(app_context):
    factory, gateway, settings, quota, actions, _user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=now + timedelta(minutes=1))
    await AdminService(factory, quota, actions).set_quota(Decimal("700"), 1)
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await poll_once(quota, actions, now=now + timedelta(minutes=2))

    assert gateway.members_calls == members_calls + 1
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_future_member_snapshot_does_not_execute_cached_action(app_context):
    factory, gateway, settings, quota, actions, user, _now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=datetime.now(UTC) + timedelta(seconds=30))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await AdminService(factory, quota, actions).set_quota(Decimal("700"), 1)

    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    async with factory() as session:
        assert await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id)) is None


@pytest.mark.asyncio
async def test_fresh_member_snapshot_executes_cached_action_immediately(app_context):
    factory, gateway, settings, quota, actions, _user, _now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=datetime.now(UTC) - timedelta(seconds=1))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await AdminService(factory, quota, actions).set_quota(Decimal("700"), 1)

    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_last_day_percent_below_100_restores_all_revoked_once(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="800")
    gateway.me_response.current_account.usage_snapshot.limits[0].percent = Decimal("50")
    last_day = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
    await poll_once(quota, actions, now=last_day)
    assert gateway.me_calls == 2
    assert gateway.assign_calls == ["u-1"]
    await poll_once(quota, actions, now=last_day + timedelta(minutes=1))
    assert gateway.me_calls == 2


@pytest.mark.asyncio
async def test_last_day_percent_at_100_does_not_restore(app_context):
    factory, gateway, settings, quota, actions, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    gateway.me_response.current_account.usage_snapshot.limits[0].percent = Decimal("100")
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="800")
    await poll_once(quota, actions, now=datetime(2026, 8, 24, 6, 0, tzinfo=UTC))
    assert gateway.assign_calls == []
    assert gateway.me_calls == 2
