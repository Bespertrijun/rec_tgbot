from datetime import UTC, datetime

import pytest

from reclaude_bot.application.quota import QuotaService
from reclaude_bot.infrastructure.reclaude.models import Member


@pytest.mark.asyncio
async def test_list_upstream_members_returns_all_members_sorted_by_email(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)

    await quota.sync_cycle_from_me(now=now)
    gateway.member_rows = {
        "u-2": Member(user_id="u-2", email="Zed@example.com", account_id=None, total_usage_usd="2"),
        "u-3": Member(user_id="u-3", email="alice@example.com", account_id=None, total_usage_usd="3"),
        "u-1": Member(user_id="u-1", email="Bob@example.com", account_id=None, total_usage_usd="1"),
    }
    await quota.sync_members(now=now)

    rows = await quota.list_upstream_members()

    assert [(row.email, row.reclaude_user_id) for row in rows] == [
        ("alice@example.com", "u-3"),
        ("Bob@example.com", "u-1"),
        ("Zed@example.com", "u-2"),
    ]
    assert all(row.sampled_at.replace(tzinfo=UTC) == now for row in rows)


@pytest.mark.asyncio
async def test_list_upstream_members_returns_empty_list_before_sync(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)

    assert await quota.list_upstream_members() == []
