from __future__ import annotations

from datetime import UTC, datetime

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.quota import QuotaService


async def poll_once(quota: QuotaService, actions: QuotaActionService, *, now: datetime | None = None) -> int:
    gate = getattr(actions, "gate", None)
    if gate is not None and not await gate.is_task_enabled():
        return 0
    moment = now or datetime.now(UTC)
    await quota.ensure_cycle(now=moment)
    await quota.maybe_check_last_day(now=moment)
    count = await quota.sync_members(now=moment)
    await actions.reconcile_cached(now=moment)
    return count
