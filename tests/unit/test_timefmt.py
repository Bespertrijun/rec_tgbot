from datetime import UTC, datetime

from reclaude_bot.domain.timefmt import format_beijing


def test_format_beijing_converts_utc_to_beijing_time() -> None:
    assert format_beijing(datetime(2026, 8, 18, 1, 0, 0, tzinfo=UTC)) == "2026-08-18 09:00:00（北京时间）"


def test_format_beijing_treats_naive_datetime_as_utc() -> None:
    assert format_beijing(datetime(2026, 8, 18, 16, 30, 0)) == "2026-08-19 00:30:00（北京时间）"
