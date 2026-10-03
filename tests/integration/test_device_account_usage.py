from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_context import OrgAccountUsage
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import (
    Device,
    DeviceCycleLedger,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceResampleJob,
    DeviceUsageSegment,
    QuotaTask,
    ServiceState,
)
from reclaude_bot.infrastructure.reclaude.models import CurrentAccount, MeResponse, SevenDay, UsageSnapshot, UsageWindow, WeeklyLimit
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_ledger import NOW, association, end_association, ledger_service, snapshot


def source_usage(*, percent="10", reset=None, sampled_at=NOW, account_id="7022"):
    reset = reset or NOW + timedelta(days=6)
    return OrgAccountUsage(org_id=178, account_id=account_id, me=MeResponse(current_account=CurrentAccount(
        status="bound", email_masked="a***@example.invalid", usage_updated_at=sampled_at,
        usage_snapshot=UsageSnapshot(
            limits=[WeeklyLimit(group="weekly", kind="weekly_all", scope=None, percent=percent, resets_at=reset, is_active=True)],
            seven_day=SevenDay(utilization=percent, resets_at=reset),
            five_hour=UsageWindow(utilization="35", resets_at=NOW + timedelta(hours=3)),
        ),
    )))


def service(factory, usage=None, *, clock=lambda: NOW):
    source = SimpleNamespace(get_usage=AsyncMock(return_value=usage or source_usage()))
    return DeviceAccountUsageService(factory, source, 178, clock=clock), source


async def seeded(factory, *, total="120"):
    cycle_id = await ready_cycle(factory)
    association_id, ledger_id = await metered_user(factory, cycle_id, total)
    return cycle_id, association_id, ledger_id


@pytest.mark.parametrize("is_active", [False, True])
async def test_estimate_uses_confirmed_spend_and_is_read_only(lifecycle_db, is_active):
    factory, _ = lifecycle_db
    await seeded(factory)
    usage = source_usage()
    usage.me.weekly_all().is_active = is_active
    app, source = service(factory, usage)
    statements = []
    engine = factory.kw["bind"].sync_engine

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = await app.get_account_usage(1)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.managed_used_usd == Decimal("120")
    assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None
    assert result.five_hour_utilization == Decimal("35")
    assert result.account_status == "bound"
    source.get_usage.assert_awaited_once_with(178)
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)


async def test_unbound_upstream_account_status_is_preserved_and_not_estimated(lifecycle_db):
    factory, _ = lifecycle_db
    await seeded(factory)
    usage = source_usage()
    usage.me.current_account.status = "unbound"
    result = await service(factory, usage)[0].get_account_usage(1)
    assert result.account_status == "unbound"
    assert result.estimated_total_usd is None


async def test_repeated_auth_revoke_counts_each_segment_once_with_late_tail(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    ledger, _ = ledger_service(factory, now=NOW)
    async with factory.begin() as session:
        session.add(Device(org_id=178, device_id=44503, name="third", first_synced_at=NOW, last_synced_at=NOW))
    for index, (device_id, amount) in enumerate([(44500, "20"), (44501, "30"), (44503, "10")]):
        started = NOW - timedelta(hours=8 - 3 * index)
        aid = await association(factory, device_id=device_id, started=started)
        await ledger.apply(aid, cycle_id, await snapshot(factory, amount, device_id=device_id, sampled=started + timedelta(minutes=10)))
        if index < 2:
            ended = started + timedelta(minutes=20)
            await end_association(factory, aid, at=ended)
            # The first device reports a further $2 during its final collection.
            final = str(Decimal(amount) + (2 if index == 0 else 0))
            await ledger.apply(aid, cycle_id, await snapshot(factory, final, device_id=device_id, sampled=ended + timedelta(hours=1)))
            async with factory.begin() as session:
                session.add(DeviceResampleJob(
                    association_id=aid, sequence=7, status="COMPLETED", attempt_count=1,
                    run_after=ended + timedelta(hours=1), completed_at=ended + timedelta(hours=1),
                    created_at=ended, updated_at=ended + timedelta(hours=1),
                ))
        else:
            await ledger.apply(aid, cycle_id, await snapshot(factory, amount, device_id=device_id, sampled=NOW))
    result = await service(factory)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("62")
    assert result.estimated_total_usd == Decimal("620") and result.estimate_reason is None


async def test_empty_current_member_scope_still_counts_historical_users_and_ignores_adjustments(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, ledger_id = await seeded(factory, total="20")
    await metered_user(factory, cycle_id, "100", user_id=2, device_id=44501)
    async with factory.begin() as session:
        task = await session.get(QuotaTask, 1)
        task.scope_mode = "ALLOWLIST"  # No DeviceTaskMember rows: the display scope is empty.
        session.add(DeviceQuotaAdjustment(
            ledger_id=ledger_id, amount_usd=Decimal("500"), reason="bonus", operator_telegram_id=1,
            operation_key=uuid4().hex, created_at=NOW,
        ))
    result = await service(factory)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("120") and result.estimated_total_usd == Decimal("1200")


@pytest.mark.parametrize("case", ["missing_ledger", "unknown_amount", "unknown_with_amount", "needs_review", "stale", "imported", "final_pending"])
async def test_estimate_uses_known_user_consumption_without_completeness_gate(lifecycle_db, case):
    factory, _ = lifecycle_db
    _, aid, ledger_id = await seeded(factory)
    if case == "missing_ledger":
        await association(factory, user_id=2, device_id=44501, started=NOW - timedelta(minutes=1))
    elif case == "final_pending":
        await end_association(factory, aid, at=NOW)
    else:
        async with factory.begin() as session:
            row = await session.get(DeviceCycleLedger, ledger_id)
            segment = await session.scalar(select(DeviceUsageSegment).where(DeviceUsageSegment.ledger_id == ledger_id))
            if case == "unknown_amount":
                row.quality, row.confirmed_used_usd = "UNKNOWN", None
            elif case == "unknown_with_amount":
                row.quality = "UNKNOWN"
            elif case == "needs_review":
                row.quality = "NEEDS_REVIEW"
            elif case == "stale":
                segment.latest_sampled_at = NOW - timedelta(minutes=5)
            elif case == "imported":
                segment.imported_used_usd = Decimal("100")
    result = await service(factory)[0].get_account_usage(1)
    if case == "unknown_amount":
        assert result.estimated_total_usd is None
    else:
        assert result.managed_used_usd == Decimal("120")
        assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None
    assert "不完整" not in (result.estimate_reason or "")


@pytest.mark.parametrize("case", ["zero_spend", "zero_percent", "future_source", "wrong_account", "unselected", "wrong_window", "cycle_review"])
async def test_invalid_cycle_or_source_degrades_without_mutating_usage(lifecycle_db, case):
    factory, _ = lifecycle_db
    cycle_id, _, _ = await seeded(factory, total="0" if case == "zero_spend" else "120")
    usage = source_usage(percent="0" if case == "zero_percent" else "10")
    if case == "future_source":
        usage.me.current_account.usage_updated_at = NOW + timedelta(seconds=1)
    elif case == "wrong_account":
        usage = source_usage(account_id="other")
    elif case == "wrong_window":
        usage.me.current_account.usage_snapshot.seven_day.resets_at += timedelta(hours=1)
    elif case in {"unselected", "cycle_review"}:
        async with factory.begin() as session:
            if case == "unselected":
                (await session.get(ServiceState, 1)).selected_account_id = None
            else:
                row = await session.get(DeviceQuotaCycle, cycle_id)
                row.status = "NEEDS_REVIEW"
    result = await service(factory, usage)[0].get_account_usage(1)
    assert result.estimated_total_usd is None and result.estimate_reason


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
async def test_nonfinite_or_negative_percentage_never_reaches_formula(lifecycle_db, value):
    factory, _ = lifecycle_db
    await seeded(factory)
    usage = source_usage()
    usage.me.current_account.usage_snapshot.seven_day.utilization = Decimal(value)
    result = await service(factory, usage)[0].get_account_usage(1)
    assert result.estimated_total_usd is None


async def test_missing_cycle_keeps_account_windows(lifecycle_db):
    factory, _ = lifecycle_db
    result = await service(factory)[0].get_account_usage(1)
    assert result.estimated_total_usd is None
    assert result.five_hour_utilization == Decimal("35") and result.seven_day_utilization == Decimal("10")


async def test_other_org_task_rejected_before_upstream_query(lifecycle_db):
    factory, _ = lifecycle_db
    app, source = service(factory)
    with pytest.raises(EligibilityError):
        await app.get_account_usage(2)
    source.get_usage.assert_not_awaited()


async def test_cycle_change_during_account_request_invalidates_estimate(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, _ = await seeded(factory)
    app, source = service(factory)

    async def change_cycle(_org):
        async with factory.begin() as session:
            (await session.get(DeviceQuotaCycle, cycle_id)).started_at = NOW - timedelta(hours=1)
        return source_usage()

    source.get_usage.side_effect = change_cycle
    result = await app.get_account_usage(1)
    assert result.estimated_total_usd is None and result.estimate_reason == "账号或周期变化"


async def test_snapshot_refreshed_while_request_is_in_flight_is_not_future_data(lifecycle_db):
    factory, _ = lifecycle_db
    await seeded(factory)
    clock = [NOW]
    app, source = service(factory, clock=lambda: clock[0])

    async def receive_fresh_snapshot(_org):
        clock[0] += timedelta(seconds=2)
        return source_usage(sampled_at=NOW + timedelta(seconds=1))

    source.get_usage.side_effect = receive_fresh_snapshot
    result = await app.get_account_usage(1)
    assert result.estimated_total_usd == Decimal("1200")


async def test_unresolved_auth_does_not_block_estimate_from_existing_user_consumption(lifecycle_db):
    factory, _ = lifecycle_db
    await seeded(factory)
    # The second authorization may have succeeded remotely; local outcome is unknown.
    await association(factory, user_id=2, device_id=44501, auth_status="UNKNOWN", started=NOW - timedelta(minutes=1))
    result = await service(factory)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("120")
    assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None


async def test_old_samples_in_same_cycle_remain_usable(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, ledger_id = await seeded(factory)
    sampled_at = NOW - timedelta(minutes=5)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, cycle_id)).last_day_checked_at = sampled_at
        segment = await session.scalar(select(DeviceUsageSegment).where(DeviceUsageSegment.ledger_id == ledger_id))
        segment.latest_sampled_at = sampled_at
    result = await service(factory, source_usage(sampled_at=sampled_at))[0].get_account_usage(1)
    assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None


async def test_user_without_known_amount_does_not_discard_other_users_known_amount(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id, _, _ = await seeded(factory)
    async with factory.begin() as session:
        session.add(DeviceCycleLedger(
            user_id=2, cycle_id=cycle_id, task_id=1, confirmed_used_usd=None,
            quality="UNKNOWN", created_at=NOW, updated_at=NOW,
        ))
    result = await service(factory)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("120")
    assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None


async def test_partial_local_period_uses_current_ledger_without_full_week_gate(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, cycle_id)).started_at = NOW - timedelta(hours=1)
    await metered_user(factory, cycle_id, "120")
    result = await service(factory)[0].get_account_usage(1)
    assert result.estimated_total_usd == Decimal("1200") and result.estimate_reason is None


async def test_other_periods_and_other_organizations_do_not_enter_sum(lifecycle_db):
    factory, _ = lifecycle_db
    await seeded(factory)
    async with factory.begin() as session:
        for task_id, started, reset in [
            (1, NOW - timedelta(days=8), NOW - timedelta(days=1)),
            (2, NOW - timedelta(days=1), NOW + timedelta(days=6)),
        ]:
            old = DeviceQuotaCycle(
                task_id=task_id, started_at=started, reset_at=reset, created_at=started,
                status="EXPIRED" if task_id == 1 else "VERIFIED", account_id="7022",
            )
            session.add(old)
            await session.flush()
            session.add(DeviceCycleLedger(
                user_id=1, cycle_id=old.id, task_id=task_id, confirmed_used_usd=Decimal("999"),
                quality="VERIFIED", created_at=started, updated_at=NOW,
            ))
    result = await service(factory)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("120") and result.estimated_total_usd == Decimal("1200")


@pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)
async def test_account_switch_during_local_aggregation_invalidates_estimate(lifecycle_db, monkeypatch):
    factory, _ = lifecycle_db
    await seeded(factory)
    app, _ = service(factory)
    aggregate = app._aggregate_cycle

    async def switch_after_aggregation(session, cycle, moment):
        result = await aggregate(session, cycle, moment)
        # Simulate control-plane account state changing while the command is reading.
        # Normal /use currently refuses switching an already selected account.
        async with factory.begin() as concurrent:
            (await concurrent.get(ServiceState, 1)).selected_account_id = "different-account"
        return result

    monkeypatch.setattr(app, "_aggregate_cycle", switch_after_aggregation)
    result = await app.get_account_usage(1)
    assert result.estimated_total_usd is None and result.estimate_reason == "账号或周期变化"


async def test_cycle_expiring_during_account_request_invalidates_estimate(lifecycle_db):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(seconds=1)
    cycle_id = await ready_cycle(factory, reset=reset)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, cycle_id)).started_at = reset - timedelta(days=7)
    await metered_user(factory, cycle_id, "120")
    clock = [NOW]
    app, source = service(factory, clock=lambda: clock[0])

    async def receive_after_reset(_org):
        clock[0] = reset + timedelta(seconds=1)
        return source_usage(reset=reset)

    source.get_usage.side_effect = receive_after_reset
    result = await app.get_account_usage(1)
    assert result.estimated_total_usd is None


@pytest.mark.parametrize("seconds,allowed", [(-301, False), (-300, True), (-1, True), (1, True), (300, True), (301, False)])
@pytest.mark.parametrize("changed_time", ["upstream", "seven_day"])
async def test_estimate_uses_five_minute_reset_tolerance(lifecycle_db, seconds, allowed, changed_time):
    factory, _ = lifecycle_db
    await seeded(factory, total="1205.51")
    usage = source_usage(percent="73")
    if changed_time == "upstream":
        usage = source_usage(percent="73", reset=NOW + timedelta(days=6, seconds=seconds))
    else:
        usage.me.current_account.usage_snapshot.seven_day.resets_at += timedelta(seconds=seconds)
    result = await service(factory, usage)[0].get_account_usage(1)
    assert result.managed_used_usd == Decimal("1205.51")
    if allowed:
        assert result.estimated_total_usd == Decimal("1205.51") * 100 / 73
        assert result.estimate_reason is None
    else:
        assert result.estimated_total_usd is None
        assert result.estimate_reason in {"账号或周期变化", "账号 7天窗口与周期不一致"}


async def test_one_second_drift_recovers_reported_estimate_after_sync(lifecycle_db):
    from reclaude_bot.application.device_cycle import DeviceCycleService

    factory, _ = lifecycle_db
    cycle_id, _, ledger_id = await seeded(factory, total="1205.51")
    async with factory.begin() as session:
        cycle = await session.get(DeviceQuotaCycle, cycle_id)
        original_reset = cycle.reset_at
        cycle.status = "NEEDS_REVIEW"
        cycle.weekly_percent = None
    app, source = service(factory, source_usage(percent="73", reset=original_reset + timedelta(seconds=1)))
    assert (await app.get_account_usage(1)).estimate_reason == "账号周期未核实"
    recovered = await DeviceCycleService(factory, source, 178, clock=lambda: NOW).sync()
    result = await app.get_account_usage(1)
    assert recovered.id == cycle_id and recovered.status == "VERIFIED"
    assert result.estimate_reason is None
    assert result.estimated_total_usd.quantize(Decimal("0.01")) == Decimal("1651.38")
    async with factory() as session:
        assert (await session.get(DeviceCycleLedger, ledger_id)).confirmed_used_usd == Decimal("1205.51")
