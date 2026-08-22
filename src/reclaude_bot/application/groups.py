from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.domain.enums import ManagedGroupStatus
from reclaude_bot.domain.errors import GroupError
from reclaude_bot.infrastructure.db.models import ManagedGroup


@dataclass(frozen=True)
class GroupSnapshot:
    """Bot membership and permission state returned by the Telegram adapter."""

    chat_id: int
    title: str
    is_bot_member: bool
    is_bot_administrator: bool
    can_restrict_members: bool
    chat_type: str = ""
    permissions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def bot_is_member(self) -> bool:
        return self.is_bot_member

    @property
    def bot_is_administrator(self) -> bool:
        return self.is_bot_administrator

    @property
    def bot_can_restrict_members(self) -> bool:
        return self.can_restrict_members

    def as_dict(self) -> dict[str, Any]:
        snapshot = dict(self.permissions)
        snapshot.update(
            {
                "is_bot_member": self.is_bot_member,
                "is_bot_administrator": self.is_bot_administrator,
                "can_restrict_members": self.can_restrict_members,
                "chat_type": self.chat_type,
            }
        )
        return snapshot


GroupPermissionSnapshot = GroupSnapshot


class GroupGateway(Protocol):
    async def get_group_snapshot(self, chat_id: int) -> GroupSnapshot | Mapping[str, Any] | None:
        """Return the current bot identity/permission snapshot for a group."""


class GroupService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: GroupGateway,
        owner_telegram_ids: Iterable[int],
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.owner_telegram_ids = frozenset(owner_telegram_ids)

    def _require_owner(self, operator_telegram_id: int) -> None:
        if operator_telegram_id not in self.owner_telegram_ids:
            raise GroupError("无权管理群组")

    @staticmethod
    def _coerce_snapshot(chat_id: int, value: GroupSnapshot | Mapping[str, Any]) -> GroupSnapshot:
        if isinstance(value, GroupSnapshot):
            snapshot = value
        elif isinstance(value, Mapping):
            permissions = value.get("permissions", value.get("bot_permissions", {}))
            raw_chat_type = value.get("chat_type", "")
            snapshot = GroupSnapshot(
                chat_id=int(value.get("chat_id", chat_id)),
                title=str(value.get("title", "")),
                is_bot_member=bool(value.get("is_bot_member", value.get("bot_is_member", False))),
                is_bot_administrator=bool(value.get("is_bot_administrator", value.get("bot_is_administrator", False))),
                can_restrict_members=bool(value.get("can_restrict_members", value.get("bot_can_restrict_members", False))),
                chat_type=str(getattr(raw_chat_type, "value", raw_chat_type)),
                permissions=dict(permissions or {}),
            )
        else:
            raise GroupError("群组权限快照格式无效")
        if snapshot.chat_id != chat_id:
            raise GroupError("群组权限快照与目标群组不匹配")
        return snapshot

    async def _fetch_snapshot(self, chat_id: int) -> GroupSnapshot | None:
        try:
            value = await self.gateway.get_group_snapshot(chat_id)
        except GroupError:
            raise
        except Exception as exc:
            raise GroupError("无法获取群组权限，请稍后重试") from exc
        if value is None:
            return None
        return self._coerce_snapshot(chat_id, value)

    @staticmethod
    def _is_approvable(snapshot: GroupSnapshot | None) -> bool:
        return (
            snapshot is not None
            and snapshot.chat_type == "supergroup"
            and snapshot.is_bot_member
            and snapshot.is_bot_administrator
            and snapshot.can_restrict_members
        )

    @staticmethod
    def _approval_error(snapshot: GroupSnapshot | None) -> GroupError:
        if snapshot is not None and snapshot.chat_type == "group":
            return GroupError("请先将群组升级为超级群后再启用托管")
        return GroupError("Bot 必须在超级群中担任管理员并拥有限制成员权限")

    @staticmethod
    def _snapshot_payload(snapshot: GroupSnapshot | None) -> dict[str, Any]:
        return {} if snapshot is None else snapshot.as_dict()

    @staticmethod
    def _title(title: str | None, snapshot: GroupSnapshot | None, *, fallback: str | None = None) -> str:
        value = (title or "").strip() or (snapshot.title.strip() if snapshot is not None else "") or (fallback or "").strip()
        if not value:
            raise GroupError("群组标题不能为空")
        return value

    async def discover(
        self,
        chat_id: int,
        title: str | None,
        actor_telegram_id: int | None,
        *,
        newly_added: bool = False,
        event_snapshot: GroupSnapshot | Mapping[str, Any] | None = None,
    ) -> ManagedGroup:
        """Create or refresh a managed group from a Telegram membership update."""

        snapshot = self._coerce_snapshot(chat_id, event_snapshot) if event_snapshot is not None else await self._fetch_snapshot(chat_id)
        try:
            return await self._persist_discovery(chat_id, title, actor_telegram_id, snapshot, newly_added)
        except IntegrityError:
            try:
                return await self._recover_discovery_conflict(chat_id, title, actor_telegram_id, snapshot, newly_added)
            except IntegrityError as recovery_error:
                raise GroupError("群组发现发生并发冲突，请稍后重试") from recovery_error

    async def _persist_discovery(
        self,
        chat_id: int,
        title: str | None,
        actor_telegram_id: int | None,
        snapshot: GroupSnapshot | None,
        newly_added: bool,
    ) -> ManagedGroup:
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    row = ManagedGroup(
                        chat_id=chat_id,
                        title=self._title(title, snapshot),
                        status=ManagedGroupStatus.PENDING.value,
                        discovered_by_telegram_id=actor_telegram_id,
                        bot_permissions=self._snapshot_payload(snapshot),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    await session.flush()
                    await audit(
                        session,
                        actor_telegram_id=actor_telegram_id,
                        actor_type="SYSTEM",
                        action="GROUP_DISCOVER",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"title": row.title},
                    )
                else:
                    rediscovered = self._update_discovered_row(row, title, actor_telegram_id, snapshot, newly_added, now)
                    await self._audit_discovery_transition(session, row, chat_id, actor_telegram_id, snapshot, rediscovered)
                return row

    async def _recover_discovery_conflict(
        self,
        chat_id: int,
        title: str | None,
        actor_telegram_id: int | None,
        snapshot: GroupSnapshot | None,
        newly_added: bool,
    ) -> ManagedGroup:
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    raise GroupError("群组发现发生并发冲突，请稍后重试")
                rediscovered = self._update_discovered_row(row, title, actor_telegram_id, snapshot, newly_added, now)
                await self._audit_discovery_transition(session, row, chat_id, actor_telegram_id, snapshot, rediscovered)
                return row

    def _update_discovered_row(
        self,
        row: ManagedGroup,
        title: str | None,
        actor_telegram_id: int | None,
        snapshot: GroupSnapshot | None,
        newly_added: bool,
        now: datetime,
    ) -> bool:
        row.title = self._title(title, snapshot, fallback=row.title)
        row.discovered_by_telegram_id = actor_telegram_id
        row.bot_permissions = self._snapshot_payload(snapshot)
        row.updated_at = now
        rediscovered = row.status == ManagedGroupStatus.REJECTED.value and newly_added and snapshot is not None and snapshot.is_bot_member
        if rediscovered:
            row.status = ManagedGroupStatus.PENDING.value
            row.approved_by_telegram_id = None
            row.approved_at = None
            row.disabled_at = None
            row.disable_reason = None
        return rediscovered

    async def _audit_discovery_transition(
        self,
        session: AsyncSession,
        row: ManagedGroup,
        chat_id: int,
        actor_telegram_id: int | None,
        snapshot: GroupSnapshot | None,
        rediscovered: bool,
    ) -> None:
        if rediscovered:
            await audit(
                session,
                actor_telegram_id=actor_telegram_id,
                actor_type="SYSTEM",
                action="GROUP_REDISCOVER",
                target_type="MANAGED_GROUP",
                target_id=str(chat_id),
                parameters_summary={"status": row.status},
            )
        elif row.status == ManagedGroupStatus.ACTIVE.value and not self._is_approvable(snapshot):
            self._disable_row(row, utcnow(), "bot_membership_or_permissions_lost")
            await audit(
                session,
                actor_telegram_id=None,
                actor_type="SYSTEM",
                action="GROUP_PERMISSION_LOST",
                target_type="MANAGED_GROUP",
                target_id=str(chat_id),
                parameters_summary={"reason": row.disable_reason},
            )

    @staticmethod
    def _disable_row(row: ManagedGroup, now: datetime, reason: str) -> None:
        row.status = ManagedGroupStatus.DISABLED.value
        row.disable_reason = reason
        row.disabled_at = now
        row.updated_at = now

    async def approve(self, chat_id: int, operator_telegram_id: int) -> ManagedGroup:
        self._require_owner(operator_telegram_id)
        snapshot = await self._fetch_snapshot(chat_id)
        now = utcnow()
        failure: GroupError | None = None
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    raise GroupError("群组不存在")
                if row.status != ManagedGroupStatus.PENDING.value:
                    raise GroupError("群组当前不在待审批状态")
                row.bot_permissions = self._snapshot_payload(snapshot)
                row.updated_at = now
                if not self._is_approvable(snapshot):
                    await audit(
                        session,
                        actor_telegram_id=operator_telegram_id,
                        actor_type="ADMIN",
                        action="GROUP_APPROVE",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"reason": "bot_not_admin_or_missing_restrict_permission"},
                        result="REJECTED",
                    )
                    failure = self._approval_error(snapshot)
                else:
                    row.status = ManagedGroupStatus.ACTIVE.value
                    row.approved_by_telegram_id = operator_telegram_id
                    row.approved_at = now
                    row.disabled_at = None
                    row.disable_reason = None
                    await audit(
                        session,
                        actor_telegram_id=operator_telegram_id,
                        actor_type="ADMIN",
                        action="GROUP_APPROVE",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"status": row.status},
                    )
        if failure is not None:
            raise failure
        return row

    async def reject(self, chat_id: int, operator_telegram_id: int) -> ManagedGroup:
        self._require_owner(operator_telegram_id)
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    raise GroupError("群组不存在")
                if row.status == ManagedGroupStatus.REJECTED.value:
                    return row
                if row.status != ManagedGroupStatus.PENDING.value:
                    raise GroupError("只有待审批群组可以拒绝")
                row.status = ManagedGroupStatus.REJECTED.value
                row.updated_at = now
                await audit(
                    session,
                    actor_telegram_id=operator_telegram_id,
                    actor_type="ADMIN",
                    action="GROUP_REJECT",
                    target_type="MANAGED_GROUP",
                    target_id=str(chat_id),
                    parameters_summary={"status": row.status},
                )
                return row

    async def disable(self, chat_id: int, operator_telegram_id: int, reason: str) -> ManagedGroup:
        self._require_owner(operator_telegram_id)
        reason = reason.strip()
        if not reason:
            raise GroupError("停用原因不能为空")
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    raise GroupError("群组不存在")
                if row.status == ManagedGroupStatus.DISABLED.value:
                    return row
                if row.status == ManagedGroupStatus.REJECTED.value:
                    raise GroupError("已拒绝的群组不能停用")
                self._disable_row(row, now, reason)
                await audit(
                    session,
                    actor_telegram_id=operator_telegram_id,
                    actor_type="ADMIN",
                    action="GROUP_DISABLE",
                    target_type="MANAGED_GROUP",
                    target_id=str(chat_id),
                    parameters_summary={"reason": reason},
                )
                return row

    async def re_enable(self, chat_id: int, operator_telegram_id: int) -> ManagedGroup:
        self._require_owner(operator_telegram_id)
        snapshot = await self._fetch_snapshot(chat_id)
        now = utcnow()
        failure: GroupError | None = None
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    raise GroupError("群组不存在")
                if row.status == ManagedGroupStatus.ACTIVE.value:
                    if self._is_approvable(snapshot):
                        return row
                    row.bot_permissions = self._snapshot_payload(snapshot)
                    self._disable_row(row, now, "bot_membership_or_permissions_lost")
                    await audit(
                        session,
                        actor_telegram_id=None,
                        actor_type="SYSTEM",
                        action="GROUP_PERMISSION_LOST",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"reason": row.disable_reason, "source": "re_enable"},
                    )
                    failure = self._approval_error(snapshot)
                elif row.status != ManagedGroupStatus.DISABLED.value:
                    raise GroupError("只有已停用群组可以重新启用")
                elif not self._is_approvable(snapshot):
                    row.bot_permissions = self._snapshot_payload(snapshot)
                    row.updated_at = now
                    await audit(
                        session,
                        actor_telegram_id=operator_telegram_id,
                        actor_type="ADMIN",
                        action="GROUP_REENABLE",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"reason": "bot_not_admin_or_missing_restrict_permission"},
                        result="REJECTED",
                    )
                    failure = self._approval_error(snapshot)
                else:
                    row.bot_permissions = self._snapshot_payload(snapshot)
                    row.updated_at = now
                    row.status = ManagedGroupStatus.ACTIVE.value
                    row.approved_by_telegram_id = operator_telegram_id
                    row.approved_at = now
                    row.disabled_at = None
                    row.disable_reason = None
                    await audit(
                        session,
                        actor_telegram_id=operator_telegram_id,
                        actor_type="ADMIN",
                        action="GROUP_REENABLE",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"status": row.status},
                    )
        if failure is not None:
            raise failure
        return row

    async def enable(self, chat_id: int, operator_telegram_id: int) -> ManagedGroup:
        return await self.re_enable(chat_id, operator_telegram_id)

    async def refresh(self, chat_id: int) -> ManagedGroup | None:
        snapshot = await self._fetch_snapshot(chat_id)
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
                if row is None:
                    return None
                if snapshot is not None and snapshot.title.strip():
                    row.title = snapshot.title.strip()
                row.bot_permissions = self._snapshot_payload(snapshot)
                row.updated_at = now
                if row.status == ManagedGroupStatus.ACTIVE.value and not self._is_approvable(snapshot):
                    self._disable_row(row, now, "bot_membership_or_permissions_lost")
                    await audit(
                        session,
                        actor_telegram_id=None,
                        actor_type="SYSTEM",
                        action="GROUP_PERMISSION_LOST",
                        target_type="MANAGED_GROUP",
                        target_id=str(chat_id),
                        parameters_summary={"reason": row.disable_reason},
                    )
                return row

    async def list_groups(self, status: ManagedGroupStatus | str | None = None) -> list[ManagedGroup]:
        status_value = status.value if isinstance(status, ManagedGroupStatus) else status
        async with self.session_factory() as session:
            query = select(ManagedGroup).order_by(ManagedGroup.chat_id.asc())
            if status_value is not None:
                query = query.where(ManagedGroup.status == status_value)
            return list((await session.scalars(query)).all())

    async def get_status(self, chat_id: int) -> ManagedGroup | None:
        async with self.session_factory() as session:
            return await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id))

    async def status(self, chat_id: int) -> ManagedGroup | None:
        return await self.get_status(chat_id)

    async def is_active(self, chat_id: int) -> bool:
        async with self.session_factory() as session:
            status = await session.scalar(select(ManagedGroup.status).where(ManagedGroup.chat_id == chat_id))
            return status == ManagedGroupStatus.ACTIVE.value
