from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reclaude_bot.domain.quota import baseline_is_timely, cycle_used, is_last_24h, project_window_utilization


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


def test_project_window_utilization_linear_burn_rate() -> None:
    reset = datetime(2026, 8, 25, tzinfo=UTC)
    window = timedelta(days=7)
    # Half the window elapsed at 6% → projected 12%.
    assert project_window_utilization(Decimal("6"), reset, window, reset - timedelta(days=3, hours=12)) == Decimal("12")
    # 5h window: 50% with 2.5h elapsed → projected exactly 100%.
    five_hour_reset = datetime(2026, 8, 21, 2, 30, tzinfo=UTC)
    assert project_window_utilization(Decimal("50"), five_hour_reset, timedelta(hours=5), datetime(2026, 8, 21, tzinfo=UTC)) == Decimal("100")


def test_project_window_utilization_unknown_when_window_not_active_or_stale() -> None:
    reset = datetime(2026, 8, 25, tzinfo=UTC)
    window = timedelta(days=7)
    assert project_window_utilization(Decimal("6"), None, window, reset) is None
    # Window has not started yet (future-dated reset) or the snapshot is stale.
    assert project_window_utilization(Decimal("6"), reset + window, window, reset) is None
    assert project_window_utilization(Decimal("6"), reset, window, reset) is None
    assert project_window_utilization(Decimal("6"), reset, window, reset - window) is None
