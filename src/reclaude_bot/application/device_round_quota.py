from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from reclaude_bot.infrastructure.db.models import DeviceQuotaCycle, QuotaTask


def apply_round_quota(task: QuotaTask, previous: DeviceQuotaCycle | None, cycle: DeviceQuotaCycle, now: datetime) -> None:
    """Apply once, inside the caller's locked round-creation transaction."""
    estimate = previous.estimated_total_usd if previous is not None else None
    cycle.quota_source_cycle_id = previous.id if previous is not None else None
    cycle.quota_reason = "NO_VALID_ESTIMATE"
    if estimate is not None and estimate.is_finite() and Decimal("0") < estimate < Decimal("100000000"):
        amount = (max(Decimal("0"), estimate - Decimal("100")) / Decimal("4")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        task.limit_usd = amount
        task.updated_at = now
        task.updated_by = None
        cycle.quota_source_estimate_usd = estimate
        cycle.quota_reason = "PREVIOUS_ROUND_ESTIMATE"
    cycle.opening_limit_usd = task.limit_usd


def round_quota_summary(cycle: DeviceQuotaCycle) -> dict[str, object]:
    return {
        "opening_limit_usd": str(cycle.opening_limit_usd) if cycle.opening_limit_usd is not None else None,
        "quota_source_cycle_id": cycle.quota_source_cycle_id,
        "quota_source_estimate_usd": str(cycle.quota_source_estimate_usd) if cycle.quota_source_estimate_usd is not None else None,
        "quota_reason": cycle.quota_reason,
    }
