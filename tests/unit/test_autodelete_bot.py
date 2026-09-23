from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
async def bot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Bot, "delete_message", AsyncMock(return_value=True))
    instance = AutoDeleteBot(_TOKEN, auto_delete_delay=0.01)
    yield instance
    await instance.session.close()


@pytest.mark.asyncio
async def test_group_message_is_deleted_after_delay(bot: AutoDeleteBot, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.SUPERGROUP)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))

    message = await bot.send_message(chat.id, "验证提醒")
    await asyncio.sleep(0.05)

    assert message.chat.id == chat.id
    bot.delete_message.assert_awaited_once_with(chat.id, message.message_id)


@pytest.mark.asyncio
async def test_private_message_is_not_deleted(bot: AutoDeleteBot, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=7, type=ChatType.PRIVATE)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))

    await bot.send_message(chat.id, "私聊回复")
    await asyncio.sleep(0.05)

    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_message_uses_longer_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.SUPERGROUP)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="验证并绑定", url="https://t.me/x")]])
    plain = _sent_message(chat, message_id=10)
    with_button = _sent_message(chat, message_id=11, reply_markup=markup)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(side_effect=[plain, with_button]))
    monkeypatch.setattr(Bot, "delete_message", AsyncMock(return_value=True))
    bot = AutoDeleteBot(_TOKEN, auto_delete_delay=10.0, button_delete_delay=0.01)
    try:
        await bot.send_message(chat.id, "普通消息")
        await bot.send_message(chat.id, "带按钮消息", reply_markup=markup)
        await asyncio.sleep(0.05)

        bot.delete_message.assert_awaited_once_with(chat.id, with_button.message_id)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed(bot: AutoDeleteBot, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(id=-1001, type=ChatType.GROUP)
    monkeypatch.setattr(Bot, "send_message", AsyncMock(return_value=_sent_message(chat)))
    bot.delete_message.side_effect = RuntimeError("message to delete not found")

    await bot.send_message(chat.id, "验证提醒")
    await asyncio.sleep(0.05)

    bot.delete_message.assert_awaited_once_with(chat.id, 10)


def test_delay_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AutoDeleteBot(_TOKEN, auto_delete_delay=0)
