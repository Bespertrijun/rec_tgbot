import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, text

from reclaude_bot.application.device import DeviceAuthorizationService
from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.application.device_sampling import DeviceSamplingService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen
from reclaude_bot.infrastructure.db.models import DeviceAction, DeviceAssociation, DeviceResampleJob, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse
from tests.integration.test_device_admin import record
from tests.integration.test_device_authorization import LINK, STATE, auth_fixture
from tests.integration.test_device_ledger import NOW, association, cycle, end_association
from tests.integration.test_device_metering import job_row, ledger_rows, services
from tests.integration.test_device_usage import associated, usage

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def wired(factory, *, hook=None, **sampling_kwargs):
    _, gateway, quota, clock = auth_fixture(factory)
    gateway.account_id = 7022
    gateway.device_usage = AsyncMock(return_value=usage("25"))
    gateway.list_devices = AsyncMock(return_value=[record()])
    gateway.revoke_device = AsyncMock(return_value=DeviceRevokeResponse(ok=True))
    metering, _, _ = services(factory, clock=clock, gateway=gateway)
    sampling = DeviceSamplingService(factory, metering, 178, clock=lambda: clock[0], **sampling_kwargs)
    auth = DeviceAuthorizationService(factory, gateway, 178, quota, clock=lambda: clock[0],
                                      on_authorized=hook or sampling.after_authorized)
    admin = DeviceAdminService(factory, gateway, 178, quota, clock=lambda: clock[0],
                               on_authorized=hook or sampling.after_authorized)
    revoke = DeviceRevocationService(factory, gateway, 178, clock=lambda: clock[0],
                                     before_revoke=sampling.before_revoke, after_revoked=sampling.after_revoked)
    return SimpleNamespace(auth=auth, admin=admin, revoke=revoke, sampling=sampling,
                           metering=metering, gateway=gateway, clock=clock)


async def jobs(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceResampleJob).order_by(DeviceResampleJob.sequence))).all())


async def test_authorize_now_collects_after_commit_and_replays_initial_job(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)

    async def stats(*args, **kwargs):
        async with factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            row = await session.scalar(select(DeviceAssociation).with_for_update())
            assert row.state == "ACTIVE"
            action = await session.scalar(select(DeviceAction).where(DeviceAction.kind == "AUTH"))
            assert action.status == "SUCCEEDED"
        return usage("25")

    stack.gateway.device_usage.side_effect = stats
    first = await stack.auth.auth(1, LINK)
    assert first.status == "SUCCEEDED"
    assert await stack.auth.auth(1, LINK) == first
    rows = await jobs(factory)
    assert len(rows) == 1 and rows[0].sequence == 0 and rows[0].status == "COMPLETED"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    stack.gateway.device_usage.assert_awaited_once()
    stack.gateway.approve_device_auth.assert_awaited_once()
    stack.gateway.revoke_device.assert_not_called()


async def test_first_sample_failure_keeps_authorization_and_tick_retries(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)
    stack.gateway.device_usage.side_effect = httpx.ReadTimeout("private response")
    result = await stack.auth.auth(1, LINK)
    assert result.status == "SUCCEEDED"
    async with factory() as session:
        assert (await session.get(DeviceAssociation, result.association_id)).state == "ACTIVE"
    row = (await jobs(factory))[0]
    assert row.status == "PENDING"
    assert (await ledger_rows(factory))[0].confirmed_used_usd is None
    stack.clock[0] = row.run_after
    stack.gateway.device_usage.side_effect = None
    assert (await stack.sampling.tick())[0].status == "COMPLETED"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    stack.gateway.approve_device_auth.assert_awaited_once()
    stack.gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("error_type", [RuntimeError, AuthenticationCircuitOpen])
async def test_hook_failure_does_not_mask_success_and_restart_recovers_missing_job(lifecycle_db, caplog, error_type):
    factory, _ = lifecycle_db
    await cycle(factory)
    failed_hook = AsyncMock(side_effect=error_type(f"private {STATE}"))
    stack = wired(factory, hook=failed_hook)
    result = await stack.auth.auth(1, LINK)
    assert result.status == "SUCCEEDED" and await jobs(factory) == []
    assert STATE not in caplog.text
    restarted = DeviceSamplingService(factory, stack.metering, 178, clock=lambda: stack.clock[0])
    await restarted.tick()
    assert (await jobs(factory))[0].status == "COMPLETED"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    stack.gateway.approve_device_auth.assert_awaited_once()


async def test_unknown_approval_never_creates_sampling_job(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    hook = AsyncMock()
    stack = wired(factory, hook=hook)
    stack.gateway.approve_device_auth.side_effect = httpx.ReadTimeout("uncertain")
    assert (await stack.auth.auth(1, LINK)).status == "UNKNOWN"
    hook.assert_not_called()
    await stack.sampling.tick()
    assert await jobs(factory) == []
    stack.gateway.device_usage.assert_not_called()


async def test_admin_association_collects_existing_device_without_claiming_zero_origin(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)
    result = await stack.admin.authuser(1, 44503, 999)
    assert result.status == "SUCCEEDED"
    row = (await ledger_rows(factory))[0]
    assert row.confirmed_used_usd == Decimal("0") and row.quality == "NEEDS_REVIEW"
    assert (await jobs(factory))[0].status == "COMPLETED"
    stack.gateway.approve_device_auth.assert_not_called()
    stack.gateway.revoke_device.assert_not_called()


async def test_revoke_samples_after_reservation_then_schedules_durable_history(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    stack = wired(factory)
    stack.clock[0] += timedelta(minutes=1)
    trace = []

    async def stats(*args, **kwargs):
        async with factory() as session:
            action = await session.scalar(select(DeviceAction).where(DeviceAction.kind == "REVOKE"))
            linked = await session.get(DeviceAssociation, association_id)
            if linked.state == "ENDED":
                assert action.status == "SUCCEEDED"
                trace.append("post")
                return usage("30")
            assert action.status == "PENDING" and action.attempt_count == 0
            trace.append("pre")
            return usage("25")

    async def revoke(device_id):
        trace.append("revoke")
        # The request takes time; pre/post samples must not share a frozen timestamp.
        stack.clock[0] += timedelta(seconds=1)
        return DeviceRevokeResponse(ok=True)

    stack.gateway.device_usage.side_effect = stats
    stack.gateway.revoke_device.side_effect = revoke
    result = await stack.revoke.deauth(1)
    assert result.status == "SUCCEEDED"
    assert trace == ["pre", "revoke"]
    rows = {row.sequence: row for row in await jobs(factory)}
    assert set(rows) == {1, 7}
    assert rows[1].status == "COMPLETED"
    due = stack.clock[0] + timedelta(hours=1)
    assert rows[7].status == "PENDING" and rows[7].run_after == due
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    await stack.sampling.after_revoked(association_id)
    assert len(await jobs(factory)) == 2
    stack.clock[0] = due - timedelta(seconds=1)
    await stack.sampling.tick()
    assert trace == ["pre", "revoke"]
    stack.clock[0] = due
    await stack.sampling.tick()
    assert trace == ["pre", "revoke", "post"]
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("30")
    stack.clock[0] += timedelta(days=2)
    await stack.sampling.tick()
    assert trace == ["pre", "revoke", "post"]


async def test_failed_pre_sample_does_not_block_revoke_or_erase_history(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)
    await stack.auth.auth(1, LINK)
    stack.clock[0] += timedelta(minutes=1)
    stack.gateway.device_usage.side_effect = RuntimeError("usage unavailable")
    result = await stack.revoke.deauth(1)
    assert result.status == "SUCCEEDED"
    stack.gateway.revoke_device.assert_awaited_once()
    async with factory() as session:
        assert (await session.get(DeviceAssociation, result.association_id)).state == "ENDED"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    rows = {row.sequence: row for row in await jobs(factory)}
    assert set(rows) == {0, 1, 7}
    assert rows[1].status == rows[7].status == "PENDING"
    assert rows[7].run_after == stack.clock[0] + timedelta(hours=1)


async def test_timed_out_sampling_cannot_hold_revoke_forever(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    await association(factory)
    stack = wired(factory, operation_timeout_seconds=0.5)
    stack.clock[0] += timedelta(minutes=1)

    async def hung(*args, **kwargs):
        await asyncio.Event().wait()

    stack.gateway.device_usage.side_effect = hung
    result = await asyncio.wait_for(stack.revoke.deauth(1), 5)
    assert result.status == "SUCCEEDED"
    stack.gateway.revoke_device.assert_awaited_once()
    assert any(row.status == "PENDING" for row in await jobs(factory))


async def test_revoke_unknown_waits_for_reconciliation_before_post_schedule(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    await association(factory)
    stack = wired(factory)
    stack.clock[0] += timedelta(minutes=1)
    stack.gateway.revoke_device.side_effect = httpx.ReadTimeout("unknown revoke")
    unknown = await stack.revoke.deauth(1)
    assert unknown.status == "UNKNOWN"
    assert {row.sequence for row in await jobs(factory)} == {1}
    stack.gateway.list_devices.return_value = []
    result = await stack.revoke.reconcile_revoke(unknown.action_id)
    assert result.status == "SUCCEEDED"
    assert {row.sequence for row in await jobs(factory)} == {1, 7}
    stack.gateway.revoke_device.assert_awaited_once()


async def test_recovery_schedules_ended_devices_from_local_history_and_preserves_due_times(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    await end_association(factory, association_id, at=NOW + timedelta(minutes=1))
    stack = wired(factory)
    stack.clock[0] = NOW + timedelta(minutes=30)
    await stack.sampling.tick(limit=1)
    before = {row.sequence: (row.id, row.run_after) for row in await jobs(factory) if row.sequence in range(2, 8)}
    assert len(before) == 1
    future_id, due = before[7]
    assert due == NOW + timedelta(minutes=1, hours=1)
    stack.clock[0] += timedelta(minutes=1)
    restarted = DeviceSamplingService(factory, stack.metering, 178, clock=lambda: stack.clock[0])
    await restarted.tick(limit=1)
    after = await job_row(factory, future_id)
    assert after.run_after == due and after.status == "PENDING"
    stack.gateway.list_devices.assert_not_called()
    stack.gateway.revoke_device.assert_not_called()


async def test_tick_coalesces_active_polling_and_works_while_task_stopped(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)
    await stack.auth.auth(1, LINK)
    await stack.sampling.tick()
    assert len(await jobs(factory)) == 1
    stack.clock[0] += timedelta(seconds=299)
    await stack.sampling.tick()
    assert stack.gateway.device_usage.await_count == 1
    stack.clock[0] += timedelta(seconds=1)
    await asyncio.wait_for(asyncio.gather(stack.sampling.tick(), stack.sampling.tick()), 20)
    rows = await jobs(factory)
    assert len(rows) == 2 and rows[-1].sequence >= 8
    assert rows[-1].status == "COMPLETED"
    assert stack.gateway.device_usage.await_count == 2
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_pending_first_sample_coalesces_polling_instead_of_growing_queue(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    stack = wired(factory)
    stack.gateway.device_usage.side_effect = RuntimeError("unavailable")
    await stack.auth.auth(1, LINK)
    for _ in range(3):
        stack.clock[0] += timedelta(minutes=1)
        await stack.sampling.tick()
    assert len(await jobs(factory)) == 1
    assert (await jobs(factory))[0].status == "PENDING"
    stack.gateway.revoke_device.assert_not_called()


async def test_tick_does_not_sample_other_org_or_unknown_auth(lifecycle_db):
    factory, _ = lifecycle_db
    await associated(factory, user_id=1, org_id=179, task_id=2, device_id=44502)
    await association(factory, user_id=2, auth_status="UNKNOWN")
    stack = wired(factory)
    await stack.sampling.tick()
    assert await jobs(factory) == []
    stack.gateway.device_usage.assert_not_called()


async def test_tick_keeps_consumption_for_banned_user_and_propagates_circuit(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    await association(factory)
    async with factory.begin() as session:
        (await session.get(User, 1)).status = "BANNED"
    stack = wired(factory)
    stack.gateway.device_usage.side_effect = AuthenticationCircuitOpen("private")
    with pytest.raises(AuthenticationCircuitOpen):
        await stack.sampling.tick()
    assert (await jobs(factory))[0].status == "PENDING"
    assert (await ledger_rows(factory))[0].confirmed_used_usd is None


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_callback_exception_does_not_change_successful_revocation(lifecycle_db, stage, caplog):
    factory, _ = lifecycle_db
    await association(factory)
    hook = AsyncMock(side_effect=RuntimeError("private-callback-error"))
    gateway = SimpleNamespace(revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)))
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW + timedelta(minutes=1),
                                      before_revoke=hook if stage == "before" else None,
                                      after_revoked=hook if stage == "after" else None)
    result = await service.deauth(1)
    assert result.status == "SUCCEEDED"
    hook.assert_awaited_once_with(result.association_id)
    gateway.revoke_device.assert_awaited_once_with(44500)
    async with factory() as session:
        assert (await session.get(DeviceAssociation, result.association_id)).state == "ENDED"
    assert "private-callback-error" not in caplog.text


async def test_failed_admin_sampling_hook_preserves_committed_association(lifecycle_db):
    factory, _ = lifecycle_db
    stack = wired(factory, hook=AsyncMock(side_effect=RuntimeError("failed enqueue")))
    result = await stack.admin.authuser(1, 44503, 999)
    assert result.status == "SUCCEEDED"
    async with factory() as session:
        assert (await session.get(DeviceAssociation, result.association_id)).state == "ACTIVE"
    stack.gateway.approve_device_auth.assert_not_called()
    stack.gateway.revoke_device.assert_not_called()


async def test_cancel_after_approval_preserves_device_and_tick_recovers_sampling(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    entered = asyncio.Event()

    async def interrupted_hook(association_id):
        entered.set()
        await asyncio.Event().wait()

    stack = wired(factory, hook=interrupted_hook)
    task = asyncio.create_task(stack.auth.auth(1, LINK))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
    async with factory() as session:
        linked = await session.scalar(select(DeviceAssociation))
        assert linked.state == "ACTIVE"
        assert (await session.scalar(select(DeviceAction).where(DeviceAction.kind == "AUTH"))).status == "SUCCEEDED"
    await stack.sampling.tick()
    assert (await jobs(factory))[0].status == "COMPLETED"
    stack.gateway.approve_device_auth.assert_awaited_once()
    stack.gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("completed_final", [False, True])
async def test_old_revoke_queue_is_reduced_without_replaying_completed_history(lifecycle_db, completed_final):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    ended_at = NOW + timedelta(minutes=1)
    await end_association(factory, association_id, at=ended_at)
    stack = wired(factory)
    for sequence, delay in zip(range(2, 8), (0, 60, 300, 1800, 7200, 86400), strict=True):
        await stack.metering.enqueue(association_id, sequence=sequence, run_after=ended_at + timedelta(seconds=delay))
    async with factory.begin() as session:
        rows = list((await session.scalars(select(DeviceResampleJob))).all())
        for row in rows:
            if row.sequence == 2 or (row.sequence == 7 and completed_final):
                row.status = "COMPLETED"
                row.completed_at = ended_at
    stack.clock[0] = ended_at + timedelta(minutes=30)
    await stack.sampling.tick()
    stack.gateway.device_usage.assert_not_called()
    rows = {row.sequence: row for row in await jobs(factory)}
    assert set(rows) == set(range(2, 8))
    assert rows[2].status == "COMPLETED"
    assert all(rows[n].status == "CANCELLED" for n in range(3, 7))
    assert rows[7].status == ("COMPLETED" if completed_final else "PENDING")
    if not completed_final:
        assert rows[7].run_after == ended_at + timedelta(hours=1)
    stack.clock[0] = ended_at + timedelta(hours=1)
    await stack.sampling.tick()
    assert stack.gateway.device_usage.await_count == (0 if completed_final else 1)


async def test_hour_followup_failure_retries_same_job_without_resetting_retry_time(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    ended_at = NOW + timedelta(minutes=1)
    await end_association(factory, association_id, at=ended_at)
    stack = wired(factory)
    await stack.sampling.after_revoked(association_id)
    stack.clock[0] = ended_at + timedelta(hours=1)
    stack.gateway.device_usage.side_effect = RuntimeError("unavailable")
    await stack.sampling.tick()
    row = (await jobs(factory))[0]
    assert row.sequence == 7 and row.status == "PENDING" and row.attempt_count == 1
    due = row.run_after
    calls = stack.gateway.device_usage.await_count
    await stack.sampling.tick()
    assert stack.gateway.device_usage.await_count == calls
    assert (await jobs(factory))[0].run_after == due
    stack.clock[0] = due
    stack.gateway.device_usage.side_effect = None
    await stack.sampling.tick()
    rows = await jobs(factory)
    assert len(rows) == 1 and rows[0].status == "COMPLETED"
