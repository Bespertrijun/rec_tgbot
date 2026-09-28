import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceAction, DeviceAssociation, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRecord, DeviceRevokeResponse

NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def active(factory, *, device_id=44500, org_id=178, task_id=1, state="ACTIVE"):
    async with factory.begin() as session:
        row = DeviceAssociation(user_id=1, task_id=task_id, org_id=org_id, device_id=device_id,
                                state=state, started_at=NOW - timedelta(hours=1), updated_at=NOW)
        session.add(row)
        await session.flush()
        return row.id


def device_record(*, org_id=178, revoked=False):
    return DeviceRecord(id=44500, org_id=org_id, name="laptop", created_at=NOW - timedelta(days=1),
                        revoked_at=NOW if revoked else None)


async def uncertain_revoke(factory, *, operator_id=None):
    association_id = await active(factory)
    gateway = SimpleNamespace(account_id=7022,
                              revoke_device=AsyncMock(side_effect=httpx.ReadTimeout("private failure")),
                              list_devices=AsyncMock(return_value=[]))
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    result = await service.deauth(1, operator_id=operator_id)
    assert result.status == "UNKNOWN"
    return service, gateway, result, association_id


@pytest.mark.parametrize("operator_id", [None, 999])
@pytest.mark.parametrize("listed", [False, True])
async def test_complete_list_confirms_revoke_preserving_original_reason_and_identity(lifecycle_db, operator_id, listed):
    factory, _ = lifecycle_db
    service, gateway, previous, association_id = await uncertain_revoke(factory, operator_id=operator_id)
    gateway.list_devices.return_value = [device_record(revoked=True)] if listed else []
    result = await service.reconcile_revoke(previous.action_id)
    assert (result.status, result.association_id, result.device_id) == ("SUCCEEDED", association_id, 44500)
    assert await service.reconcile_revoke(previous.action_id) == result
    gateway.revoke_device.assert_awaited_once_with(44500)
    gateway.list_devices.assert_awaited_once()
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.state == "ENDED" and association.ended_at is not None
        assert association.end_reason == ("USER_DEAUTH" if operator_id is None else "ADMIN_DEAUTH")
        user = await session.get(User, 1)
        assert (user.binding_status, user.email) == ("BOUND", "1@example.invalid")
        action = await session.get(DeviceAction, previous.action_id)
        assert action.attempt_count == 1 and action.completed_at is not None


@pytest.mark.parametrize("case", ["active", "timeout", "invalid", "wrong_org", "duplicate", "account_changed", "circuit"])
async def test_unconfirmed_evidence_never_releases_slot_or_retries_revoke(lifecycle_db, case):
    factory, _ = lifecycle_db
    service, gateway, previous, association_id = await uncertain_revoke(factory)
    if case == "active":
        gateway.list_devices.return_value = [device_record()]
    elif case == "timeout":
        gateway.list_devices.side_effect = httpx.ReadTimeout("private session")
    elif case == "invalid":
        gateway.list_devices.return_value = {"devices": []}
    elif case == "wrong_org":
        gateway.list_devices.return_value = [device_record(org_id=179)]
    elif case == "duplicate":
        gateway.list_devices.return_value = [device_record(), device_record(revoked=True)]
    elif case == "account_changed":
        async def change_account():
            gateway.account_id = 8000
            return []
        gateway.list_devices.side_effect = change_account
    else:
        gateway.list_devices.side_effect = AuthenticationCircuitOpen("private session")
    if case == "circuit":
        with pytest.raises(AuthenticationCircuitOpen):
            await service.reconcile_revoke(previous.action_id)
    else:
        try:
            result = await service.reconcile_revoke(previous.action_id)
        except EligibilityError:
            pass
        else:
            assert result.status == "UNKNOWN"
    gateway.revoke_device.assert_awaited_once()
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None
        action = await session.get(DeviceAction, previous.action_id)
        assert action.status == "UNKNOWN"
        assert "private" not in (action.last_error_code or "")


@pytest.mark.parametrize("case", ["missing", "invalid_id", "auth", "other_org", "no_origin"])
async def test_invalid_or_unowned_action_is_rejected_without_list_request(lifecycle_db, case):
    factory, _ = lifecycle_db
    gateway = SimpleNamespace(account_id=7022, revoke_device=AsyncMock(), list_devices=AsyncMock(return_value=[]))
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    if case == "missing":
        action_id = 999
    elif case == "invalid_id":
        action_id = True
    else:
        other = case == "other_org"
        association_id = await active(factory, org_id=179 if other else 178, task_id=2 if other else 1,
                                      device_id=44502 if other else 44500, state="UNKNOWN")
        async with factory.begin() as session:
            action = DeviceAction(association_id=association_id, kind="AUTH" if case == "auth" else "REVOKE",
                                  status="UNKNOWN", idempotency_key=f"reconciliation-{case}",
                                  target_device_id=44502 if other else 44500,
                                  attempt_count=1, created_at=NOW, updated_at=NOW)
            session.add(action)
            await session.flush()
            action_id = action.id
    with pytest.raises(EligibilityError):
        await service.reconcile_revoke(action_id)
    gateway.list_devices.assert_not_called()
    gateway.revoke_device.assert_not_called()


async def test_unattempted_revoke_is_not_sent_or_confirmed_by_reconciliation(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await active(factory, state="PENDING_REVOKE")
    async with factory.begin() as session:
        action = DeviceAction(association_id=association_id, kind="REVOKE", status="PENDING", idempotency_key="not-sent",
                              target_device_id=44500, attempt_count=0, created_at=NOW, updated_at=NOW)
        session.add(action)
        await session.flush()
        action_id = action.id
    gateway = SimpleNamespace(account_id=7022, revoke_device=AsyncMock(), list_devices=AsyncMock(return_value=[]))
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    result = await service.reconcile_revoke(action_id)
    assert result.status == "PENDING"
    gateway.list_devices.assert_not_called()
    gateway.revoke_device.assert_not_called()


async def test_target_changed_during_list_does_not_end_replacement_device(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, previous, association_id = await uncertain_revoke(factory)

    async def change_target():
        async with factory.begin() as session:
            (await session.get(DeviceAssociation, association_id)).device_id = 44501
        return []

    gateway.list_devices.side_effect = change_target
    assert (await service.reconcile_revoke(previous.action_id)).status == "UNKNOWN"
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.device_id == 44501 and association.ended_at is None
    gateway.revoke_device.assert_awaited_once_with(44500)


async def test_postgresql_concurrent_reconciliation_only_completes_action_once(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("row lock serialization requires PostgreSQL")
    service, gateway, previous, _ = await uncertain_revoke(factory)
    results = await asyncio.wait_for(asyncio.gather(*(service.reconcile_revoke(previous.action_id) for _ in range(3))), 20)
    assert all(result.status == "SUCCEEDED" for result in results)
    gateway.revoke_device.assert_awaited_once()
    async with factory() as session:
        completed = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == str(previous.action_id),
                                                                      AuditLog.target_type == "DEVICE_ACTION",
                                                                      AuditLog.result == "SUCCEEDED"))).all())
        assert len(completed) == 1


async def test_original_request_can_complete_while_reconciliation_lists_devices(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("independent transactions require PostgreSQL")
    await active(factory)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_revoke(device_id):
        entered.set()
        await release.wait()
        return DeviceRevokeResponse(ok=True)

    gateway = SimpleNamespace(account_id=7022, revoke_device=AsyncMock(side_effect=slow_revoke), list_devices=AsyncMock())
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    original = asyncio.create_task(service.deauth(1))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with factory() as session:
            action_id = await session.scalar(select(DeviceAction.id))

        async def release_original():
            release.set()
            assert (await asyncio.wait_for(original, 10)).status == "SUCCEEDED"
            return []

        gateway.list_devices.side_effect = release_original
        assert (await service.reconcile_revoke(action_id)).status == "SUCCEEDED"
    finally:
        release.set()
        await asyncio.wait_for(original, 10)
    gateway.revoke_device.assert_awaited_once()


async def test_reconciliation_database_failure_never_releases_slot_or_retries_post(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device_revocation

    factory, _ = lifecycle_db
    service, gateway, previous, association_id = await uncertain_revoke(factory)
    original = device_revocation.audit

    async def fail_success(session, **kwargs):
        if kwargs.get("result") == "SUCCEEDED":
            raise RuntimeError("cannot save success")
        return await original(session, **kwargs)

    monkeypatch.setattr(device_revocation, "audit", fail_success)
    assert (await service.reconcile_revoke(previous.action_id)).status == "UNKNOWN"
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None
    gateway.revoke_device.assert_awaited_once()
    monkeypatch.setattr(device_revocation, "audit", original)
    assert (await service.reconcile_revoke(previous.action_id)).status == "SUCCEEDED"
    gateway.revoke_device.assert_awaited_once()


async def test_completed_reconciliation_replay_cannot_touch_new_association(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, previous, old_id = await uncertain_revoke(factory)
    completed = await service.reconcile_revoke(previous.action_id)
    assert completed.status == "SUCCEEDED"
    new_id = await active(factory, device_id=44501)
    assert await service.reconcile_revoke(previous.action_id) == completed
    async with factory() as session:
        assert (await session.get(DeviceAssociation, old_id)).state == "ENDED"
        replacement = await session.get(DeviceAssociation, new_id)
        assert replacement.state == "ACTIVE" and replacement.ended_at is None
    gateway.list_devices.assert_awaited_once()
    gateway.revoke_device.assert_awaited_once_with(44500)
