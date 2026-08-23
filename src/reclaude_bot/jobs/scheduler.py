from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.task import QuotaTaskService
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
    ) -> None:
        self.quota = quota
        self.actions = actions
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
            if await self.task_service.is_enabled():
                await self.resume_quota_task()
        elif self.gate is None:
            # Keep the lightweight constructor useful for isolated scheduler tests.
            await self.start_quota_task(persist=False)

    async def stop(self) -> None:
        self._shutdown.set()
        await self._cancel_quota_loop()
        if self.onboarding is not None:
            await self.onboarding.stop()

    async def start_quota_task(self, operator_id: int | None = None, *, persist: bool = True) -> bool:
        """Start at most one quota loop, after its durable transition has succeeded."""

        if self.task_service is not None and persist:
            changed = await self.task_service.start(operator_id)
        else:
            changed = self._task is None or self._task.done()
            if self.task_service is not None and not persist:
                if not await self.task_service.is_enabled():
                    return False
                await self.task_service.enable_latch()
        if self._task is not None and not self._task.done():
            return changed
        self._quota_stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("quota_task_started", operator_id=operator_id, persisted=persist)
        return changed

    async def resume_quota_task(self) -> bool:
        if self.task_service is not None:
            if not await self.task_service.is_enabled():
                return False
            await self.task_service.enable_latch()
        elif self.gate is not None:
            if not await self.gate.is_task_enabled():
                return False
            await self.gate.enable_latch()
        return await self.start_quota_task(persist=False)

    async def stop_quota_task(self, operator_id: int | None = None) -> bool:
        if self.task_service is not None:
            changed = await self.task_service.stop(operator_id)
        elif self.gate is not None:
            await self.gate.force_stop("quota_task_stopped")
            changed = True
        else:
            changed = self._task is not None and not self._task.done()
        await self._cancel_quota_loop()
        log.info("quota_task_stopped", operator_id=operator_id)
        return changed

    async def run_tick(self) -> int:
        if not await self._task_is_enabled():
            return 0
        return await self._run_tick()

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
        while not self._shutdown.is_set() and not self._quota_stop.is_set():
            if not await self._task_is_enabled():
                return
            try:
                await self._run_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # _run_tick records and logs failures; keep the 60-second loop alive.
                pass
            try:
                await asyncio.wait_for(self._quota_stop.wait(), timeout=60)
            except TimeoutError:
                pass

    async def _run_tick(self) -> int:
        if not await self._task_is_enabled():
            return 0
        job_run_id = str(uuid4())
        started = datetime.now(UTC)
        self.last_tick_started = started
        log.info("quota_task_tick_started", job_run_id=job_run_id)
        try:
            result = await poll_once(self.quota, self.actions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_tick_error = str(exc)
            log.exception("quota_task_tick_failed", job_run_id=job_run_id, error=str(exc))
            raise
        self.last_tick_finished = datetime.now(UTC)
        self.last_tick_error = None
        self.last_result_count = result
        log.info("quota_task_tick_finished", job_run_id=job_run_id, result=result)
        return result

    async def _task_is_enabled(self) -> bool:
        if self.task_service is not None:
            return await self.task_service.is_enabled()
        if self.gate is not None:
            return await self.gate.is_task_enabled()
        return True

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
