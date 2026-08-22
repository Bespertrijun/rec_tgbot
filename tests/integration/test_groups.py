from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.application.groups import GroupService, GroupSnapshot
from reclaude_bot.domain.enums import ManagedGroupStatus
from reclaude_bot.domain.errors import GroupError
from reclaude_bot.infrastructure.db.base import Base
from reclaude_bot.infrastructure.db.models import AuditLog


@dataclass
class FakeGroupGateway:
    snapshots: dict[int, GroupSnapshot] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)
    fail: bool = False

    async def get_group_snapshot(self, chat_id: int) -> GroupSnapshot | None:
        self.calls.append(chat_id)
        if self.fail:
            raise RuntimeError("gateway unavailable")
        return self.snapshots.get(chat_id)


def snapshot(chat_id: int, *, title: str = "Managed group", admin: bool = True, restrict: bool = True, chat_type: str = "supergroup") -> GroupSnapshot:
    return GroupSnapshot(
        chat_id=chat_id,
        title=title,
        is_bot_member=True,
        is_bot_administrator=admin,
        can_restrict_members=restrict,
        chat_type=chat_type,
        permissions={"can_delete_messages": True},
    )


@pytest.mark.asyncio
async def test_discover_stays_pending_until_owner_approves_with_current_permissions(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001, admin=False)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])

    row = await service.discover(-1001, "New group", actor_telegram_id=42)
    assert row.status == ManagedGroupStatus.PENDING.value
    assert await service.is_active(-1001) is False

    with pytest.raises(GroupError, match="无权"):
        await service.approve(-1001, operator_telegram_id=8)
    with pytest.raises(GroupError, match="管理员"):
        await service.approve(-1001, operator_telegram_id=7)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.PENDING.value

    gateway.snapshots[-1001] = snapshot(-1001)
    approved = await service.approve(-1001, operator_telegram_id=7)
    assert approved.status == ManagedGroupStatus.ACTIVE.value
    with pytest.raises(GroupError, match="待审批"):
        await service.approve(-1001, operator_telegram_id=7)

    async with factory() as session:
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert [row.action for row in actions].count("GROUP_DISCOVER") == 1
    assert [row.action for row in actions].count("GROUP_APPROVE") == 2
    assert sum(row.result == "REJECTED" for row in actions if row.action == "GROUP_APPROVE") == 1


@pytest.mark.asyncio
async def test_multiple_groups_can_be_active_and_listed_independently(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({chat_id: snapshot(chat_id) for chat_id in (-1001, -1002)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])

    for chat_id in (-1001, -1002):
        await service.discover(chat_id, None, actor_telegram_id=42)
        await service.approve(chat_id, operator_telegram_id=7)

    active = await service.list_groups(ManagedGroupStatus.ACTIVE)
    assert [row.chat_id for row in active] == [-1002, -1001]
    assert await service.is_active(-1001) is True
    assert await service.is_active(-1002) is True
    assert await service.is_active(-1003) is False


@pytest.mark.asyncio
async def test_permission_loss_disables_group_and_owner_can_reenable_after_recheck(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001), -1002: snapshot(-1002)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)
    await service.approve(-1001, operator_telegram_id=7)

    gateway.snapshots[-1001] = snapshot(-1001, restrict=False)
    await service.refresh(-1001)
    assert await service.is_active(-1001) is False
    disabled = await service.get_status(-1001)
    assert disabled.status == ManagedGroupStatus.DISABLED.value
    assert disabled.disable_reason == "bot_membership_or_permissions_lost"

    with pytest.raises(GroupError, match="管理员"):
        await service.re_enable(-1001, operator_telegram_id=7)
    gateway.snapshots[-1001] = snapshot(-1001)
    enabled = await service.re_enable(-1001, operator_telegram_id=7)
    assert enabled.status == ManagedGroupStatus.ACTIVE.value
    assert await service.is_active(-1001) is True

    async with factory() as session:
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert any(row.action == "GROUP_PERMISSION_LOST" for row in actions)
    assert sum(row.action == "GROUP_REENABLE" for row in actions) == 2


@pytest.mark.asyncio
async def test_reject_and_disable_callbacks_are_owner_only_and_replay_safe(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001), -1002: snapshot(-1002)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)

    with pytest.raises(GroupError, match="无权"):
        await service.reject(-1001, operator_telegram_id=8)
    rejected = await service.reject(-1001, operator_telegram_id=7)
    assert rejected.status == ManagedGroupStatus.REJECTED.value
    assert (await service.reject(-1001, operator_telegram_id=7)).status == ManagedGroupStatus.REJECTED.value
    with pytest.raises(GroupError, match="待审批"):
        await service.approve(-1001, operator_telegram_id=7)

    await service.discover(-1002, None, actor_telegram_id=42)
    disabled = await service.disable(-1002, operator_telegram_id=7, reason="manual maintenance")
    assert disabled.status == ManagedGroupStatus.DISABLED.value
    assert (await service.disable(-1002, operator_telegram_id=7, reason="ignored")).status == ManagedGroupStatus.DISABLED.value


@pytest.mark.asyncio
async def test_is_active_reads_database_without_gateway_call_or_failure(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)
    await service.approve(-1001, operator_telegram_id=7)
    gateway.calls.clear()
    gateway.fail = True

    assert await service.is_active(-1001) is True
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_duplicate_discovery_conflict_is_recovered_without_integrity_error(tmp_path: Path):
    database = tmp_path / "groups.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    gateway = FakeGroupGateway({-1001: snapshot(-1001)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])

    rows = await asyncio.gather(
        service.discover(-1001, None, actor_telegram_id=42),
        service.discover(-1001, None, actor_telegram_id=43),
    )

    assert {row.chat_id for row in rows} == {-1001}
    async with factory() as session:
        stored = list((await session.scalars(select(AuditLog).where(AuditLog.action == "GROUP_DISCOVER"))).all())
    assert len(stored) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_group_requires_explicit_true_rejoin_to_return_to_pending(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)
    await service.reject(-1001, operator_telegram_id=7)

    await service.discover(-1001, "Updated title", actor_telegram_id=42)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.REJECTED.value
    await service.discover(-1001, None, actor_telegram_id=42, newly_added=True)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.PENDING.value

    await service.discover(-1001, None, actor_telegram_id=42, newly_added=True)
    async with factory() as session:
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert sum(row.action == "GROUP_REDISCOVER" for row in actions) == 1


@pytest.mark.asyncio
async def test_event_snapshot_discovers_and_disables_without_gateway_lookup(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)
    await service.approve(-1001, operator_telegram_id=7)

    gateway.calls.clear()
    gateway.fail = True
    removed_snapshot = snapshot(-1001)
    removed_snapshot = GroupSnapshot(
        chat_id=removed_snapshot.chat_id,
        title=removed_snapshot.title,
        is_bot_member=False,
        is_bot_administrator=False,
        can_restrict_members=False,
        chat_type="supergroup",
    )
    await service.discover(-1001, None, actor_telegram_id=42, event_snapshot=removed_snapshot)

    assert gateway.calls == []
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.DISABLED.value
    async with factory() as session:
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert sum(row.action == "GROUP_PERMISSION_LOST" for row in actions) == 1

    await service.discover(-1002, "Event discovered", actor_telegram_id=42, event_snapshot=GroupSnapshot(
        chat_id=-1002,
        title="Event discovered",
        is_bot_member=True,
        is_bot_administrator=False,
        can_restrict_members=False,
        chat_type="supergroup",
    ))
    assert (await service.get_status(-1002)).status == ManagedGroupStatus.PENDING.value


@pytest.mark.asyncio
async def test_reenable_rechecks_active_group_and_does_not_record_success_on_stale_permissions(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001)})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])
    await service.discover(-1001, None, actor_telegram_id=42)
    await service.approve(-1001, operator_telegram_id=7)

    gateway.snapshots[-1001] = snapshot(-1001, restrict=False)
    with pytest.raises(GroupError, match="当前不在群中|限制成员"):
        await service.re_enable(-1001, operator_telegram_id=7)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.DISABLED.value

    async with factory() as session:
        failed_actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert sum(row.action == "GROUP_REENABLE" and row.result == "SUCCESS" for row in failed_actions) == 0
    assert sum(row.action == "GROUP_PERMISSION_LOST" for row in failed_actions) == 1

    gateway.snapshots[-1001] = snapshot(-1001)
    await service.re_enable(-1001, operator_telegram_id=7)
    async with factory() as session:
        actions = list((await session.scalars(select(AuditLog).where(AuditLog.target_id == "-1001"))).all())
    assert sum(row.action == "GROUP_REENABLE" and row.result == "SUCCESS" for row in actions) == 1


@pytest.mark.asyncio
async def test_basic_group_can_be_pending_but_cannot_be_approved_or_reenabled(app_context):
    factory, _, _ = app_context
    gateway = FakeGroupGateway({-1001: snapshot(-1001, chat_type="group")})
    service = GroupService(factory, gateway, owner_telegram_ids=[7])

    row = await service.discover(-1001, None, actor_telegram_id=42)
    assert row.status == ManagedGroupStatus.PENDING.value
    with pytest.raises(GroupError, match="升级为超级群"):
        await service.approve(-1001, operator_telegram_id=7)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.PENDING.value

    await service.disable(-1001, operator_telegram_id=7, reason="test")
    with pytest.raises(GroupError, match="升级为超级群"):
        await service.re_enable(-1001, operator_telegram_id=7)
    assert (await service.get_status(-1001)).status == ManagedGroupStatus.DISABLED.value
