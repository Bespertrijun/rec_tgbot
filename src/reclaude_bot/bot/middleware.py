from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

log = structlog.get_logger(__name__)


class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: set[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id not in self.admin_ids:
            return None
        return await handler(event, data)


class GroupAccessMiddleware(BaseMiddleware):
    """Reject group messages from groups that are not currently ACTIVE."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Message) or event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return await handler(event, data)

        groups = data.get("groups")
        if groups is None:
            log.error("group_access_service_missing", chat_id=event.chat.id)
            await event.answer("群组权限暂时无法确认，请稍后重试。")
            return None
        try:
            active = await groups.is_active(event.chat.id)
        except Exception as exc:
            log.error("group_access_check_failed", chat_id=event.chat.id, error=str(exc))
            await event.answer("群组权限暂时无法确认，请稍后重试。")
            return None
        if not active:
            await event.answer("该群组尚未获批或已停用。")
            return None
        return await handler(event, data)
