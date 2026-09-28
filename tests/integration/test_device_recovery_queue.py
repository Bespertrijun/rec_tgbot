from datetime import timedelta

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.infrastructure.db.models import DeviceAction, DeviceAssociation
from tests.integration.test_device_reconciliation import NOW, uncertain_revoke

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


@pytest.mark.parametrize("operator", [None, 999])
async def test_periodic_queue_reconciles_old_manual_revocations_without_cycle_ledger(lifecycle_db, operator):
    factory, _ = lifecycle_db
    _, gateway, result, association_id = await uncertain_revoke(factory, operator_id=operator)
    restarted = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW + timedelta(days=8))
    assert await restarted.reconcile_pending() == 1
    assert await restarted.reconcile_pending() == 0
    gateway.revoke_device.assert_awaited_once()
    gateway.list_devices.assert_awaited_once()
    async with factory() as session:
        row = await session.get(DeviceAssociation, association_id)
        assert row.state == "ENDED"
        assert row.end_reason == ("USER_DEAUTH" if operator is None else "ADMIN_DEAUTH")
        assert (await session.get(DeviceAction, result.action_id)).status == "SUCCEEDED"


async def test_periodic_queue_does_not_repeat_unarmed_revoke_or_guess_unknown_auth(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, result, _ = await uncertain_revoke(factory)
    async with factory.begin() as session:
        action = await session.get(DeviceAction, result.action_id)
        action.status = "PENDING"
        action.attempt_count = 0
        linked = await session.scalar(select(DeviceAssociation))
        linked.state = "PENDING_REVOKE"
    assert await service.reconcile_pending() == 0
    gateway.list_devices.assert_not_called()
    gateway.revoke_device.assert_awaited_once()


async def test_periodic_queue_is_org_isolated(lifecycle_db):
    factory, _ = lifecycle_db
    _, gateway, _, _ = await uncertain_revoke(factory)
    other_org = DeviceRevocationService(factory, gateway, 179, clock=lambda: NOW)
    assert await other_org.reconcile_pending() == 0
    gateway.list_devices.assert_not_called()
