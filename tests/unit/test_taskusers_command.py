from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from structlog.testing import capture_logs

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from tests.fixtures.local_user_store import local_user_store


def handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(item.callback for item in router.message.handlers if item.callback.__name__ == "task_users")


def setup(*, mode="ALLOWLIST", count=2, chat_type="private", error=None):
    event = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type=chat_type), answer=AsyncMock())
    command = SimpleNamespace(args="vip")
    task = SimpleNamespace(resolve=AsyncMock(return_value="vip"), snapshot=AsyncMock(return_value=SimpleNamespace(
        id=1, name="vip", enabled=True, scope_mode=mode, limit_usd=Decimal("700"))))
    users = [SimpleNamespace(id=i, email=f"user-{i}<tag>@example.com", binding_status="BOUND", status="ACTIVE") for i in range(1, count + 1)]
    members = SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace(covered_user_ids=tuple(range(1, count + 1)))))
    quota = SimpleNamespace(session_factory=local_user_store(users, error=error), status=AsyncMock(return_value=SimpleNamespace(
        used_usd=Decimal("25"), remaining_usd=Decimal("675"), device_id=44500, quality="VERIFIED")))
    return event, command, task, members, quota


async def test_taskusers_displays_local_device_accounting_and_escapes_email():
    args = setup()
    await handler()(*args)
    event, _, _, _, quota = args
    text = event.answer.await_args.args[0]
    assert "本地用户：2 个" in text and "设备 44500" in text and "已用 $25.00" in text and "剩余 $675.00" in text
    assert "user-1&lt;tag&gt;@example.com" in text
    quota.status.assert_any_await(1, task_id=1)


async def test_taskusers_discloses_unsynced_amounts():
    args = setup(count=1)
    args[-1].status.return_value = SimpleNamespace(used_usd=None, remaining_usd=None, device_id=44500, quality="UNKNOWN")
    await handler()(*args)
    text = args[0].answer.await_args.args[0]
    assert "待同步" in text and "剩余 未知" in text and "已用 $0.00" not in text


async def test_taskusers_ignores_group_chats():
    args = setup(chat_type="supergroup")
    await handler()(*args)
    args[2].resolve.assert_not_called()
    args[0].answer.assert_not_called()


async def test_taskusers_reports_resolution_errors():
    args = setup()
    args[2].resolve.side_effect = EligibilityError("任务不存在")
    await handler()(*args)
    args[0].answer.assert_awaited_once_with("任务不存在")


async def test_taskusers_sends_empty_allowlist_state():
    args = setup(count=0)
    await handler()(*args)
    assert "白名单为空" in args[0].answer.await_args.args[0]
    args[-1].status.assert_not_called()


async def test_taskusers_sends_empty_all_scope_state():
    args = setup(count=0, mode="ALL")
    await handler()(*args)
    args[0].answer.assert_awaited_once_with("任务范围内暂无本地用户。")


async def test_taskusers_splits_long_listing_without_losing_users():
    args = setup(count=80)
    await handler()(*args)
    messages = [call.args[0] for call in args[0].answer.await_args_list]
    assert len(messages) > 1 and all(len(text) <= 4000 for text in messages)
    assert sum(line.startswith("- ") for text in messages for line in text.splitlines()) == 80


async def test_taskusers_failure_logs_type_without_exposing_database_text():
    args = setup(error=RuntimeError("private database string"))
    with capture_logs() as logs:
        await handler()(*args)
    args[0].answer.assert_awaited_once_with("任务成员使用状况暂时不可用。")
    event = next(row for row in logs if row.get("event") == "task_usage_listing_failed")
    assert event["error_type"] == "RuntimeError" and event["traceback"]
    assert "private database string" not in repr(logs)
