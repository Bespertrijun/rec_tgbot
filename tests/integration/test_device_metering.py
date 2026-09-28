import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_metering import DeviceMeteringService
from reclaude_bot.application.device_usage import DeviceUsageCollector
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceResampleJob,
    DeviceUsageSnapshot,
)
from tests.integration.test_device_ledger import NOW, association, cycle, end_association
from tests.integration.test_device_usage import associated, usage

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def services(factory, *, clock=None, gateway=None, ledger=None, **kwargs):
    clock = clock if clock is not None else [NOW + timedelta(minutes=1)]
    gateway = gateway if gateway is not None else SimpleNamespace(
        account_id=7022, device_usage=AsyncMock(return_value=usage("25")),
        approve_device_auth=AsyncMock(), revoke_device=AsyncMock(),
    )
    collector = DeviceUsageCollector(factory, gateway, 178, clock=lambda: clock[0])
    ledger = ledger if ledger is not None else DeviceLedgerService(factory, 178, clock=lambda: clock[0])
    service = DeviceMeteringService(factory, collector, ledger, 178, clock=lambda: clock[0], **kwargs)
    return service, gateway, clock


async def job_row(factory, job_id):
    async with factory() as session:
        return await session.get(DeviceResampleJob, job_id)


async def ledger_rows(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceCycleLedger).order_by(DeviceCycleLedger.id))).all())


async def test_enqueue_and_run_persist_exact_usage_without_device_actions(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    assert await service.enqueue(association_id, sequence=0, run_after=clock[0] + timedelta(days=1)) == job_id
    result = await service.run_job(job_id)
    assert result.status == "COMPLETED" and result.snapshot_id is not None
    row = await job_row(factory, job_id)
    assert row.status == "COMPLETED" and row.attempt_count == 1 and row.completed_at == clock[0]
    ledgers = await ledger_rows(factory)
    assert len(ledgers) == 1 and ledgers[0].cycle_id == cycle_id
    assert ledgers[0].confirmed_used_usd == Decimal("25") and ledgers[0].quality == "VERIFIED"
    assert (await service.run_job(job_id)).status == "COMPLETED"
    assert await service.enqueue(association_id, sequence=0) == job_id
    gateway.device_usage.assert_awaited_once_with(44500, 178, range="all")
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


async def test_failed_first_sample_remains_active_and_restarts_from_durable_job(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    gateway.device_usage.side_effect = RuntimeError("secret-response-do-not-save")
    job_id = await service.enqueue(association_id, sequence=0)
    result = await service.run_job(job_id)
    assert result.status == "PENDING" and result.snapshot_id is None
    row = await job_row(factory, job_id)
    assert row.run_after > clock[0] and "secret" not in row.last_error_code
    ledgers = await ledger_rows(factory)
    assert ledgers[0].confirmed_used_usd is None and ledgers[0].quality == "UNKNOWN"
    async with factory() as session:
        assert (await session.get(DeviceAssociation, association_id)).state == "ACTIVE"
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSnapshot)) == 0
    assert await service.run_due() == ()
    clock[0] = row.run_after
    gateway.device_usage.side_effect = None
    restarted, _, _ = services(factory, clock=clock, gateway=gateway)
    assert (await restarted.run_due())[0].status == "COMPLETED"
    assert (await job_row(factory, job_id)).attempt_count == 2
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_apply_failure_reuses_snapshot_after_restart_without_second_get(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    clock = [NOW + timedelta(minutes=1)]
    real = DeviceLedgerService(factory, 178, clock=lambda: clock[0])
    broken = SimpleNamespace(ensure=real.ensure, apply=AsyncMock(side_effect=RuntimeError("db-failure-secret")))
    service, gateway, _ = services(factory, clock=clock, ledger=broken)
    job_id = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(job_id)).status == "PENDING"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSnapshot)) == 1
    clock[0] = (await job_row(factory, job_id)).run_after
    restarted, _, _ = services(factory, clock=clock, gateway=gateway)
    assert (await restarted.run_job(job_id)).status == "COMPLETED"
    gateway.device_usage.assert_awaited_once()
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_concurrent_enqueue_and_execution_claim_one_durable_job(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, _ = services(factory)
    ids = await asyncio.wait_for(asyncio.gather(*(service.enqueue(association_id, sequence=0) for _ in range(3))), 20)
    assert len(set(ids)) == 1
    await asyncio.wait_for(asyncio.gather(*(service.run_job(ids[0]) for _ in range(3))), 20)
    assert (await job_row(factory, ids[0])).status == "COMPLETED"
    assert (await job_row(factory, ids[0])).attempt_count == 1
    gateway.device_usage.assert_awaited_once()
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_cancellation_releases_job_only_after_lease_and_restart_retries(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    entered = asyncio.Event()

    async def hung(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    gateway.device_usage.side_effect = hung
    task = asyncio.create_task(service.run_job(job_id))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        row = await job_row(factory, job_id)
        assert row.attempt_count == 1 and row.run_after > clock[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
    assert (await service.run_job(job_id)).status == "PENDING"
    assert gateway.device_usage.await_count == 1
    clock[0] = row.run_after
    gateway.device_usage.side_effect = None
    restarted, _, _ = services(factory, clock=clock, gateway=gateway)
    assert (await restarted.run_job(job_id)).status == "COMPLETED"
    assert (await job_row(factory, job_id)).attempt_count == 2


async def test_timeout_is_durable_and_never_revokes_device(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, _ = services(factory, timeout_seconds=0.5, lease_seconds=5)
    job_id = await service.enqueue(association_id, sequence=0)

    async def hung(*args, **kwargs):
        await asyncio.Event().wait()

    gateway.device_usage.side_effect = hung
    result = await asyncio.wait_for(service.run_job(job_id), 5)
    assert result.status == "PENDING"
    assert (await job_row(factory, job_id)).last_error_code
    assert (await ledger_rows(factory))[0].confirmed_used_usd is None
    gateway.revoke_device.assert_not_called()


async def test_auth_circuit_records_retry_and_stops_batch(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    first = await service.enqueue(association_id, sequence=0)
    second = await service.enqueue(association_id, sequence=1)
    gateway.device_usage.side_effect = AuthenticationCircuitOpen("secret")
    with pytest.raises(AuthenticationCircuitOpen):
        await service.run_due()
    row = await job_row(factory, first)
    assert row.status == "PENDING" and row.run_after > clock[0]
    assert row.last_error_code and "secret" not in row.last_error_code
    assert (await job_row(factory, second)).attempt_count == 0
    gateway.device_usage.assert_awaited_once()


@pytest.mark.parametrize("status", [None, "NEEDS_REVIEW"])
async def test_missing_or_unverified_cycle_stays_pending_then_recovers(lifecycle_db, status):
    factory, _ = lifecycle_db
    cycle_id = await cycle(factory, status=status) if status else None
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(job_id)).status == "PENDING"
    gateway.device_usage.assert_not_called()
    if cycle_id:
        async with factory.begin() as session:
            (await session.get(DeviceQuotaCycle, cycle_id)).status = "VERIFIED"
    else:
        await cycle(factory)
    clock[0] = (await job_row(factory, job_id)).run_after
    assert (await service.run_job(job_id)).status == "COMPLETED"


async def test_historical_revoked_device_is_sampled_without_upstream_device_list(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(minutes=30)
    await cycle(factory, reset=reset, status="EXPIRED")
    association_id = await association(factory)
    await end_association(factory, association_id, at=NOW + timedelta(minutes=10))
    service, gateway, _ = services(factory, clock=[reset + timedelta(hours=1)])
    job_id = await service.enqueue(association_id, sequence=1)
    assert (await service.run_job(job_id)).status == "COMPLETED"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    gateway.device_usage.assert_awaited_once_with(44500, 178, range="all")


async def test_cross_cycle_sample_preserves_old_amount_and_marks_boundary(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(minutes=30)
    old_cycle = await cycle(factory, reset=reset)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    first_job = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(first_job)).status == "COMPLETED"
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, old_cycle)).status = "EXPIRED"
    new_cycle = await cycle(factory, started=reset, reset=reset + timedelta(days=7))
    clock[0] = reset + timedelta(minutes=1)
    gateway.device_usage.return_value = usage("50")
    second_job = await service.enqueue(association_id, sequence=1)
    assert (await service.run_job(second_job)).status == "COMPLETED"
    rows = {row.cycle_id: row for row in await ledger_rows(factory)}
    assert rows[old_cycle].confirmed_used_usd == Decimal("25")
    assert rows[old_cycle].quality == "NEEDS_REVIEW"
    assert rows[new_cycle].confirmed_used_usd == Decimal("0") and rows[new_cycle].quality == "NEEDS_REVIEW"


@pytest.mark.parametrize("case", ["unknown_auth", "other_org", "missing"])
async def test_enqueue_rejects_unconfirmed_or_foreign_identity(lifecycle_db, case):
    factory, _ = lifecycle_db
    association_id = await association(factory, auth_status="UNKNOWN" if case == "unknown_auth" else "SUCCEEDED")
    if case == "other_org":
        async with factory.begin() as session:
            row = await session.get(DeviceAssociation, association_id)
            row.org_id, row.task_id, row.device_id = 179, 2, 44502
            action = await session.scalar(select(DeviceAction).where(DeviceAction.association_id == association_id))
            action.target_device_id = 44502
    service, gateway, _ = services(factory)
    with pytest.raises(EligibilityError):
        await service.enqueue(999 if case == "missing" else association_id, sequence=0)
    gateway.device_usage.assert_not_called()


async def test_due_batch_is_bounded_org_scoped_and_respects_future_jobs(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    first = await service.enqueue(association_id, sequence=0)
    second = await service.enqueue(association_id, sequence=1)
    future = await service.enqueue(association_id, sequence=2, run_after=clock[0] + timedelta(days=1))
    foreign_id = await associated(factory, user_id=2, device_id=44502, org_id=179, task_id=2)
    async with factory.begin() as session:
        foreign = await session.get(DeviceAssociation, foreign_id)
        foreign.org_id, foreign.task_id = 179, 2
        job = DeviceResampleJob(association_id=foreign_id, sequence=0, run_after=clock[0], status="PENDING",
                                attempt_count=0, created_at=clock[0], updated_at=clock[0])
        session.add(job)
        await session.flush()
        foreign_job = job.id
    results = await service.run_due(limit=1)
    assert len(results) == 1 and results[0].job_id == first
    assert (await job_row(factory, second)).attempt_count == 0
    assert (await job_row(factory, future)).attempt_count == 0
    assert (await job_row(factory, foreign_job)).attempt_count == 0
    with pytest.raises(EligibilityError):
        await service.run_job(foreign_job)
    gateway.device_usage.assert_awaited_once()


async def test_expired_worker_failure_cannot_overwrite_new_worker_completion(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("late-old-worker-error")
        return usage("25")

    gateway.device_usage.side_effect = delayed
    old_worker = asyncio.create_task(service.run_job(job_id))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        clock[0] = (await job_row(factory, job_id)).run_after
        restarted, _, _ = services(factory, clock=clock, gateway=gateway)
        assert (await restarted.run_job(job_id)).status == "COMPLETED"
        release.set()
        await asyncio.wait_for(old_worker, 10)
    finally:
        release.set()
        if not old_worker.done():
            old_worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old_worker
    row = await job_row(factory, job_id)
    assert row.status == "COMPLETED" and row.attempt_count == 2
    assert row.last_error_code is None
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_pre_reset_saved_snapshot_is_not_applied_to_new_cycle_on_retry(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(minutes=30)
    await cycle(factory, reset=reset)
    association_id = await association(factory)
    clock = [NOW + timedelta(minutes=1)]
    real = DeviceLedgerService(factory, 178, clock=lambda: clock[0])
    broken = SimpleNamespace(ensure=real.ensure, apply=AsyncMock(side_effect=RuntimeError("interrupted")))
    service, gateway, _ = services(factory, clock=clock, ledger=broken)
    job_id = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(job_id)).status == "PENDING"
    new_cycle = await cycle(factory, started=reset, reset=reset + timedelta(days=7))
    clock[0] = reset + timedelta(minutes=1)
    restarted, _, _ = services(factory, clock=clock, gateway=gateway)
    assert (await restarted.run_job(job_id)).status == "COMPLETED"
    gateway.device_usage.assert_awaited_once()
    async with factory() as session:
        new_ledger = await session.scalar(select(DeviceCycleLedger).where(DeviceCycleLedger.cycle_id == new_cycle))
        assert new_ledger is None or new_ledger.confirmed_used_usd is None


async def test_partial_cycle_apply_is_replayed_without_double_charging(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(minutes=30)
    old = await cycle(factory, reset=reset)
    new = await cycle(factory, started=reset, reset=reset + timedelta(days=7))
    association_id = await association(factory)
    clock = [reset + timedelta(minutes=1)]
    real = DeviceLedgerService(factory, 178, clock=lambda: clock[0])
    failed = False

    async def fail_once(association_id, cycle_id, snapshot_id):
        nonlocal failed
        result = await real.apply(association_id, cycle_id, snapshot_id)
        if cycle_id == old and not failed:
            failed = True
            raise RuntimeError("response lost after commit")
        return result

    wrapper = SimpleNamespace(ensure=real.ensure, apply=fail_once)
    service, gateway, _ = services(factory, clock=clock, ledger=wrapper)
    job_id = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(job_id)).status == "PENDING"
    clock[0] = (await job_row(factory, job_id)).run_after
    assert (await service.run_job(job_id)).status == "COMPLETED"
    rows = {row.cycle_id: row for row in await ledger_rows(factory)}
    assert rows[old].confirmed_used_usd is None and rows[old].quality == "NEEDS_REVIEW"
    assert rows[new].confirmed_used_usd == Decimal("0") and rows[new].quality == "NEEDS_REVIEW"
    gateway.device_usage.assert_awaited_once()


@pytest.mark.parametrize("kwargs", [{"sequence": -1}, {"sequence": True}, {"sequence": 0, "run_after": NOW.replace(tzinfo=None)}])
async def test_invalid_enqueue_parameters_create_no_jobs(lifecycle_db, kwargs):
    factory, _ = lifecycle_db
    association_id = await association(factory)
    service, gateway, _ = services(factory)
    with pytest.raises(EligibilityError):
        await service.enqueue(association_id, **kwargs)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceResampleJob)) == 0
    gateway.device_usage.assert_not_called()


async def test_cancelled_job_remains_terminal_without_network(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    async with factory.begin() as session:
        job = await session.get(DeviceResampleJob, job_id)
        job.status, job.completed_at = "CANCELLED", clock[0]
    assert (await service.run_job(job_id)).status == "CANCELLED"
    assert await service.enqueue(association_id, sequence=0) == job_id
    assert await service.run_due() == ()
    gateway.device_usage.assert_not_called()


async def test_regressed_usage_completes_processing_without_clearing_confirmed_spend(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    initial = await service.enqueue(association_id, sequence=0)
    assert (await service.run_job(initial)).status == "COMPLETED"
    clock[0] += timedelta(minutes=1)
    gateway.device_usage.return_value = usage("0")
    regressed = await service.enqueue(association_id, sequence=1)
    assert (await service.run_job(regressed)).status == "COMPLETED"
    ledger = (await ledger_rows(factory))[0]
    assert ledger.confirmed_used_usd == Decimal("25") and ledger.quality == "NEEDS_REVIEW"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DeviceUsageSnapshot)) == 2


@pytest.mark.parametrize("limit", [0, -1, True])
async def test_invalid_batch_limit_is_rejected(lifecycle_db, limit):
    factory, _ = lifecycle_db
    service, gateway, _ = services(factory)
    with pytest.raises(EligibilityError):
        await service.run_due(limit=limit)
    gateway.device_usage.assert_not_called()


async def test_completion_write_failure_replays_saved_snapshot_and_ledger(lifecycle_db):
    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    service, gateway, clock = services(factory)
    job_id = await service.enqueue(association_id, sequence=0)
    failed = False

    def fail_completion(session, flush_context, instances):
        nonlocal failed
        if not failed and any(isinstance(row, DeviceResampleJob) and row.status == "COMPLETED" for row in session.dirty):
            failed = True
            raise RuntimeError("completion commit failed")

    event.listen(Session, "before_flush", fail_completion)
    try:
        try:
            await service.run_job(job_id)
        except RuntimeError:
            pass
    finally:
        event.remove(Session, "before_flush", fail_completion)
    assert failed, "fault injection must intercept the completion write"
    row = await job_row(factory, job_id)
    assert row.status == "PENDING"
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")
    clock[0] = row.run_after
    restarted, _, _ = services(factory, clock=clock, gateway=gateway)
    assert (await restarted.run_job(job_id)).status == "COMPLETED"
    gateway.device_usage.assert_awaited_once()
    assert (await ledger_rows(factory))[0].confirmed_used_usd == Decimal("25")


async def test_three_http_attempts_log_failure_and_leave_job_for_next_cron(lifecycle_db):
    import httpx
    from structlog.testing import capture_logs

    from tests.unit.test_device_api import client_for

    factory, _ = lifecycle_db
    await cycle(factory)
    association_id = await association(factory)
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-test-token", request=request)

    with capture_logs() as logs:
        async with client_for(fail, max_retries=2) as gateway:
            gateway.account_id = 7022
            service, _, _ = services(factory, gateway=gateway)
            job_id = await service.enqueue(association_id, sequence=0)
            result = await service.run_job(job_id)
    assert len(requests) == 3
    assert result.status == "PENDING"
    assert (await job_row(factory, job_id)).last_error_code
    assert (await ledger_rows(factory))[0].confirmed_used_usd is None
    assert any(row.get("log_level") in {"warning", "error"} for row in logs)
    assert "private-test-token" not in repr(logs)
