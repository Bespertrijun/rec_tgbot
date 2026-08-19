from __future__ import annotations

import asyncio
from uuid import uuid4

import structlog

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.jobs.usage_poll import poll_once

log = structlog.get_logger(__name__)


class BackgroundJobs:
    def __init__(self, quota: QuotaService, actions: QuotaActionService) -> None:
        self.quota = quota
        self.actions = actions
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            job_run_id = str(uuid4())
            try:
                log.info("job_started", event="quota_tick", job_run_id=job_run_id)
                result = await poll_once(self.quota, self.actions)
                log.info("job_finished", event="quota_tick", job_run_id=job_run_id, result=result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("job_failed", event="quota_tick", job_run_id=job_run_id, error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except TimeoutError:
                pass

    async def run_tick(self) -> int:
        return await poll_once(self.quota, self.actions)
