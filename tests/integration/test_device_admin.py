import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, Device, DeviceAction, DeviceAssociation, DeviceCycleLedger, DeviceTaskScope, QuotaTask, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRecord

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def record(device_id=44503, org_id=178, *, revoked_at=None):
    return DeviceRecord(id=device_id, org_id=org_id, name="admin's laptop", created_at=NOW - timedelta(days=2),
                        revoked_at=revoked_at, last_used_ip="private-ip", sk_prefix="private-prefix",
                        machine_summary={"hostname": "private-machine"})


def admin_fixture(factory, *, devices=None, quota=None):
    gateway = SimpleNamespace(
        list_devices=AsyncMock(return_value=devices if devices is not None else [record()]),
        approve_device_auth=AsyncMock(side_effect=AssertionError("admin association cannot approve")),
        revoke_device=AsyncMock(side_effect=AssertionError("admin association cannot revoke")),
    )
    check = quota if quota is not None else AsyncMock(return_value=None)
    service = DeviceAdminService(factory, gateway, 178, check, clock=lambda: NOW)
    return service, gateway, check


async def associations(factory):
    async with factory() as session:
        return list((await session.scalars(select(DeviceAssociation).order_by(DeviceAssociation.id))).all())


async def test_admin_associates_existing_device_without_remote_writes_or_new_device_evidence(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, quota = admin_fixture(factory)
    result = await service.authuser(1, 44503, 999)
    assert (result.status, result.device_id) == ("SUCCEEDED", 44503)
    assert await service.authuser(1, 44503, 999) == result
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()
    assert quota.await_count >= 2
    async with factory() as session:
        rows = list((await session.scalars(select(DeviceAssociation))).all())
        assert len(rows) == 1 and rows[0].state == "ACTIVE"
        actions = list((await session.scalars(select(DeviceAction))).all())
        assert len(actions) == 1
        assert (actions[0].kind, actions[0].status, actions[0].attempt_count, actions[0].result_reused) == ("AUTH", "SUCCEEDED", 0, None)
        assert actions[0].target_device_id == 44503
        user = await session.get(User, 1)
        assert (user.email, user.reclaude_user_id) == ("1@example.invalid", "legacy-1")
        logs = list((await session.scalars(select(AuditLog))).all())
        assert len(logs) == 1 and logs[0].actor_type == "ADMIN" and logs[0].actor_telegram_id == 999
        assert await session.scalar(select(DeviceCycleLedger)) is None


@pytest.mark.parametrize("case", ["unbound", "banned", "scope", "quota", "missing_user", "invalid_user", "invalid_device"])
async def test_local_rejection_happens_before_rec_request(lifecycle_db, case):
    factory, _ = lifecycle_db
    service, gateway, quota = admin_fixture(factory)
    user_id = 1
    device_id = 44503
    async with factory.begin() as session:
        user = await session.get(User, 1)
        if case == "unbound":
            user.binding_status = "UNBOUND"
        elif case == "banned":
            user.status = "BANNED"
        elif case == "scope":
            (await session.get(DeviceTaskScope, 1)).scope_mode = "ALLOWLIST"
        elif case == "missing_user":
            user_id = 999
        elif case == "invalid_user":
            user_id = True
        elif case == "invalid_device":
            device_id = True
        else:
            quota.side_effect = EligibilityError("cycle quota locked")
    with pytest.raises(EligibilityError):
        await service.authuser(user_id, device_id, 999)
    gateway.list_devices.assert_not_called()
    assert await associations(factory) == []


@pytest.mark.parametrize("case", [
    "absent", "other_org", "remote_revoked",
    pytest.param("local_revoked", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError,
        reason="D4 issue 1 deferred by user: REC list assumed not to return previously revoked device as active",
    )),
    "owned", "user_has_device", "user_has_unknown",
])
async def test_device_or_user_occupancy_is_never_overridden(lifecycle_db, case):
    factory, _ = lifecycle_db
    devices = [record(44500)]
    if case == "absent":
        devices = []
    elif case == "other_org":
        devices = [record(44500, 179)]
    elif case == "remote_revoked":
        devices = [record(44500, revoked_at=NOW)]
    elif case == "local_revoked":
        async with factory.begin() as session:
            (await session.scalar(select(Device).where(Device.device_id == 44500))).revoked_at = NOW
    elif case in {"owned", "user_has_device", "user_has_unknown"}:
        async with factory.begin() as session:
            session.add(DeviceAssociation(user_id=2 if case == "owned" else 1, task_id=1, org_id=178,
                                          device_id=None if case == "user_has_unknown" else 44500 if case == "owned" else 44501,
                                          state="UNKNOWN" if case == "user_has_unknown" else "ACTIVE", started_at=NOW, updated_at=NOW))
    service, gateway, _ = admin_fixture(factory, devices=devices)
    try:
        result = await service.authuser(1, 44500, 999)
    except EligibilityError:
        pass
    else:
        assert result.status != "SUCCEEDED", "unavailable or occupied device must not be associated"
    rows = await associations(factory)
    assert len(rows) == (1 if case in {"owned", "user_has_device", "user_has_unknown"} else 0)
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("change", ["banned", "unbound", "scope", "quota", "limit"])
async def test_eligibility_and_quota_are_refreshed_after_device_list_request(lifecycle_db, change):
    factory, _ = lifecycle_db

    async def quota_check(session, user, context, now):
        if context.limit_usd < Decimal("600"):
            raise EligibilityError("not enough quota")

    service, gateway, quota = admin_fixture(factory, quota=AsyncMock(side_effect=quota_check))

    async def list_and_change():
        async with factory.begin() as session:
            if change == "banned":
                (await session.get(User, 1)).status = "BANNED"
            elif change == "unbound":
                (await session.get(User, 1)).binding_status = "UNBOUND"
            elif change == "scope":
                (await session.get(DeviceTaskScope, 1)).scope_mode = "ALLOWLIST"
            elif change == "limit":
                (await session.get(QuotaTask, 1)).limit_usd = Decimal("500")
            else:
                quota.side_effect = EligibilityError("quota locked during request")
        return [record()]

    gateway.list_devices.side_effect = list_and_change
    with pytest.raises(EligibilityError):
        await service.authuser(1, 44503, 999)
    assert await associations(factory) == []


async def test_list_failure_does_not_create_device_or_association(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, _ = admin_fixture(factory)
    gateway.list_devices.side_effect = httpx.ReadTimeout("private session")
    with pytest.raises((httpx.ReadTimeout, EligibilityError)):
        await service.authuser(1, 44503, 999)
    assert await associations(factory) == []
    async with factory() as session:
        assert await session.scalar(select(Device).where(Device.device_id == 44503)) is None
        assert await session.scalar(select(DeviceAction)) is None


async def test_listing_contains_local_ownership_and_unassociated_devices_without_sensitive_fields(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceAssociation(user_id=1, task_id=1, org_id=178, device_id=44500,
                                      state="ACTIVE", started_at=NOW, updated_at=NOW))
    service, gateway, quota = admin_fixture(factory, devices=[record(44500), record(44503), record(44502, 179)])
    entries = await service.list_devices()
    assert isinstance(entries, tuple)
    by_id = {entry.device_id: entry for entry in entries}
    assert set(by_id) == {44500, 44503}
    assert (by_id[44500].owner_user_id, by_id[44500].owner_email, by_id[44500].association_state) == (1, "1@example.invalid", "ACTIVE")
    assert by_id[44503].owner_user_id is None and by_id[44503].association_id is None
    for secret in ("private-ip", "private-prefix", "private-machine"):
        assert secret not in repr(entries)
    quota.assert_not_called()
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()
    assert len(await associations(factory)) == 1
    async with factory() as session:
        assert await session.scalar(select(Device).where(Device.device_id == 44503)) is None


@pytest.mark.parametrize("collision", ["user", "device", "same"])
async def test_postgresql_concurrent_admin_association_obeys_hard_limits(lifecycle_db, collision):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    service, gateway, _ = admin_fixture(factory, devices=[record(44500), record(44501)])
    second_user = 2 if collision == "device" else 1
    second_device = 44501 if collision == "user" else 44500
    results = await asyncio.wait_for(asyncio.gather(service.authuser(1, 44500, 999),
                                                    service.authuser(second_user, second_device, 999), return_exceptions=True), 20)
    successful = [result for result in results if not isinstance(result, Exception)]
    assert len(successful) == (2 if collision == "same" else 1)
    assert all(result.status == "SUCCEEDED" for result in successful)
    assert all(isinstance(result, EligibilityError) for result in results if isinstance(result, Exception))
    assert len(await associations(factory)) == 1
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


async def test_local_failure_rolls_back_association_without_remote_compensation(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device_admin

    factory, _ = lifecycle_db
    service, gateway, _ = admin_fixture(factory)

    async def audit_unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(device_admin, "audit", audit_unavailable)
    with pytest.raises((EligibilityError, RuntimeError)):
        await service.authuser(1, 44503, 999)
    assert await associations(factory) == []
    async with factory() as session:
        assert await session.scalar(select(DeviceAction)) is None
        assert await session.scalar(select(Device).where(Device.device_id == 44503)) is None
    gateway.revoke_device.assert_not_called()


async def test_empty_complete_list_does_not_silently_end_local_association(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceAssociation(user_id=1, task_id=1, org_id=178, device_id=44500,
                                      state="ACTIVE", started_at=NOW, updated_at=NOW))
    service, _, _ = admin_fixture(factory, devices=[])
    assert await service.list_devices() == ()
    rows = await associations(factory)
    assert rows[0].state == "ACTIVE" and rows[0].ended_at is None


async def test_pending_slot_in_other_org_blocks_admin_association(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceAssociation(user_id=1, task_id=2, org_id=179, state="UNKNOWN", started_at=NOW, updated_at=NOW))
    service, gateway, _ = admin_fixture(factory)
    with pytest.raises(EligibilityError):
        await service.authuser(1, 44503, 999)
    assert len(await associations(factory)) == 1
    gateway.approve_device_auth.assert_not_called()


async def test_first_quota_check_uses_current_task_limit(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db

    async def quota_check(session, user, context, now):
        if context.limit_usd < Decimal("600"):
            raise EligibilityError("quota too low")

    service, gateway, _ = admin_fixture(factory, quota=AsyncMock(side_effect=quota_check))
    original = service.task_service.resolve_task

    async def lower_after_resolve(name=None):
        context = await original(name)
        async with factory.begin() as session:
            (await session.get(QuotaTask, 1)).limit_usd = Decimal("500")
        return context

    monkeypatch.setattr(service.task_service, "resolve_task", lower_after_resolve)
    with pytest.raises(EligibilityError):
        await service.authuser(1, 44503, 999)
    gateway.list_devices.assert_not_called()
    assert await associations(factory) == []


async def test_deauth_during_device_list_lookup_cannot_return_old_authorization_success(lifecycle_db):
    from reclaude_bot.application.device_revocation import DeviceRevocationService
    from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse

    factory, _ = lifecycle_db
    service, gateway, _ = admin_fixture(factory)
    first = await service.authuser(1, 44503, 999)
    revoke_gateway = SimpleNamespace(revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)))
    revocation = DeviceRevocationService(factory, revoke_gateway, 178, clock=lambda: NOW)

    async def stale_list():
        response = [record()]
        assert (await revocation.deauth(1)).status == "SUCCEEDED"
        return response

    gateway.list_devices.side_effect = stale_list
    try:
        result = await service.authuser(1, 44503, 999)
    except EligibilityError:
        pass
    else:
        assert result.status != "SUCCEEDED", "authuser returned an authorization that was ended during its GET request"
    rows = await associations(factory)
    assert len(rows) == 1 and rows[0].id == first.association_id
    assert rows[0].state == "ENDED"
    revoke_gateway.revoke_device.assert_awaited_once_with(44503)


async def test_authuser_inflight_replay_does_not_touch_replacement_device(lifecycle_db):
    from reclaude_bot.application.device_revocation import DeviceRevocationService
    from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse

    factory, _ = lifecycle_db
    service, gateway, _ = admin_fixture(factory)
    old = await service.authuser(1, 44503, 999)
    revoke_gateway = SimpleNamespace(revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)))
    revocation = DeviceRevocationService(factory, revoke_gateway, 178, clock=lambda: NOW)
    replacement_service, _, _ = admin_fixture(factory, devices=[record(44504)])

    async def replace_during_lookup():
        response = [record()]
        assert (await revocation.deauth(1)).status == "SUCCEEDED"
        assert (await replacement_service.authuser(1, 44504, 999)).status == "SUCCEEDED"
        return response

    gateway.list_devices.side_effect = replace_during_lookup
    with pytest.raises(EligibilityError):
        await service.authuser(1, 44503, 999)
    rows = await associations(factory)
    assert len(rows) == 2
    assert rows[0].id == old.association_id and rows[0].state == "ENDED"
    assert rows[1].state == "ACTIVE" and rows[1].device_id == 44504
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()
    revoke_gateway.revoke_device.assert_awaited_once_with(44503)


async def test_first_association_can_be_completed_idempotently_by_concurrent_request(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, _ = admin_fixture(factory)
    other, _, _ = admin_fixture(factory)
    completed = []

    async def associate_during_lookup():
        completed.append(await other.authuser(1, 44503, 999))
        return [record()]

    gateway.list_devices.side_effect = associate_during_lookup
    assert await service.authuser(1, 44503, 999) == completed[0]
    assert len(await associations(factory)) == 1
    async with factory() as session:
        actions = list((await session.scalars(select(DeviceAction))).all())
        assert len(actions) == 1
        logs = list((await session.scalars(select(AuditLog))).all())
        assert len(logs) == 1
