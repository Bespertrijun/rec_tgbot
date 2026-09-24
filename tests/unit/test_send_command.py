from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MessageEntity
from aiogram.types import User as TgUser

from reclaude_bot.bot.handlers import build_router
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError


def _send_handler():
    router = build_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == "send")


def _quota() -> SimpleNamespace:
    return SimpleNamespace(
        record_username=AsyncMock(),
        transfer_quota=AsyncMock(
            return_value={
                "amount_usd": Decimal("12.5"),
                "recipient_email": "bob@example.com",
                "sender_remaining_usd": Decimal("87.5"),
            }
        ),
    )


def _message(*, chat_type: str = "supergroup", text: str, entities: list[MessageEntity]) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42, username="alice"),
        chat=SimpleNamespace(type=chat_type),
        text=text,
        entities=entities,
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_send_rejects_private_chat() -> None:
    handler = _send_handler()
    message = _message(chat_type="private", text="/send @bob 10", entities=[MessageEntity(type="bot_command", offset=0, length=5)])
    quota = _quota()

    await handler(message, quota)

    message.answer.assert_awaited_once_with("只能在群组中使用：请在群里 @对方 后转账。")
    quota.transfer_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_requires_a_mention() -> None:
    handler = _send_handler()
    message = _message(text="/send 10", entities=[MessageEntity(type="bot_command", offset=0, length=5)])
    quota = _quota()

    await handler(message, quota)

    message.answer.assert_awaited_once_with("用法：/send @对方 金额")
    quota.transfer_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_rejects_invalid_amount() -> None:
    handler = _send_handler()
    message = _message(
        text="/send @bob abc",
        entities=[MessageEntity(type="bot_command", offset=0, length=5), MessageEntity(type="mention", offset=6, length=4)],
    )
    quota = _quota()

    await handler(message, quota)

    message.answer.assert_awaited_once_with("用法：/send @对方 金额")
    quota.transfer_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_transfers_to_plain_mention_username() -> None:
    handler = _send_handler()
    message = _message(
        text="/send @bob 12.5",
        entities=[MessageEntity(type="bot_command", offset=0, length=5), MessageEntity(type="mention", offset=6, length=4)],
    )
    quota = _quota()

    await handler(message, quota)

    quota.record_username.assert_awaited_once_with(42, "alice")
    quota.transfer_quota.assert_awaited_once_with(42, amount=Decimal("12.5"), recipient_username="bob")
    message.answer.assert_awaited_once_with("已转账 $12.50 给 b***@example.com，你本周期剩余额度 $87.50。")


@pytest.mark.asyncio
async def test_send_transfers_to_text_mention_user_id_with_utf16_offsets() -> None:
    handler = _send_handler()
    # "😀Bob" is 5 UTF-16 code units: the emoji would break naive Python string slicing.
    message = _message(
        text="/send 😀Bob 10",
        entities=[
            MessageEntity(type="bot_command", offset=0, length=5),
            MessageEntity(type="text_mention", offset=6, length=5, user=TgUser(id=999, is_bot=False, first_name="Bob")),
        ],
    )
    quota = _quota()

    await handler(message, quota)

    quota.transfer_quota.assert_awaited_once_with(42, amount=Decimal("10"), recipient_telegram_id=999)
    message.answer.assert_awaited_once_with("已转账 $12.50 给 b***@example.com，你本周期剩余额度 $87.50。")


@pytest.mark.asyncio
async def test_send_passes_domain_errors_through() -> None:
    handler = _send_handler()
    message = _message(
        text="/send @bob 10",
        entities=[MessageEntity(type="bot_command", offset=0, length=5), MessageEntity(type="mention", offset=6, length=4)],
    )
    quota = _quota()
    quota.transfer_quota.side_effect = EligibilityError("剩余额度不足：当前剩余 $5.00")

    await handler(message, quota)

    message.answer.assert_awaited_once_with("剩余额度不足：当前剩余 $5.00")
