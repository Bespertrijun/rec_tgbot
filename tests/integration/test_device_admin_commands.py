from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.db.models import DeviceAssociation, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRevokeResponse
from tests.fixtures.device_runtime import ready_cycle
from tests.integration.test_device_admin import record
from tests.integration.test_device_ledger import NOW, association
from tests.unit.test_device_command_wiring import command_callbacks

pytestmark = pytest.mark.parametrize("lifecycle_db", ["postgresql"], indirect=True)


def wired(factory):
    configured = Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[999])
    callbacks = command_callbacks(build_admin_router(configured))
    gateway = SimpleNamespace(account_id=7022, list_devices=AsyncMock(return_value=[record()]),
                              approve_device_auth=AsyncMock(), revoke_device=AsyncMock(return_value=DeviceRevokeResponse(ok=True)))
    quota = DeviceQuotaService(factory, 178, clock=lambda: NOW)
    admin = DeviceAdminService(factory, gateway, 178, quota.auth_quota_check, clock=lambda: NOW)
    revoke = DeviceRevocationService(factory, gateway, 178, clock=lambda: NOW)
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    return callbacks, gateway, admin, revoke, message


async def test_authuser_accepts_email_and_resolves_existing_local_owner(lifecycle_db):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    callbacks, gateway, admin, _, message = wired(factory)
    await callbacks["authuser"](message, SimpleNamespace(args="1@EXAMPLE.INVALID 44503"), admin)
    async with factory() as session:
        linked = await session.scalar(select(DeviceAssociation))
        assert linked is not None and linked.user_id == 1 and linked.device_id == 44503 and linked.state == "ACTIVE"
    gateway.approve_device_auth.assert_not_called()
    gateway.revoke_device.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "unbound", "banned"])
async def test_authuser_does_not_auto_create_or_authorize_ineligible_email(lifecycle_db, case):
    factory, _ = lifecycle_db
    await ready_cycle(factory)
    callbacks, gateway, admin, _, message = wired(factory)
    if case != "missing":
        async with factory.begin() as session:
            user = await session.get(User, 1)
            if case == "unbound":
                user.binding_status = "UNBOUND"
            else:
                user.status = "BANNED"
    email = "missing@example.invalid" if case == "missing" else "1@example.invalid"
    await callbacks["authuser"](message, SimpleNamespace(args=f"{email} 44503"), admin)
    gateway.list_devices.assert_not_called()
    async with factory() as session:
        assert await session.scalar(select(DeviceAssociation)) is None
        assert len((await session.scalars(select(User))).all()) == 2
    assert message.answer.await_count == 1


async def test_deauthuser_accepts_email_preserving_email_binding(lifecycle_db):
    factory, _ = lifecycle_db
    linked_id = await association(factory)
    callbacks, gateway, _, revoke, message = wired(factory)
    await callbacks["deauthuser"](message, SimpleNamespace(args="1@EXAMPLE.INVALID"), revoke)
    gateway.revoke_device.assert_awaited_once_with(44500)
    async with factory() as session:
        assert (await session.get(DeviceAssociation, linked_id)).state == "ENDED"
        assert (await session.get(User, 1)).binding_status == "BOUND"
