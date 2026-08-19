from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reclaude_bot.domain.quota import baseline_is_timely, cycle_used, is_last_24h


def test_cycle_used_uses_decimal_baseline_and_adjustments() -> None:
    assert cycle_used("712.1697491500", "12.1697491500", ["1.25"]) == Decimal("701.25")
    assert cycle_used("1", "2") == Decimal("0.00")


def test_quota_boundary_and_last_day() -> None:
    reset = datetime(2026, 8, 25, 14, 30, tzinfo=UTC)
    assert not is_last_24h(reset - timedelta(hours=24, seconds=1), reset)
    assert is_last_24h(reset - timedelta(hours=24), reset)
    assert not is_last_24h(reset, reset)


def test_baseline_window_is_measured_from_cycle_start() -> None:
    start = datetime(2026, 8, 18, tzinfo=UTC)
    assert baseline_is_timely(start, start, timedelta(minutes=1))
    assert not baseline_is_timely(start + timedelta(minutes=1, seconds=1), start, timedelta(minutes=1))
