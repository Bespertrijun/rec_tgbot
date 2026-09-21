from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings


def _start_task_handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == "start_task")


def _message() -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())


def _deps(*, sync_enabled: bool) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    command = SimpleNamespace(args="default")
    task = SimpleNamespace(resolve=AsyncMock(return_value="default"), sync_enabled=AsyncMock(return_value=sync_enabled))
    recovery = SimpleNamespace(validate_selected_account=AsyncMock(return_value=SimpleNamespace(account_id=4949)))
    jobs = SimpleNamespace(start_quota_task=AsyncMock())
    return command, task, recovery, jobs


@pytest.mark.asyncio
async def test_start_task_warns_when_usage_sync_is_stopped() -> None:
    handler = _start_task_handler()
    message = _message()
    command, task, recovery, jobs = _deps(sync_enabled=False)

    await handler(message, command, task, recovery, jobs)

    jobs.start_quota_task.assert_awaited_once_with("default", 1)
    message.answer.assert_awaited_once_with(
        "限额任务 default 已启动，写操作已开启（账号 4949）。\n注意：数据统计当前已停止，配额动作不会自动执行；恢复统计请使用 /startstats。"
    )


@pytest.mark.asyncio
async def test_start_task_reports_plain_success_while_usage_sync_runs() -> None:
    handler = _start_task_handler()
    message = _message()
    command, task, recovery, jobs = _deps(sync_enabled=True)

    await handler(message, command, task, recovery, jobs)

    message.answer.assert_awaited_once_with("限额任务 default 已启动，写操作已开启（账号 4949）。")
