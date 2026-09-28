from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, text

from reclaude_bot.application.device import DeviceAuthorizationService
from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceAssociation, DeviceQuotaCycle
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_admin import record
from tests.integration.test_device_authorization import LINK, auth_fixture
from tests.integration.test_device_cycle import snapshot
from tests.integration.test_device_ledger import NOW, end_association

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def wired(factory, *, fresh=None):
    _, gateway, _, clock = auth_fixture(factory)
    gateway.account_id = "7022"
    gateway.list_devices = AsyncMock(return_value=[record()])
    source = SimpleNamespace(get_usage=AsyncMock(return_value=fresh or snapshot()))
    cycle = DeviceCycleService(factory, source, 178, clock=lambda: clock[0])
    quota = DeviceQuotaService(factory, 178, clock=lambda: clock[0])
    auth = DeviceAuthorizationService(factory, gateway, 178, quota.auth_quota_check,
                                      clock=lambda: clock[0], before_authorize=cycle.sync)
    admin = DeviceAdminService(factory, gateway, 178, quota.auth_quota_check,
                               clock=lambda: clock[0], before_authorize=cycle.sync)
    return SimpleNamespace(auth=auth, admin=admin, cycle=cycle, source=source, gateway=gateway, clock=clock)


async def authorize(runtime, method):
    if method == "self":
        return await runtime.auth.auth(1, LINK)
    return await runtime.admin.authuser(1, 44503, 999)


@pytest.mark.parametrize("method", ["self", "admin"])
@pytest.mark.parametrize("snapshot_age", [timedelta(0), timedelta(seconds=91), timedelta(hours=6)])
async def test_authorization_refreshes_stale_cycle_before_accepting_even_when_task_stopped(lifecycle_db, method, snapshot_age):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory, running=False)
    async with factory.begin() as session:
        (await session.get(DeviceQuotaCycle, cycle_id)).last_day_checked_at = NOW - timedelta(hours=1)
    runtime = wired(factory, fresh=snapshot(sampled_at=NOW - snapshot_age))
    result = await authorize(runtime, method)
    assert result.status == "SUCCEEDED"
    runtime.source.get_usage.assert_awaited_once_with(178)
    async with factory() as session:
        assert (await session.get(DeviceQuotaCycle, cycle_id)).last_day_checked_at == NOW


@pytest.mark.parametrize("method", ["self", "admin"])
async def test_refresh_failure_cannot_fall_back_to_valid_looking_cached_evidence(lifecycle_db, method):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    runtime = wired(factory)
    runtime.source.get_usage.side_effect = httpx.ReadTimeout("private cookie")
    with pytest.raises((httpx.ReadTimeout, EligibilityError)):
        await authorize(runtime, method)
    runtime.gateway.approve_device_auth.assert_not_called()
    runtime.gateway.list_devices.assert_not_called()
    async with factory() as session:
        assert (await session.scalars(select(DeviceAssociation))).all() == []


@pytest.mark.parametrize("method", ["self", "admin"])
@pytest.mark.parametrize("snapshot_age", [timedelta(0), timedelta(hours=6)])
async def test_cached_last_day_permission_is_withdrawn_when_live_weekly_usage_is_full(lifecycle_db, method, snapshot_age):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=23)
    cycle_id = await ready_cycle(factory, reset=reset, allow_last_day=True)
    association_id, _ = await metered_user(factory, cycle_id, "700", locked=True)
    await end_association(factory, association_id, at=NOW)
    runtime = wired(factory, fresh=snapshot(reset=reset, percent="100", sampled_at=NOW - snapshot_age))
    with pytest.raises(EligibilityError):
        await authorize(runtime, method)
    runtime.source.get_usage.assert_awaited_once_with(178)
    runtime.gateway.approve_device_auth.assert_not_called()
    runtime.gateway.list_devices.assert_not_called()


@pytest.mark.parametrize("method", ["self", "admin"])
@pytest.mark.parametrize("snapshot_age", [timedelta(0), timedelta(hours=6)])
async def test_live_last_day_permission_allows_manual_reauthorization(lifecycle_db, method, snapshot_age):
    factory, _ = lifecycle_db
    reset = NOW + timedelta(hours=23)
    cycle_id = await ready_cycle(factory, reset=reset, allow_last_day=False)
    association_id, _ = await metered_user(factory, cycle_id, "700", locked=True)
    await end_association(factory, association_id, at=NOW)
    runtime = wired(factory, fresh=snapshot(reset=reset, percent="90", sampled_at=NOW - snapshot_age))
    assert (await authorize(runtime, method)).status == "SUCCEEDED"
    runtime.gateway.revoke_device.assert_not_called()


async def test_refresh_occurs_without_holding_scope_or_user_database_locks(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    runtime = wired(factory)

    async def fresh(org_id):
        async with factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            await session.execute(text("SELECT task_id FROM device_task_scopes FOR UPDATE"))
            await session.execute(text("SELECT id FROM users FOR UPDATE"))
        return snapshot()

    runtime.source.get_usage.side_effect = fresh
    assert (await runtime.auth.auth(1, LINK)).status == "SUCCEEDED"


async def test_repeat_successful_auth_never_repeats_approval(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    runtime = wired(factory)
    result = await runtime.auth.auth(1, LINK)
    assert result.status == "SUCCEEDED"
    assert await runtime.auth.auth(1, LINK) == result
    runtime.gateway.approve_device_auth.assert_awaited_once()


@pytest.mark.parametrize("method", ["self", "admin"])
async def test_authorization_recovers_cycle_marked_for_fractional_reset_drift(lifecycle_db, method):
    factory, _ = lifecycle_db
    reset = (NOW + timedelta(days=6)).replace(microsecond=555729)
    cycle_id = await ready_cycle(factory, reset=reset)
    async with factory.begin() as session:
        cycle = await session.get(DeviceQuotaCycle, cycle_id)
        cycle.status = "NEEDS_REVIEW"
        cycle.weekly_percent = None
    runtime = wired(factory, fresh=snapshot(reset=reset.replace(microsecond=541290)))
    assert (await authorize(runtime, method)).status == "SUCCEEDED"
    runtime.source.get_usage.assert_awaited_once_with(178)
    async with factory() as session:
        cycle = await session.get(DeviceQuotaCycle, cycle_id)
        assert cycle.status == "VERIFIED" and cycle.reset_at == reset
        assert len((await session.scalars(select(DeviceQuotaCycle))).all()) == 1
