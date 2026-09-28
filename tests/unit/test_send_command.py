from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MessageEntity
from aiogram.types import User as TgUser

from reclaude_bot.bot import handlers
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError


@pytest.fixture
def deps(monkeypatch):
    async def lookup(factory, *, telegram_user_id=None, username=None, **kwargs):
        if telegram_user_id == 42:
            return (1, "alice@example.com")
        if telegram_user_id == 999 or username == "bob":
            return (2, "bob@example.com")
        return None

    lookup_mock = AsyncMock(side_effect=lookup)
    refresh = AsyncMock()
    original_refresh = handlers._record_username_safely_from_store
    monkeypatch.setattr(handlers, "_find_local_user", lookup_mock)
    monkeypatch.setattr(handlers, "_record_username_safely_from_store", refresh)
    quota = SimpleNamespace(session_factory=object(), status=AsyncMock(return_value=SimpleNamespace(cycle_id=7)),
                            transfer=AsyncMock(return_value=SimpleNamespace(amount_usd=Decimal("12.5"), sender_remaining_usd=Decimal("87.5"))))
    router = handlers.build_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "send")
    return SimpleNamespace(handler=handler, quota=quota, lookup=lookup_mock, refresh=refresh, original_refresh=original_refresh)


def message(*, chat_type="supergroup", text="/send @bob 12.5", entities=None):
    return SimpleNamespace(from_user=SimpleNamespace(id=42, username="alice"),
                           chat=SimpleNamespace(type=chat_type, id=-100), message_id=123, text=text, answer=AsyncMock(),
                           entities=entities if entities is not None else [MessageEntity(type="bot_command", offset=0, length=5),
                                                                          MessageEntity(type="mention", offset=6, length=4)])


async def test_send_rejects_private_chat(deps):
    event = message(chat_type="private")
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("只能在群组中使用：请在群里 @对方 后转账。")
    deps.quota.transfer.assert_not_called()


async def test_send_requires_a_mention(deps):
    event = message(text="/send 10", entities=[MessageEntity(type="bot_command", offset=0, length=5)])
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("用法：/send @对方 金额")
    deps.quota.transfer.assert_not_called()


async def test_send_rejects_invalid_amount(deps):
    event = message(text="/send @bob abc")
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("用法：/send @对方 金额")
    deps.quota.transfer.assert_not_called()


async def test_send_transfers_to_local_user_with_stable_telegram_operation_key(deps):
    event = message()
    await deps.handler(event, deps.quota)
    deps.refresh.assert_awaited_once_with(deps.quota.session_factory, 42, "alice")
    deps.lookup.assert_any_await(deps.quota.session_factory, username="bob")
    deps.quota.transfer.assert_awaited_once_with(1, 2, 7, Decimal("12.5"), operation_key="telegram:-100:123")
    event.answer.assert_awaited_once_with("已转账 $12.50 给 b***@example.com，你本周期剩余额度 $87.50。", skip_auto_delete=True)


async def test_send_text_mention_preserves_utf16_offsets(deps):
    event = message(text="/send 😀Bob 10", entities=[MessageEntity(type="bot_command", offset=0, length=5),
                   MessageEntity(type="text_mention", offset=6, length=5, user=TgUser(id=999, is_bot=False, first_name="Bob"))])
    await deps.handler(event, deps.quota)
    deps.lookup.assert_any_await(deps.quota.session_factory, telegram_user_id=999)
    deps.quota.transfer.assert_awaited_once_with(1, 2, 7, Decimal("10"), operation_key="telegram:-100:123")


async def test_send_passes_safe_domain_error_through(deps):
    deps.quota.transfer.side_effect = EligibilityError("剩余额度不足：当前剩余 $5.00")
    event = message()
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("剩余额度不足：当前剩余 $5.00", skip_auto_delete=True)


async def test_send_does_not_expose_unexpected_error(deps):
    deps.quota.transfer.side_effect = RuntimeError("private database failure")
    event = message()
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("转账失败，请稍后重试。", skip_auto_delete=True)


async def test_username_refresh_failure_does_not_prevent_transfer(deps, monkeypatch):
    def broken_factory():
        raise RuntimeError("column missing")

    deps.quota.session_factory = broken_factory
    monkeypatch.setattr(handlers, "_record_username_safely_from_store", deps.original_refresh)
    event = message()
    await deps.handler(event, deps.quota)
    deps.quota.transfer.assert_awaited_once()
    assert "已转账" in event.answer.await_args.args[0]


async def test_send_explains_anonymous_senders_cannot_transfer(deps):
    event = message()
    event.from_user = None
    await deps.handler(event, deps.quota)
    event.answer.assert_awaited_once_with("无法识别发送者：匿名管理员或频道身份不能使用 /send，请换回本人身份后重试。", skip_auto_delete=True)
    deps.quota.transfer.assert_not_called()


async def test_send_without_current_cycle_does_not_transfer(deps):
    deps.quota.status.return_value = SimpleNamespace(cycle_id=None)
    event = message()
    await deps.handler(event, deps.quota)
    deps.quota.transfer.assert_not_called()
    assert "周期" in event.answer.await_args.args[0]
