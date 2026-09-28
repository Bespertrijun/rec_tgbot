"""Independent database setup for device quota and complete runtime acceptance."""
from datetime import timedelta
from decimal import Decimal

from reclaude_bot.infrastructure.db.models import DeviceCycleLedger, DeviceQuotaCycle, QuotaTask, ServiceState
from tests.integration.test_device_ledger import NOW, association, cycle, snapshot
from tests.integration.test_device_ledger import ledger_service as make_ledger


async def ready_cycle(factory, *, now=NOW, reset=None, running=True, allow_last_day=False):
    cycle_id = await cycle(factory, started=now - timedelta(days=1), reset=reset or now + timedelta(days=6))
    async with factory.begin() as session:
        row = await session.get(DeviceQuotaCycle, cycle_id)
        row.weekly_percent = Decimal("10")
        row.last_day_checked_at = now
        row.last_day_allow = allow_last_day
        task = await session.get(QuotaTask, 1)
        task.status = "RUNNING" if running else "STOPPED"
        state = await session.get(ServiceState, 1)
        if state is None:
            session.add(ServiceState(id=1, selected_account_id="7022", write_enabled=running,
                                     reason="test", updated_at=now))
        else:
            state.selected_account_id = "7022"
            state.write_enabled = running
    return cycle_id


async def metered_user(factory, cycle_id, total="100", *, user_id=1, device_id=44500,
                       now=NOW, quality=None, locked=False):
    association_id = await association(factory, user_id=user_id, device_id=device_id, started=now - timedelta(minutes=2))
    ledger, _ = make_ledger(factory, now=now)
    result = await ledger.apply(association_id, cycle_id, await snapshot(factory, total, device_id=device_id, sampled=now))
    if quality is not None or locked:
        async with factory.begin() as session:
            row = await session.get(DeviceCycleLedger, result.ledger_id)
            if quality is not None:
                row.quality = quality
            if locked:
                row.quota_locked_at = now
    return association_id, result.ledger_id
