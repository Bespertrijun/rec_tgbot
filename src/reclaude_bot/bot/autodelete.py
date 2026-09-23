from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

log = structlog.get_logger(__name__)

AUTO_DELETE_DELAY_SECONDS = 60.0
BUTTON_MESSAGE_DELETE_DELAY_SECONDS = 300.0
_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
_PENDING_TASKS: set[asyncio.Task[None]] = set()


def schedule_auto_delete(bot: Bot, chat_id: int, message_id: int, *, delay: float = AUTO_DELETE_DELAY_SECONDS) -> None:
    """Best-effort deletion of a message after a delay; failures are only logged."""
    task = asyncio.create_task(_delete_later(bot, chat_id, message_id, delay))
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except asyncio.CancelledError:
        raise
    except TelegramBadRequest as exc:
        # Already deleted by an admin, or the bot lacks delete rights.
        log.debug("auto_delete_skipped", chat_id=chat_id, message_id=message_id, error=str(exc))
    except Exception as exc:
        log.warning("auto_delete_failed", chat_id=chat_id, message_id=message_id, error=str(exc))


class AutoDeleteBot(Bot):
    """Bot that recalls its own group messages: plain ones after `auto_delete_delay`,
    messages carrying an inline keyboard after `button_delete_delay`."""

    def __init__(
        self,
        *args: Any,
        auto_delete_delay: float = AUTO_DELETE_DELAY_SECONDS,
        button_delete_delay: float = BUTTON_MESSAGE_DELETE_DELAY_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if auto_delete_delay <= 0:
            raise ValueError("auto_delete_delay must be positive")
        if button_delete_delay <= 0:
            raise ValueError("button_delete_delay must be positive")
        self.auto_delete_delay = auto_delete_delay
        self.button_delete_delay = button_delete_delay

    async def send_message(self, chat_id: int | str, text: str, *args: Any, **kwargs: Any) -> Message:
        message = await super().send_message(chat_id, text, *args, **kwargs)
        if message.chat is not None and message.chat.type in _GROUP_TYPES:
            delay = self.button_delete_delay if message.reply_markup is not None else self.auto_delete_delay
            schedule_auto_delete(self, message.chat.id, message.message_id, delay=delay)
        return message
