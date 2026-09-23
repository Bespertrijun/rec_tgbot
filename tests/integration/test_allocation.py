from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.enums import QuotaRevocationStatus
from reclaude_bot.infrastructure.db.models import AuditLog, QuotaRevocation, ServiceState, UsageNotification
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
    task = QuotaTaskService(factory, gateway)
    await task.create_task("default", None, 1)
    async with factory() as session:
        async with session.begin():
            session.add(ServiceState(id=1, write_enabled=False, reason="test", selected_account_id="4949", updated_at=now))
    await task.start("default", 1)
    return factory, gateway, settings, quota, actions, task, user, now


@pytest.mark.asyncio
async def test_normal_tick_has_one_members_call_and_reconciles_quota_revoke(app_context):
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
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
async def test_set_task_quota_uses_local_cache_and_increased_limit_restores(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    await quota.sync_members(now=now + timedelta(minutes=2))
    members_calls = gateway.members_calls
    fixed_clock[0] = now + timedelta(minutes=3)
    admin = AdminService(factory, quota, actions, task)
    name, value = await admin.set_task_quota("default", Decimal("900"), 1)
    assert (name, value) == ("default", Decimal("900"))
    assert gateway.members_calls == members_calls
    assert gateway.assign_calls == ["u-1"]

    await poll_once(quota, actions, now=now + timedelta(minutes=3))
    async with factory() as session:
        row = await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id))
        assert row.state == QuotaRevocationStatus.RESTORED.value


@pytest.mark.asyncio
async def test_lowering_task_limit_revoke_uses_cached_member_without_read(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=now + timedelta(minutes=1))
    members_calls = gateway.members_calls
    fixed_clock[0] = now + timedelta(minutes=2)
    admin = AdminService(factory, quota, actions, task)
    await admin.set_task_quota("default", Decimal("700"), 1)
    assert gateway.members_calls == members_calls
    assert gateway.revoke_calls == ["u-1"]
    async with factory() as session:
        audit_rows = list((await session.scalars(select(AuditLog).where(AuditLog.action == "SET_TASK_QUOTA"))).all())
        assert audit_rows[0].parameters_summary == {"name": "default", "old_limit_usd": "700.0000000000", "new_limit_usd": "700"}


@pytest.mark.asyncio
async def test_set_task_quota_ignores_stale_member_snapshot_without_reads_or_writes(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=now + timedelta(minutes=1))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls
    fixed_clock[0] = now + timedelta(minutes=3)

    name, value = await AdminService(factory, quota, actions, task).set_task_quota("default", Decimal("700"), 1)

    assert (name, value) == ("default", Decimal("700"))
    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    async with factory() as session:
        assert await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id)) is None


@pytest.mark.asyncio
async def test_next_members_tick_executes_after_stale_task_quota_snapshot(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, _user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=now + timedelta(minutes=1))
    fixed_clock[0] = now + timedelta(minutes=3)
    await AdminService(factory, quota, actions, task).set_task_quota("default", Decimal("700"), 1)
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await poll_once(quota, actions, now=now + timedelta(minutes=2))

    assert gateway.members_calls == members_calls + 1
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_future_member_snapshot_does_not_execute_cached_action(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, user, _now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=_now + timedelta(seconds=30))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await AdminService(factory, quota, actions, task).set_task_quota("default", Decimal("700"), 1)

    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == []
    assert gateway.assign_calls == []
    async with factory() as session:
        assert await session.scalar(select(QuotaRevocation).where(QuotaRevocation.user_id == user.id)) is None


@pytest.mark.asyncio
async def test_fresh_member_snapshot_executes_cached_action_immediately(app_context, fixed_clock):
    factory, gateway, settings, quota, actions, task, _user, _now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await quota.sync_members(now=_now - timedelta(seconds=1))
    members_calls = gateway.members_calls
    me_calls = gateway.me_calls

    await AdminService(factory, quota, actions, task).set_task_quota("default", Decimal("700"), 1)

    assert gateway.members_calls == members_calls
    assert gateway.me_calls == me_calls
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_quota_revoke_notifies_user_privately(app_context, fixed_clock):
    factory, gateway, settings, quota, _actions, task, user, now = await seed(app_context)
    notices: list[tuple[int, str]] = []

    async def notify(telegram_id: int, text: str) -> None:
        notices.append((telegram_id, text))

    actions = QuotaActionService(factory, gateway, quota, settings, user_notify_callback=notify)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    assert gateway.revoke_calls == ["u-1"]
    assert len(notices) == 1
    telegram_id, text = notices[0]
    assert telegram_id == 200
    assert "额度已用完" in text

    await quota.sync_members(now=now + timedelta(minutes=2))
    fixed_clock[0] = now + timedelta(minutes=3)
    await AdminService(factory, quota, actions, task).set_task_quota("default", Decimal("900"), 1)
    assert gateway.assign_calls == ["u-1"]
    # 800/900 = 88.9%: the restore first triggers the pending 80% reminder.
    assert len(notices) == 3
    assert "80%" in notices[1][1]
    restore_telegram_id, restore_text = notices[2]
    assert restore_telegram_id == 200
    assert "额度已恢复" in restore_text


@pytest.mark.asyncio
async def test_user_notice_failure_does_not_block_revoke(app_context):
    factory, gateway, settings, quota, _actions, _task, _user, now = await seed(app_context)

    async def failing_notify(telegram_id: int, text: str) -> None:
        raise RuntimeError("user blocked the bot")

    actions = QuotaActionService(factory, gateway, quota, settings, user_notify_callback=failing_notify)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    assert gateway.revoke_calls == ["u-1"]


@pytest.mark.asyncio
async def test_usage_threshold_notices_sent_once_per_cycle(app_context):
    factory, gateway, settings, quota, _actions, _task, _user, now = await seed(app_context)
    notices: list[tuple[int, str]] = []

    async def notify(telegram_id: int, text: str) -> None:
        notices.append((telegram_id, text))

    actions = QuotaActionService(factory, gateway, quota, settings, user_notify_callback=notify)
    # Default limit is $700: 50% = $350, 80% = $560.
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="400")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    assert len(notices) == 1
    assert notices[0][0] == 200
    assert "50%" in notices[0][1]

    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    assert len(notices) == 1

    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="600")
    await poll_once(quota, actions, now=now + timedelta(minutes=3))
    assert len(notices) == 2
    assert "80%" in notices[1][1]
    assert "100%" in notices[1][1]

    # 100%+ is covered by the revoke notice, with no extra threshold message.
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=4))
    assert gateway.revoke_calls == ["u-1"]
    assert len(notices) == 3
    assert "额度已用完" in notices[2][1]


@pytest.mark.asyncio
async def test_usage_threshold_notice_failure_does_not_block_tick(app_context):
    factory, gateway, settings, quota, _actions, _task, _user, now = await seed(app_context)

    async def failing_notify(telegram_id: int, text: str) -> None:
        raise RuntimeError("user blocked the bot")

    actions = QuotaActionService(factory, gateway, quota, settings, user_notify_callback=failing_notify)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="400")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    async with factory() as session:
        rows = list((await session.scalars(select(UsageNotification))).all())
    assert [row.threshold_percent for row in rows] == [50]


@pytest.mark.asyncio
async def test_last_day_percent_below_100_restores_all_revoked_once(app_context):
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
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
    factory, gateway, settings, quota, actions, task, user, now = await seed(app_context)
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=4949, total_usage_usd="800")
    await poll_once(quota, actions, now=now + timedelta(minutes=1))
    await poll_once(quota, actions, now=now + timedelta(minutes=2))
    gateway.me_response.current_account.usage_snapshot.limits[0].percent = Decimal("100")
    gateway.member_rows["u-1"] = Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="800")
    await poll_once(quota, actions, now=datetime(2026, 8, 24, 6, 0, tzinfo=UTC))
    assert gateway.assign_calls == []
    assert gateway.me_calls == 2
