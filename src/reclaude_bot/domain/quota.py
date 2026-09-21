from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0.00")
DEFAULT_QUOTA_LIMIT = Decimal("700.00")


def as_decimal(value: Decimal | str | int | float) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc


def cycle_used(
    latest_total_usage_usd: Decimal | str | int | float,
    baseline_total_usd: Decimal | str | int | float,
    adjustments: list[Decimal | str | int | float] | tuple[Decimal | str | int | float, ...] = (),
) -> Decimal:
    raw = as_decimal(latest_total_usage_usd) - as_decimal(baseline_total_usd)
    adjusted = raw + sum((as_decimal(value) for value in adjustments), ZERO)
    return max(ZERO, adjusted)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_last_24h(now: datetime, reset_at: datetime) -> bool:
    now_utc = ensure_utc(now)
    reset_utc = ensure_utc(reset_at)
    return reset_utc - timedelta(hours=24) <= now_utc < reset_utc


def baseline_is_timely(captured_at: datetime, cycle_started_at: datetime, max_delay: timedelta) -> bool:
    captured = ensure_utc(captured_at)
    started = ensure_utc(cycle_started_at)
    return captured >= started and captured - started <= max_delay


def estimate_window_total(
    used_usd: Decimal | str | int | float | None,
    utilization: Decimal | str | int | float | None,
) -> Decimal | None:
    """Estimate a window's total dollar budget from consumed dollars and utilization percent."""

    if used_usd is None or utilization is None:
        return None
    used = as_decimal(used_usd)
    percent = as_decimal(utilization)
    if used <= ZERO or percent <= ZERO:
        return None
    return used * Decimal("100") / percent
