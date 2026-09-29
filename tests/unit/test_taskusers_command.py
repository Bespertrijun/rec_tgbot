from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from structlog.testing import capture_logs

from reclaude_bot.application.device_account_usage import DeviceAccountUsage
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
    now = datetime(2026, 9, 29, tzinfo=UTC)
    account = SimpleNamespace(get_account_usage=AsyncMock(return_value=DeviceAccountUsage(
        email_masked="a<masked>@example.com", usage_updated_at=now,
        five_hour_utilization=Decimal("35"), five_hour_resets_at=now + timedelta(hours=3),
        seven_day_utilization=Decimal("10"), seven_day_resets_at=now + timedelta(days=4),
        managed_used_usd=Decimal("120"), estimated_total_usd=Decimal("1200"), estimate_reason=None,
    )))
    return event, command, task, members, quota, account


async def test_taskusers_displays_local_device_accounting_and_escapes_email():
    args = setup()
    await handler()(*args)
    event, _, _, _, quota, account = args
    text = event.answer.await_args.args[0]
    assert "本地用户：2 个" in text and "设备 44500" in text and "已用 $25.00" in text and "剩余 $675.00" in text
    assert "user-1&lt;tag&gt;@example.com" in text
    quota.status.assert_any_await(1, task_id=1)
    account.get_account_usage.assert_awaited_once_with(1)
    assert "预估总额度：≈$1200.00" in text and "按托管设备消费估算，仅供参考" in text
    assert "本周期托管设备已用：$120.00" in text and "5h 限额：已用 35.0%" in text
    assert "7天限额：已用 10.0%" in text and "a&lt;masked&gt;@example.com" in text


async def test_taskusers_discloses_unsynced_amounts():
    args = setup(count=1)
    args[4].status.return_value = SimpleNamespace(used_usd=None, remaining_usd=None, device_id=44500, quality="UNKNOWN")
    await handler()(*args)
    text = args[0].answer.await_args.args[0]
    assert "待同步" in text and "剩余 未知" in text and "已用 $0.00" not in text


async def test_taskusers_ignores_group_chats():
    args = setup(chat_type="supergroup")
    await handler()(*args)
    args[2].resolve.assert_not_called()
    args[-1].get_account_usage.assert_not_called()
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
    args[4].status.assert_not_called()
    assert "预估总额度：≈$1200.00" in args[0].answer.await_args.args[0]


async def test_taskusers_sends_empty_all_scope_state():
    args = setup(count=0, mode="ALL")
    await handler()(*args)
    text = args[0].answer.await_args.args[0]
    assert "任务范围内暂无本地用户。" in text and "账号：" in text


async def test_taskusers_splits_long_listing_without_losing_users():
    args = setup(count=80)
    await handler()(*args)
    messages = [call.args[0] for call in args[0].answer.await_args_list]
    assert len(messages) > 1 and all(len(text) <= 4000 for text in messages)
    assert sum(line.startswith("- ") for text in messages for line in text.splitlines()) == 80
    args[-1].get_account_usage.assert_awaited_once_with(1)


async def test_taskusers_failure_logs_type_without_exposing_database_text():
    args = setup(error=RuntimeError("private database string"))
    with capture_logs() as logs:
        await handler()(*args)
    args[0].answer.assert_awaited_once_with("任务成员使用状况暂时不可用。")
    event = next(row for row in logs if row.get("event") == "task_usage_listing_failed")
    assert event["error_type"] == "RuntimeError" and event["traceback"]
    assert "private database string" not in repr(logs)


async def test_taskusers_non_admin_never_reads_account_data():
    args = setup()
    args[0].from_user.id = 55
    await handler()(*args)
    args[-1].get_account_usage.assert_not_called()
    args[0].answer.assert_not_called()


async def test_account_query_failure_preserves_member_details_and_hides_error_payload():
    args = setup()
    args[-1].get_account_usage.side_effect = RuntimeError("private upstream payload")
    with capture_logs() as logs:
        await handler()(*args)
    text = args[0].answer.await_args.args[0]
    assert "账号用量：暂时不可用" in text
    assert text.count("- 本地 ID") == 2 and "剩余 $675.00" in text
    assert "private upstream payload" not in text + repr(logs)


async def test_unavailable_estimate_displays_reason_and_no_fabricated_zero():
    args = setup()
    account = args[-1]
    account.get_account_usage.return_value = replace(
        account.get_account_usage.return_value, estimated_total_usd=None, managed_used_usd=None,
        estimate_reason="账号<周期>不匹配", five_hour_utilization=None, five_hour_resets_at=None,
    )
    await handler()(*args)
    text = args[0].answer.await_args.args[0]
    assert "预估总额度：—（账号&lt;周期&gt;不匹配）" in text
    assert "本周期托管设备已用：—" in text and "5h 限额：已用 未知 | 重置：未激活" in text
    assert "≈$0.00" not in text
