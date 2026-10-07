from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from reclaude_bot import main
from reclaude_bot.application.actions import DeviceQuotaActionService
from reclaude_bot.application.device import DeviceAuthorizationService
from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.application.device_account_reconcile import DeviceAccountReconcileService
from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.db.models import QuotaTask, ServiceState
from reclaude_bot.infrastructure.reclaude.fake import FakeReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import AccountsResponse
from reclaude_bot.jobs.scheduler import BackgroundJobs
from tests.fixtures.device_runtime import ready_cycle
from tests.integration.test_device_cycle import snapshot

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


@pytest.mark.parametrize("transient_account_failure", [False, True])
async def test_main_injects_device_services_fresh_query_and_write_gate_without_legacy_api(lifecycle_db, monkeypatch, transient_account_failure):
    factory, _ = lifecycle_db
    now = datetime.now(UTC)
    await ready_cycle(factory, now=now)
    me = snapshot(sampled_at=now, reset=now + timedelta(days=6)).me
    gateway: Any = FakeReclaudeGateway([], me, accounts=[{
        "id": 1, "account_id": 7022, "account_email": "owner@example.invalid",
        "health": "healthy", "lifecycle": "bound", "org_id": 178,
    }])
    gateway.members = AsyncMock(side_effect=AssertionError("legacy member query reached main"))
    gateway.assign = AsyncMock(side_effect=AssertionError("legacy assign reached main"))
    gateway.revoke = AsyncMock(side_effect=AssertionError("legacy member revoke reached main"))
    gateway.list_devices = AsyncMock(return_value=[])
    gateway.close = AsyncMock()
    gateway.revoke_device = AsyncMock()
    gateway.device_usage = AsyncMock()
    gateway.describe_device_auth = AsyncMock()
    gateway.approve_device_auth = AsyncMock()
    if transient_account_failure:
        inventory = AccountsResponse.model_validate({"items": gateway.account_rows})
        gateway.accounts = AsyncMock(side_effect=[TimeoutError("upstream unavailable"), inventory, inventory])
    configured = Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
                          TELEGRAM_BOT_TOKEN="123:test-token", TELEGRAM_ADMIN_IDS=[999],
                          RECLAUDE_ORG_ID=178, RECLAUDE_BASE_URL="https://rec.example.invalid", LOG_FILE_PATH=None)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), send_message=AsyncMock())
    dispatchers = []
    jobs_created = []

    class FakeDispatcher(dict):
        def __init__(self):
            super().__init__()
            dispatchers.append(self)

        def include_router(self, router):
            pass

        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, bot, **kwargs):
            pass

    def jobs_factory(*args, **kwargs):
        jobs = BackgroundJobs(*args, **kwargs)
        jobs.start = AsyncMock()
        jobs.resume_quota_task = AsyncMock()
        jobs.stop = AsyncMock()
        jobs_created.append(jobs)
        return jobs

    monkeypatch.setattr(main, "get_settings", lambda: configured)
    monkeypatch.setattr(main, "create_session_factory", lambda _: factory)
    monkeypatch.setattr(main, "ReclaudeClient", lambda *args, **kwargs: gateway)
    monkeypatch.setattr(main, "AutoDeleteBot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(main, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(main, "BackgroundJobs", jobs_factory)
    monkeypatch.setattr(main, "configure_logging", lambda *args: None)
    for name in ("register_command_menus", "restore_group_admin_menus", "cleanup_stale_updating_container", "consume_restart_notification"):
        monkeypatch.setattr(main, name, AsyncMock())
    await main.run()
    if transient_account_failure:
        async with factory() as session:
            assert (await session.get(QuotaTask, 1)).status == "RUNNING"
            assert not (await session.get(ServiceState, 1)).write_enabled
    values = list(dispatchers[0].values())
    assert isinstance(dispatchers[0]["device_reset"], DeviceTaskResetService)
    auth = next(value for value in values if isinstance(value, DeviceAuthorizationService))
    admin = next(value for value in values if isinstance(value, DeviceAdminService))
    actions = jobs_created[0].device_actions
    assert isinstance(actions, DeviceQuotaActionService)
    assert isinstance(auth.before_authorize.__self__, DeviceAccountReconcileService)
    assert admin.before_authorize.__self__ is auth.before_authorize.__self__
    assert actions.gate is not None
    assert actions.revocation.quota_revoke_check.gate is actions.gate
    assert jobs_created[0].device_account_reconcile is auth.before_authorize.__self__
    notices = jobs_created[0].device_account_notifications
    assert isinstance(notices, DeviceAccountNotificationService)
    assert notices.admin_ids == (999,)
    assert dispatchers[0]["device_reset"].account_notifications is notices
    assert auth.before_authorize.__self__.account_notifications is notices
    assert notices.user_notify_callback is not None
    assert jobs_created[0].device_cycle is auth.before_authorize.__self__.cycle_service
    assert jobs_created[0].device_account_usage is dispatchers[0]["device_account_usage"]
    assert jobs_created[0].device_actions is actions
    assert jobs_created[0].device_sampling is not None
    await jobs_created[0].device_cycle.sync()
    gateway.members.assert_not_called()
    gateway.assign.assert_not_called()
    gateway.revoke.assert_not_called()
    gateway.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()
