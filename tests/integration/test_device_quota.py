import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_context import SingleOrgTaskService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceCycleLedger, DeviceQuotaAdjustment, DeviceQuotaCycle, QuotaTask, User
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_ledger import NOW, association, end_association

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def service(factory, now=NOW):
    return DeviceQuotaService(factory, 178, clock=lambda: now)


async def check_auth(factory, quota, *, user_id=1, now=NOW):
    context = await SingleOrgTaskService(factory, 178).resolve_task()
    async with factory.begin() as session:
        user = await session.get(User, user_id, with_for_update=True)
        await quota.auth_quota_check(session, user, context, now)


async def test_first_auth_and_stopped_task_do_not_require_prior_device_usage(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory, running=False)
    await check_auth(factory, service(factory))


@pytest.mark.parametrize("used,allowed", [("699.9999999999", True), ("700", False), ("700.0000000001", False)])
async def test_auth_uses_exact_confirmed_spend_even_without_existing_quota_lock(lifecycle_db, used, allowed):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, used)
    await end_association(factory, association_id, at=NOW)
    quota = service(factory)
    if allowed:
        await check_auth(factory, quota)
    else:
        with pytest.raises(EligibilityError):
            await check_auth(factory, quota)


async def test_existing_lock_survives_positive_adjustment(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, ledger_id = await metered_user(factory, cycle_id, "700", locked=True)
    await end_association(factory, association_id, at=NOW)
    quota = service(factory)
    await quota.adjust(1, cycle_id, Decimal("100"), "top up", 999, operation_key="top-up")
    status = await quota.status(1)
    assert status.used_usd == Decimal("700")
    assert status.effective_limit_usd == Decimal("800")
    with pytest.raises(EligibilityError):
        await check_auth(factory, quota)
    async with factory() as session:
        ledger = await session.get(DeviceCycleLedger, ledger_id)
        assert ledger.quota_locked_at is not None and ledger.quota_unlocked_at is None


@pytest.mark.parametrize("case,allowed", [("eligible", True), ("weekly_full", False), ("stale", False), ("not_final_day", False), ("invalid_cycle", False)])
async def test_last_day_auth_requires_current_valid_account_evidence(lifecycle_db, case, allowed):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory, reset=NOW + timedelta(hours=23), allow_last_day=True)
    association_id, _ = await metered_user(factory, cycle_id, "750", locked=True)
    await end_association(factory, association_id, at=NOW)
    async with factory.begin() as session:
        cycle = await session.get(DeviceQuotaCycle, cycle_id)
        if case == "weekly_full":
            cycle.weekly_percent = Decimal("100")
        elif case == "stale":
            cycle.last_day_checked_at = NOW - timedelta(hours=1)
        elif case == "not_final_day":
            cycle.reset_at = NOW + timedelta(hours=25)
        elif case == "invalid_cycle":
            cycle.status = "NEEDS_REVIEW"
    quota = service(factory)
    if allowed:
        await check_auth(factory, quota)
    else:
        with pytest.raises(EligibilityError):
            await check_auth(factory, quota)


async def test_new_cycle_does_not_carry_old_lock_or_old_usage(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, "750", locked=True)
    await end_association(factory, association_id, at=NOW)
    later = NOW + timedelta(days=7)
    new_id = await ready_cycle(factory, now=later)
    quota = service(factory, now=later)
    await check_auth(factory, quota, now=later)
    status = await quota.status(1)
    assert status.cycle_id == new_id and status.used_usd == Decimal("0")
    async with factory() as session:
        old = await session.scalar(select(DeviceCycleLedger).where(DeviceCycleLedger.cycle_id == cycle_id))
        assert old.confirmed_used_usd == Decimal("750") and old.quota_locked_at is not None


async def test_missing_sample_after_deauth_is_not_free_allowance(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    association_id = await association(factory, started=NOW - timedelta(minutes=1))
    await end_association(factory, association_id, at=NOW)
    with pytest.raises(EligibilityError):
        await check_auth(factory, service(factory))


async def test_zero_limit_denies_initial_auth_without_division(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    async with factory.begin() as session:
        (await session.get(QuotaTask, 1)).limit_usd = Decimal("0")
    with pytest.raises(EligibilityError):
        await check_auth(factory, service(factory))


async def test_transfer_changes_effective_limits_not_consumption_and_is_idempotent(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "100")
    await metered_user(factory, cycle_id, "200", user_id=2, device_id=44501)
    quota = service(factory)
    for _ in range(2):
        await quota.transfer(1, 2, cycle_id, Decimal("25.1234567890"), operation_key="transfer-1")
    one, two = await quota.status(1), await quota.status(2)
    assert one.used_usd == Decimal("100") and two.used_usd == Decimal("200")
    assert one.effective_limit_usd == Decimal("674.8765432110")
    assert two.effective_limit_usd == Decimal("725.1234567890")
    assert one.remaining_usd == Decimal("574.8765432110")
    async with factory() as session:
        rows = (await session.scalars(select(DeviceQuotaAdjustment))).all()
        assert len(rows) == 2 and sum(row.amount_usd for row in rows) == 0
        assert rows[0].transfer_key == rows[1].transfer_key


async def test_concurrent_transfers_cannot_overdraw(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "600")
    await metered_user(factory, cycle_id, "0", user_id=2, device_id=44501)
    quota = service(factory)
    results = await asyncio.gather(
        quota.transfer(1, 2, cycle_id, Decimal("80"), operation_key="a"),
        quota.transfer(1, 2, cycle_id, Decimal("80"), operation_key="b"), return_exceptions=True)
    assert sum(isinstance(result, EligibilityError) for result in results) == 1
    assert (await quota.status(1)).remaining_usd == Decimal("20")
    assert (await quota.status(2)).effective_limit_usd == Decimal("780")


async def test_opposite_transfers_do_not_deadlock_or_create_money(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "0")
    await metered_user(factory, cycle_id, "0", user_id=2, device_id=44501)
    quota = service(factory)
    await asyncio.wait_for(asyncio.gather(
        quota.transfer(1, 2, cycle_id, Decimal("10"), operation_key="ab"),
        quota.transfer(2, 1, cycle_id, Decimal("20"), operation_key="ba")), timeout=5)
    assert (await quota.status(1)).effective_limit_usd == Decimal("710")
    assert (await quota.status(2)).effective_limit_usd == Decimal("690")


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "100000000", "0.00000000001"])
async def test_invalid_transfer_amount_is_atomic(lifecycle_db, amount):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    quota = service(factory)
    with pytest.raises(EligibilityError):
        await quota.transfer(1, 2, cycle_id, Decimal(amount), operation_key="invalid")
    async with factory() as session:
        assert (await session.scalars(select(DeviceQuotaAdjustment))).all() == []


async def test_unknown_usage_cannot_be_transferred_as_zero(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "100", quality="NEEDS_REVIEW")
    quota = service(factory)
    with pytest.raises(EligibilityError):
        await quota.transfer(1, 2, cycle_id, Decimal("1"), operation_key="unknown")


async def test_adjustment_key_replay_is_not_double_credit_and_conflict_rejected(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    quota = service(factory)
    for _ in range(2):
        await quota.adjust(1, cycle_id, Decimal("50"), "top up", 999, operation_key="same")
    assert (await quota.status(1)).effective_limit_usd == Decimal("750")
    with pytest.raises(EligibilityError):
        await quota.adjust(2, cycle_id, Decimal("50"), "top up", 999, operation_key="same")


async def test_real_auth_callback_allows_reserved_slot_and_blocks_second_device(lifecycle_db):
    from tests.integration.test_device_authorization import LINK, auth_fixture

    factory, _ = lifecycle_db
    await ready_cycle(factory, running=False)
    quota = service(factory)
    auth, gateway, _, _ = auth_fixture(factory, quota_check=quota.auth_quota_check)
    result = await auth.auth(1, LINK)
    assert result.status == "SUCCEEDED"
    with pytest.raises(EligibilityError):
        await auth.auth(1, LINK.replace("private-state", "different-state"))
    gateway.approve_device_auth.assert_awaited_once()


async def test_admin_link_and_self_auth_both_obey_quota_lock(lifecycle_db):
    from reclaude_bot.application.device_admin import DeviceAdminService
    from tests.integration.test_device_authorization import LINK, auth_fixture

    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    association_id, _ = await metered_user(factory, cycle_id, "700", locked=True)
    await end_association(factory, association_id, at=NOW)
    quota = service(factory)
    auth, gateway, _, _ = auth_fixture(factory, quota_check=quota.auth_quota_check)
    admin = DeviceAdminService(factory, gateway, 178, quota.auth_quota_check, clock=lambda: NOW)
    with pytest.raises(EligibilityError):
        await auth.auth(1, LINK)
    with pytest.raises(EligibilityError):
        await admin.authuser(1, 44501, 999)
    gateway.approve_device_auth.assert_not_called()


async def test_transfer_in_does_not_clear_existing_recipient_lock(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "0")
    ended, ledger_id = await metered_user(factory, cycle_id, "700", user_id=2, device_id=44501, locked=True)
    await end_association(factory, ended, at=NOW)
    quota = service(factory)
    await quota.transfer(1, 2, cycle_id, Decimal("100"), operation_key="locked-in")
    assert (await quota.status(2)).effective_limit_usd == Decimal("800")
    with pytest.raises(EligibilityError):
        await check_auth(factory, quota, user_id=2)
    async with factory() as session:
        row = await session.get(DeviceCycleLedger, ledger_id)
        assert row.quota_locked_at is not None and row.quota_unlocked_at is None


@pytest.mark.parametrize("case", ["expired", "other_org", "excluded_sender", "excluded_recipient", "banned_recipient", "unbound_sender", "self"])
async def test_transfer_rejects_ineligible_participants_or_cycle(lifecycle_db, case):
    from reclaude_bot.application.device_task_members import DeviceTaskMemberService
    from tests.integration.test_device_ledger import cycle

    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    await metered_user(factory, cycle_id, "100")
    await metered_user(factory, cycle_id, "0", user_id=2, device_id=44501)
    target = 2
    if case == "other_org":
        cycle_id = await cycle(factory, task_id=2)
    elif case.startswith("excluded"):
        await DeviceTaskMemberService(factory, 178).delete_members(None, [1 if case.endswith("sender") else 2], 999)
    elif case in {"banned_recipient", "unbound_sender", "expired"}:
        async with factory.begin() as session:
            if case == "expired":
                row = await session.get(DeviceQuotaCycle, cycle_id)
                row.reset_at = NOW
            elif case == "banned_recipient":
                (await session.get(User, 2)).status = "BANNED"
            else:
                (await session.get(User, 1)).binding_status = "UNBOUND"
    else:
        target = 1
    with pytest.raises(EligibilityError):
        await service(factory).transfer(1, target, cycle_id, Decimal("1"), operation_key="ineligible")
    async with factory() as session:
        assert (await session.scalars(select(DeviceQuotaAdjustment))).all() == []


async def test_transfer_idempotency_key_conflict_cannot_redirect_credit(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    quota = service(factory)
    await quota.transfer(1, 2, cycle_id, Decimal("10"), operation_key="fixed")
    with pytest.raises(EligibilityError):
        await quota.transfer(1, 2, cycle_id, Decimal("20"), operation_key="fixed")
    assert (await quota.status(1)).effective_limit_usd == Decimal("690")
    assert (await quota.status(2)).effective_limit_usd == Decimal("710")


@pytest.mark.parametrize("case", ["missing", "expired", "stale", "future_evidence"])
async def test_auth_rejects_unavailable_cycle_evidence(lifecycle_db, case):
    factory, _ = lifecycle_db
    if case != "missing":
        cycle_id = await ready_cycle(factory)
        async with factory.begin() as session:
            row = await session.get(DeviceQuotaCycle, cycle_id)
            if case == "expired":
                row.reset_at = NOW
            elif case == "stale":
                row.last_day_checked_at = NOW - timedelta(minutes=10)
            else:
                row.last_day_checked_at = NOW + timedelta(minutes=1)
    with pytest.raises(EligibilityError):
        await check_auth(factory, service(factory))


async def test_scope_isolation_does_not_use_other_org_lowest_limit(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    async with factory.begin() as session:
        (await session.get(QuotaTask, 2)).limit_usd = Decimal("1")
    quota = service(factory)
    await check_auth(factory, quota)
    assert (await quota.status(1)).effective_limit_usd == Decimal("700")


@pytest.mark.parametrize("amount", ["-100000000", "NaN", "Infinity", "0.00000000001",
    pytest.param("99999999", marks=pytest.mark.xfail(strict=True, reason="用户明确不增加管理员调整后的总额度上界检查"))])
async def test_invalid_or_overflowing_adjustment_returns_domain_error_atomically(lifecycle_db, amount):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    with pytest.raises(EligibilityError):
        await service(factory).adjust(1, cycle_id, Decimal(amount), "test", 999, operation_key="bad-adjustment")
    async with factory() as session:
        assert (await session.scalars(select(DeviceQuotaAdjustment))).all() == []


@pytest.mark.xfail(strict=True, reason="用户不采纳新增缺段授权/转出拦截，保留采集重试及日志")
async def test_missing_second_device_segment_cannot_reuse_old_verified_balance(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    first_id, _ = await metered_user(factory, cycle_id, "100")
    await end_association(factory, first_id, at=NOW)
    later = NOW + timedelta(seconds=2)
    second_id = await association(factory, device_id=44501, started=NOW + timedelta(seconds=1))
    await end_association(factory, second_id, at=later)
    # Simulate sampling queue creation being unavailable for the second device.
    # The previous ledger remains VERIFIED, but is no longer the complete cycle.
    quota = service(factory, now=later)
    with pytest.raises(EligibilityError):
        await check_auth(factory, quota, now=later)
    with pytest.raises(EligibilityError):
        await quota.transfer(1, 2, cycle_id, Decimal("1"), operation_key="missing-segment")
