from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError


def _taskusers_handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == "task_users")


def _message(chat_type: str = "private", args: str = "vip") -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type=chat_type), answer=AsyncMock())


def _command(args: str = "vip") -> SimpleNamespace:
    return SimpleNamespace(args=args)


def _snapshot(scope_mode: str = "ALLOWLIST", member_ids: tuple[str, ...] = ("u-1",)) -> SimpleNamespace:
    return SimpleNamespace(name="vip", enabled=True, scope_mode=scope_mode, limit_usd=Decimal("50"), member_ids=member_ids)


def _task(snapshot: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(resolve=AsyncMock(return_value="vip"), snapshot=AsyncMock(return_value=snapshot))


def _usage(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"limit_usd": Decimal("50"), "reset_at": datetime(2026, 8, 25, tzinfo=UTC), "members": entries}


def _account() -> SimpleNamespace:
    return SimpleNamespace(
        email_masked="ma****@rekwa.com",
        usage_updated_at=datetime(2026, 8, 18, 5, 0, tzinfo=UTC),
        five_hour_utilization=Decimal("27"),
        five_hour_resets_at=None,
        seven_day_utilization=Decimal("6"),
        seven_day_resets_at=datetime(2026, 8, 25, 5, 0, tzinfo=UTC),
        seven_day_estimated_total=Decimal("3500"),
    )


_ACCOUNT_LINES = (
    "账号：ma****@rekwa.com（快照 2026-08-18T05:00:00+00:00）\n"
    "5h 限额：已用 27.0% | 重置：未激活\n"
    "7天限额：已用 6.0% | 重置：2026-08-25T05:00:00+00:00 | 预估总额度：≈$3500.00"
)


def _entry(rid: str, email: str | None, tg: int | None, used: Decimal | None, remaining: Decimal | None, missing: bool = False) -> dict[str, object]:
    return {
        "reclaude_user_id": rid,
        "email": email,
        "telegram_user_id": tg,
        "user_status": "ACTIVE" if tg is not None else None,
        "used_usd": used,
        "remaining_usd": remaining,
        "missing_upstream": missing,
    }


@pytest.mark.asyncio
async def test_taskusers_handler_renders_all_member_shapes() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot())
    quota = SimpleNamespace(
        list_task_usage=AsyncMock(
            return_value=_usage(
                [
                    _entry("u-1", "alice<admin>@example.com", 301, Decimal("25"), Decimal("25")),
                    _entry("u-2", "bob@example.com", None, Decimal("3"), Decimal("47")),
                    _entry("u-3", "carol@example.com", 303, None, None),
                    _entry("u-9", None, None, None, None, missing=True),
                ]
            )
        ),
        get_account_usage=AsyncMock(return_value=_account()),
    )

    await handler(message, _command(), task, quota)

    message.answer.assert_awaited_once_with(
        "任务：vip | RUNNING | 范围：ALLOWLIST | 成员：4 个 | 任务额度 $50.00 | 周期刷新：2026-08-25T00:00:00+00:00\n"
        f"{_ACCOUNT_LINES}\n"
        "- alice&lt;admin&gt;@example.com | u-1 | TG 301 | ACTIVE | 已用 $25.00 | 剩余 $25.00\n"
        "- bob@example.com | u-2 | 未绑定 | 已用 $3.00 | 剩余 $47.00\n"
        "- carol@example.com | u-3 | TG 303 | ACTIVE | 数据未同步\n"
        "- u-9 | 成员已从上游消失"
    )


@pytest.mark.asyncio
async def test_taskusers_handler_degrades_when_account_usage_unavailable() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot())
    quota = SimpleNamespace(
        list_task_usage=AsyncMock(return_value=_usage([_entry("u-1", "alice@example.com", 301, Decimal("25"), Decimal("25"))])),
        get_account_usage=AsyncMock(side_effect=RuntimeError("me unavailable")),
    )

    await handler(message, _command(), task, quota)

    message.answer.assert_awaited_once_with(
        "任务：vip | RUNNING | 范围：ALLOWLIST | 成员：1 个 | 任务额度 $50.00 | 周期刷新：2026-08-25T00:00:00+00:00\n"
        "账号用量：暂时不可用（上游查询失败）\n"
        "- alice@example.com | u-1 | TG 301 | ACTIVE | 已用 $25.00 | 剩余 $25.00"
    )


@pytest.mark.asyncio
async def test_taskusers_handler_ignores_group_chats() -> None:
    handler = _taskusers_handler()
    message = _message(chat_type="group")
    task = _task(_snapshot())
    quota = SimpleNamespace(list_task_usage=AsyncMock())

    await handler(message, _command(), task, quota)

    task.resolve.assert_not_called()
    quota.list_task_usage.assert_not_called()
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_taskusers_handler_reports_resolution_errors() -> None:
    handler = _taskusers_handler()
    message = _message(args="")
    task = SimpleNamespace(resolve=AsyncMock(side_effect=EligibilityError("存在多个任务，请指定名称：base, vip")), snapshot=AsyncMock())
    quota = SimpleNamespace(list_task_usage=AsyncMock())

    await handler(message, _command(args=""), task, quota)

    message.answer.assert_awaited_once_with("存在多个任务，请指定名称：base, vip")
    quota.list_task_usage.assert_not_called()


@pytest.mark.asyncio
async def test_taskusers_handler_sends_empty_allowlist_state() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot(member_ids=()))
    quota = SimpleNamespace(list_task_usage=AsyncMock(return_value=_usage([])))

    await handler(message, _command(), task, quota)

    message.answer.assert_awaited_once_with("任务 vip 白名单为空，请使用 /addtaskmember 添加成员。")


@pytest.mark.asyncio
async def test_taskusers_handler_sends_empty_all_scope_state() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot(scope_mode="ALL", member_ids=()))
    quota = SimpleNamespace(list_task_usage=AsyncMock(return_value=_usage([])))

    await handler(message, _command(), task, quota)

    message.answer.assert_awaited_once_with("任务范围内暂无成员，请先执行 /sync")


@pytest.mark.asyncio
async def test_taskusers_handler_splits_long_listing_on_line_boundaries() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot())
    entries = [_entry(f"u-{index}", f"user-{index}-{'x' * 40}@example.com", 1000 + index, Decimal("1"), Decimal("49")) for index in range(80)]
    quota = SimpleNamespace(list_task_usage=AsyncMock(return_value=_usage(entries)), get_account_usage=AsyncMock(return_value=_account()))

    await handler(message, _command(), task, quota)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert len(responses) > 1
    assert all(len(response) <= 4000 for response in responses)
    assert responses[0].startswith("任务：vip | RUNNING | 范围：ALLOWLIST | 成员：80 个")
    assert sum(line.startswith("- ") for response in responses for line in response.splitlines()) == 80


@pytest.mark.asyncio
async def test_taskusers_handler_logs_unexpected_error_and_returns_safe_message() -> None:
    handler = _taskusers_handler()
    message = _message()
    task = _task(_snapshot())
    quota = SimpleNamespace(list_task_usage=AsyncMock(side_effect=RuntimeError("usage-secret")))

    with capture_logs() as logs:
        await handler(message, _command(), task, quota)

    event = next(item for item in logs if item.get("event") == "task_usage_listing_failed")
    assert event.get("error_type") == "RuntimeError"
    assert event.get("traceback")
    assert "usage-secret" not in repr(logs)
    message.answer.assert_awaited_once_with("任务成员使用状况暂时不可用。")
