from __future__ import annotations

from datetime import UTC, datetime

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService


async def poll_once(quota: QuotaService, actions: QuotaActionService, *, now: datetime | None = None) -> int:
    # Usage sync always runs; enforcement writes gate themselves on the write
    # latch and each task's RUNNING coverage inside reconcile_cached.
    moment = now or datetime.now(UTC)
    await quota.ensure_cycle(now=moment)
    await quota.maybe_check_last_day(now=moment)
    count = await quota.sync_members(now=moment)
    await actions.reconcile_cached(now=moment)
    return count
