import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from reclaude_bot.application.actions import DeviceQuotaActionService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.application.recovery import RecoveryGate
from reclaude_bot.infrastructure.db.models import DeviceAssociation, DeviceCycleLedger, DeviceNotification, DeviceQuotaCycle, QuotaTask, ServiceState, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_ledger import NOW

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def stack(factory, *, now=NOW, before_revoke=None):
    clock = [now]
    gateway = SimpleNamespace(account_id="7022", revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)),
                              list_devices=AsyncMock(return_value=[]))
    quota = DeviceQuotaService(factory, 178, clock=lambda: clock[0])
    revoke = DeviceRevocationService(factory, gateway, 178, clock=lambda: clock[0], before_revoke=before_revoke)
    notify = AsyncMock()
    actions = DeviceQuotaActionService(factory, quota, revoke, clock=lambda: clock[0], user_notify_callback=notify,
                                       gate=RecoveryGate(factory))
    return SimpleNamespace(quota=quota, revoke=revoke, actions=actions, notify=notify, gateway=gateway, clock=clock)


async def notices(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceNotification).order_by(DeviceNotification.id))).all())


@pytest.mark.parametrize("used,events,revoked", [
    ("349.9999999999", set(), False), ("350", {"THRESHOLD_50"}, False),
    ("560", {"THRESHOLD_50", "THRESHOLD_80"}, False),
    ("699.9999999999", {"THRESHOLD_50", "THRESHOLD_80"}, False),
    ("700", {"THRESHOLD_50", "THRESHOLD_80", "THRESHOLD_100"}, True),
])
async def test_thresholds_exact_boundary_and_repeated_tick_is_idempotent(lifecycle_db, used, events, revoked):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, ledger_id = await metered_user(factory, cycle_id, used)
    runtime = stack(factory)
    await runtime.actions.run_once()
    sent_count = runtime.notify.await_count
    await runtime.actions.run_once()
    assert runtime.notify.await_count == sent_count
    rows = await notices(factory)
    assert {row.event for row in rows} == events
    assert all(row.status == "SENT" for row in rows)
    assert runtime.gateway.revoke_device.await_count == int(revoked)
    async with factory() as session:
        linked = await session.get(DeviceAssociation, association_id)
        ledger = await session.get(DeviceCycleLedger, ledger_id)
        assert (linked.state == "ENDED") == revoked
        assert (ledger.quota_locked_at is not None) == revoked
        assert ledger.confirmed_used_usd == Decimal(used)
        if revoked:
            assert linked.end_reason == "QUOTA"


async def test_effective_quota_is_notification_denominator(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "350")
    runtime = stack(factory)
    await runtime.quota.adjust(1, cycle_id, Decimal("700"), "double limit", 999, operation_key="double")
    await runtime.actions.run_once()
    assert await notices(factory) == []
    runtime.gateway.revoke_device.assert_not_called()


async def test_stopped_task_does_not_automatically_revoke_or_lock(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory, running=False)
    _, ledger_id = await metered_user(factory, cycle_id, "750")
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_not_called()
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, ledger_id)).quota_locked_at is None


async def test_last_day_allows_overquota_device_without_revoke(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory, reset=NOW + timedelta(hours=23), allow_last_day=True)
    await metered_user(factory, cycle_id, "750")
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_not_called()


async def test_known_lower_bound_overquota_can_still_revoke(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "750", quality="NEEDS_REVIEW")
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_awaited_once_with(44500)


async def test_parallel_instances_revoke_and_send_once(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "700")
    one, two = stack(factory), stack(factory)
    two.gateway.revoke_device = one.gateway.revoke_device
    two.actions.user_notify_callback = one.notify
    await asyncio.wait_for(asyncio.gather(one.actions.run_once(), two.actions.run_once()), timeout=10)
    one.gateway.revoke_device.assert_awaited_once_with(44500)
    assert all(row.status == "SENT" for row in await notices(factory))
    assert one.notify.await_count <= 3


async def test_failed_notification_survives_restart_and_retries(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "350")
    runtime = stack(factory)
    runtime.notify.side_effect = RuntimeError("private message")
    await runtime.actions.run_once()
    row = (await notices(factory))[0]
    assert row.status == "PENDING" and row.sent_at is None
    restarted = stack(factory, now=row.next_retry_at or NOW + timedelta(minutes=1))
    await restarted.actions.run_once()
    assert (await notices(factory))[0].status == "SENT"
    restarted.notify.assert_awaited_once()


async def test_unknown_revoke_keeps_slot_then_reconciles_without_repeating_post(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, "700")
    runtime = stack(factory)
    runtime.gateway.revoke_device.side_effect = httpx.ReadTimeout("unknown")
    await runtime.actions.run_once()
    async with factory() as session:
        assert (await session.get(DeviceAssociation, association_id)).ended_at is None
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_awaited_once()
    runtime.gateway.list_devices.assert_awaited_once()
    async with factory() as session:
        linked = await session.get(DeviceAssociation, association_id)
        assert linked.state == "ENDED" and linked.end_reason == "QUOTA"


@pytest.mark.parametrize("change", ["stop", "new_cycle", "last_day", "write_latch"])
async def test_rechecks_before_post_after_pre_revoke_sampling(lifecycle_db, change):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "700")

    async def change_during_sample(association_id):
        async with factory.begin() as session:
            if change == "stop":
                (await session.get(QuotaTask, 1)).status = "STOPPED"
            elif change == "new_cycle":
                (await session.get(DeviceQuotaCycle, cycle_id)).reset_at = NOW
            elif change == "write_latch":
                (await session.get(ServiceState, 1)).write_enabled = False
            else:
                row = await session.get(DeviceQuotaCycle, cycle_id)
                row.reset_at = NOW + timedelta(hours=23)
                row.last_day_allow = True
                row.last_day_checked_at = NOW

    runtime = stack(factory, before_revoke=change_during_sample)
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("new_cycle", [False, True])
async def test_quota_revoked_user_gets_durable_auth_available_notice(lifecycle_db, new_cycle):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "700")
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.notify.reset_mock()
    if new_cycle:
        runtime.clock[0] = NOW + timedelta(days=7)
        await ready_cycle(factory, now=runtime.clock[0])
        expected = "AUTH_AVAILABLE_NEW_CYCLE"
    else:
        async with factory.begin() as session:
            row = await session.get(DeviceQuotaCycle, cycle_id)
            row.reset_at = NOW + timedelta(hours=23)
            row.last_day_allow = True
            row.last_day_checked_at = NOW
        expected = "AUTH_AVAILABLE_LAST_DAY"
    await runtime.actions.run_once()
    await runtime.actions.run_once()
    rows = [row for row in await notices(factory) if row.event == expected]
    assert len(rows) == 1 and rows[0].status == "SENT"
    runtime.notify.assert_awaited_once()
    assert "/auth" in runtime.notify.await_args.args[1]
    runtime.gateway.revoke_device.assert_awaited_once()


async def test_banned_user_is_not_told_auth_is_available(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "700")
    runtime = stack(factory)
    await runtime.actions.run_once()
    async with factory.begin() as session:
        (await session.get(User, 1)).status = "BANNED"
        row = await session.get(DeviceQuotaCycle, cycle_id)
        row.reset_at = NOW + timedelta(hours=23)
        row.last_day_allow = True
    await runtime.actions.run_once()
    assert not any(row.event.startswith("AUTH_AVAILABLE") and row.status == "SENT" for row in await notices(factory))


async def test_closed_operational_latch_does_not_send_revoke(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "700")
    async with factory.begin() as session:
        (await session.get(ServiceState, 1)).write_enabled = False
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("case", ["excluded", "unbound", "pending_auth", "manual_deauth"])
async def test_new_cycle_notice_requires_actual_auth_eligibility(lifecycle_db, case):
    from reclaude_bot.application.device_task_members import DeviceTaskMemberService

    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, "700")
    runtime = stack(factory)
    if case == "manual_deauth":
        await runtime.revoke.deauth(1)
    else:
        await runtime.actions.run_once()
    runtime.clock[0] = NOW + timedelta(days=7)
    await ready_cycle(factory, now=runtime.clock[0])
    if case == "excluded":
        await DeviceTaskMemberService(factory, 178).delete_members(None, [1], 999)
    elif case in {"unbound", "pending_auth"}:
        async with factory.begin() as session:
            if case == "unbound":
                (await session.get(User, 1)).binding_status = "UNBOUND"
            else:
                session.add(DeviceAssociation(user_id=1, task_id=1, org_id=178, state="PENDING_AUTH",
                                              started_at=runtime.clock[0], updated_at=runtime.clock[0]))
    runtime.notify.reset_mock()
    await runtime.actions.run_once()
    assert not any(row.event.startswith("AUTH_AVAILABLE") and row.status == "SENT" for row in await notices(factory))
    runtime.notify.assert_not_called()


async def test_current_cycle_quota_revoke_gets_last_day_notice_not_another_new_cycle_notice(lifecycle_db):
    factory, _ = lifecycle_db
    first_cycle = await ready_cycle(factory)
    await metered_user(factory, first_cycle, "700")
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.clock[0] = NOW + timedelta(days=7)
    next_cycle = await ready_cycle(factory, now=runtime.clock[0])
    await metered_user(factory, next_cycle, "700", device_id=44501, now=runtime.clock[0])
    await runtime.actions.run_once()
    async with factory.begin() as session:
        row = await session.get(DeviceQuotaCycle, next_cycle)
        row.reset_at = runtime.clock[0] + timedelta(hours=23)
        row.last_day_allow = True
        row.last_day_checked_at = runtime.clock[0]
    await runtime.actions.run_once()
    async with factory() as session:
        rows = (await session.scalars(select(DeviceNotification).join(DeviceCycleLedger)
                                      .where(DeviceCycleLedger.cycle_id == next_cycle,
                                             DeviceNotification.event.like("AUTH_AVAILABLE%")))).all()
    assert {row.event for row in rows if row.status == "SENT"} == {"AUTH_AVAILABLE_LAST_DAY"}


@pytest.mark.xfail(strict=True, reason="当前仅四个用户，用户暂不调整批量扫描推进规则")
async def test_bounded_enforcement_does_not_forever_skip_later_active_user(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    async with factory.begin() as session:
        session.add(User(id=3, telegram_user_id=1003, email="3@example.invalid", email_normalized="3@example.invalid",
                         bound_at=NOW, updated_at=NOW))
        # Earlier ledgers can exist for users who received a transfer but have no device.
        for user_id in (1, 2):
            session.add(DeviceCycleLedger(user_id=user_id, cycle_id=cycle_id, task_id=1,
                                          quality="UNKNOWN", confirmed_used_usd=None, created_at=NOW, updated_at=NOW))
    await metered_user(factory, cycle_id, "700", user_id=3)
    runtime = stack(factory)
    for _ in range(3):
        await runtime.actions.run_once(limit=2)
    runtime.gateway.revoke_device.assert_awaited_once_with(44500)


@pytest.mark.xfail(strict=True, reason="当前仅四个用户，用户暂不调整批量扫描推进规则")
async def test_bounded_reauthorization_scan_eventually_notifies_later_users(lifecycle_db):
    from reclaude_bot.infrastructure.db.models import Device

    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    async with factory.begin() as session:
        session.add(User(id=3, telegram_user_id=1003, email="3@example.invalid", email_normalized="3@example.invalid",
                         bound_at=NOW, updated_at=NOW))
        session.add(Device(org_id=178, device_id=44503, name="third", first_synced_at=NOW, last_synced_at=NOW))
    for user_id, device_id in [(1, 44500), (2, 44501), (3, 44503)]:
        await metered_user(factory, cycle_id, "700", user_id=user_id, device_id=device_id)
    runtime = stack(factory)
    await runtime.actions.run_once()
    runtime.clock[0] = NOW + timedelta(days=7)
    await ready_cycle(factory, now=runtime.clock[0])
    for _ in range(3):
        await runtime.actions.run_once(limit=2)
    async with factory() as session:
        notified = set((await session.scalars(select(DeviceCycleLedger.user_id).join(DeviceNotification)
                                              .where(DeviceNotification.event == "AUTH_AVAILABLE_NEW_CYCLE",
                                                     DeviceNotification.status == "SENT"))).all())
    assert notified == {1, 2, 3}
