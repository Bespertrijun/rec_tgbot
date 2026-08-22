from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.bot.groups import build_group_router
from reclaude_bot.domain.enums import GroupMembershipState, ManagedGroupStatus
from reclaude_bot.infrastructure.db.base import Base
from reclaude_bot.infrastructure.db.models import ManagedGroup
from reclaude_bot.jobs.onboarding import OnboardingWorker


@pytest_asyncio.fixture
async def worker_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    async with factory() as session:
        async with session.begin():
            session.add(
                ManagedGroup(
                    chat_id=-1001,
                    title="<Managed>",
                    status=ManagedGroupStatus.ACTIVE.value,
                    created_at=now,
                    updated_at=now,
                )
            )
    yield factory, now
    await engine.dispose()


class FakeGateway:
    def __init__(self, bot: AsyncMock) -> None:
        self.bot = bot
        self.restrict_member = AsyncMock(return_value=True)
        self.restore_member = AsyncMock(return_value=True)
        self.remove_member = AsyncMock(return_value=True)
        self.bot_username = AsyncMock(return_value="test_bot")


@pytest.mark.asyncio
async def test_join_restricts_and_sends_escaped_verification_button(worker_context):
    factory, now = worker_context
    bot = AsyncMock()
    gateway = FakeGateway(bot)
    titles = SimpleNamespace(get_status=AsyncMock(return_value=SimpleNamespace(title="<Managed>")))
    worker = OnboardingWorker(OnboardingService(factory), gateway, bot, group_titles=titles)

    assert await worker.handle_join(-1001, 42, joined_at=now, display_name="A <B>")

    membership = await OnboardingService(factory).get_membership(-1001, 42)
    assert membership is not None
    assert membership.state == GroupMembershipState.MUTED.value
    gateway.restrict_member.assert_awaited_once_with(-1001, 42)
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.args[1]
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert "&lt;Managed&gt;" in text
    assert "A &lt;B&gt;" in text
    assert markup.inline_keyboard[0][0].url.startswith("https://t.me/test_bot?start=verify_")


@pytest.mark.asyncio
async def test_timeout_uses_unban_without_ban(worker_context):
    factory, now = worker_context
    bot = AsyncMock()
    gateway = FakeGateway(bot)
    service = OnboardingService(factory)
    worker = OnboardingWorker(service, gateway, bot)
    await service.begin_join(-1001, 42, real_join=True, joined_at=now - timedelta(minutes=5))

    assert await worker.run_once(now=now)
    gateway.remove_member.assert_awaited_once_with(-1001, 42)
    bot.ban_chat_member.assert_not_awaited()
    membership = await service.get_membership(-1001, 42)
    assert membership is not None and membership.state == GroupMembershipState.REMOVED.value


@pytest.mark.asyncio
async def test_chat_member_router_uses_worker_for_real_transitions(worker_context):
    factory, now = worker_context
    worker = AsyncMock()
    onboarding = OnboardingService(factory)
    router = build_group_router([7])
    handler = router.chat_member.handlers[0].callback
    target = User(id=42, is_bot=False, first_name="Member")
    update = ChatMemberUpdated(
        chat=Chat(id=-1001, type=ChatType.SUPERGROUP, title="Managed"),
        from_user=User(id=7, is_bot=False, first_name="owner"),
        date=now,
        old_chat_member=ChatMemberLeft(user=target),
        new_chat_member=ChatMemberMember(user=target),
    )

    await handler(update=update, onboarding=onboarding, onboarding_worker=worker)
    worker.handle_join.assert_awaited_once()
    assert worker.handle_join.await_args.kwargs["display_name"] == "Member"
