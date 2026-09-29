from datetime import UTC, datetime, timedelta, timezone

import pytest

from reclaude_bot.domain.quota import same_cycle_reset


@pytest.mark.parametrize("offset,expected", [
    (timedelta(0), True),
    (timedelta(microseconds=999999), True),
    (timedelta(seconds=1), True),
    (timedelta(minutes=5), True),
    (timedelta(minutes=5, microseconds=1), False),
    (timedelta(seconds=301), False),
    (timedelta(days=7), False),
])
def test_tolerance_is_symmetric_and_includes_exactly_five_minutes(offset, expected):
    reset = datetime(2026, 10, 3, 10, tzinfo=UTC)
    assert same_cycle_reset(reset, reset + offset) is expected
    assert same_cycle_reset(reset + offset, reset) is expected


def test_tolerance_compares_instants_across_timezones_and_naive_utc():
    utc_reset = datetime(2026, 10, 3, 10, tzinfo=UTC)
    beijing_reset = datetime(2026, 10, 3, 18, 5, tzinfo=timezone(timedelta(hours=8)))
    assert same_cycle_reset(utc_reset, beijing_reset)
    assert same_cycle_reset(utc_reset.replace(tzinfo=None), beijing_reset)
    assert not same_cycle_reset(utc_reset, beijing_reset + timedelta(microseconds=1))
