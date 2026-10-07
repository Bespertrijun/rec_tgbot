from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from reclaude_bot.application.actions import DeviceQuotaActionService, QuotaActionService
from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.application.device_account_reconcile import DeviceAccountReconcileService
from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_sampling import DeviceSamplingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen
from reclaude_bot.jobs.onboarding import OnboardingWorker
from reclaude_bot.jobs.usage_poll import poll_once

log = structlog.get_logger(__name__)


class BackgroundJobs:
    def __init__(
        self,
        quota: QuotaService,
        actions: QuotaActionService,
        onboarding: OnboardingWorker | None = None,
        task_service: QuotaTaskService | None = None,
        *,
        device_cycle: DeviceCycleService | None = None,
        device_sampling: DeviceSamplingService | None = None,
        device_actions: DeviceQuotaActionService | None = None,
        device_account_reconcile: DeviceAccountReconcileService | None = None,
        device_account_notifications: DeviceAccountNotificationService | None = None,
        device_account_usage: DeviceAccountUsageService | None = None,
    ) -> None:
        device_services = (device_cycle, device_sampling, device_actions)
        if any(service is not None for service in device_services) and not all(
            service is not None for service in device_services
        ):
            raise ValueError("device cycle, sampling, and action services must be configured together")
        self.quota = quota
        self.actions = actions
        self.device_cycle = device_cycle
        self.device_sampling = device_sampling
        self.device_actions = device_actions
        self.device_account_reconcile = device_account_reconcile
        self.device_account_notifications = device_account_notifications
        self.device_account_usage = device_account_usage
        self.onboarding = onboarding
        self.task_service = task_service
        self.gate = getattr(actions, "gate", None)
        self._shutdown = asyncio.Event()
        self._quota_stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.started_at: datetime | None = None
        self.last_tick_started: datetime | None = None
        self.last_tick_finished: datetime | None = None
        self.last_tick_error: str | None = None
        self.last_result_count: int | None = None

    async def start(self, *, start_quota: bool = True) -> None:
        self._shutdown.clear()
        self.started_at = datetime.now(UTC)
        if self.onboarding is not None:
            await self.onboarding.start()
        if not start_quota:
            return
        if self.task_service is not None:
            # The loop syncs usage data even while every task is STOPPED.
            await self.resume_quota_task()
        elif self.gate is None:
            # Keep the lightweight constructor useful for isolated scheduler tests.
            await self.start_quota_task(persist=False)

    async def stop(self) -> None:
        self._shutdown.set()
        await self._cancel_quota_loop()
        if self.onboarding is not None:
            await self.onboarding.stop()

    async def start_quota_task(self, name: str | None = None, operator_id: int | None = None, *, persist: bool = True) -> bool:
        """Start at most one shared quota loop, after its durable transition has succeeded.

        The loop keeps syncing usage data while every task is STOPPED; enforcement
        writes gate on the write latch and each task's RUNNING coverage instead.
        While usage sync is paused the durable transition still happens, but no
        loop runs — ``loop_running`` then honestly reports the statistics state.
        """

        if self.task_service is not None and persist:
            if self.device_account_reconcile is not None and await self.device_account_reconcile.is_configured(name):
                await self.device_account_reconcile.reconcile(name, operator_id=operator_id)
            changed = await self.task_service.start(name, operator_id)
        else:
            changed = self._task is None or self._task.done()
        if self.task_service is not None and not await self.task_service.sync_enabled():
            return changed
        if self._task is not None and not self._task.done():
            return changed
        self._quota_stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("quota_task_started", task=name, operator_id=operator_id, persisted=persist)
        return changed

    async def resume_quota_task(self) -> bool:
        if self.task_service is not None:
            if not await self.task_service.sync_enabled():
                return False
            if self.device_account_reconcile is not None and await self.device_account_reconcile.is_configured():
                try:
                    await self.device_account_reconcile.reconcile()
                except Exception as exc:
                    # Keep the loop available for a retry; enable_latch below
                    # is guarded by the durable reconcile failure reason.
                    log.warning("device_account_reconcile_startup_failed", error_type=type(exc).__name__)
            # Re-opens the write latch only while a task is RUNNING; the loop starts regardless.
            await self.task_service.enable_latch()
        elif self.gate is not None:
            if not await self.gate.is_enabled():
                return False
        return await self.start_quota_task(persist=False)

    async def start_usage_sync(self, operator_id: int | None = None) -> bool:
        """Open the durable usage-sync switch and start the statistics loop."""

        if self.task_service is None:
            return await self.start_quota_task(persist=False)
        changed = await self.task_service.set_sync_enabled(True, operator_id)
        await self.start_quota_task(persist=False)
        log.info("usage_sync_started", operator_id=operator_id)
        return changed

    async def stop_usage_sync(self, operator_id: int | None = None) -> bool:
        """Close the durable usage-sync switch and stop the statistics loop.

        Task states and the write latch are left untouched; enforcement already
        blocks itself once member snapshots go stale.
        """

        if self.task_service is not None:
            changed = await self.task_service.set_sync_enabled(False, operator_id)
        else:
            changed = self._task is not None and not self._task.done()
        await self._cancel_quota_loop()
        log.info("usage_sync_stopped", operator_id=operator_id)
        return changed

    async def stop_quota_task(self, name: str | None = None, operator_id: int | None = None) -> bool:
        if self.task_service is not None:
            # Only enforcement stops: the shared loop keeps syncing usage data while
            # the write latch closes once no task remains RUNNING.
            changed = await self.task_service.stop(name, operator_id)
            log.info("quota_task_stopped", task=name, operator_id=operator_id)
            return changed
        if self.gate is not None:
            await self.gate.force_stop("quota_task_stopped")
            changed = True
        else:
            changed = self._task is not None and not self._task.done()
        await self._cancel_quota_loop()
        log.info("quota_task_stopped", task=name, operator_id=operator_id)
        return changed

    async def run_tick(self, *, now: datetime | None = None) -> int:
        return await self._run_tick(now=now)

    def status(self) -> dict[str, datetime | str | int | None]:
        return {
            "started_at": self.started_at,
            "last_tick_started": self.last_tick_started,
            "last_tick_finished": self.last_tick_finished,
            "last_tick_error": self.last_tick_error,
            "last_result_count": self.last_result_count,
            "loop_running": self._task is not None and not self._task.done(),
        }

    async def _loop(self) -> None:
        poll_interval = self.device_sampling.poll_seconds if self.device_sampling is not None else 60
        while not self._shutdown.is_set() and not self._quota_stop.is_set():
            try:
                await self._run_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # _run_tick records and logs failures; keep the loop alive.
                pass
            try:
                await asyncio.wait_for(self._quota_stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    async def _run_tick(self, *, now: datetime | None = None) -> int:
        if self.task_service is not None and not await self.task_service.sync_enabled():
            return 0
        job_run_id = str(uuid4())
        started = datetime.now(UTC)
        self.last_tick_started = started
        log.info("quota_task_tick_started", job_run_id=job_run_id)
        try:
            if self.device_cycle is None or self.device_sampling is None or self.device_actions is None:
                result = await poll_once(self.quota, self.actions, now=now)
            else:
                if self.device_account_reconcile is not None and await self.device_account_reconcile.is_configured():
                    # Account replacement is a safety boundary.  Let the
                    # exception escape before sampling or quota actions can
                    # continue against stale cycle evidence.
                    await self.device_account_reconcile.reconcile()
                cycle = None
                try:
                    cycle = await self.device_cycle.sync()
                except AuthenticationCircuitOpen:
                    raise
                except Exception as exc:
                    log.warning(
                        "device_cycle_sync_failed",
                        error_type=type(exc).__name__,
                        authentication_circuit=isinstance(exc, AuthenticationCircuitOpen),
                    )
                try:
                    sampled = await self.device_sampling.tick()
                except AuthenticationCircuitOpen:
                    raise
                except Exception as exc:
                    sampled = ()
                    log.warning(
                        "device_sampling_tick_failed",
                        error_type=type(exc).__name__,
                        authentication_circuit=isinstance(exc, AuthenticationCircuitOpen),
                    )
                else:
                    if (cycle is not None and self.device_account_usage is not None
                            and not any(getattr(item, "status", None) == "PENDING" for item in sampled)):
                        try:
                            await self.device_account_usage.record_estimate(cycle.task_id, expected_cycle_id=cycle.id)
                        except AuthenticationCircuitOpen:
                            raise
                        except Exception as exc:
                            log.warning("device_estimate_record_failed", error_type=type(exc).__name__)
                actions = await self.device_actions.run_once(now=now)
                result = len(sampled) + actions
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_tick_error = str(exc)
            log.exception("quota_task_tick_failed", job_run_id=job_run_id, error=str(exc))
            raise
        finally:
            if self.device_account_notifications is not None:
                try:
                    await self.device_account_notifications.deliver_pending(now=now)
                except Exception as exc:
                    log.warning("device_account_notification_delivery_failed", error_type=type(exc).__name__)
        self.last_tick_finished = datetime.now(UTC)
        self.last_tick_error = None
        self.last_result_count = result
        log.info("quota_task_tick_finished", job_run_id=job_run_id, result=result)
        return result

    async def _cancel_quota_loop(self) -> None:
        self._quota_stop.set()
        task = self._task
        if task is not None:
            try:
                # Let an in-flight remote action reach its confirmation boundary first.
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = None
