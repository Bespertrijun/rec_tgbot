from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.bot.commands import admin_commands, user_commands
from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from tests.integration.test_device_ledger import NOW
from tests.unit.test_device_command_wiring import command_callbacks


def setup():
    configured = Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[999])
    callback = command_callbacks(build_admin_router(configured))["reset"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="private", id=999),
                              message_id=321, answer=AsyncMock())
    result = SimpleNamespace(task_id=1, task_name="device", cycle_id=2, reset_at=NOW,
                             device_count=4, user_count=4, replayed=False)
    service = SimpleNamespace(reset=AsyncMock(return_value=result))
    return callback, message, service


def test_reset_is_only_in_admin_menu():
    assert "reset" in {command.command for command in admin_commands()}
    assert "reset" not in {command.command for command in user_commands()}


@pytest.mark.parametrize("actor,chat_type", [(123, "private"), (999, "group"), (999, "supergroup")])
async def test_reset_requires_admin_private_chat(actor, chat_type):
    callback, message, service = setup()
    message.from_user.id = actor
    message.chat.type = chat_type
    await callback(message, SimpleNamespace(args="device"), service)
    service.reset.assert_not_called()


@pytest.mark.parametrize("args", [None, "", "device extra"])
async def test_reset_requires_exactly_one_task_name(args):
    callback, message, service = setup()
    await callback(message, SimpleNamespace(args=args), service)
    service.reset.assert_not_called()
    message.answer.assert_awaited_once()


async def test_reset_passes_operator_task_and_stable_message_idempotency_key():
    callback, message, service = setup()
    await callback(message, SimpleNamespace(args="device"), service)
    args, kwargs = service.reset.await_args
    assert args == ("device", 999)
    key = kwargs["operation_key"]
    assert "999" in key and "321" in key
    await callback(message, SimpleNamespace(args="device"), service)
    assert service.reset.await_args.kwargs["operation_key"] == key
    message.message_id += 1
    await callback(message, SimpleNamespace(args="device"), service)
    assert service.reset.await_args.kwargs["operation_key"] != key


@pytest.mark.parametrize("error", [EligibilityError("设备用量查询失败，本次未重置"), RuntimeError("database unavailable")])
async def test_reset_reports_failure_without_claiming_success(error):
    callback, message, service = setup()
    service.reset.side_effect = error
    await callback(message, SimpleNamespace(args="device"), service)
    message.answer.assert_awaited_once()
    assert "未重置" in message.answer.await_args.args[0]


async def test_failed_success_reply_does_not_claim_committed_reset_was_rolled_back():
    callback, message, service = setup()
    message.answer.side_effect = [RuntimeError("Telegram unavailable"), None]
    try:
        await callback(message, SimpleNamespace(args="device"), service)
    except RuntimeError:
        pass
    service.reset.assert_awaited_once()
    assert all("未重置" not in call.args[0] for call in message.answer.await_args_list)
