from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.jobs.scheduler import BackgroundJobs


def setup_jobs():
    old_quota = SimpleNamespace(ensure_cycle=AsyncMock(side_effect=AssertionError("legacy quota called")),
                                sync_members=AsyncMock(side_effect=AssertionError("legacy members called")))
    old_actions = SimpleNamespace(reconcile_cached=AsyncMock(side_effect=AssertionError("legacy actions called")))
    task = SimpleNamespace(sync_enabled=AsyncMock(return_value=True), stop=AsyncMock(return_value=True))
    cycle = SimpleNamespace(sync=AsyncMock())
    sampling = SimpleNamespace(tick=AsyncMock(return_value=("sample",)), poll_seconds=300)
    actions = SimpleNamespace(run_once=AsyncMock(return_value=2))
    jobs = BackgroundJobs(old_quota, old_actions, task_service=task, device_cycle=cycle,
                          device_sampling=sampling, device_actions=actions)
    return jobs, task, cycle, sampling, actions


async def test_device_tick_orders_cycle_sampling_actions_without_legacy_calls():
    jobs, _, cycle, sampling, actions = setup_jobs()
    calls = []

    async def sync():
        calls.append("cycle")

    async def sample():
        calls.append("sampling")
        return ("sample",)

    async def act(**kwargs):
        calls.append("actions")
        return 2

    cycle.sync.side_effect = sync
    sampling.tick.side_effect = sample
    actions.run_once.side_effect = act
    assert await jobs.run_tick() == 3
    assert calls == ["cycle", "sampling", "actions"]
    assert jobs.status()["last_tick_error"] is None


async def test_stoptask_preserves_statistics_tick():
    jobs, task, cycle, sampling, _ = setup_jobs()
    await jobs.stop_quota_task("default", 999)
    task.stop.assert_awaited_once_with("default", 999)
    await jobs.run_tick()
    cycle.sync.assert_awaited_once()
    sampling.tick.assert_awaited_once()


async def test_disabled_statistics_do_not_run_device_tick():
    jobs, task, cycle, sampling, actions = setup_jobs()
    task.sync_enabled.return_value = False
    assert await jobs.run_tick() == 0
    cycle.sync.assert_not_called()
    sampling.tick.assert_not_called()
    actions.run_once.assert_not_called()


async def test_account_reconcile_precedes_cycle_sampling_and_actions():
    jobs, _, cycle, sampling, actions = setup_jobs()
    calls = []
    jobs.device_account_reconcile = SimpleNamespace(
        is_configured=AsyncMock(return_value=True), reconcile=AsyncMock(side_effect=lambda: calls.append("account")),
    )
    cycle.sync.side_effect = lambda: calls.append("cycle")
    sampling.tick.side_effect = lambda: calls.append("sampling") or ()
    actions.run_once.side_effect = lambda **kwargs: calls.append("actions") or 0
    await jobs.run_tick()
    assert calls == ["account", "cycle", "sampling", "actions"]


@pytest.mark.parametrize("failure", [TimeoutError("account lookup failed"), EligibilityError("reset pending")])
async def test_failed_account_reconcile_never_samples_or_enforces_old_account(failure):
    jobs, _, cycle, sampling, actions = setup_jobs()
    jobs.device_account_reconcile = SimpleNamespace(is_configured=AsyncMock(return_value=True), reconcile=AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await jobs.run_tick()
    cycle.sync.assert_not_called()
    sampling.tick.assert_not_called()
    actions.run_once.assert_not_called()


async def test_authentication_circuit_stops_rest_of_tick():
    jobs, _, cycle, sampling, actions = setup_jobs()
    cycle.sync.side_effect = AuthenticationCircuitOpen("authentication unavailable")
    with pytest.raises(AuthenticationCircuitOpen):
        await jobs.run_tick()
    sampling.tick.assert_not_called()
    actions.run_once.assert_not_called()
    assert jobs.status()["last_tick_error"] is not None


async def test_sampling_circuit_does_not_run_actions():
    jobs, _, _, sampling, actions = setup_jobs()
    sampling.tick.side_effect = AuthenticationCircuitOpen("authentication unavailable")
    with pytest.raises(AuthenticationCircuitOpen):
        await jobs.run_tick()
    actions.run_once.assert_not_called()
    assert jobs.status()["last_tick_error"] is not None


@pytest.mark.parametrize("phase", ["cycle", "sampling"])
async def test_tick_can_resume_after_authentication_is_restored(phase):
    jobs, _, cycle, sampling, actions = setup_jobs()
    operation = cycle.sync if phase == "cycle" else sampling.tick
    operation.side_effect = AuthenticationCircuitOpen("authentication unavailable")
    with pytest.raises(AuthenticationCircuitOpen):
        await jobs.run_tick()
    operation.side_effect = None
    assert await jobs.run_tick() == 3
    actions.run_once.assert_awaited_once()
    assert jobs.status()["last_tick_error"] is None


@pytest.mark.parametrize("phase", ["cycle", "sampling"])
async def test_ordinary_phase_failure_preserves_existing_continuation(phase):
    jobs, _, cycle, sampling, actions = setup_jobs()
    operation = cycle.sync if phase == "cycle" else sampling.tick
    operation.side_effect = TimeoutError("request attempts exhausted")
    assert await jobs.run_tick() == (3 if phase == "cycle" else 2)
    operation.assert_awaited_once()
    sampling.tick.assert_awaited_once()
    actions.run_once.assert_awaited_once()


@pytest.mark.parametrize("provided", ["cycle", "sampling", "actions"])
def test_partial_device_runtime_is_rejected_instead_of_legacy_fallback(provided):
    kwargs = {"device_" + provided: SimpleNamespace()}
    with pytest.raises(ValueError):
        BackgroundJobs(SimpleNamespace(), SimpleNamespace(), **kwargs)


@pytest.mark.parametrize("device_mode,expected_interval", [(True, 300), (False, 60)])
async def test_loop_waits_full_interval_and_remains_stoppable(monkeypatch, device_mode, expected_interval):
    jobs = setup_jobs()[0] if device_mode else BackgroundJobs(SimpleNamespace(), SimpleNamespace())
    tick = AsyncMock(return_value=0)
    monkeypatch.setattr(jobs, "_run_tick", tick)
    intervals = []

    async def wait(awaitable, *, timeout):
        intervals.append(timeout)
        jobs._quota_stop.set()
        return await awaitable

    monkeypatch.setattr("reclaude_bot.jobs.scheduler.asyncio.wait_for", wait)
    await jobs._loop()
    tick.assert_awaited_once()
    assert intervals == [expected_interval]


@pytest.mark.parametrize("reset_fails", [False, True])
async def test_account_notices_deliver_even_when_reset_fails_or_task_is_stopped(reset_fails):
    jobs, task, _, _, _ = setup_jobs()
    await jobs.stop_quota_task("default", 999)
    jobs.device_account_notifications = SimpleNamespace(deliver_pending=AsyncMock(return_value=1))
    jobs.device_account_reconcile = SimpleNamespace(
        is_configured=AsyncMock(return_value=True),
        reconcile=AsyncMock(side_effect=EligibilityError("baseline unavailable") if reset_fails else None),
    )
    if reset_fails:
        with pytest.raises(EligibilityError):
            await jobs.run_tick()
    else:
        await jobs.run_tick()
    jobs.device_account_notifications.deliver_pending.assert_awaited_once()


async def test_notification_transport_failure_does_not_fail_successful_statistics():
    jobs, _, _, _, _ = setup_jobs()
    jobs.device_account_notifications = SimpleNamespace(deliver_pending=AsyncMock(side_effect=RuntimeError("notice transport unavailable")))
    assert await jobs.run_tick() == 3
    assert jobs.status()["last_tick_error"] is None
