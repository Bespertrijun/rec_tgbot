from __future__ import annotations

from collections.abc import Iterable

import structlog
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

log = structlog.get_logger(__name__)


_USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "开始使用"),
    ("bind", "绑定邮箱（需要参数）"),
    ("status", "查看额度状态"),
)

_ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("sync", "同步上游成员"),
    ("setquota", "设置当前周期额度"),
    ("ban", "禁用用户"),
    ("unban", "解禁用户"),
    ("unbind", "解绑用户"),
    ("audit", "查看审计记录"),
    ("groups", "查看托管群组"),
    ("account", "恢复并校验 Reclaude 账号"),
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
