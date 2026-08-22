from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.domain.enums import BindingStatus, GroupMembershipState, ManagedGroupStatus, UserStatus
from reclaude_bot.domain.errors import OnboardingError
from reclaude_bot.infrastructure.db.models import GroupMembership, ManagedGroup, User

JOIN_DEADLINE = timedelta(minutes=5)
VERIFICATION_CLAIM_LEASE = timedelta(seconds=30)
ONBOARDING_ACTION_LEASE = VERIFICATION_CLAIM_LEASE
TOKEN_PREFIX = "verify_"
TOKEN_MAX_LENGTH = 64
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
TELEGRAM_CHAT_ID_MIN = -(2**63)
TOKEN_PAYLOAD_PATTERN = re.compile(rf"^{re.escape(TOKEN_PREFIX)}(-[0-9]+)_([A-Za-z0-9_-]+)$")


@dataclass(frozen=True)
class OnboardingCandidate:
    """Read-only candidate metadata returned to the reconciliation worker."""

    chat_id: int
    telegram_user_id: int
    generation: int
    state: str
    pending_action: str | None
    deadline: datetime | None
    next_retry_at: datetime | None
    retry_count: int
    last_alerted_retry_count: int | None
    last_alerted_action: str | None
    verification_started_at: datetime | None
    has_verification_token: bool


class OnboardingAction(StrEnum):
    RESTRICT = "RESTRICT"
    VERIFICATION_NOTIFICATION = "VERIFICATION_NOTIFICATION"
    UNMUTE = "UNMUTE"
    REMOVE = "REMOVE"


class OnboardingAuditAction(StrEnum):
    JOIN = "GROUP_JOIN"
    REJOIN = "GROUP_REJOIN"
    EXEMPT = "GROUP_EXEMPT"
    ALREADY_BOUND = "GROUP_ALREADY_BOUND"
    RESTRICT_CONFIRMED = "GROUP_RESTRICT_CONFIRMED"
    RESTRICT_FAILURE = "GROUP_RESTRICT_FAILURE"
    TOKEN_ISSUED = "GROUP_TOKEN_ISSUED"
    TOKEN_VALIDATED = "GROUP_TOKEN_VALIDATED"
    LEFT = "GROUP_LEFT"
    REMOVAL_SUCCESS = "GROUP_REMOVAL_SUCCESS"
    NOTIFICATION_SUCCESS = "GROUP_NOTIFICATION_SUCCESS"
    NOTIFICATION_FAILURE = "GROUP_NOTIFICATION_FAILURE"
    UNMUTE_SUCCESS = "GROUP_UNMUTE_SUCCESS"
    UNMUTE_FAILURE = "GROUP_UNMUTE_FAILURE"
    REMOVAL_FAILURE = "GROUP_REMOVAL_FAILURE"


class OnboardingService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, join_deadline: timedelta = JOIN_DEADLINE) -> None:
        if join_deadline <= timedelta(0):
            raise ValueError("join_deadline must be positive")
        self.session_factory = session_factory
        self.join_deadline = join_deadline

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        result = value or utcnow()
        if result.tzinfo is None or result.utcoffset() is None:
            raise OnboardingError("时间必须包含时区信息")
        return result.astimezone(UTC)

    @staticmethod
    def _persisted_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _state_value(value: GroupMembershipState | str) -> str:
        return value.value if isinstance(value, GroupMembershipState) else value

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _new_token() -> str:
        token = secrets.token_urlsafe(24)
        payload = f"{TOKEN_PREFIX}{token}"
        if len(payload) > TOKEN_MAX_LENGTH or not TOKEN_PATTERN.fullmatch(token):
            raise OnboardingError("无法生成有效的验证链接")
        return token

    @staticmethod
    def _parse_token_payload(payload: str) -> tuple[int, str] | None:
        if not isinstance(payload, str) or not payload.startswith(TOKEN_PREFIX):
            raise OnboardingError("验证链接无效")
        if len(payload) > TOKEN_MAX_LENGTH:
            return None
        match = TOKEN_PAYLOAD_PATTERN.fullmatch(payload)
        if match is None:
            return None
        chat_id = int(match.group(1))
        if chat_id < TELEGRAM_CHAT_ID_MIN or chat_id > -1:
            return None
        return chat_id, match.group(2)

    @classmethod
    def _verification_attempt_hash(cls, payload: str | None, chat_id: int) -> str | None:
        if payload is None:
            return None
        try:
            parsed = cls._parse_token_payload(payload)
        except OnboardingError:
            return None
        if parsed is None or parsed[0] != chat_id:
            return None
        return cls._token_hash(parsed[1])

    @staticmethod
    def _is_bound_active(user: User | None) -> bool:
        return user is not None and user.binding_status == BindingStatus.BOUND.value and user.status == UserStatus.ACTIVE.value

    @staticmethod
    def _is_banned(user: User | None) -> bool:
        return user is not None and user.status == UserStatus.BANNED.value

    def _attempt_values(
        self,
        user: User | None,
        joined_at: datetime,
        *,
        is_telegram_admin: bool,
        is_group_owner: bool,
    ) -> tuple[GroupMembershipState, datetime | None, datetime | None]:
        if is_telegram_admin or is_group_owner:
            return GroupMembershipState.EXEMPT, None, None
        if self._is_bound_active(user):
            assert user is not None
            return GroupMembershipState.ACTIVE, None, user.bound_at
        return GroupMembershipState.RESTRICT_PENDING, joined_at + self.join_deadline, None

    async def begin_join(
        self,
        chat_id: int,
        telegram_user_id: int,
        *,
        real_join: bool,
        joined_at: datetime | None = None,
        is_telegram_admin: bool = False,
        is_group_owner: bool = False,
    ) -> GroupMembership:
        if not real_join:
            raise OnboardingError("仅允许处理真实加入事件")
        joined_time = self._aware(joined_at)
        try:
            return await self._persist_begin_join(
                chat_id,
                telegram_user_id,
                joined_time,
                is_telegram_admin=is_telegram_admin,
                is_group_owner=is_group_owner,
            )
        except IntegrityError:
            try:
                return await self._recover_join_conflict(
                    chat_id,
                    telegram_user_id,
                    joined_time,
                    is_telegram_admin=is_telegram_admin,
                    is_group_owner=is_group_owner,
                )
            except IntegrityError as recovery_error:
                raise OnboardingError("加入事件发生并发冲突，请稍后重试") from recovery_error

    async def register_join(self, *args: Any, **kwargs: Any) -> GroupMembership:
        return await self.begin_join(*args, **kwargs)

    async def _persist_begin_join(
        self,
        chat_id: int,
        telegram_user_id: int,
        joined_at: datetime,
        *,
        is_telegram_admin: bool,
        is_group_owner: bool,
    ) -> GroupMembership:
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                await self._lock_active_group(session, chat_id)
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None:
                    user = await self._user(session, telegram_user_id)
                    row = self._new_row(
                        chat_id,
                        telegram_user_id,
                        joined_at,
                        now,
                        user,
                        is_telegram_admin=is_telegram_admin,
                        is_group_owner=is_group_owner,
                    )
                    session.add(row)
                    await session.flush()
                    await self._audit_join(session, row, user, rejoin=False)
                elif row.state in {GroupMembershipState.REMOVED.value, GroupMembershipState.LEFT.value}:
                    user = await self._user(session, telegram_user_id)
                    self._reset_for_rejoin(
                        row,
                        joined_at,
                        now,
                        user,
                        is_telegram_admin=is_telegram_admin,
                        is_group_owner=is_group_owner,
                    )
                    await self._audit_join(session, row, user, rejoin=True)
                return row

    async def _recover_join_conflict(
        self,
        chat_id: int,
        telegram_user_id: int,
        joined_at: datetime,
        *,
        is_telegram_admin: bool,
        is_group_owner: bool,
    ) -> GroupMembership:
        now = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
                await self._lock_active_group(session, chat_id)
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None:
                    raise OnboardingError("加入事件发生并发冲突，请稍后重试")
                if row.state in {GroupMembershipState.REMOVED.value, GroupMembershipState.LEFT.value}:
                    user = await self._user(session, telegram_user_id)
                    self._reset_for_rejoin(
                        row,
                        joined_at,
                        now,
                        user,
                        is_telegram_admin=is_telegram_admin,
                        is_group_owner=is_group_owner,
                    )
                    await self._audit_join(session, row, user, rejoin=True)
                return row

    async def _lock_active_group(self, session: AsyncSession, chat_id: int) -> ManagedGroup:
        group = await session.scalar(select(ManagedGroup).where(ManagedGroup.chat_id == chat_id).with_for_update())
        if group is None:
            raise OnboardingError("群组不存在")
        if group.status != ManagedGroupStatus.ACTIVE.value:
            raise OnboardingError("群组尚未获批或已停用")
        return group

    @staticmethod
    async def _membership(session: AsyncSession, chat_id: int, telegram_user_id: int, *, lock: bool) -> GroupMembership | None:
        query = select(GroupMembership).where(GroupMembership.chat_id == chat_id, GroupMembership.telegram_user_id == telegram_user_id)
        if lock:
            query = query.with_for_update()
        return await session.scalar(query)

    @staticmethod
    async def _user(session: AsyncSession, telegram_user_id: int) -> User | None:
        return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())

    def _new_row(
        self,
        chat_id: int,
        telegram_user_id: int,
        joined_at: datetime,
        now: datetime,
        user: User | None,
        *,
        is_telegram_admin: bool,
        is_group_owner: bool,
    ) -> GroupMembership:
        state, deadline, bound_at = self._attempt_values(
            user,
            joined_at,
            is_telegram_admin=is_telegram_admin,
            is_group_owner=is_group_owner,
        )
        return GroupMembership(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            generation=1,
            joined_at=joined_at,
            deadline=deadline,
            state=state.value,
            bound_at=bound_at,
            retry_count=0,
            created_at=now,
            updated_at=now,
        )

    def _reset_for_rejoin(
        self,
        row: GroupMembership,
        joined_at: datetime,
        now: datetime,
        user: User | None,
        *,
        is_telegram_admin: bool,
        is_group_owner: bool,
    ) -> None:
        state, deadline, bound_at = self._attempt_values(
            user,
            joined_at,
            is_telegram_admin=is_telegram_admin,
            is_group_owner=is_group_owner,
        )
        row.generation += 1
        row.joined_at = joined_at
        row.deadline = deadline
        row.state = state.value
        row.bound_at = bound_at
        row.unmute_requested_at = None
        row.unmuted_at = None
        row.removal_requested_at = None
        row.removed_at = None
        row.verification_token_hash = None
        row.verification_started_at = None
        row.pending_action = None
        row.action_attempt_id = None
        row.retry_count = 0
        row.last_alerted_retry_count = None
        row.last_alerted_action = None
        row.last_attempt_at = None
        row.next_retry_at = None
        row.last_error = None
        row.updated_at = now

    async def _audit_join(self, session: AsyncSession, row: GroupMembership, user: User | None, *, rejoin: bool) -> None:
        if row.state == GroupMembershipState.EXEMPT.value:
            action = OnboardingAuditAction.EXEMPT
        elif row.state == GroupMembershipState.ACTIVE.value:
            action = OnboardingAuditAction.ALREADY_BOUND
        else:
            action = OnboardingAuditAction.REJOIN if rejoin else OnboardingAuditAction.JOIN
        await audit(
            session,
            actor_telegram_id=row.telegram_user_id,
            actor_type="USER",
            action=action,
            target_type="GROUP_MEMBERSHIP",
            target_id=f"{row.chat_id}:{row.telegram_user_id}",
            parameters_summary={"generation": row.generation, "state": row.state, "user_id": user.id if user else None},
        )

    async def get_membership(self, chat_id: int, telegram_user_id: int) -> GroupMembership | None:
        async with self.session_factory() as session:
            return await self._membership(session, chat_id, telegram_user_id, lock=False)

    async def is_bound_active(self, telegram_user_id: int) -> bool:
        """Return the current binding authority without exposing ORM rows."""
        async with self.session_factory() as session:
            user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
            return self._is_bound_active(user)

    async def reconcile_bound_memberships(self, *, limit: int = 100, now: datetime | None = None) -> list[GroupMembership]:
        """Repair rows left behind by a bind commit or a process restart.

        The scan only returns identifiers from the read transaction; each user's
        queue transition acquires the same membership locks as the bind path.
        """
        if limit <= 0:
            return []
        instant = self._aware(now)
        async with self.session_factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(distinct(GroupMembership.telegram_user_id))
                        .join(User, User.telegram_user_id == GroupMembership.telegram_user_id)
                        .where(
                            User.binding_status == BindingStatus.BOUND.value,
                            User.status == UserStatus.ACTIVE.value,
                            GroupMembership.state.in_(
                                [
                                    GroupMembershipState.RESTRICT_PENDING.value,
                                    GroupMembershipState.MUTED.value,
                                    GroupMembershipState.UNMUTE_PENDING.value,
                                ]
                            ),
                        )
                        .order_by(GroupMembership.telegram_user_id)
                        .limit(limit)
                    )
                ).all()
            )
        queued: list[GroupMembership] = []
        for telegram_user_id in ids:
            queued.extend(await self.queue_unmute_for_user(telegram_user_id, now=instant))
        return queued

    async def scan_candidates(self, *, now: datetime | None = None, limit: int = 100) -> list[OnboardingCandidate]:
        """Return bounded, read-only candidates for the Telegram worker."""
        if limit <= 0:
            return []
        instant = self._aware(now)
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(GroupMembership)
                        .where(
                            GroupMembership.state.in_(
                                [
                                    GroupMembershipState.RESTRICT_PENDING.value,
                                    GroupMembershipState.MUTED.value,
                                    GroupMembershipState.UNMUTE_PENDING.value,
                                    GroupMembershipState.REMOVE_PENDING.value,
                                ]
                            )
                        )
                        .order_by(GroupMembership.chat_id, GroupMembership.telegram_user_id)
                        .limit(limit * 2)
                    )
                ).all()
            )
        candidates: list[OnboardingCandidate] = []
        for row in rows:
            deadline = self._persisted_utc(row.deadline)
            retry_at = self._persisted_utc(row.next_retry_at)
            deadline_due = deadline is not None and instant >= deadline
            retry_due = (
                (retry_at is not None and instant >= retry_at)
                or (
                    retry_at is None
                    and (
                        row.pending_action is not None
                        or row.state
                        in {
                            GroupMembershipState.RESTRICT_PENDING.value,
                            GroupMembershipState.UNMUTE_PENDING.value,
                            GroupMembershipState.REMOVE_PENDING.value,
                        }
                    )
                )
            )
            notification_recovery = (
                row.state == GroupMembershipState.MUTED.value
                and row.pending_action is None
                and row.verification_started_at is None
                and row.verification_token_hash is None
                and not deadline_due
            )
            if not deadline_due and not retry_due and not notification_recovery:
                continue
            candidates.append(
                OnboardingCandidate(
                    chat_id=row.chat_id,
                    telegram_user_id=row.telegram_user_id,
                    generation=row.generation,
                    state=row.state,
                    pending_action=row.pending_action,
                    deadline=deadline,
                    next_retry_at=retry_at,
                    retry_count=row.retry_count,
                    last_alerted_retry_count=row.last_alerted_retry_count,
                    last_alerted_action=row.last_alerted_action,
                    verification_started_at=self._persisted_utc(row.verification_started_at),
                    has_verification_token=row.verification_token_hash is not None,
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    list_pending_candidates = scan_candidates
    list_candidates = scan_candidates

    async def mark_failure_alerted(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        action: str,
        retry_count: int,
    ) -> bool:
        """Persist an owner-alert marker so restarts do not duplicate alerts."""
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation or row.retry_count != retry_count:
                    return False
                if row.last_alerted_action == action and row.last_alerted_retry_count == retry_count:
                    return False
                row.last_alerted_action = action
                row.last_alerted_retry_count = retry_count
                row.updated_at = utcnow()
                return True

    @staticmethod
    def _new_action_attempt_id() -> str:
        attempt_id = secrets.token_urlsafe(24)
        if not attempt_id or len(attempt_id) > 64 or not TOKEN_PATTERN.fullmatch(attempt_id):
            raise OnboardingError("无法生成有效的限制操作标识")
        return attempt_id

    @staticmethod
    def _clear_action_retry(row: GroupMembership) -> None:
        row.pending_action = None
        row.action_attempt_id = None
        row.retry_count = 0
        row.last_attempt_at = None
        row.next_retry_at = None
        row.last_error = None
        row.last_alerted_retry_count = None
        row.last_alerted_action = None

    def _queue_unmute_locked(self, row: GroupMembership, user: User, instant: datetime) -> bool:
        """Move an eligible membership to the durable unmute queue.

        The membership row is already locked by the caller.  Keeping this helper
        synchronous makes it usable from both binding reconciliation and the
        restriction completion path without opening a second transaction.
        """
        if not self._is_bound_active(user):
            return False
        if row.state in {
            GroupMembershipState.ACTIVE.value,
            GroupMembershipState.EXEMPT.value,
            GroupMembershipState.REMOVED.value,
            GroupMembershipState.LEFT.value,
            GroupMembershipState.REMOVE_PENDING.value,
        }:
            return False
        if row.state == GroupMembershipState.UNMUTE_PENDING.value:
            if row.pending_action in {None, OnboardingAction.UNMUTE.value}:
                if row.unmute_requested_at is None:
                    row.unmute_requested_at = instant
                row.bound_at = user.bound_at
                row.updated_at = instant
                return True
            return False
        if row.state not in {GroupMembershipState.RESTRICT_PENDING.value, GroupMembershipState.MUTED.value}:
            return False

        restrict_lease = row.next_retry_at
        had_restrict_attempt = (
            row.state == GroupMembershipState.RESTRICT_PENDING.value
            and row.pending_action == OnboardingAction.RESTRICT.value
            and row.action_attempt_id is not None
        )
        row.state = GroupMembershipState.UNMUTE_PENDING.value
        row.bound_at = user.bound_at
        row.unmute_requested_at = row.unmute_requested_at or instant
        row.verification_token_hash = None
        row.verification_started_at = None
        # A restrict call may still be in flight.  Preserve its lease so an
        # unmute worker waits for that remote call to settle before claiming.
        self._clear_action_retry(row)
        if had_restrict_attempt:
            row.next_retry_at = restrict_lease
        row.updated_at = instant
        return True

    async def queue_unmute_for_user(
        self,
        telegram_user_id: int,
        *,
        now: datetime | None = None,
    ) -> list[GroupMembership]:
        """Queue every current eligible membership for a successfully bound user.

        This reconciliation is intentionally restart-safe: callers can run it
        after every successful bind and during startup.  All membership rows
        are locked in chat-id order before the binding row is read, so timeout
        removal and bind handling serialize without acquiring locks in
        opposite orders.
        """
        instant = self._aware(now)
        queued: list[GroupMembership] = []
        async with self.session_factory() as session:
            async with session.begin():
                rows = list(
                    (
                        await session.scalars(
                            select(GroupMembership)
                            .where(
                                GroupMembership.telegram_user_id == telegram_user_id,
                                GroupMembership.state.in_(
                                    [
                                        GroupMembershipState.RESTRICT_PENDING.value,
                                        GroupMembershipState.MUTED.value,
                                        GroupMembershipState.UNMUTE_PENDING.value,
                                    ]
                                ),
                            )
                            .order_by(GroupMembership.chat_id)
                            .with_for_update()
                        )
                    ).all()
                )
                user = await self._user(session, telegram_user_id) if rows else None
                if user is None:
                    return queued
                for row in rows:
                    # The candidate predicate is evaluated before locking; the
                    # state check here revalidates the locked row before any
                    # transition.
                    if row.telegram_user_id != telegram_user_id or row.state not in {
                        GroupMembershipState.RESTRICT_PENDING.value,
                        GroupMembershipState.MUTED.value,
                        GroupMembershipState.UNMUTE_PENDING.value,
                    }:
                        continue
                    if self._queue_unmute_locked(row, user, instant):
                        queued.append(row)
        return queued

    async def claim_unmute(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically claim a permission-restoration attempt."""
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                user = await self._user(session, telegram_user_id)
                if user is None or not self._is_bound_active(user):
                    return None
                if row.state in {GroupMembershipState.MUTED.value, GroupMembershipState.RESTRICT_PENDING.value}:
                    if not self._queue_unmute_locked(row, user, instant):
                        return None
                if row.state != GroupMembershipState.UNMUTE_PENDING.value:
                    return None
                if row.pending_action is None:
                    if row.action_attempt_id is not None:
                        return None
                    retry_at = self._persisted_utc(row.next_retry_at)
                    if retry_at is not None and instant < retry_at:
                        return None
                elif row.pending_action != OnboardingAction.UNMUTE.value:
                    return None
                else:
                    retry_at = self._persisted_utc(row.next_retry_at)
                    if retry_at is not None and instant < retry_at:
                        return None
                attempt_id = self._new_action_attempt_id()
                row.pending_action = OnboardingAction.UNMUTE.value
                row.action_attempt_id = attempt_id
                row.last_attempt_at = instant
                row.next_retry_at = instant + ONBOARDING_ACTION_LEASE
                row.updated_at = instant
                return attempt_id

    async def confirm_unmute(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        attempt_id: str,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                if row.state == GroupMembershipState.ACTIVE.value:
                    return row
                if (
                    row.state != GroupMembershipState.UNMUTE_PENDING.value
                    or row.pending_action != OnboardingAction.UNMUTE.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                row.state = GroupMembershipState.ACTIVE.value
                row.unmuted_at = instant
                row.deadline = None
                row.verification_token_hash = None
                row.verification_started_at = None
                self._clear_action_retry(row)
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.UNMUTE_SUCCESS,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation},
                )
                return row

    async def fail_unmute(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        error: str,
        *,
        attempt_id: str,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        retry_at = self._aware(next_retry_at) if next_retry_at is not None else instant + ONBOARDING_ACTION_LEASE
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if (
                    row is None
                    or row.generation != generation
                    or row.state != GroupMembershipState.UNMUTE_PENDING.value
                    or row.pending_action != OnboardingAction.UNMUTE.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                row.action_attempt_id = None
                row.retry_count += 1
                row.last_attempt_at = instant
                row.next_retry_at = retry_at
                row.last_error = error
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.UNMUTE_FAILURE,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation, "error": error},
                    result="FAILED",
                )
                return row

    async def claim_restriction(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation or row.state != GroupMembershipState.RESTRICT_PENDING.value:
                    return None
                user = await self._user(session, telegram_user_id)
                if user is not None and self._is_bound_active(user):
                    self._queue_unmute_locked(row, user, instant)
                    return None
                if row.pending_action is None:
                    if row.action_attempt_id is not None:
                        return None
                elif row.pending_action != OnboardingAction.RESTRICT.value:
                    return None
                else:
                    retry_at = self._persisted_utc(row.next_retry_at)
                    if retry_at is not None and instant < retry_at:
                        return None
                attempt_id = self._new_action_attempt_id()
                row.pending_action = OnboardingAction.RESTRICT.value
                row.action_attempt_id = attempt_id
                row.last_attempt_at = instant
                row.next_retry_at = instant + VERIFICATION_CLAIM_LEASE
                row.updated_at = instant
                return attempt_id

    async def confirm_restriction(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        attempt_id: str,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                if row.state == GroupMembershipState.MUTED.value:
                    return row
                if row.state != GroupMembershipState.RESTRICT_PENDING.value:
                    return None
                if (
                    row.pending_action != OnboardingAction.RESTRICT.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                user = await self._user(session, telegram_user_id)
                if user is not None and self._is_bound_active(user):
                    self._queue_unmute_locked(row, user, instant)
                else:
                    row.state = GroupMembershipState.MUTED.value
                row.pending_action = None
                row.action_attempt_id = None
                row.retry_count = 0
                row.last_attempt_at = None
                row.next_retry_at = None
                row.last_error = None
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.RESTRICT_CONFIRMED,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation},
                )
                return row

    async def fail_restriction(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        error: str,
        *,
        attempt_id: str,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        retry_at = self._aware(next_retry_at) if next_retry_at is not None else instant + VERIFICATION_CLAIM_LEASE
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if (
                    row is None
                    or row.generation != generation
                    or row.state != GroupMembershipState.RESTRICT_PENDING.value
                    or row.pending_action != OnboardingAction.RESTRICT.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                row.action_attempt_id = None
                row.retry_count += 1
                row.last_attempt_at = instant
                row.next_retry_at = retry_at
                row.last_error = error
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.RESTRICT_FAILURE,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation, "error": error},
                    result="FAILED",
                )
                return row

    async def _issue_verification_token_locked(
        self,
        session: AsyncSession,
        row: GroupMembership,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        instant: datetime,
        *,
        attempt: str,
    ) -> str:
        token = self._new_token()
        payload = f"{TOKEN_PREFIX}{chat_id}_{token}"
        if len(payload) > TOKEN_MAX_LENGTH or TOKEN_PAYLOAD_PATTERN.fullmatch(payload) is None:
            raise OnboardingError("无法生成有效的验证链接")
        row.verification_token_hash = self._token_hash(token)
        row.verification_started_at = None
        row.pending_action = OnboardingAction.VERIFICATION_NOTIFICATION.value
        row.last_attempt_at = instant
        row.next_retry_at = instant + VERIFICATION_CLAIM_LEASE
        row.updated_at = instant
        await audit(
            session,
            actor_telegram_id=None,
            actor_type="SYSTEM",
            action=OnboardingAuditAction.TOKEN_ISSUED,
            target_type="GROUP_MEMBERSHIP",
            target_id=f"{chat_id}:{telegram_user_id}",
            parameters_summary={"generation": generation, "attempt": attempt},
        )
        return payload

    async def issue_verification_token(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation or row.state != GroupMembershipState.MUTED.value:
                    return None
                deadline = self._persisted_utc(row.deadline)
                if deadline is not None and instant >= deadline:
                    return None
                if row.pending_action is not None or row.verification_token_hash is not None or row.action_attempt_id is not None:
                    return None
                return await self._issue_verification_token_locked(
                    session,
                    row,
                    chat_id,
                    telegram_user_id,
                    generation,
                    instant,
                    attempt="initial",
                )

    async def retry_verification_token(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation or row.state != GroupMembershipState.MUTED.value:
                    return None
                deadline = self._persisted_utc(row.deadline)
                if deadline is not None and instant >= deadline:
                    return None
                if row.pending_action != OnboardingAction.VERIFICATION_NOTIFICATION.value or row.action_attempt_id is not None:
                    return None
                retry_at = self._persisted_utc(row.next_retry_at)
                if retry_at is None or instant < retry_at:
                    return None
                return await self._issue_verification_token_locked(
                    session,
                    row,
                    chat_id,
                    telegram_user_id,
                    generation,
                    instant,
                    attempt="retry",
                )

    async def verify_token(
        self,
        telegram_user_id: int,
        payload: str,
        *,
        generation: int | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        parsed = self._parse_token_payload(payload)
        if parsed is None:
            return None
        chat_id, token = parsed
        candidate_hash = self._token_hash(token)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or (generation is not None and row.generation != generation):
                    return None
                if row.state != GroupMembershipState.MUTED.value or row.verification_token_hash is None:
                    return None
                deadline = self._persisted_utc(row.deadline)
                if deadline is None or instant >= deadline:
                    return None
                if not hmac.compare_digest(row.verification_token_hash, candidate_hash):
                    return None
                row.verification_token_hash = None
                row.verification_started_at = instant
                if row.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value:
                    row.pending_action = None
                    row.retry_count = 0
                    row.last_attempt_at = None
                    row.next_retry_at = None
                    row.last_error = None
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=telegram_user_id,
                    actor_type="USER",
                    action=OnboardingAuditAction.TOKEN_VALIDATED,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": row.generation},
                )
                return row

    async def mark_notification_success(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        attempt_payload: str | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if (
                    row is None
                    or row.generation != generation
                    or row.state != GroupMembershipState.MUTED.value
                    or row.pending_action != OnboardingAction.VERIFICATION_NOTIFICATION.value
                    or row.action_attempt_id is not None
                ):
                    return None
                attempt_hash = self._verification_attempt_hash(attempt_payload, chat_id)
                if attempt_hash is None or row.verification_token_hash is None or not hmac.compare_digest(row.verification_token_hash, attempt_hash):
                    return None
                row.pending_action = None
                row.retry_count = 0
                row.last_attempt_at = None
                row.next_retry_at = None
                row.last_error = None
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.NOTIFICATION_SUCCESS,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation, "action": OnboardingAction.VERIFICATION_NOTIFICATION.value},
                )
                return row

    async def mark_notification_failure(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        error: str,
        *,
        attempt_payload: str | None = None,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        retry_at = self._aware(next_retry_at) if next_retry_at is not None else instant + VERIFICATION_CLAIM_LEASE
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if (
                    row is None
                    or row.generation != generation
                    or row.state != GroupMembershipState.MUTED.value
                    or row.pending_action != OnboardingAction.VERIFICATION_NOTIFICATION.value
                    or row.action_attempt_id is not None
                ):
                    return None
                attempt_hash = self._verification_attempt_hash(attempt_payload, chat_id)
                if row.verification_token_hash is None or attempt_hash is None or not hmac.compare_digest(row.verification_token_hash, attempt_hash):
                    return None
                row.verification_token_hash = None
                row.verification_started_at = None
                row.retry_count += 1
                row.last_attempt_at = instant
                row.next_retry_at = retry_at
                row.last_error = error
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.NOTIFICATION_FAILURE,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={
                        "generation": generation,
                        "action": OnboardingAction.VERIFICATION_NOTIFICATION.value,
                        "error": error,
                    },
                    result="FAILED",
                )
                return row

    async def claim_removal(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically claim an overdue removal attempt.

        The row lock is acquired before re-reading the binding.  A concurrent
        bind therefore either queues an unmute first or observes this removal
        claim and cannot produce a second valid remote-action identity.
        """
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                if row.state not in {
                    GroupMembershipState.RESTRICT_PENDING.value,
                    GroupMembershipState.MUTED.value,
                    GroupMembershipState.REMOVE_PENDING.value,
                }:
                    return None
                deadline = self._persisted_utc(row.deadline)
                if deadline is None or instant < deadline:
                    return None

                user = await self._user(session, telegram_user_id)
                if user is not None and self._is_bound_active(user):
                    self._queue_unmute_locked(row, user, instant)
                    return None

                if row.pending_action is None:
                    if row.action_attempt_id is not None:
                        return None
                    retry_at = self._persisted_utc(row.next_retry_at)
                    if retry_at is not None and instant < retry_at:
                        return None
                elif row.pending_action == OnboardingAction.REMOVE.value:
                    retry_at = self._persisted_utc(row.next_retry_at)
                    if retry_at is not None and instant < retry_at:
                        return None
                elif row.pending_action == OnboardingAction.RESTRICT.value:
                    # A restriction claim is in flight only while its action
                    # identity is persisted.  A failed claim clears that
                    # identity but keeps its retry backoff; the deadline
                    # removal must supersede that backoff immediately.
                    if row.action_attempt_id is not None:
                        retry_at = self._persisted_utc(row.next_retry_at)
                        if retry_at is not None and instant < retry_at:
                            return None
                elif row.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value:
                    # The token hash is the notification attempt identity.  A
                    # failed notification clears it while retaining retry
                    # metadata, so only a token-backed in-flight send may
                    # retain its short claim lease.
                    if row.verification_token_hash is not None:
                        retry_at = self._persisted_utc(row.next_retry_at)
                        if retry_at is not None and instant < retry_at:
                            return None
                else:
                    return None

                replacing_remove = row.state == GroupMembershipState.REMOVE_PENDING.value and row.pending_action == OnboardingAction.REMOVE.value
                if not replacing_remove:
                    row.retry_count = 0
                    row.last_error = None
                row.state = GroupMembershipState.REMOVE_PENDING.value
                row.removal_requested_at = row.removal_requested_at or instant
                row.verification_token_hash = None
                row.verification_started_at = None
                row.pending_action = OnboardingAction.REMOVE.value
                row.action_attempt_id = self._new_action_attempt_id()
                row.last_attempt_at = instant
                row.next_retry_at = instant + ONBOARDING_ACTION_LEASE
                row.updated_at = instant
                return row.action_attempt_id

    async def fail_removal(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        error: str,
        *,
        attempt_id: str,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        retry_at = self._aware(next_retry_at) if next_retry_at is not None else instant + ONBOARDING_ACTION_LEASE
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if (
                    row is None
                    or row.generation != generation
                    or row.state != GroupMembershipState.REMOVE_PENDING.value
                    or row.pending_action != OnboardingAction.REMOVE.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                row.action_attempt_id = None
                row.retry_count += 1
                row.last_attempt_at = instant
                row.next_retry_at = retry_at
                row.last_error = error
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.REMOVAL_FAILURE,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation, "error": error},
                    result="FAILED",
                )
                return row

    async def confirm_removal(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        attempt_id: str | None = None,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                if row.state == GroupMembershipState.REMOVED.value:
                    return row
                if row.state != GroupMembershipState.REMOVE_PENDING.value:
                    return None
                if attempt_id is None:
                    # Keep compatibility with rows created by the original
                    # explicit-removal path.  New claims always carry an
                    # action identity, so this branch cannot accept a stale
                    # completion from the new worker protocol.
                    if row.action_attempt_id is not None:
                        return None
                elif (
                    row.pending_action != OnboardingAction.REMOVE.value
                    or row.action_attempt_id is None
                    or not isinstance(attempt_id, str)
                    or not hmac.compare_digest(row.action_attempt_id, attempt_id)
                ):
                    return None
                row.state = GroupMembershipState.REMOVED.value
                row.removed_at = instant
                row.deadline = None
                row.verification_token_hash = None
                row.verification_started_at = None
                self._clear_action_retry(row)
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action=OnboardingAuditAction.REMOVAL_SUCCESS,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation},
                )
                return row

    async def mark_left(
        self,
        chat_id: int,
        telegram_user_id: int,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> GroupMembership | None:
        instant = self._aware(now)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._membership(session, chat_id, telegram_user_id, lock=True)
                if row is None or row.generation != generation:
                    return None
                if row.state in {GroupMembershipState.LEFT.value, GroupMembershipState.REMOVED.value}:
                    return row
                row.state = GroupMembershipState.LEFT.value
                row.deadline = None
                row.unmute_requested_at = None
                row.unmuted_at = None
                row.removal_requested_at = None
                row.removed_at = None
                row.verification_token_hash = None
                row.verification_started_at = None
                row.pending_action = None
                row.action_attempt_id = None
                row.retry_count = 0
                row.last_attempt_at = None
                row.next_retry_at = None
                row.last_error = None
                row.updated_at = instant
                await audit(
                    session,
                    actor_telegram_id=telegram_user_id,
                    actor_type="TELEGRAM",
                    action=OnboardingAuditAction.LEFT,
                    target_type="GROUP_MEMBERSHIP",
                    target_id=f"{chat_id}:{telegram_user_id}",
                    parameters_summary={"generation": generation},
                )
                return row
