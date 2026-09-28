from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.bot.handlers import build_router
from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.db.models import DeviceAction, DeviceAssociation
from tests.fixtures.device_runtime import metered_user, ready_cycle
from tests.integration.test_device_ledger import NOW, association, end_association
from tests.unit.test_device_command_wiring import command_callbacks

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def entry(factory):
    configured = Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test")
    callback = command_callbacks(build_router(configured))["status"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1001, username="alice"),
                              chat=SimpleNamespace(type="private", id=1001), answer=AsyncMock())
    return callback, message, DeviceQuotaService(factory, 178, clock=lambda: NOW)


async def test_status_discloses_unknown_auth_instead_of_telling_user_to_reauthorize(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    async with factory.begin() as session:
        row = DeviceAssociation(user_id=1, task_id=1, org_id=178, state="UNKNOWN", started_at=NOW, updated_at=NOW)
        session.add(row)
        await session.flush()
        session.add(DeviceAction(association_id=row.id, kind="AUTH", status="UNKNOWN", attempt_count=1,
                                 idempotency_key="unknown-auth", created_at=NOW, updated_at=NOW))
    callback, message, quota = entry(factory)
    await callback(message, quota)
    text = message.answer.await_args.args[0]
    assert "待核对" in text and "授权" in text


async def test_status_keeps_authorized_device_visible_when_first_usage_is_pending(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    await association(factory)
    callback, message, quota = entry(factory)
    await callback(message, quota)
    text = message.answer.await_args.args[0]
    assert "44500" in text and "待同步" in text
    assert "本周期已用：$0.00" not in text


async def test_status_without_active_device_retains_user_cycle_usage(lifecycle_db):
    factory, _ = lifecycle_db
    cycle_id = await ready_cycle(factory)
    linked, _ = await metered_user(factory, cycle_id, "123.45")
    await end_association(factory, linked, at=NOW)
    callback, message, quota = entry(factory)
    await callback(message, quota)
    text = message.answer.await_args.args[0]
    assert "$123.45" in text and "$576.55" in text
    async with factory() as session:
        assert (await session.scalar(select(DeviceAssociation))).state == "ENDED"
