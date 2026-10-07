from __future__ import annotations

import asyncio

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from reclaude_bot.application.account_usage_refresh import AccountUsageRefreshService
from reclaude_bot.application.actions import DeviceQuotaActionService, QuotaActionService
from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.device import DeviceAuthorizationService
from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.application.device_account_reconcile import DeviceAccountReconcileService
from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_context import SingleOrgAccountSource
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_metering import DeviceMeteringService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.application.device_sampling import DeviceSamplingService
from reclaude_bot.application.device_task_members import DeviceTaskMemberService
from reclaude_bot.application.device_usage import DeviceUsageCollector
from reclaude_bot.application.groups import GroupService
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.application.updater import UpdateService, cleanup_stale_updating_container, consume_restart_notification
from reclaude_bot.bot.autodelete import AutoDeleteBot
from reclaude_bot.bot.commands import register_command_menus, restore_group_admin_menus
from reclaude_bot.bot.groups import TelegramGroupGateway, build_group_router
from reclaude_bot.bot.handlers import build_admin_router, build_router
from reclaude_bot.config import get_settings
from reclaude_bot.infrastructure.db.database import create_session_factory
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient
from reclaude_bot.jobs.onboarding import OnboardingWorker
from reclaude_bot.jobs.scheduler import BackgroundJobs
from reclaude_bot.logging import configure_logging


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    session_factory = create_session_factory(settings)
    gate = RecoveryGate(session_factory)
    startup_state = await gate.ensure_disabled()
    bot = AutoDeleteBot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), admin_ids=settings.telegram_admin_ids)
    group_gateway = TelegramGroupGateway(bot)
    groups = GroupService(session_factory, group_gateway, settings.telegram_admin_ids)
    onboarding = OnboardingService(session_factory)

    async def auth_alert() -> None:
        await gate.disable_from_401()
        for admin_id in settings.telegram_admin_ids:
            await bot.send_message(admin_id, "高优先级告警：限额执行暂停。Reclaude 会话返回 401，请完成 Cookie 恢复和全量 reconcile。")

    async def operational_alert(message: str) -> None:
        for admin_id in settings.telegram_admin_ids:
            await bot.send_message(admin_id, f"高优先级告警：{message}")

    async def user_notify(telegram_id: int, text: str) -> None:
        await bot.send_message(telegram_id, text)

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
    actions = QuotaActionService(session_factory, gateway, quota, settings, gate=gate, alert_callback=operational_alert, user_notify_callback=user_notify)
    onboarding_worker = OnboardingWorker(
        onboarding,
        group_gateway,
        bot,
        owner_ids=settings.telegram_admin_ids,
        alert_callback=operational_alert,
        group_titles=groups,
    )
    binding = BindingService(session_factory, gateway, settings.bind_attempts_per_hour, gate=gate, onboarding=onboarding)
    recovery = RecoveryService(gate, quota, gateway, settings)
    await recovery.restore_persisted_account(startup_state)
    task = QuotaTaskService(session_factory, gateway, org_id=settings.reclaude_org_id)
    device_quota = DeviceQuotaService(session_factory, settings.reclaude_org_id)
    device_account_source = SingleOrgAccountSource(gateway, settings.reclaude_org_id)
    device_cycle = DeviceCycleService(
        session_factory,
        device_account_source,
        settings.reclaude_org_id,
    )
    device_account_usage = DeviceAccountUsageService(
        session_factory,
        device_account_source,
        settings.reclaude_org_id,
        refresh=AccountUsageRefreshService(session_factory, gateway),
    )
    device_collector = DeviceUsageCollector(session_factory, gateway, settings.reclaude_org_id)
    device_ledger = DeviceLedgerService(session_factory, settings.reclaude_org_id)
    device_metering = DeviceMeteringService(
        session_factory,
        device_collector,
        device_ledger,
        settings.reclaude_org_id,
        timeout_seconds=settings.api_timeout_seconds,
    )
    device_sampling = DeviceSamplingService(
        session_factory,
        device_metering,
        settings.reclaude_org_id,
    )
    device_revocation = DeviceRevocationService(
        session_factory,
        gateway,
        settings.reclaude_org_id,
        before_revoke=device_sampling.before_revoke,
        after_revoked=device_sampling.after_revoked,
    )
    device_quota_actions = DeviceQuotaActionService(
        session_factory,
        device_quota,
        device_revocation,
        user_notify_callback=user_notify,
        alert_callback=operational_alert,
        gate=gate,
    )
    device_account_notifications = DeviceAccountNotificationService(
        session_factory,
        user_notify_callback=user_notify,
        admin_ids=settings.telegram_admin_ids,
    )
    device_reset = DeviceTaskResetService(
        session_factory,
        gateway,
        device_cycle,
        settings.reclaude_org_id,
        account_notifications=device_account_notifications,
    )
    device_account_reconcile = DeviceAccountReconcileService(
        session_factory,
        gateway,
        device_account_source,
        device_cycle,
        device_reset,
        settings.reclaude_org_id,
        gate=gate,
        task_service=task,
        account_notifications=device_account_notifications,
    )
    device_authorization = DeviceAuthorizationService(
        session_factory,
        gateway,
        settings.reclaude_org_id,
        device_quota.quota_check,
        on_authorized=device_sampling.after_authorized,
        before_authorize=device_account_reconcile.prepare,
    )
    device_admin = DeviceAdminService(
        session_factory,
        gateway,
        settings.reclaude_org_id,
        device_quota.quota_check,
        on_authorized=device_sampling.after_authorized,
        before_authorize=device_account_reconcile.prepare,
    )
    device_task_members = DeviceTaskMemberService(session_factory, settings.reclaude_org_id)
    admin = AdminService(session_factory, quota, task=task)
    jobs = BackgroundJobs(
        quota,
        actions,
        onboarding_worker,
        task_service=task,
        device_cycle=device_cycle,
        device_account_usage=device_account_usage,
        device_sampling=device_sampling,
        device_actions=device_quota_actions,
        device_account_reconcile=device_account_reconcile,
        device_account_notifications=device_account_notifications,
    )
    updater = UpdateService(settings)
    dp = Dispatcher()
    dp["binding"] = binding
    dp["quota"] = quota
    dp["actions"] = actions
    dp["recovery"] = recovery
    dp["task"] = task
    dp["jobs"] = jobs
    dp["admin"] = admin
    dp["device_quota"] = device_quota
    dp["device_account_usage"] = device_account_usage
    dp["device_cycle"] = device_cycle
    dp["device_account_reconcile"] = device_account_reconcile
    dp["device_account_notifications"] = device_account_notifications
    dp["device_sampling"] = device_sampling
    dp["device_auth"] = device_authorization
    dp["device_admin"] = device_admin
    dp["device_revocation"] = device_revocation
    dp["device_task_members"] = device_task_members
    dp["device_reset"] = device_reset
    dp["groups"] = groups
    dp["onboarding"] = onboarding
    dp["onboarding_worker"] = onboarding_worker
    dp["updater"] = updater
    dp.include_router(build_router(settings))
    dp.include_router(build_admin_router(settings))
    dp.include_router(build_group_router(settings))
    try:
        await register_command_menus(bot, settings.telegram_admin_ids)
        await restore_group_admin_menus(bot, groups, settings.telegram_admin_ids)
        await cleanup_stale_updating_container()
        asyncio.create_task(consume_restart_notification(bot, settings))
        await jobs.start(start_quota=False)
        if await task.any_enabled():
            try:
                await device_account_reconcile.reconcile()
            except Exception as exc:
                # Keep the durable task intent so the reconciler can retry on
                # the next statistics tick; the reconcile failure reason keeps
                # the write latch closed across this restart.
                await gate.disable("account_reconcile_failed")
                await operational_alert(f"限额任务账号核对失败，写操作已暂停并将自动重试：{exc}")
        # The quota loop always runs: usage sync continues while tasks are STOPPED;
        # resume_quota_task re-opens the write latch only when a task is RUNNING.
        await jobs.resume_quota_task()
        allowed_updates = dp.resolve_used_update_types()
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        await jobs.stop()
        await gateway.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
