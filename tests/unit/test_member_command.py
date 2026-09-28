from types import SimpleNamespace
from unittest.mock import AsyncMock

from structlog.testing import capture_logs

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from tests.fixtures.local_user_store import local_user_store


def handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(item.callback for item in router.message.handlers if item.callback.__name__ == "member_list")


def message():
    return SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())


def user(index, email):
    return SimpleNamespace(id=index, email=email, binding_status="BOUND", status="ACTIVE", telegram_user_id=300 + index)


async def test_member_handler_escapes_local_fields_and_reports_local_ids():
    event = message()
    quota = SimpleNamespace(session_factory=local_user_store([user(1, "alice<admin>@example.com"), user(2, "bob@example.com")]))
    await handler()(event, quota)
    text = event.answer.await_args.args[0]
    assert "本地用户：2 个" in text and "本地 ID 1" in text and "本地 ID 2" in text
    assert "alice&lt;admin&gt;@example.com" in text and "alice<admin>" not in text


async def test_member_handler_sends_empty_state():
    event = message()
    await handler()(event, SimpleNamespace(session_factory=local_user_store([])))
    event.answer.assert_awaited_once_with("暂无本地用户")


async def test_member_handler_splits_long_listing_on_line_boundaries():
    event = message()
    users = [user(index, f"user-{index}-{'x' * 45}@example.com") for index in range(80)]
    await handler()(event, SimpleNamespace(session_factory=local_user_store(users)))
    responses = [call.args[0] for call in event.answer.await_args_list]
    assert len(responses) > 1 and all(len(response) <= 4000 for response in responses)
    assert responses[0].startswith("本地用户：80 个")
    assert sum(line.startswith("- ") for response in responses for line in response.splitlines()) == 80


async def test_member_handler_logs_unexpected_error_and_returns_safe_message():
    event = message()
    quota = SimpleNamespace(session_factory=local_user_store([], error=RuntimeError("member-secret")))
    with capture_logs() as logs:
        await handler()(event, quota)
    item = next(row for row in logs if row.get("event") == "local_member_listing_failed")
    assert item.get("error_type") == "RuntimeError" and item.get("traceback")
    assert "member-secret" not in repr(logs)
    event.answer.assert_awaited_once_with("成员列表暂时不可用。")
