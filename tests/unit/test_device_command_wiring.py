import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import Command

from reclaude_bot.bot.commands import admin_commands, user_commands
from reclaude_bot.bot.handlers import build_admin_router, build_router
from reclaude_bot.config import Settings


def settings():
    return Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1])


def command_callbacks(router):
    result = {}
    for handler in router.message.handlers:
        for item in handler.filters:
            if isinstance(item.callback, Command):
                for command in item.callback.commands:
                    if isinstance(command, str):
                        result[command] = handler.callback
    return result


def test_new_device_commands_and_existing_commands_remain_registered():
    user = command_callbacks(build_router(settings()))
    admin = command_callbacks(build_admin_router(settings()))
    expected_user = {"start", "bind", "status", "send", "auth", "deauth"}
    expected_admin = {"sync", "member", "setquota", "ban", "unban", "unbind", "audit", "groups", "account",
                      "newtask", "deltatask", "task", "starttask", "stoptask", "startstats", "stopstats", "addtaskmember",
                      "deletetaskmember", "settaskquota", "taskusers", "update", "recovery_enable", "device", "authuser", "deauthuser", "reset"}
    assert expected_user <= user.keys()
    # The group router owns /groups; the existing shared account callback owns /account.
    assert (expected_admin - {"groups"}) <= admin.keys()
    assert expected_user <= {entry.command for entry in user_commands()}
    assert expected_user | (expected_admin - {"account"}) <= {entry.command for entry in admin_commands()}


def parameters(callback, message, args):
    provided = {"message": message, "command": SimpleNamespace(args=args)}
    deps = {}
    for name in inspect.signature(callback).parameters:
        if name not in provided:
            deps[name] = AsyncMock()
            provided[name] = deps[name]
    return {name: provided[name] for name in inspect.signature(callback).parameters}, deps


@pytest.mark.parametrize("name,args", [("device", ""), ("authuser", "one@example.invalid 44500"), ("deauthuser", "one@example.invalid")])
async def test_non_admin_cannot_invoke_device_admin_services(name, args):
    handler = command_callbacks(build_admin_router(settings()))[name]
    message = SimpleNamespace(from_user=SimpleNamespace(id=55), chat=SimpleNamespace(type="private"),
                              text=f"/{name} {args}", answer=AsyncMock())
    kwargs, deps = parameters(handler, message, args)
    await handler(**kwargs)
    for dep in deps.values():
        assert dep.mock_calls == []
    message.answer.assert_not_called()


async def test_auth_link_is_rejected_in_group_before_any_device_service_call():
    handler = command_callbacks(build_router(settings()))["auth"]
    link = "https://www.recode.cat/cli/auth?state=private-test-state"
    message = SimpleNamespace(from_user=SimpleNamespace(id=55), chat=SimpleNamespace(type="supergroup"),
                              text=f"/auth {link}", answer=AsyncMock())
    kwargs, deps = parameters(handler, message, link)
    await handler(**kwargs)
    for dep in deps.values():
        assert dep.mock_calls == []
    message.answer.assert_awaited_once()
    assert "private-test-state" not in str(message.answer.await_args)


async def test_manual_sync_checks_account_before_sync_and_sampling():
    callback = command_callbacks(build_admin_router(settings()))["sync"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
    kwargs, deps = parameters(callback, message, "")
    calls = []
    deps["device_account_reconcile"].reconcile.side_effect = lambda **kwargs: calls.append("account")
    deps["device_cycle"].sync.side_effect = lambda: calls.append("cycle") or SimpleNamespace(
        weekly_percent=None, status="VERIFIED", account_id="8123",
    )
    deps["device_sampling"].tick.side_effect = lambda: calls.append("sampling") or ()
    await callback(**kwargs)
    assert calls == ["account", "cycle", "sampling"]
    assert "同步完成" in message.answer.await_args.args[0]


async def test_manual_sync_failure_never_samples_previous_account():
    callback = command_callbacks(build_admin_router(settings()))["sync"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
    kwargs, deps = parameters(callback, message, "")
    deps["device_account_reconcile"].reconcile.side_effect = TimeoutError("unavailable")
    await callback(**kwargs)
    deps["device_cycle"].sync.assert_not_called()
    deps["device_sampling"].tick.assert_not_called()
    assert "同步失败" in message.answer.await_args.args[0]
