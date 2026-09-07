from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings


def _member_handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == "member_list")


def _message() -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())


@pytest.mark.asyncio
async def test_member_handler_escapes_fields_and_reports_latest_sample() -> None:
    handler = _member_handler()
    message = _message()
    quota = SimpleNamespace(
        list_upstream_members=AsyncMock(
            return_value=[
                SimpleNamespace(
                    email="alice<admin>@example.com",
                    reclaude_user_id="u<&1",
                    sampled_at=datetime(2026, 8, 18, tzinfo=UTC),
                ),
                SimpleNamespace(
                    email="bob@example.com",
                    reclaude_user_id="u-2",
                    sampled_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
                ),
            ]
        )
    )

    await handler(message, quota)

    message.answer.assert_awaited_once_with(
        "上游成员：2 个 | 最近同步：2026-08-18T01:00:00+00:00\n"
        "- alice&lt;admin&gt;@example.com | u&lt;&amp;1\n"
        "- bob@example.com | u-2"
    )


@pytest.mark.asyncio
async def test_member_handler_sends_empty_state() -> None:
    handler = _member_handler()
    message = _message()
    quota = SimpleNamespace(list_upstream_members=AsyncMock(return_value=[]))

    await handler(message, quota)

    message.answer.assert_awaited_once_with("暂无上游成员，请先执行 /sync")


@pytest.mark.asyncio
async def test_member_handler_splits_long_listing_on_line_boundaries() -> None:
    handler = _member_handler()
    message = _message()
    sampled_at = datetime(2026, 8, 18, tzinfo=UTC)
    members = [
        SimpleNamespace(
            email=f"user-{index}-{'x' * 45}@example.com",
            reclaude_user_id=f"u-{index}-{'y' * 30}",
            sampled_at=sampled_at,
        )
        for index in range(80)
    ]
    quota = SimpleNamespace(list_upstream_members=AsyncMock(return_value=members))

    await handler(message, quota)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert len(responses) > 1
    assert all(len(response) <= 4000 for response in responses)
    assert responses[0].startswith("上游成员：80 个 | 最近同步：")
    assert sum(line.startswith("- ") for response in responses for line in response.splitlines()) == 80


@pytest.mark.asyncio
async def test_member_handler_logs_unexpected_error_and_returns_safe_message() -> None:
    handler = _member_handler()
    message = _message()
    quota = SimpleNamespace(list_upstream_members=AsyncMock(side_effect=RuntimeError("member-secret")))

    with capture_logs() as logs:
        await handler(message, quota)

    event = next(item for item in logs if item.get("event") == "upstream_member_listing_failed")
    assert event.get("error_type") == "RuntimeError"
    assert event.get("traceback")
    assert "member-secret" not in repr(logs)
    message.answer.assert_awaited_once_with("成员列表暂时不可用。")
