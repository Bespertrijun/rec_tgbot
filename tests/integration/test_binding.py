import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select

from reclaude_bot.application.binding import BindingService
from reclaude_bot.domain.errors import BindingError
from reclaude_bot.infrastructure.db.models import AuditLog, DeviceAssociation, DeviceCycleLedger, DeviceQuotaCycle, User

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def forbidden_gateway():
    return SimpleNamespace(**{name: AsyncMock(side_effect=AssertionError("binding must remain local")) for name in ("me", "members", "assign", "revoke", "authenticate")})


async def test_bind_without_rec_member_or_network_and_with_masked_audit(lifecycle_db):
    factory, _ = lifecycle_db
    gateway = forbidden_gateway()
    service = BindingService(factory, gateway)
    seen = []
    engine = factory.kw["bind"]

    def observe(_, __, statement, ___, ____, _____):
        seen.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        user = await service.bind(3001, " MixedCase@example.invalid ", telegram_username="Alice")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert user.reclaude_user_id is None
    assert user.email_normalized == "mixedcase@example.invalid"
    assert user.telegram_username == "alice"
    assert not any("upstream_members" in sql or "member_cycle_baselines" in sql for sql in seen)
    for method in vars(gateway).values():
        method.assert_not_called()
    async with factory() as session:
        notice = await session.scalar(select(AuditLog).where(AuditLog.action == "BIND"))
        assert notice is not None
        assert "mixedcase@" not in str(notice.parameters_summary).lower()
        assert "reclaude_user_id" not in notice.parameters_summary


async def test_idempotent_bind_updates_username_but_cannot_change_bound_email(lifecycle_db):
    factory, _ = lifecycle_db
    service = BindingService(factory)
    first = await service.bind(3001, "one@example.invalid", telegram_username="OldName")
    same = await service.bind(3001, "ONE@example.invalid", telegram_username="NewName")
    assert (same.id, same.telegram_username) == (first.id, "newname")
    with pytest.raises(BindingError):
        await service.bind(3001, "other@example.invalid")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "BIND")) == 1


async def test_private_chat_rate_limit_and_email_format_still_apply(lifecycle_db):
    factory, _ = lifecycle_db
    service = BindingService(factory, attempts_per_hour=1)
    with pytest.raises(BindingError):
        await service.bind(3001, "one@example.invalid", private_chat=False)
    with pytest.raises(BindingError):
        await service.bind(3001, "not-an-email")
    with pytest.raises(BindingError, match="次数"):
        await service.bind(3001, "one@example.invalid")
    async with factory() as session:
        assert await session.scalar(select(User).where(User.telegram_user_id == 3001)) is None


async def test_unbind_and_rebind_keep_identity_history_locks_and_email_reservations(lifecycle_db):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceQuotaCycle(id=1, task_id=1, started_at=NOW, reset_at=NOW + timedelta(days=7), status="VERIFIED", created_at=NOW))
        await session.flush()
        session.add(DeviceCycleLedger(user_id=1, task_id=1, cycle_id=1, quality="VERIFIED", confirmed_used_usd=Decimal("700"), quota_locked_at=NOW, created_at=NOW, updated_at=NOW))
    service = BindingService(factory, forbidden_gateway())
    await service.unbind(1001, operator_telegram_id=1)
    with pytest.raises(BindingError):
        await service.bind(3001, "1@example.invalid")
    with pytest.raises(BindingError):
        await service.bind(1001, "2@example.invalid")
    user = await service.bind(1001, "new@example.invalid")
    assert user.id == 1
    assert user.reclaude_user_id == "legacy-1"
    async with factory() as session:
        ledger = await session.scalar(select(DeviceCycleLedger).where(DeviceCycleLedger.user_id == 1))
        assert ledger.confirmed_used_usd == Decimal("700")
        assert ledger.quota_locked_at is not None
        assert (await session.get(User, 2)).email_normalized == "2@example.invalid"


@pytest.mark.parametrize("field,value", [("status", "BANNED"), ("binding_status", "DISPUTED")])
async def test_bind_never_clears_restrictions(lifecycle_db, field, value):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        user = await session.get(User, 1)
        user.binding_status = "UNBOUND"
        setattr(user, field, value)
    with pytest.raises(BindingError):
        await BindingService(factory).bind(1001, "1@example.invalid")
    async with factory() as session:
        assert getattr(await session.get(User, 1), field) == value


@pytest.mark.parametrize("state", ["PENDING_AUTH", "UNKNOWN", "ACTIVE", "PENDING_REVOKE"])
async def test_unbind_cannot_release_an_unresolved_device_even_with_force(lifecycle_db, state):
    factory, _ = lifecycle_db
    async with factory.begin() as session:
        session.add(DeviceAssociation(user_id=1, task_id=1, org_id=178, device_id=None if state in {"PENDING_AUTH", "UNKNOWN"} else 44500,
                                      state=state, started_at=NOW, updated_at=NOW))
    gateway = forbidden_gateway()
    for force in (False, True):
        with pytest.raises(BindingError):
            await BindingService(factory, gateway).unbind(1001, operator_telegram_id=1, force_revoke=force)
    async with factory() as session:
        assert (await session.get(User, 1)).binding_status == "BOUND"
    gateway.revoke.assert_not_called()


async def test_group_unmute_failure_does_not_roll_back_local_binding(lifecycle_db):
    factory, _ = lifecycle_db
    onboarding = SimpleNamespace(queue_unmute_for_user=AsyncMock(side_effect=RuntimeError("test delivery failure")))
    user = await BindingService(factory, onboarding=onboarding).bind(3001, "one@example.invalid")
    onboarding.queue_unmute_for_user.assert_awaited_once_with(3001)
    async with factory() as session:
        assert (await session.get(User, user.id)).binding_status == "BOUND"


@pytest.mark.parametrize("collision", ["email", "telegram"])
async def test_postgresql_concurrent_first_binding_has_one_winner(lifecycle_db, collision):
    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies independent binding transactions")
    if collision == "email":
        args = [(3001, "one@example.invalid"), (3002, "ONE@example.invalid")]
    else:
        args = [(3001, "one@example.invalid"), (3001, "two@example.invalid")]
    results = await asyncio.wait_for(asyncio.gather(
        *(BindingService(factory).bind(tg, email) for tg, email in args), return_exceptions=True,
    ), 10)
    assert sum(isinstance(row, BindingError) for row in results) == 1
    assert sum(isinstance(row, User) for row in results) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(User).where(User.telegram_user_id.in_([3001, 3002]))) == 1
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "BIND")) == 1


async def test_local_users_do_not_enter_legacy_quota_actions_or_transfers(app_context):
    from reclaude_bot.application.actions import QuotaActionService
    from reclaude_bot.application.quota import QuotaService
    from reclaude_bot.application.task import QuotaTaskService
    from reclaude_bot.domain.errors import EligibilityError
    from reclaude_bot.infrastructure.db.models import QuotaAdjustment, ServiceState

    factory, gateway, settings = app_context
    moment = datetime(2026, 8, 18, tzinfo=UTC)
    gateway.configure_account_id(4949)
    quota = QuotaService(factory, gateway, settings)
    await quota.sync_cycle_from_me(now=moment)
    await quota.sync_members(now=moment)
    first = await BindingService(factory, gateway).bind(3001, "one@example.com")
    second = await BindingService(factory, gateway).bind(3002, "two@example.invalid")
    assert first.reclaude_user_id is second.reclaude_user_id is None
    async with factory.begin() as session:
        session.add(ServiceState(id=1, selected_account_id="4949", write_enabled=False, updated_at=moment))
    tasks = QuotaTaskService(factory, gateway)
    await tasks.create_task("test", Decimal("700"), 1)
    await tasks.start("test", 1)
    actions = QuotaActionService(factory, gateway, quota, settings)
    assert await actions.reconcile_cached(now=moment) == 0
    assert await actions._prepare_action(first.id, effective_limit=Decimal("700"), now=moment) is None
    assert await actions._record_usage_notice(first.id, effective_limit=Decimal("700"), now=moment) is None
    assert not gateway.assign_calls and not gateway.revoke_calls
    with pytest.raises(EligibilityError):
        await quota.transfer_quota(3001, amount=Decimal("1"), recipient_telegram_id=3002, now=moment)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(QuotaAdjustment)) == 0


@pytest.mark.parametrize("email", ["@", "user@", "@example.com", "two@@example.com", "some name@example.com", "user@exa mple.com", "user\t@example.com", "user@exam\nple.com", "user\u00a0@example.com"])
async def test_local_binding_rejects_malformed_email_without_upstream_validation(lifecycle_db, email):
    factory, _ = lifecycle_db
    with pytest.raises(BindingError):
        await BindingService(factory).bind(3001, email)
    async with factory() as session:
        assert await session.scalar(select(User).where(User.telegram_user_id == 3001)) is None


async def test_postgresql_cross_email_claims_fail_cleanly_without_deadlock(lifecycle_db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    factory, backend = lifecycle_db
    if backend != "postgresql":
        pytest.skip("PostgreSQL verifies conflicting row lock order")
    async with factory.begin() as session:
        (await session.get(User, 1)).binding_status = "UNBOUND"
        (await session.get(User, 2)).binding_status = "UNBOUND"
    original_scalar = AsyncSession.scalar
    first_lookups_complete = asyncio.Event()
    arrivals = 0

    async def synchronized_lookup(self, statement, *args, **kwargs):
        nonlocal arrivals
        result = await original_scalar(self, statement, *args, **kwargs)
        if "users.telegram_user_id =" in str(statement):
            # Make both requests observe their own identities before checking the
            # other email. This is a valid overlap between independent requests.
            arrivals += 1
            if arrivals == 2:
                first_lookups_complete.set()
            await asyncio.wait_for(first_lookups_complete.wait(), 5)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", synchronized_lookup)
    results = await asyncio.wait_for(asyncio.gather(
        BindingService(factory).bind(1001, "2@example.invalid"),
        BindingService(factory).bind(1002, "1@example.invalid"),
        return_exceptions=True,
    ), 10)
    assert all(isinstance(result, BindingError) for result in results), [type(result).__name__ for result in results]
    async with factory() as session:
        users = (await session.scalars(select(User).order_by(User.id))).all()
        assert [(user.email_normalized, user.binding_status) for user in users] == [
            ("1@example.invalid", "UNBOUND"), ("2@example.invalid", "UNBOUND"),
        ]
