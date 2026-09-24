from __future__ import annotations

import asyncio
import html
from collections.abc import Iterable
from typing import Any

import structlog
from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message

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
    """Bot that recalls its own group messages (plain ones after `auto_delete_delay`,
    messages carrying an inline keyboard after `button_delete_delay`) and mirrors
    private messages sent to non-admin users to `admin_ids`.

    Pass ``skip_auto_delete=True`` to `send_message` (or `Message.answer`) to keep
    a group message permanently, e.g. for admin announcements."""

    def __init__(
        self,
        *args: Any,
        auto_delete_delay: float = AUTO_DELETE_DELAY_SECONDS,
        button_delete_delay: float = BUTTON_MESSAGE_DELETE_DELAY_SECONDS,
        admin_ids: Iterable[int] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if auto_delete_delay <= 0:
            raise ValueError("auto_delete_delay must be positive")
        if button_delete_delay <= 0:
            raise ValueError("button_delete_delay must be positive")
        self.auto_delete_delay = auto_delete_delay
        self.button_delete_delay = button_delete_delay
        self.admin_ids = tuple(admin_ids)

    async def send_message(self, chat_id: int | str, text: str, *args: Any, **kwargs: Any) -> Message:
        skip_auto_delete = bool(kwargs.pop("skip_auto_delete", False))
        message = await super().send_message(chat_id, text, *args, **kwargs)
        chat = message.chat
        if chat is None:
            return message
        if chat.type in _GROUP_TYPES:
            if not skip_auto_delete:
                delay = self.button_delete_delay if message.reply_markup is not None else self.auto_delete_delay
                schedule_auto_delete(self, chat.id, message.message_id, delay=delay)
        elif chat.type == ChatType.PRIVATE and chat.id not in self.admin_ids:
            await self._mirror_to_admins(chat, text)
        return message

    async def _mirror_to_admins(self, chat: Chat, text: str) -> None:
        """Best-effort copy of a user-bound private message to each admin."""
        name = " ".join(part for part in (chat.first_name, chat.last_name) if part)
        if not name and chat.username:
            name = f"@{chat.username}"
        label = f"{html.escape(name)}（{chat.id}）" if name else str(chat.id)
        for admin_id in self.admin_ids:
            try:
                await super().send_message(admin_id, f"【私聊镜像】发给 {label}：\n{text}")
            except Exception as exc:
                log.warning("private_mirror_failed", admin_id=admin_id, chat_id=chat.id, error=str(exc))
