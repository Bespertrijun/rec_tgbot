from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from reclaude_bot.application.actions import QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.bot.handlers import build_admin_router, build_router
from reclaude_bot.config import get_settings
from reclaude_bot.infrastructure.db.database import create_session_factory
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient
from reclaude_bot.jobs.scheduler import BackgroundJobs
from reclaude_bot.logging import configure_logging


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    session_factory = create_session_factory(settings)
    gate = RecoveryGate(session_factory)
    await gate.ensure_disabled()
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def auth_alert() -> None:
        await gate.disable_from_401()
        for admin_id in settings.telegram_admin_ids:
            await bot.send_message(admin_id, "高优先级告警：限额执行暂停。Reclaude 会话返回 401，请完成 Cookie 恢复和全量 reconcile。")

    async def operational_alert(message: str) -> None:
        for admin_id in settings.telegram_admin_ids:
            await bot.send_message(admin_id, f"高优先级告警：{message}")

    gateway = ReclaudeClient(
        settings.reclaude_base_url,
        session_cookie=settings.reclaude_session_cookie,
        login_email=settings.reclaude_login_email,
        login_password=settings.reclaude_login_password,
        cookie_jar_path=settings.reclaude_cookie_jar_path,
        user_agent=settings.reclaude_user_agent,
        timeout=settings.api_timeout_seconds,
        max_retries=settings.api_max_retries,
        org_id=settings.reclaude_org_id,
        auth_alert_callback=auth_alert,
    )
    quota = QuotaService(session_factory, gateway, settings)
    actions = QuotaActionService(session_factory, gateway, quota, settings, gate=gate, alert_callback=operational_alert)
    binding = BindingService(session_factory, gateway, settings.bind_attempts_per_hour, gate=gate)
    recovery = RecoveryService(gate, quota, gateway, settings)
    admin = AdminService(session_factory, quota, actions)
    jobs = BackgroundJobs(quota, actions)
    dp = Dispatcher()
    dp["binding"] = binding
    dp["quota"] = quota
    dp["actions"] = actions
    dp["recovery"] = recovery
    dp["admin"] = admin
    dp.include_router(build_router(settings))
    dp.include_router(build_admin_router(settings))
    try:
        await jobs.start()
        await dp.start_polling(bot)
    finally:
        await jobs.stop()
        await gateway.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
