"""Account reset delivery uses fake Telegram sends and a real isolated database."""
import asyncio
from collections import Counter
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import (
    DeviceAccountNotification,
    DeviceTaskScope,
    QuotaTask,
    ServiceState,
)
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_auto_org_account_reset import cycle_count, latest_cycle, runtime
from tests.integration.test_device_ledger import NOW, end_association
from tests.unit.test_device_context import accounts

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def notification_runtime(factory, *, account_id=8123, notify=None):
    rt = runtime(factory, account_id=account_id)
    rt.notify = notify or AsyncMock()
    rt.notices = DeviceAccountNotificationService(
        factory, user_notify_callback=rt.notify, admin_ids=[999, 998], clock=lambda: rt.clock[0],
    )
    rt.reset.account_notifications = rt.notices
    rt.reconciler.account_notifications = rt.notices
    return rt


async def notices(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceAccountNotification).order_by(DeviceAccountNotification.id))).all())


@pytest.mark.parametrize("running", [True, False])
async def test_committed_reset_privately_notifies_users_and_mirrors_each_to_each_admin(lifecycle_db, running):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory, running=running)
    await metered_user(factory, old, "20")
    await metered_user(factory, old, "30", user_id=2, device_id=44501)
    async with factory.begin() as session:
        task = await session.get(QuotaTask, 1)
        task.name = "task <owner> & test"
        task.name_normalized = task.name.casefold()
    rt = notification_runtime(factory)
    await rt.reconcile()
    rows = await notices(factory)
    assert len(rows) == 6
    assert all(row.status == "PENDING" for row in rows)
    assert {row.cycle_id for row in rows} == {(await latest_cycle(factory)).id}
    rt.notify.assert_not_called()
    assert await rt.notices.deliver_pending() == 6
    delivered = rt.notify.await_args_list
    assert Counter(call.args[0] for call in delivered) == {1001: 1, 1002: 1, 999: 2, 998: 2}
    for user_id in (1001, 1002):
        user_text = next(call.args[1] for call in delivered if call.args[0] == user_id)
        assert "0" in user_text and "历史" in user_text
        for admin_id in (999, 998):
            mirror = next(call.args[1] for call in delivered if call.args[0] == admin_id and str(user_id) in call.args[1])
            assert user_text in mirror
            assert "&lt;owner&gt;" in mirror and "&amp;" in mirror
            assert "<owner>" not in mirror
    async with factory() as session:
        assert (await session.get(ServiceState, 1)).write_enabled is running
    assert all(row.status == "SENT" for row in await notices(factory))


async def test_failed_user_and_one_admin_retry_independently_after_restart_without_reset(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")

    async def flaky_send(recipient, text):
        if recipient in (1001, 999):
            raise TimeoutError("fake telegram timeout")

    rt = notification_runtime(factory, notify=AsyncMock(side_effect=flaky_send))
    await rt.reconcile()
    generation = (await latest_cycle(factory)).id
    await rt.notices.deliver_pending()
    pending = [row for row in await notices(factory) if row.status == "PENDING"]
    assert Counter(row.recipient_id for row in pending) == {1001: 1, 999: 2}
    assert await rt.notices.deliver_pending() == 0
    restarted = notification_runtime(factory)
    restarted.clock[0] += timedelta(minutes=10)
    await restarted.reconcile()
    assert await restarted.notices.deliver_pending() == 3
    assert Counter(call.args[0] for call in restarted.notify.await_args_list) == {1001: 1, 999: 2}
    assert (await latest_cycle(factory)).id == generation and await cycle_count(factory) == 2
    restarted.gateway.device_usage.assert_not_called()
    await restarted.reconcile()
    assert await restarted.notices.deliver_pending() == 0
    assert len(await notices(factory)) == 6


async def test_returning_account_is_a_new_notification_generation(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = notification_runtime(factory)
    await rt.reconcile()
    await rt.notices.deliver_pending()
    rt.clock[0] += timedelta(minutes=1)
    rt.gateway.accounts.return_value = accounts(7022)
    await rt.reconcile()
    assert await rt.notices.deliver_pending() == 6
    assert len(await notices(factory)) == 12
    assert len({row.cycle_id for row in await notices(factory)}) == 2


async def test_baseline_failure_alerts_admins_once_then_success_notifies_users(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = notification_runtime(factory)
    rt.gateway.device_usage.side_effect = TimeoutError("baseline <unavailable>")
    for _ in range(2):
        with pytest.raises((TimeoutError, EligibilityError)):
            await rt.reconcile()
        await rt.notices.deliver_pending()
    assert Counter(call.args[0] for call in rt.notify.await_args_list) == {999: 1, 998: 1}
    rows = await notices(factory)
    assert len(rows) == 2 and all(row.kind == "ACCOUNT_RESET_FAILURE" for row in rows)
    assert await cycle_count(factory) == 1
    recovered = notification_runtime(factory)
    await recovered.reconcile()
    assert await recovered.notices.deliver_pending() == 6
    assert await cycle_count(factory) == 2


async def test_success_notification_write_failure_rolls_back_cycle_and_all_success_notices(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = notification_runtime(factory)
    queue = rt.notices.queue_reset_success

    async def insert_then_fail(*args, **kwargs):
        await queue(*args, **kwargs)
        raise RuntimeError("simulated transaction failure after queue insertion")

    monkeypatch.setattr(rt.notices, "queue_reset_success", insert_then_fail)
    with pytest.raises(RuntimeError):
        await rt.reconcile()
    assert await cycle_count(factory) == 1
    assert all(row.kind == "ACCOUNT_RESET_FAILURE" for row in await notices(factory))
    async with factory() as session:
        state = await session.get(ServiceState, 1)
        assert state.selected_account_id == "7022" and not state.write_enabled
    await rt.notices.deliver_pending()
    assert {call.args[0] for call in rt.notify.await_args_list} <= {999, 998}


@pytest.mark.parametrize("initialized", [False, True])
async def test_initial_discovery_and_same_account_do_not_notify(lifecycle_db, initialized):
    factory, _ = lifecycle_db
    if initialized:
        await ready_cycle(factory)
    rt = notification_runtime(factory, account_id=7022)
    await rt.reconcile()
    assert await rt.notices.deliver_pending() == 0
    assert await notices(factory) == []
    rt.notify.assert_not_called()


async def test_concurrent_delivery_claims_each_recipient_only_once(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = notification_runtime(factory)
    await rt.reconcile()
    other = DeviceAccountNotificationService(factory, user_notify_callback=rt.notify, clock=lambda: rt.clock[0])
    counts = await asyncio.gather(rt.notices.deliver_pending(), other.deliver_pending())
    assert sum(counts) == 6
    assert Counter(call.args[0] for call in rt.notify.await_args_list) == {1001: 1, 1002: 1, 999: 2, 998: 2}


async def test_ended_user_with_current_period_usage_is_notified_even_outside_member_scope(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    aid, _ = await metered_user(factory, old, "20")
    await end_association(factory, aid, at=NOW + timedelta(minutes=10))
    async with factory.begin() as session:
        (await session.get(DeviceTaskScope, 1)).scope_mode = "ALLOWLIST"
    rt = notification_runtime(factory)
    await rt.reconcile()
    await rt.notices.deliver_pending()
    assert Counter(call.args[0] for call in rt.notify.await_args_list) == {1001: 1, 999: 1, 998: 1}


async def test_concurrent_failure_alerts_are_deduplicated_without_a_user_id(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    rt = notification_runtime(factory)
    other = DeviceAccountNotificationService(factory, admin_ids=[999, 998], clock=lambda: rt.clock[0])
    kwargs = dict(task_id=1, cycle_id=old, generation_key="same-reset-attempt", task_name="task-1",
                  account_id="8123", previous_account_id="7022", error_text="baseline unavailable")
    await asyncio.gather(rt.notices.queue_reset_failure(**kwargs), other.queue_reset_failure(**kwargs))
    assert len(await notices(factory)) == 2
    assert await rt.notices.deliver_pending() == 2
    assert Counter(call.args[0] for call in rt.notify.await_args_list) == {999: 1, 998: 1}


async def test_interrupted_delivery_is_reclaimed_after_restart(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def interrupted_send(recipient, text):
        entered.set()
        await release.wait()

    rt = notification_runtime(factory, notify=AsyncMock(side_effect=interrupted_send))
    await rt.reconcile()
    delivery = asyncio.create_task(rt.notices.deliver_pending())
    await asyncio.wait_for(entered.wait(), timeout=10)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    rows = await notices(factory)
    assert all(row.status == "PENDING" for row in rows)
    restarted = notification_runtime(factory)
    restarted.clock[0] += timedelta(minutes=10)
    assert await restarted.notices.deliver_pending() == 6
    assert await cycle_count(factory) == 2


async def test_slow_batch_does_not_give_later_messages_already_expired_claims(lifecycle_db):
    factory, _ = lifecycle_db
    old = await ready_cycle(factory)
    await metered_user(factory, old, "20")
    rt = notification_runtime(factory)
    await rt.reconcile()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def advancing_send(recipient, text):
        calls.append((recipient, text))
        if len(calls) == 1:
            rt.clock[0] += timedelta(minutes=2)
        elif len(calls) == 2:
            entered.set()
            await release.wait()

    rt.notices.user_notify_callback = advancing_send
    other = DeviceAccountNotificationService(factory, user_notify_callback=advancing_send, clock=lambda: rt.clock[0])
    delivery = asyncio.create_task(rt.notices.deliver_pending())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await other.deliver_pending()
    finally:
        release.set()
        await delivery
    assert len(calls) == 6
    assert len(set(calls)) == 6
