import asyncio
from datetime import UTC, datetime

import pytest

from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.domain.errors import BindingError


async def seed_members(app_context, now: datetime) -> BindingService:
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)
    return BindingService(factory, gateway)


@pytest.mark.asyncio
async def test_bind_uses_local_member_cache_without_network(app_context):
    now = datetime(2026, 8, 18, tzinfo=UTC)
    service = await seed_members(app_context, now)
    _, gateway, _ = app_context
    calls_before = gateway.members_calls
    row = await service.bind(100, "ONE@example.com")
    assert row.reclaude_user_id == "u-1"
    assert gateway.members_calls == calls_before
    with pytest.raises(BindingError):
        await service.bind(101, "one@example.com")


@pytest.mark.asyncio
async def test_bind_requires_private_chat_and_cache(app_context):
    factory, gateway, settings = app_context
    service = BindingService(factory, gateway)
    with pytest.raises(BindingError, match="私聊"):
        await service.bind(100, "one@example.com", private_chat=False)
    with pytest.raises(BindingError, match="首次成员同步"):
        await service.bind(100, "one@example.com")


@pytest.mark.asyncio
async def test_unbound_user_can_rebind_and_first_claim_has_one_winner(app_context):
    now = datetime(2026, 8, 18, tzinfo=UTC)
    service = await seed_members(app_context, now)
    first = await service.bind(102, "one@example.com")
    await service.unbind(102, operator_telegram_id=1)
    rebound = await service.bind(102, "ONE@example.com")
    assert rebound.id == first.id
    await service.unbind(102, operator_telegram_id=1)
    results = await asyncio.gather(
        service.bind(110, "one@example.com"),
        service.bind(111, "one@example.com"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, BindingError) for item in results) == 2
