from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message

from reclaude_bot.bot.autodelete import AutoDeleteBot

_TOKEN = "123456789:test-token"
_SENT_AT = datetime(2026, 9, 22, tzinfo=UTC)


def _sent_message(chat: Chat, message_id: int = 10, reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    return Message(message_id=message_id, date=_SENT_AT, chat=chat, reply_markup=reply_markup)


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch):
    delete_message = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "delete_message", delete_message)
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=0.01)
    yield SimpleNamespace(bot=bot, delete_message=delete_message)
    await bot.session.close()


@pytest.mark.asyncio
async def test_group_message_is_deleted_after_delay(harness, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.SUPERGROUP)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))

    message = await harness.bot.send_message(chat.id, "验证提醒")
    await asyncio.sleep(0.05)

    assert message.chat.id == chat.id
    harness.delete_message.assert_awaited_once_with(chat.id, message.message_id)


@pytest.mark.asyncio
async def test_private_message_is_not_deleted(harness, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=7, type=ChatType.PRIVATE)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))

    await harness.bot.send_message(chat.id, "私聊回复")
    await asyncio.sleep(0.05)

    harness.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_message_uses_longer_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.SUPERGROUP)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="验证并绑定", url="https://t.me/x")]])
    plain = _sent_message(chat, message_id=10)
    with_button = _sent_message(chat, message_id=11, reply_markup=markup)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(side_effect=[plain, with_button]))
    delete_message = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "delete_message", delete_message)
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=10.0, button_delete_delay=0.01)
    try:
        await bot.send_message(chat.id, "普通消息")
        await bot.send_message(chat.id, "带按钮消息", reply_markup=markup)
        await asyncio.sleep(0.05)

        delete_message.assert_awaited_once_with(chat.id, with_button.message_id)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed(harness, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.GROUP)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))
    harness.delete_message.side_effect = RuntimeError("message to delete not found")

    await harness.bot.send_message(chat.id, "验证提醒")
    await asyncio.sleep(0.05)

    harness.delete_message.assert_awaited_once_with(chat.id, 10)


def test_delay_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AutoDeleteBot(_TOKEN, auto_delete_delay=0)


@pytest.mark.asyncio
async def test_private_message_is_mirrored_to_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=7, type=ChatType.PRIVATE, first_name="User")
    send_message = AsyncMock(return_value=_sent_message(chat))
    monkeypatch.setattr(Bot, "send_message", send_message)
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=0.01, admin_ids=(1, 2))
    try:
        await bot.send_message(chat.id, "验证成功")

        assert send_message.await_count == 3
        mirror_calls = send_message.await_args_list[1:]
        assert [call.args[0] for call in mirror_calls] == [1, 2]
        for call in mirror_calls:
            assert "私聊镜像" in call.args[1]
            assert "User（7）" in call.args[1]
            assert "验证成功" in call.args[1]
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_message_to_admin_is_not_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=1, type=ChatType.PRIVATE, first_name="Admin")
    send_message = AsyncMock(return_value=_sent_message(chat))
    monkeypatch.setattr(Bot, "send_message", send_message)
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=0.01, admin_ids=(1, 2))
    try:
        await bot.send_message(chat.id, "管理员通知")

        send_message.assert_awaited_once_with(chat.id, "管理员通知")
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_mirror_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=7, type=ChatType.PRIVATE, first_name="User")
    send_message = AsyncMock(side_effect=[_sent_message(chat), RuntimeError("admin blocked the bot"), _sent_message(chat)])
    monkeypatch.setattr(Bot, "send_message", send_message)
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=0.01, admin_ids=(1, 2))
    try:
        message = await bot.send_message(chat.id, "验证成功")

        assert message.chat.id == chat.id
        assert send_message.await_count == 3
    finally:
        await bot.session.close()
