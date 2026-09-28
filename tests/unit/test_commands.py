from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommandScopeChat, BotCommandScopeChatMember, BotCommandScopeDefault

from reclaude_bot.bot.commands import clear_group_admin_menus, register_command_menus, register_group_admin_menus, restore_group_admin_menus
from reclaude_bot.domain.enums import ManagedGroupStatus


def _commands(call: object) -> list[dict[str, str]]:
    return [{"command": command.command, "description": command.description} for command in call.args[0]]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_register_command_menus_sets_public_and_admin_scopes() -> None:
    bot = AsyncMock()

    await register_command_menus(bot, [101, 202])

    assert bot.set_my_commands.await_count == 3
    calls = bot.set_my_commands.await_args_list

    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    public = {"start", "bind", "status", "auth", "deauth", "send"}
    admin = {"sync", "member", "setquota", "ban", "unban", "unbind", "device", "authuser", "deauthuser", "audit", "groups",
             "account", "use", "newtask", "deltatask", "task", "starttask", "stoptask", "startstats", "stopstats", "addtaskmember",
             "deletetaskmember", "settaskquota", "taskusers", "update", "recovery_enable"}
    assert {entry["command"] for entry in _commands(calls[0])} == public
    for call, admin_id in zip(calls[1:], [101, 202], strict=True):
        scope = call.kwargs["scope"]
        assert isinstance(scope, BotCommandScopeChat)
        assert scope.chat_id == admin_id
        assert {entry["command"] for entry in _commands(call)} == public | admin
        assert all(entry["description"].strip() for entry in _commands(call))



@pytest.mark.asyncio
async def test_register_command_menus_continues_when_an_admin_scope_is_unavailable() -> None:
    bot = AsyncMock()
    bot.set_my_commands.side_effect = [True, RuntimeError("chat not found"), True]

    await register_command_menus(bot, [101, 202])

    assert bot.set_my_commands.await_count == 3


@pytest.mark.asyncio
async def test_register_group_admin_menus_uses_chat_member_scope() -> None:
    bot = AsyncMock()

    await register_group_admin_menus(bot, -1001, [101, 202])

    assert bot.set_my_commands.await_count == 2
    for call, admin_id in zip(bot.set_my_commands.await_args_list, [101, 202], strict=True):
        scope = call.kwargs["scope"]
        assert isinstance(scope, BotCommandScopeChatMember)
        assert scope.chat_id == -1001
        assert scope.user_id == admin_id
        assert any(command["command"] == "settaskquota" for command in _commands(call))


@pytest.mark.asyncio
async def test_register_group_admin_menus_continues_when_an_admin_is_not_in_the_group() -> None:
    bot = AsyncMock()
    bot.set_my_commands.side_effect = [RuntimeError("user not found"), True]

    await register_group_admin_menus(bot, -1001, [101, 202])

    assert bot.set_my_commands.await_count == 2


@pytest.mark.asyncio
async def test_clear_group_admin_menus_deletes_chat_member_scope() -> None:
    bot = AsyncMock()

    await clear_group_admin_menus(bot, -1001, [101, 202])

    assert bot.delete_my_commands.await_count == 2
    for call, admin_id in zip(bot.delete_my_commands.await_args_list, [101, 202], strict=True):
        scope = call.kwargs["scope"]
        assert isinstance(scope, BotCommandScopeChatMember)
        assert scope.chat_id == -1001
        assert scope.user_id == admin_id


@pytest.mark.asyncio
async def test_restore_group_admin_menus_registers_each_active_group() -> None:
    bot = AsyncMock()
    groups = SimpleNamespace(list_groups=AsyncMock(return_value=[SimpleNamespace(chat_id=-1001), SimpleNamespace(chat_id=-1002)]))

    await restore_group_admin_menus(bot, groups, [101])  # type: ignore[arg-type]

    groups.list_groups.assert_awaited_once_with(ManagedGroupStatus.ACTIVE)
    scopes = [call.kwargs["scope"] for call in bot.set_my_commands.await_args_list]
    assert [scope.chat_id for scope in scopes] == [-1001, -1002]
    assert all(isinstance(scope, BotCommandScopeChatMember) and scope.user_id == 101 for scope in scopes)


@pytest.mark.asyncio
async def test_restore_group_admin_menus_swallows_listing_failure() -> None:
    bot = AsyncMock()
    groups = SimpleNamespace(list_groups=AsyncMock(side_effect=RuntimeError("db down")))

    await restore_group_admin_menus(bot, groups, [101])  # type: ignore[arg-type]

    bot.set_my_commands.assert_not_awaited()
