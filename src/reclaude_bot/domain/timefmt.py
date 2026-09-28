from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def format_beijing(value: datetime) -> str:
    """Format a datetime for user display in Beijing time (UTC+8, no DST)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return f"{value.astimezone(BEIJING_TZ):%Y-%m-%d %H:%M:%S}（北京时间）"
