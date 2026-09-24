from __future__ import annotations

from collections.abc import Iterable

import structlog
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeChatMember, BotCommandScopeDefault

from reclaude_bot.application.groups import GroupService
from reclaude_bot.domain.enums import ManagedGroupStatus

log = structlog.get_logger(__name__)


_USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "开始使用"),
    ("bind", "绑定邮箱（需要参数）"),
    ("status", "查看额度状态"),
)

_ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("sync", "同步上游成员"),
    ("member", "查看上游成员列表"),
    ("setquota", "设置全局默认额度"),
    ("ban", "禁用用户"),
    ("unban", "解禁用户"),
    ("unbind", "解绑用户"),
    ("audit", "查看审计记录"),
    ("groups", "查看托管群组"),
    ("account", "查看 Reclaude 实时账号"),
    ("use", "选择并同步 Reclaude 账号"),
    ("newtask", "新建限额任务"),
    ("deltatask", "删除限额任务"),
    ("task", "查看限额任务状态"),
    ("starttask", "启动限额任务"),
    ("stoptask", "停止限额任务"),
    ("startstats", "启动数据统计"),
    ("stopstats", "停止数据统计"),
    ("addtaskmember", "加入限额任务成员"),
    ("deletetaskmember", "移除限额任务成员"),
    ("settaskquota", "设置任务每用户额度"),
    ("taskusers", "查看任务成员使用状况"),
    ("update", "更新并重启 Bot"),
    ("recovery_enable", "兼容旧版恢复命令"),
)


def _commands(definitions: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(command=command, description=description) for command, description in definitions]


def user_commands() -> list[BotCommand]:
    return _commands(_USER_COMMANDS)


def admin_commands() -> list[BotCommand]:
    return _commands(_USER_COMMANDS + _ADMIN_COMMANDS)


async def register_command_menus(bot: Bot, admin_ids: Iterable[int]) -> None:
    """Register the public menu and private per-admin menu before polling starts."""
    try:
        await bot.set_my_commands(user_commands(), scope=BotCommandScopeDefault())
    except Exception as exc:
        log.warning("telegram_command_menu_registration_failed", scope="default", error=str(exc))
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(admin_commands(), scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            log.warning(
                "telegram_command_menu_registration_failed",
                scope="chat",
                admin_id=admin_id,
                error=str(exc),
            )


async def register_group_admin_menus(bot: Bot, chat_id: int, admin_ids: Iterable[int]) -> None:
    """Give each admin the full command menu inside one managed group."""
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                admin_commands(),
                scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=admin_id),
            )
        except Exception as exc:
            log.warning(
                "telegram_command_menu_registration_failed",
                scope="chat_member",
                chat_id=chat_id,
                admin_id=admin_id,
                error=str(exc),
            )


async def clear_group_admin_menus(bot: Bot, chat_id: int, admin_ids: Iterable[int]) -> None:
    """Remove per-admin menus for a group; affected admins fall back to the public menu."""
    for admin_id in admin_ids:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=admin_id))
        except Exception as exc:
            log.warning(
                "telegram_command_menu_cleanup_failed",
                scope="chat_member",
                chat_id=chat_id,
                admin_id=admin_id,
                error=str(exc),
            )


async def restore_group_admin_menus(bot: Bot, groups: GroupService, admin_ids: Iterable[int]) -> None:
    """Re-register per-admin group menus for every ACTIVE managed group on startup."""
    try:
        rows = await groups.list_groups(ManagedGroupStatus.ACTIVE)
    except Exception as exc:
        log.warning("telegram_command_menu_restore_failed", error=str(exc))
        return
    for row in rows:
        await register_group_admin_menus(bot, row.chat_id, admin_ids)
