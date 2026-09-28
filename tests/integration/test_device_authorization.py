import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from reclaude_bot.application.device import DeviceAuthorizationService, parse_device_auth_link
from reclaude_bot.application.device_task_members import DeviceTaskMemberService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceAction, DeviceAssociation, DeviceCycleLedger, QuotaTask, User
from reclaude_bot.infrastructure.reclaude.client import DeviceApiError, ReclaudeClient
from reclaude_bot.infrastructure.reclaude.models import DeviceAuthApproval, DeviceAuthDescription

NOW = datetime(2026, 9, 28, tzinfo=UTC)
STATE = "private-state-only-in-memory"
LINK = f"https://www.recode.cat/cli/auth?state={STATE}"


def auth_fixture(factory, *, device_id=44503, reused=False, quota_check=None):
    async def describe(state):
        return DeviceAuthDescription(state=state, device_name="new laptop", created_at=NOW, expires_at=NOW + timedelta(minutes=10))

    gateway = SimpleNamespace(
        describe_device_auth=AsyncMock(side_effect=describe),
        approve_device_auth=AsyncMock(return_value=DeviceAuthApproval(ok=True, device_id=device_id, reused=reused, user_email="admin@example.invalid")),
        revoke_device=AsyncMock(side_effect=AssertionError("no automatic compensation")),
    )
    quota = quota_check or AsyncMock(return_value=None)
    clock = [NOW]
    service = DeviceAuthorizationService(factory, gateway, 178, quota, clock=lambda: clock[0])
    return service, gateway, quota, clock


async def rows(factory):
    async with factory() as session:
        actions = (await session.scalars(select(DeviceAction).order_by(DeviceAction.id))).all()
        associations = (await session.scalars(select(DeviceAssociation).order_by(DeviceAssociation.id))).all()
        return actions, associations


@pytest.mark.parametrize("reused", [False, True])
async def test_stopped_task_authorizes_once_and_preserves_local_identity_and_evidence(lifecycle_db, reused):
    factory, _ = lifecycle_db
    service, gateway, quota, _ = auth_fixture(factory, reused=reused)
    first = await service.auth(1, LINK)
    assert (first.status, first.device_id) == ("SUCCEEDED", 44503)
    assert await service.auth(1, LINK) == first
    gateway.describe_device_auth.assert_awaited_once_with(STATE)
    gateway.approve_device_auth.assert_awaited_once_with(STATE, "new laptop", 178)
    gateway.revoke_device.assert_not_called()
    assert quota.await_count == 2
    actions, associations = await rows(factory)
    assert len(actions) == len(associations) == 1
    assert (actions[0].attempt_count, actions[0].result_reused, actions[0].target_device_id) == (1, reused, 44503)
    assert actions[0].idempotency_key == parse_device_auth_link(LINK).fingerprint
    assert (associations[0].state, associations[0].device_id) == ("ACTIVE", 44503)
    async with factory() as session:
        user = await session.get(User, 1)
        assert (user.email, user.reclaude_user_id) == ("1@example.invalid", "legacy-1")
        logs = (await session.scalars(select(AuditLog))).all()
        assert STATE not in json.dumps([log.parameters_summary for log in logs])
        assert STATE not in repr(first)
        # Authorization cannot invent a zero usage sample or ledger.
        assert await session.scalar(select(func.count()).select_from(DeviceCycleLedger)) == 0


@pytest.mark.parametrize("case", ["missing", "unbound", "banned", "excluded", "empty_allowlist", "quota", "occupied", "invalid_id"])
async def test_local_rejections_do_not_call_rec(lifecycle_db, case):
    factory, _ = lifecycle_db
    service, gateway, quota, _ = auth_fixture(factory)
    user_id = 1
    if case == "missing":
        user_id = 999
    elif case == "invalid_id":
        user_id = True
    elif case in {"unbound", "banned"}:
        async with factory.begin() as session:
            user = await session.get(User, 1)
            if case == "unbound":
                user.binding_status = "UNBOUND"
            else:
                user.status = "BANNED"
    elif case == "excluded":
        await DeviceTaskMemberService(factory, 178).delete_members(None, [1], 99)
    elif case == "empty_allowlist":
        scope = DeviceTaskMemberService(factory, 178)
        await scope.add_members(None, [1], 99)
        await scope.delete_members(None, [1], 99)
    elif case == "quota":
        quota.side_effect = EligibilityError("quota locked")
    elif case == "occupied":
        async with factory.begin() as session:
            session.add(DeviceAssociation(user_id=1, task_id=2, org_id=179, state="UNKNOWN", started_at=NOW, updated_at=NOW))
    with pytest.raises(EligibilityError):
        await service.auth(user_id, LINK)
    gateway.describe_device_auth.assert_not_called()
    gateway.approve_device_auth.assert_not_called()
    actions, _ = await rows(factory)
    assert actions == []


@pytest.mark.parametrize("change", ["banned", "excluded", "quota", "expired", "bad_description"])
async def test_eligibility_and_expiry_are_rechecked_before_approve(lifecycle_db, change):
    factory, _ = lifecycle_db
    service, gateway, quota, clock = auth_fixture(factory)
    original = gateway.describe_device_auth.side_effect

    async def describe(state):
        result = await original(state)
        if change == "banned":
            async with factory.begin() as session:
                (await session.get(User, 1)).status = "BANNED"
        elif change == "excluded":
            await DeviceTaskMemberService(factory, 178).delete_members(None, [1], 99)
        elif change == "quota":
            quota.side_effect = EligibilityError("quota changed")
        elif change == "expired":
            clock[0] = NOW + timedelta(minutes=10)
        else:
            result.state = "wrong-state"
        return result

    gateway.describe_device_auth.side_effect = describe
    with pytest.raises(EligibilityError):
        await service.auth(1, LINK)
    gateway.approve_device_auth.assert_not_called()
    actions, associations = await rows(factory)
    assert (actions[0].status, actions[0].attempt_count) == ("FAILED", 0)
    assert associations[0].state == "ENDED"
    assert associations[0].ended_at is not None


@pytest.mark.parametrize("kind", ["capacity", "capacity_unknown", "timeout", "invalid_response"])
async def test_approve_failures_release_only_confirmed_capacity_rejection(lifecycle_db, kind):
    factory, _ = lifecycle_db
    service, gateway, _, _ = auth_fixture(factory)
    if kind.startswith("capacity"):
        gateway.approve_device_auth.side_effect = DeviceApiError(operation="device authorization", code="client.device_limit_reached", retryable=False,
                                                               outcome_unknown=kind == "capacity_unknown")
    elif kind == "timeout":
        gateway.approve_device_auth.side_effect = httpx.ReadTimeout(STATE)
    else:
        gateway.approve_device_auth.return_value = {"ok": True, "device_id": 44503}
    result = await service.auth(1, LINK)
    assert result.status == ("FAILED" if kind == "capacity" else "UNKNOWN")
    actions, associations = await rows(factory)
    assert actions[0].attempt_count == 1
    assert (associations[0].ended_at is not None) == (kind == "capacity")
    assert STATE not in str(actions[0].last_error_code)
    gateway.revoke_device.assert_not_called()
    await service.auth(1, LINK)
    gateway.approve_device_auth.assert_awaited_once()
    if kind != "capacity":
        with pytest.raises(EligibilityError):
            await service.auth(1, LINK + "-new")


async def test_owned_device_conflict_retains_evidence_without_revoking_owner(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceAssociation(user_id=2, task_id=1, org_id=178, device_id=44500, state="ACTIVE", started_at=NOW, updated_at=NOW))
    service, gateway, _, _ = auth_fixture(factory, device_id=44500, reused=True)
    result = await service.auth(1, LINK)
    assert (result.status, result.device_id) == ("UNKNOWN", 44500)
    actions, associations = await rows(factory)
    assert actions[0].target_device_id == 44500 and actions[0].result_reused is True
    by_user = {row.user_id: row for row in associations}
    assert (by_user[2].state, by_user[2].device_id) == ("ACTIVE", 44500)
    assert by_user[1].state == "UNKNOWN" and by_user[1].ended_at is None
    gateway.revoke_device.assert_not_called()


async def test_local_success_commit_failure_returns_unknown_and_keeps_result_evidence(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device

    factory, _ = lifecycle_db
    original = device.audit

    async def fail_success(session, **kwargs):
        if kwargs["action"] == "DEVICE_AUTH_SUCCEEDED":
            raise RuntimeError("simulate database failure")
        await original(session, **kwargs)

    monkeypatch.setattr(device, "audit", fail_success)
    service, gateway, _, _ = auth_fixture(factory)
    result = await service.auth(1, LINK)
    assert result.status == "UNKNOWN"
    actions, associations = await rows(factory)
    assert (actions[0].status, actions[0].target_device_id, actions[0].result_reused) == ("UNKNOWN", 44503, False)
    assert associations[0].state == "UNKNOWN" and associations[0].ended_at is None
    gateway.approve_device_auth.assert_awaited_once()
    gateway.revoke_device.assert_not_called()


async def test_replayed_old_success_after_deauth_is_not_reported_as_current_authorization(lifecycle_db):
    factory, _ = lifecycle_db
    service, gateway, _, clock = auth_fixture(factory)
    first = await service.auth(1, LINK)
    async with factory.begin() as session:
        association = await session.get(DeviceAssociation, first.association_id)
        association.state = "ENDED"
        association.ended_at = NOW + timedelta(seconds=1)
        association.end_reason = "MANUAL"
    clock[0] += timedelta(seconds=2)
    try:
        result = await service.auth(1, LINK)
    except EligibilityError:
        pass
    else:
        assert result.status != "SUCCEEDED", "ended association must not be reported as a current authorization"
    gateway.approve_device_auth.assert_awaited_once()


@pytest.mark.parametrize("change", ["revoking", "mismatched_device", "other_org"])
async def test_idempotent_success_requires_current_matching_association(lifecycle_db, change):
    factory, _ = lifecycle_db
    service, gateway, quota, _ = auth_fixture(factory)
    first = await service.auth(1, LINK)
    if change == "other_org":
        service = DeviceAuthorizationService(factory, gateway, 179, quota, clock=lambda: NOW)
    else:
        async with factory.begin() as session:
            association = await session.get(DeviceAssociation, first.association_id)
            if change == "revoking":
                association.state = "PENDING_REVOKE"
            else:
                association.device_id = 44501
    with pytest.raises(EligibilityError):
        await service.auth(1, LINK)
    gateway.approve_device_auth.assert_awaited_once()


async def test_real_client_capacity_refusal_releases_slot_for_new_link(lifecycle_db):
    factory, _ = lifecycle_db
    calls = []
    approvals = 0

    def handler(request):
        nonlocal approvals
        calls.append(request.url.path)
        if request.url.path.endswith("/describe"):
            state = json.loads(request.content)["state"]
            return httpx.Response(200, json={"state": state, "device_name": "new laptop", "created_at": NOW.isoformat(),
                                           "expires_at": (NOW + timedelta(minutes=10)).isoformat()})
        assert request.url.path.endswith("/approve")
        approvals += 1
        if approvals == 1:
            return httpx.Response(409, json={"code": "client.device_limit_reached", "retryable": False,
                                           "detail": {"current": 4, "max": 4, "scope": "account"}})
        return httpx.Response(200, json={"ok": True, "device_id": 44503, "reused": False})

    client = ReclaudeClient("https://rec.example.invalid", session_cookie="rc_sid=test-only")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        service = DeviceAuthorizationService(factory, client, 178, AsyncMock(return_value=None), clock=lambda: NOW)
        first = await service.auth(1, LINK)
        assert first.status == "FAILED"
        assert (await service.auth(1, LINK + "-new")).status == "SUCCEEDED"
    finally:
        await client.close()
    assert calls == ["/api/cli/auth/describe", "/api/cli/auth/approve"] * 2
    actions, associations = await rows(factory)
    assert [action.status for action in actions] == ["FAILED", "SUCCEEDED"]
    assert [association.state for association in associations] == ["ENDED", "ACTIVE"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="用户暂不采纳第3项修订；保留跨用户再次返回待核对device_id的场景，待确认REC reused语义",
)
async def test_known_device_from_unresolved_approval_cannot_be_claimed_by_another_user(lifecycle_db, monkeypatch):
    from reclaude_bot.application import device

    factory, _ = lifecycle_db
    original = device.audit
    failed_once = False

    async def fail_first_commit(session, **kwargs):
        nonlocal failed_once
        if kwargs["action"] == "DEVICE_AUTH_SUCCEEDED" and not failed_once:
            failed_once = True
            raise RuntimeError("first local approval commit failed")
        await original(session, **kwargs)

    monkeypatch.setattr(device, "audit", fail_first_commit)
    first, _, _, _ = auth_fixture(factory)
    second, gateway, _, _ = auth_fixture(factory, reused=True)
    assert (await first.auth(1, LINK)).status == "UNKNOWN"
    result = await second.auth(2, LINK + "-another-user")
    assert result.status == "UNKNOWN", "a known device from an unresolved approval must remain reserved"
    actions, associations = await rows(factory)
    assert actions[0].target_device_id == actions[1].target_device_id == 44503
    assert all(row.state == "UNKNOWN" for row in associations)
    gateway.revoke_device.assert_not_called()


async def test_second_quota_check_receives_current_task_limit(lifecycle_db):
    factory, _ = lifecycle_db
    limits = []

    async def quota_check(session, user, context, now):
        limits.append(context.limit_usd)
        if context.limit_usd <= 600:
            raise EligibilityError("confirmed spend exceeds new task quota")

    service, gateway, _, _ = auth_fixture(factory, quota_check=quota_check)
    original = gateway.describe_device_auth.side_effect

    async def lower_quota_during_describe(state):
        async with factory.begin() as session:
            (await session.get(QuotaTask, 1)).limit_usd = 500
        return await original(state)

    gateway.describe_device_auth.side_effect = lower_quota_during_describe
    with pytest.raises(EligibilityError):
        await service.auth(1, LINK)
    assert limits == [700, 500]
    gateway.approve_device_auth.assert_not_called()


async def test_first_quota_check_refreshes_limit_after_task_resolution(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db
    limits = []

    async def quota_check(session, user, context, now):
        limits.append(context.limit_usd)
        if context.limit_usd <= 600:
            raise EligibilityError("quota changed before reservation")

    service, gateway, _, _ = auth_fixture(factory, quota_check=quota_check)
    original = service.task_service.resolve_task

    async def lower_after_resolution(name):
        context = await original(name)
        async with factory.begin() as session:
            (await session.get(QuotaTask, 1)).limit_usd = 500
        return context

    monkeypatch.setattr(service.task_service, "resolve_task", lower_after_resolution)
    with pytest.raises(EligibilityError):
        await service.auth(1, LINK)
    assert limits == [500]
    gateway.describe_device_auth.assert_not_called()
    assert await rows(factory) == ([], [])


async def test_stopping_task_during_describe_does_not_block_new_auth(lifecycle_db):
    factory, _ = lifecycle_db
    enabled = []

    async def quota_check(session, user, context, now):
        enabled.append(context.enabled)

    async with factory.begin() as session:
        (await session.get(QuotaTask, 1)).status = "RUNNING"
    service, gateway, _, _ = auth_fixture(factory, quota_check=quota_check)
    original = gateway.describe_device_auth.side_effect

    async def stop_during_describe(state):
        async with factory.begin() as session:
            (await session.get(QuotaTask, 1)).status = "STOPPED"
        return await original(state)

    gateway.describe_device_auth.side_effect = stop_during_describe
    assert (await service.auth(1, LINK)).status == "SUCCEEDED"
    assert enabled == [True, False]
    gateway.approve_device_auth.assert_awaited_once()


@pytest.mark.parametrize("collision", ["user", "state", "device"])
async def test_postgresql_concurrent_authorizations_preserve_user_state_and_device_exclusivity(lifecycle_db, collision):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies independent authorization transactions")
    first, gateway1, _, _ = auth_fixture(factory)
    second, gateway2, _, _ = auth_fixture(factory)
    args = [(1, LINK), (1 if collision == "user" else 2, LINK if collision == "state" else LINK + "-other")]
    results = await asyncio.wait_for(asyncio.gather(first.auth(*args[0]), second.auth(*args[1]), return_exceptions=True), 10)
    if collision == "device":
        assert sorted(row.status for row in results) == ["SUCCEEDED", "UNKNOWN"]
    else:
        assert sum(isinstance(row, EligibilityError) for row in results) == 1
        assert sum(getattr(row, "status", None) == "SUCCEEDED" for row in results) == 1
        assert gateway1.approve_device_auth.await_count + gateway2.approve_device_auth.await_count == 1
    _, associations = await rows(factory)
    assert sum(row.state == "ACTIVE" for row in associations) == 1


async def test_postgresql_inflight_duplicate_never_repeats_approve(lifecycle_db):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies in-flight replay across service instances")
    first, gateway, quota, _ = auth_fixture(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    approval = gateway.approve_device_auth.return_value

    async def hold_approve(*args):
        entered.set()
        await release.wait()
        return approval

    gateway.approve_device_auth.side_effect = hold_approve
    pending = asyncio.create_task(first.auth(1, LINK))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = DeviceAuthorizationService(factory, gateway, 178, quota, clock=lambda: NOW)
        replay = await second.auth(1, LINK)
        assert replay.status in {"PENDING", "UNKNOWN"}
    finally:
        release.set()
    assert (await asyncio.wait_for(pending, 5)).status == "SUCCEEDED"
    gateway.approve_device_auth.assert_awaited_once()
