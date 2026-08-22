from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message

from reclaude_bot.application.groups import GroupService, GroupSnapshot
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.config import Settings
from reclaude_bot.domain.enums import ManagedGroupStatus
from reclaude_bot.domain.errors import GroupError
from reclaude_bot.jobs.onboarding import OnboardingWorker

log = structlog.get_logger(__name__)

_CALLBACK_PREFIX = "group"
_CALLBACK_ACTIONS = frozenset({"approve", "reject", "disable", "enable"})
_NOT_FOUND_ERRORS = ("chat not found", "member not found", "user not found", "user is not a member of the chat", "participant_id_invalid")


def _status_value(member: Any) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status))


def _type_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _member_is_present(member: Any) -> bool:
    status = _status_value(member)
    if status in {ChatMemberStatus.CREATOR.value, ChatMemberStatus.ADMINISTRATOR.value, ChatMemberStatus.MEMBER.value}:
        return True
    return status == ChatMemberStatus.RESTRICTED.value and bool(getattr(member, "is_member", False))


def _snapshot_from_member(chat_id: int, title: str, chat_type: str, member: Any) -> GroupSnapshot:
    status = _status_value(member)
    is_member = _member_is_present(member)
    is_administrator = status in {ChatMemberStatus.CREATOR.value, ChatMemberStatus.ADMINISTRATOR.value}
    can_restrict = status == ChatMemberStatus.CREATOR.value or bool(getattr(member, "can_restrict_members", False))
    if hasattr(member, "model_dump"):
        permissions = member.model_dump(mode="json", exclude_none=True)
    else:
        permissions = {"status": status}
    return GroupSnapshot(
        chat_id=chat_id,
        title=title,
        is_bot_member=is_member,
        is_bot_administrator=is_administrator,
        can_restrict_members=can_restrict,
        chat_type=chat_type,
        permissions=dict(permissions),
    )


def _is_group_chat(chat_type: str | ChatType) -> bool:
    return _type_value(chat_type) in {ChatType.GROUP.value, ChatType.SUPERGROUP.value}


class TelegramGroupGateway:
    """Aiogram adapter for the application-level group permission protocol."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @staticmethod
    def _is_not_found_error(error: TelegramBadRequest) -> bool:
        message = str(getattr(error, "message", error)).casefold()
        return any(marker in message for marker in _NOT_FOUND_ERRORS)

    @staticmethod
    def _unavailable_snapshot(chat_id: int, chat_type: str = "", error: Exception | None = None) -> GroupSnapshot:
        return GroupSnapshot(
            chat_id=chat_id,
            title="",
            is_bot_member=False,
            is_bot_administrator=False,
            can_restrict_members=False,
            chat_type=chat_type,
            permissions={"unavailable": True, "error": type(error).__name__ if error is not None else "unavailable"},
        )

    async def get_group_snapshot(self, chat_id: int) -> GroupSnapshot:
        try:
            chat = await self.bot.get_chat(chat_id)
        except TelegramForbiddenError as exc:
            return self._unavailable_snapshot(chat_id, error=exc)
        except TelegramBadRequest as exc:
            if self._is_not_found_error(exc):
                return self._unavailable_snapshot(chat_id, error=exc)
            raise

        chat_type = _type_value(getattr(chat, "type", ""))
        try:
            member = await self.bot.get_chat_member(chat_id, self.bot.id)
        except TelegramForbiddenError as exc:
            return self._unavailable_snapshot(chat_id, chat_type, exc)
        except TelegramBadRequest as exc:
            if self._is_not_found_error(exc):
                return self._unavailable_snapshot(chat_id, chat_type, exc)
            raise
        return _snapshot_from_member(chat_id, getattr(chat, "title", "") or "", chat_type, member)

    async def get_member(self, chat_id: int, user_id: int) -> Any:
        """Return Telegram's current membership object for onboarding checks."""
        return await self.bot.get_chat_member(chat_id, user_id)

    async def get_default_permissions(self, chat_id: int) -> ChatPermissions:
        """Read the group's current default member permissions."""
        chat = await self.bot.get_chat(chat_id)
        permissions = getattr(chat, "permissions", None)
        if isinstance(permissions, ChatPermissions):
            return permissions
        if isinstance(permissions, Mapping):
            return ChatPermissions(**dict(permissions))
        return ChatPermissions()

    async def restrict_member(self, chat_id: int, user_id: int) -> bool:
        """Disable all member permissions for the current onboarding attempt."""
        permissions = ChatPermissions(**{name: False for name in ChatPermissions.model_fields})
        return bool(
            await self.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=permissions,
                use_independent_chat_permissions=True,
            )
        )

    async def restore_member(self, chat_id: int, user_id: int) -> bool:
        """Restore exactly the group's current defaults, never broader rights."""
        permissions = await self.get_default_permissions(chat_id)
        return bool(
            await self.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=permissions,
                use_independent_chat_permissions=True,
            )
        )

    async def remove_member(self, chat_id: int, user_id: int) -> bool:
        """Remove a timed-out member while allowing a future rejoin."""
        return bool(await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=False))

    restrict_chat_member = restrict_member
    restore_chat_member = restore_member
    unban_chat_member = remove_member

    async def bot_username(self) -> str:
        me = await self.bot.get_me()
        username = getattr(me, "username", None)
        if not username:
            raise RuntimeError("Bot username unavailable")
        return str(username).removeprefix("@")


def _callback_data(action: str, chat_id: int) -> str:
    return f"{_CALLBACK_PREFIX}:{action}:{chat_id}"


def _keyboard(row: Any) -> InlineKeyboardMarkup:
    status = row.status
    if status == ManagedGroupStatus.PENDING.value:
        buttons = [
            InlineKeyboardButton(text="批准", callback_data=_callback_data("approve", row.chat_id)),
            InlineKeyboardButton(text="拒绝", callback_data=_callback_data("reject", row.chat_id)),
        ]
    elif status == ManagedGroupStatus.ACTIVE.value:
        buttons = [InlineKeyboardButton(text="停用", callback_data=_callback_data("disable", row.chat_id))]
    elif status == ManagedGroupStatus.DISABLED.value:
        buttons = [InlineKeyboardButton(text="重新启用", callback_data=_callback_data("enable", row.chat_id))]
    else:
        buttons = []
    return InlineKeyboardMarkup(inline_keyboard=[buttons] if buttons else [])


def _format_group(row: Any) -> str:
    return f"{html.escape(row.title)} ({row.chat_id})：{row.status}"


def _format_groups(rows: Iterable[Any]) -> str:
    values = list(rows)
    if not values:
        return "暂无托管群组。"
    return "托管群组：\n" + "\n".join(_format_group(row) for row in values)


async def _notify_owners(bot: Bot, owner_ids: Iterable[int], text: str, row: Any) -> None:
    markup = _keyboard(row)
    for owner_id in owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except Exception as exc:
            log.warning("group_owner_notification_failed", owner_id=owner_id, chat_id=row.chat_id, error=str(exc))


def build_group_router(settings: Settings | Iterable[int]) -> Router:
    owner_ids = tuple(settings.telegram_admin_ids if isinstance(settings, Settings) else settings)
    router = Router(name="groups")

    @router.my_chat_member()
    async def my_chat_member(update: ChatMemberUpdated, bot: Bot, groups: GroupService) -> None:
        if not _is_group_chat(update.chat.type):
            return
        title = update.chat.title or str(update.chat.id)
        event_snapshot = _snapshot_from_member(update.chat.id, title, _type_value(update.chat.type), update.new_chat_member)
        newly_added = not _member_is_present(update.old_chat_member) and event_snapshot.is_bot_member
        try:
            before = await groups.get_status(update.chat.id)
        except Exception as exc:
            before = None
            log.error("group_status_before_observation_failed", chat_id=update.chat.id, error=str(exc))
        try:
            row = await groups.discover(
                update.chat.id,
                title,
                update.from_user.id if update.from_user else None,
                newly_added=newly_added,
                event_snapshot=event_snapshot,
            )
        except GroupError as exc:
            log.error("group_observation_failed", chat_id=update.chat.id, error=str(exc))
            return
        except Exception as exc:
            log.error("group_observation_unexpected_failure", chat_id=update.chat.id, error=str(exc))
            return

        before_status = before.status if before is not None else None
        if row.status == ManagedGroupStatus.PENDING.value and before_status in {None, ManagedGroupStatus.REJECTED.value}:
            await _notify_owners(bot, owner_ids, f"发现待审批群组：\n{_format_group(row)}", row)
        elif row.status == ManagedGroupStatus.DISABLED.value and before_status == ManagedGroupStatus.ACTIVE.value:
            await _notify_owners(bot, owner_ids, f"群组权限已失效，群组已自动停用：\n{_format_group(row)}", row)

    @router.chat_member()
    async def chat_member(
        update: ChatMemberUpdated,
        onboarding: OnboardingService,
        onboarding_worker: OnboardingWorker,
    ) -> None:
        if not _is_group_chat(update.chat.type):
            return
        target = update.new_chat_member.user
        if target.is_bot:
            return
        was_present = _member_is_present(update.old_chat_member)
        is_present = _member_is_present(update.new_chat_member)
        if was_present and not is_present:
            row = await onboarding.get_membership(update.chat.id, target.id)
            if row is not None:
                await onboarding.mark_left(update.chat.id, target.id, row.generation, now=update.date)
            return
        if was_present or not is_present:
            return
        status = _status_value(update.new_chat_member)
        try:
            await onboarding_worker.handle_join(
                update.chat.id,
                target.id,
                joined_at=update.date,
                display_name=target.full_name or target.username or str(target.id),
                is_telegram_admin=status == ChatMemberStatus.ADMINISTRATOR.value,
                is_group_owner=status == ChatMemberStatus.CREATOR.value,
            )
        except Exception as exc:
            log.warning("group_member_onboarding_failed", chat_id=update.chat.id, user_id=target.id, error=type(exc).__name__)

    @router.message(F.new_chat_members)
    async def new_chat_members_fallback(
        message: Message,
        bot: Bot,
        onboarding_worker: OnboardingWorker,
    ) -> None:
        if not _is_group_chat(message.chat.type) or not message.new_chat_members:
            return
        gateway = TelegramGroupGateway(bot)
        for target in message.new_chat_members:
            if target.is_bot:
                continue
            is_admin = False
            is_owner = False
            try:
                member = await gateway.get_member(message.chat.id, target.id)
                status = _status_value(member)
                is_admin = status == ChatMemberStatus.ADMINISTRATOR.value
                is_owner = status == ChatMemberStatus.CREATOR.value
            except Exception:
                pass
            try:
                await onboarding_worker.handle_join(
                    message.chat.id,
                    target.id,
                    joined_at=message.date,
                    display_name=target.full_name or target.username or str(target.id),
                    is_telegram_admin=is_admin,
                    is_group_owner=is_owner,
                )
            except Exception as exc:
                log.warning("group_member_fallback_onboarding_failed", chat_id=message.chat.id, user_id=target.id, error=type(exc).__name__)

    @router.callback_query(F.data.startswith(f"{_CALLBACK_PREFIX}:"))
    async def group_callback(callback: CallbackQuery, groups: GroupService) -> None:
        if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer("请在 Bot 私聊中操作。", show_alert=True)
            return
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX or parts[1] not in _CALLBACK_ACTIONS:
            await callback.answer("操作已失效。", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
            if str(chat_id) != parts[2]:
                raise ValueError
        except ValueError:
            await callback.answer("操作已失效。", show_alert=True)
            return
        if callback.from_user is None:
            await callback.answer("无法识别操作者。", show_alert=True)
            return

        try:
            if parts[1] == "approve":
                row = await groups.approve(chat_id, callback.from_user.id)
            elif parts[1] == "reject":
                row = await groups.reject(chat_id, callback.from_user.id)
            elif parts[1] == "disable":
                row = await groups.disable(chat_id, callback.from_user.id, "owner callback")
            else:
                row = await groups.re_enable(chat_id, callback.from_user.id)
        except GroupError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception as exc:
            log.error("group_callback_failed", chat_id=chat_id, action=parts[1], error=str(exc))
            await callback.answer("操作失败，请稍后重试。", show_alert=True)
            return

        await callback.answer("操作完成。")
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(_format_group(row), reply_markup=_keyboard(row))
            except Exception as exc:
                log.warning("group_callback_message_update_failed", chat_id=chat_id, error=str(exc))

    @router.message(Command("groups"))
    async def groups_status(message: Message, groups: GroupService) -> None:
        if message.chat.type != ChatType.PRIVATE or message.from_user is None or message.from_user.id not in owner_ids:
            return
        try:
            rows = await groups.list_groups()
            await message.answer(_format_groups(rows))
            for row in rows:
                if row.status in {ManagedGroupStatus.PENDING.value, ManagedGroupStatus.ACTIVE.value, ManagedGroupStatus.DISABLED.value}:
                    await message.answer(_format_group(row), reply_markup=_keyboard(row))
        except Exception as exc:
            log.error("group_status_command_failed", error=str(exc))
            await message.answer("群组状态暂时无法获取，请稍后重试。")

    return router
