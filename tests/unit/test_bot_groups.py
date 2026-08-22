from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetChat, GetChatMember
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberUpdated,
    Message,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.application.groups import GroupService, GroupSnapshot
from reclaude_bot.bot.groups import TelegramGroupGateway, build_group_router
from reclaude_bot.bot.middleware import GroupAccessMiddleware
from reclaude_bot.domain.enums import ManagedGroupStatus
from reclaude_bot.infrastructure.db.base import Base


@pytest_asyncio.fixture
async def app_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory, None, None
    await engine.dispose()


class MutableGroupGateway:
    def __init__(self, snapshot: GroupSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.fail = False

    async def get_group_snapshot(self, chat_id: int) -> GroupSnapshot:
        self.calls += 1
        if self.fail:
            raise RuntimeError("gateway unavailable")
        return self.snapshot


def admin_member(user_id: int = 999) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=User(id=user_id, is_bot=True, first_name="bot"),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=True,
        can_promote_members=True,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=True,
        can_edit_stories=True,
        can_delete_stories=True,
    )


def member_update(chat_id: int, old_member, new_member) -> ChatMemberUpdated:
    return ChatMemberUpdated(
        chat=Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Managed"),
        from_user=User(id=7, is_bot=False, first_name="owner"),
        date=datetime.now(UTC),
        old_chat_member=old_member,
        new_chat_member=new_member,
    )


def message(chat_type: ChatType, text: str | None = "/status", caption: str | None = None) -> Message:
    value = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1001 if chat_type != ChatType.PRIVATE else 7, type=chat_type, title="Managed" if chat_type != ChatType.PRIVATE else None),
        from_user=User(id=7, is_bot=False, first_name="owner"),
        text=text,
        caption=caption,
    )
    object.__setattr__(value, "answer", AsyncMock())
    return value


@pytest.mark.asyncio
async def test_telegram_gateway_maps_member_permissions_to_json_safe_snapshot() -> None:
    bot = AsyncMock()
    bot.id = 999
    bot.get_chat.return_value = Chat(id=-1001, type=ChatType.SUPERGROUP, title="Managed")
    bot.get_chat_member.return_value = admin_member()

    result = await TelegramGroupGateway(bot).get_group_snapshot(-1001)

    bot.get_chat_member.assert_awaited_once_with(-1001, 999)
    assert result.is_bot_member is True
    assert result.is_bot_administrator is True
    assert result.can_restrict_members is True
    json.dumps(result.as_dict())


@pytest.mark.asyncio
async def test_telegram_gateway_maps_forbidden_but_propagates_transient_and_unknown_errors() -> None:
    bot = AsyncMock()
    bot.id = 999
    bot.get_chat.return_value = Chat(id=-1001, type=ChatType.SUPERGROUP, title="Managed")
    bot.get_chat_member.side_effect = TelegramForbiddenError(GetChatMember(chat_id=-1001, user_id=999), "Forbidden")
    unavailable = await TelegramGroupGateway(bot).get_group_snapshot(-1001)
    assert unavailable.is_bot_member is False
    assert unavailable.chat_type == ChatType.SUPERGROUP.value

    errors = [
        TelegramRetryAfter(GetChatMember(chat_id=-1001, user_id=999), "retry", 1),
        TelegramNetworkError(GetChatMember(chat_id=-1001, user_id=999), "network"),
        TelegramServerError(GetChatMember(chat_id=-1001, user_id=999), "server"),
        TelegramBadRequest(GetChatMember(chat_id=-1001, user_id=999), "Bad Request: malformed request"),
    ]
    for error in errors:
        bot.get_chat_member.side_effect = error
        with pytest.raises(type(error)):
            await TelegramGroupGateway(bot).get_group_snapshot(-1001)

    bot.get_chat.side_effect = TelegramBadRequest(GetChat(chat_id=-1001), "Bad Request: chat not found")
    missing = await TelegramGroupGateway(bot).get_group_snapshot(-1001)
    assert missing.is_bot_member is False


@pytest.mark.asyncio
async def test_my_chat_member_uses_event_snapshot_and_notifies_for_permission_loss(app_context) -> None:
    factory, _, _ = app_context
    gateway = MutableGroupGateway(
        GroupSnapshot(
            chat_id=-1001,
            title="Managed",
            is_bot_member=True,
            is_bot_administrator=True,
            can_restrict_members=True,
            chat_type="supergroup",
        )
    )
    groups = GroupService(factory, gateway, [7])
    router = build_group_router([7])
    handler = router.my_chat_member.handlers[0].callback
    bot = AsyncMock()

    await handler(
        update=member_update(-1001, ChatMemberLeft(user=User(id=999, is_bot=True, first_name="bot")), admin_member()),
        bot=bot,
        groups=groups,
    )
    assert gateway.calls == 0
    row = await groups.get_status(-1001)
    assert row is not None and row.status == ManagedGroupStatus.PENDING.value
    assert bot.send_message.await_count == 1

    await groups.approve(-1001, 7)
    gateway.fail = True
    removed = ChatMemberLeft(user=User(id=999, is_bot=True, first_name="bot"))
    await handler(update=member_update(-1001, admin_member(), removed), bot=bot, groups=groups)

    assert gateway.calls == 1
    row = await groups.get_status(-1001)
    assert row is not None and row.status == ManagedGroupStatus.DISABLED.value
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_my_chat_member_readd_is_detected_and_duplicate_event_is_not_re_notified(app_context) -> None:
    factory, _, _ = app_context
    event_snapshot = GroupSnapshot(
        chat_id=-1001,
        title="Managed",
        is_bot_member=True,
        is_bot_administrator=False,
        can_restrict_members=False,
        chat_type="supergroup",
    )
    gateway = MutableGroupGateway(event_snapshot)
    groups = GroupService(factory, gateway, [7])
    router = build_group_router([7])
    handler = router.my_chat_member.handlers[0].callback
    bot = AsyncMock()
    left = ChatMemberLeft(user=User(id=999, is_bot=True, first_name="bot"))

    await handler(update=member_update(-1001, left, admin_member()), bot=bot, groups=groups)
    await groups.reject(-1001, 7)
    await handler(update=member_update(-1001, left, admin_member()), bot=bot, groups=groups)
    row = await groups.get_status(-1001)
    assert row is not None and row.status == ManagedGroupStatus.PENDING.value
    await handler(update=member_update(-1001, left, admin_member()), bot=bot, groups=groups)

    assert bot.send_message.await_count == 2
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_group_callback_requires_private_chat_and_strict_data() -> None:
    router = build_group_router([7])
    filters = router.callback_query.handlers[0].filters
    assert filters is not None and len(filters) == 1
    handler = router.callback_query.handlers[0].callback
    private_message = message(ChatType.PRIVATE)
    object.__setattr__(private_message, "edit_text", AsyncMock())
    callback = CallbackQuery(
        id="callback-1",
        from_user=User(id=7, is_bot=False, first_name="owner"),
        chat_instance="instance",
        message=private_message,
        data="group:approve:-1001",
    )
    callback_answer = AsyncMock()
    object.__setattr__(callback, "answer", callback_answer)
    row = SimpleNamespace(chat_id=-1001, title="Managed", status=ManagedGroupStatus.ACTIVE.value)
    groups = SimpleNamespace(approve=AsyncMock(return_value=row))

    await handler(callback=callback, groups=groups)
    groups.approve.assert_awaited_once_with(-1001, 7)
    callback_answer.assert_awaited_once()

    group_message = message(ChatType.SUPERGROUP)
    group_callback = CallbackQuery(
        id="callback-2",
        from_user=User(id=7, is_bot=False, first_name="owner"),
        chat_instance="instance",
        message=group_message,
        data="group:approve:-1001",
    )
    group_callback_answer = AsyncMock()
    object.__setattr__(group_callback, "answer", group_callback_answer)
    await handler(callback=group_callback, groups=groups)
    assert groups.approve.await_count == 1
    group_callback_answer.assert_awaited_once()

    invalid_callback = CallbackQuery(
        id="callback-3",
        from_user=User(id=7, is_bot=False, first_name="owner"),
        chat_instance="instance",
        message=private_message,
        data="group:approve:-1001:extra",
    )
    invalid_callback_answer = AsyncMock()
    object.__setattr__(invalid_callback, "answer", invalid_callback_answer)
    await handler(callback=invalid_callback, groups=groups)
    invalid_callback_answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_access_middleware_blocks_inactive_groups_and_leaves_private_chat_alone() -> None:
    middleware = GroupAccessMiddleware()
    group_message = message(ChatType.SUPERGROUP)
    group_answer = AsyncMock()
    object.__setattr__(group_message, "answer", group_answer)
    groups = SimpleNamespace(is_active=AsyncMock(return_value=False))
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, group_message, {"groups": groups})
    assert result is None
    handler.assert_not_awaited()
    group_answer.assert_awaited_once()

    private_message = message(ChatType.PRIVATE)
    groups.is_active.reset_mock()
    result = await middleware(handler, private_message, {"groups": groups})
    assert result == "handled"
    groups.is_active.assert_not_awaited()

    caption_message = message(ChatType.SUPERGROUP, text=None, caption="/status")
    caption_answer = AsyncMock()
    object.__setattr__(caption_message, "answer", caption_answer)
    result = await middleware(handler, caption_message, {"groups": groups})
    assert result is None
    groups.is_active.assert_awaited_once_with(-1001)


def test_main_wires_group_router_and_explicit_update_types() -> None:
    source = Path("src/reclaude_bot/main.py").read_text()
    assert "TelegramGroupGateway" in source
    assert "build_group_router" in source
    assert "dp[\"groups\"] = groups" in source
    assert "dp.resolve_used_update_types()" in source
