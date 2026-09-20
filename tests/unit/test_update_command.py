from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from reclaude_bot.application.updater import UpdateCheck, UpdateError
from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings


def _update_handler():
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    return next(handler.callback for handler in router.message.handlers if handler.callback.__name__ == "update")


def _message(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=555), answer=AsyncMock())


@asynccontextmanager
async def _run_cm():
    yield


def _updater(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "available": True,
        "in_progress": False,
        "run": MagicMock(return_value=_run_cm()),
        "check_for_update": AsyncMock(),
        "apply_update": AsyncMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _check(changed: bool) -> UpdateCheck:
    return UpdateCheck(changed=changed, image_spec="ghcr.io/example/bot:latest", current_image_id="sha256:old", new_image_id="sha256:new")


@pytest.mark.asyncio
async def test_update_handler_ignores_non_admin() -> None:
    handler = _update_handler()
    message = _message(user_id=2)
    updater = _updater()

    await handler(message, updater)

    message.answer.assert_not_awaited()
    updater.check_for_update.assert_not_awaited()
    updater.run.assert_not_called()


@pytest.mark.asyncio
async def test_update_handler_reports_unavailable_without_socket() -> None:
    handler = _update_handler()
    message = _message()
    updater = _updater(available=False)

    await handler(message, updater)

    message.answer.assert_awaited_once_with("自动更新不可用：容器未挂载 Docker socket。")
    updater.run.assert_not_called()


@pytest.mark.asyncio
async def test_update_handler_rejects_concurrent_run() -> None:
    handler = _update_handler()
    message = _message()
    updater = _updater(in_progress=True)

    await handler(message, updater)

    message.answer.assert_awaited_once_with("已有更新正在进行中。")
    updater.run.assert_not_called()


@pytest.mark.asyncio
async def test_update_handler_reports_already_latest() -> None:
    handler = _update_handler()
    message = _message()
    updater = _updater(check_for_update=AsyncMock(return_value=_check(changed=False)))

    await handler(message, updater)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert responses == ["正在检查并拉取最新镜像…", "当前已是最新版本（ghcr.io/example/bot:latest）。"]
    updater.apply_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_handler_applies_new_version() -> None:
    handler = _update_handler()
    message = _message()
    check = _check(changed=True)
    updater = _updater(check_for_update=AsyncMock(return_value=check))

    await handler(message, updater)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert responses == ["正在检查并拉取最新镜像…", "发现新版本，正在更新，Bot 将短暂离线后自动恢复，完成后会通知你。"]
    updater.apply_update.assert_awaited_once_with(check, 555)


@pytest.mark.asyncio
async def test_update_handler_reports_check_failure() -> None:
    handler = _update_handler()
    message = _message()
    updater = _updater(check_for_update=AsyncMock(side_effect=UpdateError("拉取镜像失败：boom")))

    await handler(message, updater)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert responses == ["正在检查并拉取最新镜像…", "更新失败：拉取镜像失败：boom"]
    updater.apply_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_handler_reports_apply_failure() -> None:
    handler = _update_handler()
    message = _message()
    updater = _updater(
        check_for_update=AsyncMock(return_value=_check(changed=True)),
        apply_update=AsyncMock(side_effect=UpdateError("切换容器失败")),
    )

    await handler(message, updater)

    responses = [call.args[0] for call in message.answer.await_args_list]
    assert responses[-1] == "更新失败：切换容器失败"
