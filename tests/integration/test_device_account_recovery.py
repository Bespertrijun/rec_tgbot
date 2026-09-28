from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.recovery import RecoveryGate, RecoveryService
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceCycleLedger, DeviceQuotaCycle, QuotaTask, ServiceState
from reclaude_bot.infrastructure.reclaude.models import AccountsResponse
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_cycle import snapshot

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def service(factory):
    gateway = SimpleNamespace(account_id=7022, authenticate=AsyncMock(return_value=snapshot().me),
                              accounts=AsyncMock(return_value=AccountsResponse.model_validate({"items": [
                                  {"id": 1, "account_id": 7022, "health": "healthy", "lifecycle": "bound", "org_id": 178},
                                  {"id": 2, "account_id": 8123, "health": "healthy", "lifecycle": "bound", "org_id": 178},
                              ]})), members=AsyncMock(), assign=AsyncMock(), revoke=AsyncMock())
    gateway.configure_account_id = lambda value: setattr(gateway, "account_id", value)
    legacy_quota = AsyncMock()
    configured = Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test")
    return RecoveryService(RecoveryGate(factory), legacy_quota, gateway, configured), gateway, legacy_quota


async def test_account_switch_rejected_without_mutating_device_accounting_or_making_api_calls(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, cycle_id, "750", locked=True)
    recovery, gateway, old_quota = service(factory)
    with pytest.raises(EligibilityError, match="暂不支持切换"):
        await recovery.select_account(8123, 999)
    gateway.authenticate.assert_not_called()
    gateway.accounts.assert_not_called()
    assert old_quota.mock_calls == []
    async with factory() as session:
        assert (await session.get(ServiceState, 1)).selected_account_id == "7022"
        ledger = await session.get(DeviceCycleLedger, ledger_id)
        assert ledger.confirmed_used_usd == 750 and ledger.quota_locked_at is not None


async def test_reselecting_same_account_preserves_usage_and_lock_without_member_sync(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, cycle_id, "750", locked=True)
    recovery, gateway, old_quota = service(factory)
    assert (await recovery.select_account(7022, 999)).account_id == 7022
    gateway.members.assert_not_called()
    gateway.assign.assert_not_called()
    gateway.revoke.assert_not_called()
    assert old_quota.mock_calls == []
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, ledger_id)
        assert ledger.confirmed_used_usd == 750 and ledger.quota_locked_at is not None
        assert (await session.get(DeviceQuotaCycle, cycle_id)).account_id == "7022"


async def test_recovery_validation_does_not_resume_tasks_or_reassign_members(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    _, ledger_id = await metered_user(factory, cycle_id, "750", locked=True)
    recovery, gateway, old_quota = service(factory)
    await recovery.health_sync_reconcile_enable(999)
    async with factory() as session:
        assert (await session.get(QuotaTask, 1)).status == "STOPPED"
        assert (await session.get(ServiceState, 1)).write_enabled is False
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == 750
    gateway.members.assert_not_called()
    gateway.assign.assert_not_called()
    gateway.revoke.assert_not_called()
    assert old_quota.mock_calls == []
