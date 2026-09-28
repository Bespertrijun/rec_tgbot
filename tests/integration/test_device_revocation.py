import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import (
    AuditLog,
    Device,
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceTaskScope,
    User,
)
from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def revoke_fixture(factory):
    gateway = SimpleNamespace(revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)))
    service = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    return service, gateway


async def active(factory, *, user_id=1, device_id=44500, org_id=178, task_id=1, state="ACTIVE"):
    async with factory.begin() as session:
        row = DeviceAssociation(user_id=user_id, task_id=task_id, org_id=org_id, device_id=device_id,
                                state=state, started_at=NOW - timedelta(hours=1), updated_at=NOW)
        session.add(row)
        await session.flush()
        return row.id


async def action_rows(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceAction).order_by(DeviceAction.id))).all())


@pytest.mark.parametrize("operator", [None, 999])
async def test_success_ends_only_device_association_preserving_identity_ledger_and_lock(lifecycle_db, operator):
    factory, _ = lifecycle_db
    association_id = await active(factory)
    async with factory.begin() as session:
        cycle = DeviceQuotaCycle(task_id=1, started_at=NOW - timedelta(days=1), reset_at=NOW + timedelta(days=6),
                                 created_at=NOW, status="VERIFIED")
        session.add(cycle)
        await session.flush()
        ledger = DeviceCycleLedger(user_id=1, cycle_id=cycle.id, task_id=1, confirmed_used_usd=Decimal("700.1234567890"),
                                   quality="VERIFIED", quota_locked_at=NOW, created_at=NOW, updated_at=NOW)
        session.add(ledger)
    service, gateway = revoke_fixture(factory)

    result = await service.deauth(1, operator_id=operator)

    assert (result.status, result.association_id, result.device_id) == ("SUCCEEDED", association_id, 44500)
    gateway.revoke_device.assert_awaited_once_with(44500)
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.state == "ENDED"
        assert ensure_utc(association.ended_at) == NOW
        assert association.end_reason == ("USER_DEAUTH" if operator is None else "ADMIN_DEAUTH")
        device = await session.scalar(select(Device).where(Device.org_id == 178, Device.device_id == 44500))
        assert ensure_utc(device.revoked_at) == NOW
        user = await session.get(User, 1)
        assert (user.binding_status, user.email, user.telegram_user_id) == ("BOUND", "1@example.invalid", 1001)
        ledger = await session.scalar(select(DeviceCycleLedger))
        assert ledger.confirmed_used_usd == Decimal("700.1234567890")
        assert ensure_utc(ledger.quota_locked_at) == NOW and ledger.quota_unlocked_at is None
        logs = list((await session.scalars(select(AuditLog))).all())
        assert logs
        assert all(log.actor_type == ("USER" if operator is None else "ADMIN") for log in logs)
        assert all(log.actor_telegram_id == (1001 if operator is None else operator) for log in logs)
    actions = await action_rows(factory)
    assert len(actions) == 1
    assert (actions[0].kind, actions[0].status, actions[0].attempt_count) == ("REVOKE", "SUCCEEDED", 1)
    assert await service.deauth(1) is None
    replay = await service.deauth(1, association_id=association_id)
    assert replay == result
    gateway.revoke_device.assert_awaited_once()


@pytest.mark.parametrize("restriction", ["unbound", "banned", "excluded", "empty_allowlist"])
async def test_manual_revocation_remains_available_when_authorization_is_forbidden(lifecycle_db, restriction):
    factory, _ = lifecycle_db
    await active(factory)
    async with factory.begin() as session:
        user = await session.get(User, 1)
        scope = await session.get(DeviceTaskScope, 1)
        if restriction == "unbound":
            user.binding_status = "UNBOUND"
        elif restriction == "banned":
            user.status = "BANNED"
        else:
            scope.scope_mode = "ALLOWLIST"
    service, gateway = revoke_fixture(factory)
    assert (await service.deauth(1)).status == "SUCCEEDED"
    gateway.revoke_device.assert_awaited_once_with(44500)


@pytest.mark.parametrize("case", ["missing_user", "wrong_owner", "wrong_org", "unknown_auth", "pending_auth", "bad_user", "bad_association"])
async def test_rejected_targets_never_send_a_revoke(lifecycle_db, case):
    factory, _ = lifecycle_db
    user_id = 1
    kwargs = {}
    if case == "wrong_owner":
        kwargs["association_id"] = await active(factory, user_id=2)
    elif case == "wrong_org":
        await active(factory, device_id=44502, org_id=179, task_id=2)
    elif case in {"unknown_auth", "pending_auth"}:
        await active(factory, device_id=None, state="UNKNOWN" if case == "unknown_auth" else "PENDING_AUTH")
    elif case == "missing_user":
        user_id = 999
    elif case == "bad_user":
        user_id = True
    else:
        kwargs["association_id"] = True
    service, gateway = revoke_fixture(factory)
    with pytest.raises(EligibilityError):
        await service.deauth(user_id, **kwargs)
    gateway.revoke_device.assert_not_called()
    assert await action_rows(factory) == []


@pytest.mark.parametrize("failure", ["timeout", "invalid", "auth_circuit"])
async def test_uncertain_revoke_keeps_slot_and_replay_does_not_resend(lifecycle_db, failure):
    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)
    if failure == "timeout":
        gateway.revoke_device.side_effect = httpx.ReadTimeout("secret credential")
    elif failure == "auth_circuit":
        gateway.revoke_device.side_effect = AuthenticationCircuitOpen("secret credential")
    else:
        gateway.revoke_device.return_value = {"ok": True}
    if failure == "auth_circuit":
        with pytest.raises(AuthenticationCircuitOpen):
            await service.deauth(1)
    else:
        assert (await service.deauth(1)).status == "UNKNOWN"
    replay = await service.deauth(1)
    assert replay.status == "UNKNOWN"
    gateway.revoke_device.assert_awaited_once_with(44500)
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None
        assert association.state in {"UNKNOWN", "PENDING_REVOKE"}
        device = await session.scalar(select(Device).where(Device.device_id == 44500))
        assert device.revoked_at is None
    actions = await action_rows(factory)
    assert len(actions) == 1 and actions[0].attempt_count == 1
    assert "secret" not in actions[0].last_error_code


async def test_post_is_sent_only_after_durable_target_and_attempt_are_recorded(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)

    async def inspect(device_id):
        async with factory() as session:
            association = await session.get(DeviceAssociation, association_id)
            action = await session.scalar(select(DeviceAction))
            assert association.state == "PENDING_REVOKE" and association.ended_at is None
            assert (action.status, action.attempt_count, action.target_device_id) == ("PENDING", 1, device_id)
        return DeviceRevokeResponse(ok=True)

    gateway.revoke_device.side_effect = inspect
    assert (await service.deauth(1)).status == "SUCCEEDED"


async def test_replaying_old_revocation_cannot_revoke_new_generation(lifecycle_db):
    factory, _ = lifecycle_db
    old_id = await active(factory)
    service, gateway = revoke_fixture(factory)
    old_result = await service.deauth(1)
    new_id = await active(factory, device_id=44501)
    replay = await service.deauth(1, association_id=old_id)
    assert replay == old_result
    gateway.revoke_device.assert_awaited_once_with(44500)
    async with factory() as session:
        association = await session.get(DeviceAssociation, new_id)
        assert association.state == "ACTIVE" and association.ended_at is None


async def test_original_request_can_finish_after_inflight_replay_marks_unknown(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("independent transactions require PostgreSQL")
    await active(factory)
    service, gateway = revoke_fixture(factory)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_revoke(device_id):
        entered.set()
        await release.wait()
        return DeviceRevokeResponse(ok=True)

    gateway.revoke_device.side_effect = slow_revoke
    first = asyncio.create_task(service.deauth(1))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        replay = await asyncio.wait_for(service.deauth(1), 10)
        assert replay.status in {"PENDING", "UNKNOWN"}
    finally:
        release.set()
    assert (await asyncio.wait_for(first, 10)).status == "SUCCEEDED"
    gateway.revoke_device.assert_awaited_once()


async def test_postgresql_concurrent_deauth_sends_only_once(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("row lock serialization requires PostgreSQL")
    await active(factory)
    service, gateway = revoke_fixture(factory)
    results = await asyncio.wait_for(asyncio.gather(*(service.deauth(1) for _ in range(4))), 20)
    assert any(result is not None and result.status == "SUCCEEDED" for result in results)
    gateway.revoke_device.assert_awaited_once_with(44500)
    assert len(await action_rows(factory)) == 1


async def test_local_success_failure_never_reports_success_or_retries_remote(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device_revocation

    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)
    original = device_revocation.audit

    async def fail_success(session, **kwargs):
        if kwargs.get("result") == "SUCCEEDED":
            raise RuntimeError("local success persistence failed")
        return await original(session, **kwargs)

    monkeypatch.setattr(device_revocation, "audit", fail_success)
    result = await service.deauth(1)
    assert result.status == "UNKNOWN"
    assert (await service.deauth(1)).status == "UNKNOWN"
    gateway.revoke_device.assert_awaited_once()
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None
        action = await session.scalar(select(DeviceAction))
        assert action.status == "UNKNOWN" and action.target_device_id == 44500


async def test_changed_target_during_request_is_not_marked_revoked(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)

    async def change_target(device_id):
        async with factory.begin() as session:
            row = await session.get(DeviceAssociation, association_id)
            row.device_id = 44501
        return DeviceRevokeResponse(ok=True)

    gateway.revoke_device.side_effect = change_target
    result = await service.deauth(1)
    assert result.status == "UNKNOWN"
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None and association.device_id == 44501
        device = await session.scalar(select(Device).where(Device.device_id == 44501))
        assert device.revoked_at is None
    gateway.revoke_device.assert_awaited_once_with(44500)


async def test_auth_unknown_with_known_device_id_is_not_treated_as_owned(lifecycle_db):
    factory, _ = lifecycle_db
    association_id = await active(factory, state="UNKNOWN", device_id=None)
    async with factory.begin() as session:
        session.add(DeviceAction(association_id=association_id, kind="AUTH", status="UNKNOWN", idempotency_key="auth-unknown",
                                 target_device_id=44500, result_reused=True, attempt_count=1, created_at=NOW, updated_at=NOW))
    service, gateway = revoke_fixture(factory)
    with pytest.raises(EligibilityError):
        await service.deauth(1)
    gateway.revoke_device.assert_not_called()
    assert len(await action_rows(factory)) == 1


async def test_database_commit_failure_after_remote_success_keeps_durable_attempt(lifecycle_db):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)
    fail_next = [False]

    async def revoked(device_id):
        fail_next[0] = True
        return DeviceRevokeResponse(ok=True)

    def reject_commit(session):
        if fail_next[0]:
            fail_next[0] = False
            raise RuntimeError("commit failed after remote revoke")

    gateway.revoke_device.side_effect = revoked
    event.listen(Session, "before_commit", reject_commit)
    try:
        result = await service.deauth(1)
    finally:
        event.remove(Session, "before_commit", reject_commit)
    assert result.status == "UNKNOWN"
    assert (await service.deauth(1)).status == "UNKNOWN"
    gateway.revoke_device.assert_awaited_once()
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        assert association.ended_at is None
        action = await session.scalar(select(DeviceAction))
        assert action.status == "UNKNOWN" and action.attempt_count == 1


async def test_unknown_revocation_blocks_new_authorization_until_resolved(lifecycle_db):
    from reclaude_bot.application.device import DeviceAuthorizationService

    factory, _ = lifecycle_db
    await active(factory)
    service, gateway = revoke_fixture(factory)
    gateway.revoke_device.side_effect = httpx.ReadTimeout("not confirmed")
    assert (await service.deauth(1)).status == "UNKNOWN"
    auth_gateway = SimpleNamespace(describe_device_auth=AsyncMock(), approve_device_auth=AsyncMock())
    authorization = DeviceAuthorizationService(factory, auth_gateway, 178, AsyncMock(return_value=None), clock=lambda: NOW)
    with pytest.raises(EligibilityError):
        await authorization.auth(1, "https://www.reclaude.ai/cli/auth?state=next-device")
    auth_gateway.describe_device_auth.assert_not_called()
    auth_gateway.approve_device_auth.assert_not_called()


async def test_no_current_association_is_a_noop(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway = revoke_fixture(factory)
    assert await service.deauth(1) is None
    gateway.revoke_device.assert_not_called()
    assert await action_rows(factory) == []


async def test_persistent_database_failure_leaves_durable_pending_attempt_without_resend(lifecycle_db):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    factory, _ = lifecycle_db
    association_id = await active(factory)
    service, gateway = revoke_fixture(factory)
    unavailable = [False]

    async def revoked(device_id):
        unavailable[0] = True
        return DeviceRevokeResponse(ok=True)

    def reject_commit(session):
        if unavailable[0]:
            raise RuntimeError("database unavailable")

    gateway.revoke_device.side_effect = revoked
    event.listen(Session, "before_commit", reject_commit)
    try:
        result = await service.deauth(1)
    finally:
        event.remove(Session, "before_commit", reject_commit)
    assert result.status == "UNKNOWN"
    async with factory() as session:
        association = await session.get(DeviceAssociation, association_id)
        action = await session.scalar(select(DeviceAction))
        assert association.ended_at is None
        assert (action.status, action.attempt_count) == ("PENDING", 1)
    assert (await service.deauth(1)).status == "UNKNOWN"
    gateway.revoke_device.assert_awaited_once()
