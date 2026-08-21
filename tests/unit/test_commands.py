from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from reclaude_bot.bot.commands import register_command_menus


def _commands(call: object) -> list[dict[str, str]]:
    return [{"command": command.command, "description": command.description} for command in call.args[0]]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_register_command_menus_sets_public_and_admin_scopes() -> None:
    bot = AsyncMock()

    await register_command_menus(bot, [101, 202])

    assert bot.set_my_commands.await_count == 3
    calls = bot.set_my_commands.await_args_list

    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    assert _commands(calls[0]) == [
        {"command": "start", "description": "开始使用"},
        {"command": "bind", "description": "绑定邮箱（需要参数）"},
        {"command": "status", "description": "查看额度状态"},
    ]

    for call, admin_id in zip(calls[1:], [101, 202], strict=True):
        scope = call.kwargs["scope"]
        assert isinstance(scope, BotCommandScopeChat)
        assert scope.chat_id == admin_id
        assert _commands(call) == [
            {"command": "start", "description": "开始使用"},
            {"command": "bind", "description": "绑定邮箱（需要参数）"},
            {"command": "status", "description": "查看额度状态"},
            {"command": "sync", "description": "同步上游成员"},
            {"command": "setquota", "description": "设置当前周期额度"},
            {"command": "ban", "description": "禁用用户"},
            {"command": "unban", "description": "解禁用户"},
            {"command": "unbind", "description": "解绑用户"},
            {"command": "audit", "description": "查看审计记录"},
            {"command": "account", "description": "恢复并校验 Reclaude 账号"},
            {"command": "recovery_enable", "description": "兼容旧版恢复命令"},
        ]


@pytest.mark.asyncio
async def test_register_command_menus_continues_when_an_admin_scope_is_unavailable() -> None:
    bot = AsyncMock()
    bot.set_my_commands.side_effect = [True, RuntimeError("chat not found"), True]

    await register_command_menus(bot, [101, 202])

    assert bot.set_my_commands.await_count == 3
