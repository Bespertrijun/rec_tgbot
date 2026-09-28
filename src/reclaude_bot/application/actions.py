from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_context import DeviceTaskContext
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate
from reclaude_bot.config import Settings
from reclaude_bot.domain.enums import CycleStatus, QuotaRevocationStatus, TaskStatus, UserStatus
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import as_decimal, cycle_used, ensure_utc, is_last_24h
from reclaude_bot.domain.timefmt import format_beijing
from reclaude_bot.infrastructure.db.models import (
    CycleBaseline,
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceNotification,
    DeviceQuotaCycle,
    DeviceTaskScope,
    QuotaAdjustment,
    QuotaCycle,
    QuotaRevocation,
    QuotaTask,
    QuotaTaskMember,
    UpstreamMember,
    UsageNotification,
    User,
)
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway

log = structlog.get_logger(__name__)

USAGE_NOTICE_THRESHOLDS = (50, 80)


class QuotaActionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: ReclaudeGateway,
        quota: QuotaService,
        settings: Settings,
        gate: RecoveryGate | None = None,
        alert_callback: Callable[[str], Awaitable[None]] | None = None,
        user_notify_callback: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.quota = quota
        self.settings = settings
        self.gate = gate
        self.alert_callback = alert_callback
        self.user_notify_callback = user_notify_callback
        self._lock = asyncio.Lock()

    async def _writes_allowed(self) -> bool:
        if self.gate is not None and not await self.gate.is_enabled():
            return False
        return await self.any_task_enabled()

    async def any_task_enabled(self) -> bool:
        async with self.session_factory() as session:
            return bool(await session.scalar(select(func.count(QuotaTask.id)).where(QuotaTask.status == TaskStatus.RUNNING.value)))

    async def _notify(self, message: str) -> None:
        if self.alert_callback:
            try:
                await self.alert_callback(message)
            except Exception as exc:
                log.error("admin_alert_failed", error=str(exc))

    def _member_snapshot_is_fresh(self, sampled_at: datetime, now: datetime) -> bool:
        sampled = ensure_utc(sampled_at)
        moment = ensure_utc(now)
        age = moment - sampled
        return timedelta(0) <= age <= timedelta(seconds=self.settings.member_snapshot_max_age_seconds)

    async def _prepare_action(self, user_id: int, *, effective_limit: Decimal, now: datetime) -> tuple[str, str, int, str] | None:
        async with self.session_factory() as session:
            async with session.begin():
                cycle = await self.quota.current_cycle(session, now)
                if cycle is None or cycle.status != CycleStatus.VERIFIED.value:
                    return None
                user = await session.get(User, user_id, with_for_update=True)
                if user is None or user.binding_status != "BOUND" or user.status != UserStatus.ACTIVE.value or user.reclaude_user_id is None:
                    return None
                member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == user.reclaude_user_id).with_for_update())
                if member is None:
                    return None
                if not self._member_snapshot_is_fresh(member.sampled_at, now):
                    return None
                baseline = await session.scalar(
                    select(CycleBaseline).where(CycleBaseline.reclaude_user_id == user.reclaude_user_id, CycleBaseline.cycle_id == cycle.id).with_for_update()
                )
                # Baseline status is advisory: enforcement proceeds with whatever
                # baseline exists, since a missing-at-cycle-start capture must not
                # silently disable quota enforcement for the whole cycle.
                if baseline is None:
                    return None
                adjustments = (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.user_id == user.id, QuotaAdjustment.cycle_id == cycle.id))).all()
                used = cycle_used(member.total_usage_usd, baseline.baseline_total_usd, [item.amount_usd for item in adjustments])
                limit = effective_limit
                assigned = member.account_id is not None
                revocation = await session.scalar(
                    select(QuotaRevocation).where(QuotaRevocation.user_id == user.id, QuotaRevocation.cycle_id == cycle.id).with_for_update()
                )

                if revocation is not None and assigned and revocation.state in (QuotaRevocationStatus.REVOKED.value, QuotaRevocationStatus.PENDING_RESTORE.value):
                    revocation.state = QuotaRevocationStatus.RESTORED.value
                    revocation.restored_at = now
                    revocation.updated_at = now
                    revocation.last_error = None
                    await audit(
                        session,
                        actor_telegram_id=None,
                        actor_type="SYSTEM",
                        action="QUOTA_REVOCATION_RECONCILED_RESTORED",
                        target_type="USER",
                        target_id=str(user.id),
                    )
                if revocation is not None and not assigned and revocation.state == QuotaRevocationStatus.PENDING_REVOKE.value:
                    revocation.state = QuotaRevocationStatus.REVOKED.value
                    revocation.revoked_at = now
                    revocation.updated_at = now

                last_day = is_last_24h(now, cycle.reset_at)
                if last_day:
                    if cycle.last_day_allow is not True or assigned:
                        return None
                    if revocation is None or revocation.state not in (QuotaRevocationStatus.REVOKED.value, QuotaRevocationStatus.PENDING_RESTORE.value):
                        return None
                    if revocation.state == QuotaRevocationStatus.PENDING_RESTORE.value and ensure_utc(member.sampled_at) <= ensure_utc(revocation.updated_at):
                        return None
                    revocation.state = QuotaRevocationStatus.PENDING_RESTORE.value
                    revocation.updated_at = now
                    revocation.last_error = None
                    return "restore", user.reclaude_user_id, cycle.id, str(limit)

                if assigned and used >= limit:
                    if revocation is None:
                        revocation = QuotaRevocation(
                            user_id=user.id,
                            cycle_id=cycle.id,
                            state=QuotaRevocationStatus.PENDING_REVOKE.value,
                            reason="QUOTA",
                            pending_at=now,
                            updated_at=now,
                        )
                        session.add(revocation)
                    elif revocation.state == QuotaRevocationStatus.PENDING_REVOKE.value:
                        if ensure_utc(member.sampled_at) <= ensure_utc(revocation.updated_at):
                            return None
                    else:
                        revocation.state = QuotaRevocationStatus.PENDING_REVOKE.value
                        revocation.pending_at = now
                        revocation.updated_at = now
                        revocation.last_error = None
                    return "revoke", user.reclaude_user_id, cycle.id, str(limit)

                if not assigned and revocation is not None and revocation.state in (QuotaRevocationStatus.REVOKED.value, QuotaRevocationStatus.PENDING_RESTORE.value) and used < limit:
                    if revocation.state == QuotaRevocationStatus.PENDING_RESTORE.value and ensure_utc(member.sampled_at) <= ensure_utc(revocation.updated_at):
                        return None
                    revocation.state = QuotaRevocationStatus.PENDING_RESTORE.value
                    revocation.updated_at = now
                    revocation.last_error = None
                    return "restore", user.reclaude_user_id, cycle.id, str(limit)
                return None

    async def _record_usage_notice(self, user_id: int, *, effective_limit: Decimal, now: datetime) -> tuple[int, int, Decimal, Decimal, datetime] | None:
        """Persist newly crossed usage thresholds; return the highest one to notify."""
        async with self.session_factory() as session:
            async with session.begin():
                cycle = await self.quota.current_cycle(session, now)
                if cycle is None or cycle.status != CycleStatus.VERIFIED.value:
                    return None
                user = await session.get(User, user_id, with_for_update=True)
                if user is None or user.binding_status != "BOUND" or user.status != UserStatus.ACTIVE.value or user.reclaude_user_id is None:
                    return None
                member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == user.reclaude_user_id).with_for_update())
                if member is None or not self._member_snapshot_is_fresh(member.sampled_at, now):
                    return None
                baseline = await session.scalar(
                    select(CycleBaseline).where(CycleBaseline.reclaude_user_id == user.reclaude_user_id, CycleBaseline.cycle_id == cycle.id).with_for_update()
                )
                if baseline is None:
                    return None
                adjustments = (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.user_id == user.id, QuotaAdjustment.cycle_id == cycle.id))).all()
                used = cycle_used(member.total_usage_usd, baseline.baseline_total_usd, [item.amount_usd for item in adjustments])
                limit = effective_limit
                if limit <= 0 or used >= limit:
                    # 100%+ is covered by the revoke notice.
                    return None
                already = set(
                    await session.scalars(
                        select(UsageNotification.threshold_percent).where(UsageNotification.user_id == user.id, UsageNotification.cycle_id == cycle.id)
                    )
                )
                crossed = [threshold for threshold in USAGE_NOTICE_THRESHOLDS if threshold not in already and used * 100 >= limit * threshold]
                for threshold in crossed:
                    session.add(
                        UsageNotification(
                            user_id=user.id,
                            cycle_id=cycle.id,
                            threshold_percent=threshold,
                            used_usd=used,
                            limit_usd=limit,
                            created_at=now,
                        )
                    )
                if not crossed:
                    return None
                return user.telegram_user_id, max(crossed), used, limit, cycle.reset_at

    async def _execute(self, action: tuple[str, str, int, str], *, now: datetime) -> None:
        kind, reclaude_user_id, cycle_id, limit_usd = action
        if not await self._writes_allowed():
            return
        try:
            if kind == "revoke":
                await self.gateway.revoke(reclaude_user_id)
                event = "AUTO_REVOKE"
            else:
                await self.gateway.assign(reclaude_user_id)
                event = "AUTO_RESTORE"
            async with self.session_factory() as session:
                async with session.begin():
                    user = await session.scalar(select(User).where(User.reclaude_user_id == reclaude_user_id))
                    await audit(
                        session,
                        actor_telegram_id=None,
                        actor_type="SYSTEM",
                        action=event,
                        target_type="USER",
                        target_id=str(user.id) if user else reclaude_user_id,
                        parameters_summary={"cycle_id": cycle_id, "state": "PENDING_CONFIRMATION", "limit_usd": limit_usd},
                    )
        except Exception as exc:
            async with self.session_factory() as session:
                async with session.begin():
                    user = await session.scalar(select(User).where(User.reclaude_user_id == reclaude_user_id))
                    if user is not None:
                        revocation = await session.scalar(
                            select(QuotaRevocation).where(QuotaRevocation.user_id == user.id, QuotaRevocation.cycle_id == cycle_id).with_for_update()
                        )
                        if revocation is not None:
                            revocation.last_error = str(exc)
                            revocation.updated_at = now
                        await audit(
                            session,
                            actor_telegram_id=None,
                            actor_type="SYSTEM",
                            action="AUTO_REVOKE" if kind == "revoke" else "AUTO_RESTORE",
                            target_type="USER",
                            target_id=str(user.id),
                            result="PENDING_CONFIRMATION",
                            parameters_summary={"cycle_id": cycle_id, "error": str(exc), "limit_usd": limit_usd},
                        )
            await self._notify(f"{kind} {reclaude_user_id} 结果待下一次成员同步确认")
            return
        await self._notify_user(kind, reclaude_user_id, cycle_id, limit_usd)

    async def _notify_user(self, kind: str, reclaude_user_id: str, cycle_id: int, limit_usd: str) -> None:
        """Best-effort private notice to the user after a successful quota action."""
        if self.user_notify_callback is None:
            return
        try:
            async with self.session_factory() as session:
                user = await session.scalar(select(User).where(User.reclaude_user_id == reclaude_user_id))
                cycle = await session.get(QuotaCycle, cycle_id)
            if user is None:
                return
            if kind == "revoke":
                reset_at = format_beijing(cycle.reset_at) if cycle is not None else "unknown"
                text = f"本周期额度已用完（限额 ${limit_usd}），{reset_at} 刷新后会自动恢复使用。"
            else:
                text = f"本周期额度已恢复（限额 ${limit_usd}），可以继续使用了。"
            await self.user_notify_callback(user.telegram_user_id, text)
        except Exception as exc:
            log.warning("quota_action_user_notice_failed", action=kind, reclaude_user_id=reclaude_user_id, error=str(exc))

    async def _send_usage_notice(self, notice: tuple[int, int, Decimal, Decimal, datetime]) -> None:
        """Best-effort private usage-threshold reminder."""
        if self.user_notify_callback is None:
            return
        telegram_user_id, threshold, used, limit, reset_at = notice
        text = f"本周期额度已使用 {threshold}%（已用 ${used:.2f} / 限额 ${limit:.2f}），{format_beijing(reset_at)} 刷新。"
        if threshold >= 80:
            text += "达到 100% 后将暂停使用。"
        try:
            await self.user_notify_callback(telegram_user_id, text)
        except Exception as exc:
            log.warning("usage_threshold_notice_failed", telegram_user_id=telegram_user_id, threshold=threshold, error=str(exc))

    async def _running_coverage(self, session: AsyncSession) -> dict[str, Decimal]:
        """reclaude_user_id → strictest (smallest) limit across RUNNING tasks covering it."""

        tasks = list((await session.scalars(select(QuotaTask).where(QuotaTask.status == TaskStatus.RUNNING.value))).all())
        if not tasks:
            return {}
        coverage: dict[str, Decimal] = {}
        all_member_ids: set[str] | None = None
        for task in tasks:
            if task.scope_mode == "ALLOWLIST":
                ids: list[str] = list((await session.scalars(select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id))).all())
            else:
                if all_member_ids is None:
                    all_member_ids = set((await session.scalars(select(UpstreamMember.reclaude_user_id))).all())
                if task.scope_mode == "EXCLUDE":
                    excluded = set((await session.scalars(select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id))).all())
                    ids = list(all_member_ids - excluded)
                else:
                    ids = list(all_member_ids)
            limit = as_decimal(task.limit_usd)
            for reclaude_user_id in ids:
                current = coverage.get(reclaude_user_id)
                if current is None or limit < current:
                    coverage[reclaude_user_id] = limit
        return coverage

    async def reconcile_cached(self, *, now: datetime | None = None) -> int:
        moment = ensure_utc(now or utcnow())
        async with self._lock:
            if self.gate is not None and not await self.gate.is_enabled():
                return 0
            async with self.session_factory() as session:
                cycle = await self.quota.current_cycle(session, moment)
                if cycle is None:
                    return 0
                coverage = await self._running_coverage(session)
                if not coverage:
                    return 0
                rows = list(
                    (
                        await session.scalars(
                            select(User).where(
                                User.binding_status == "BOUND",
                                User.status == UserStatus.ACTIVE.value,
                                User.reclaude_user_id.in_(list(coverage)),
                            )
                        )
                    ).all()
                )
                targets = [(user.id, coverage[user.reclaude_user_id]) for user in rows if user.reclaude_user_id is not None]
            actions = 0
            for user_id, limit in targets:
                notice = await self._record_usage_notice(user_id, effective_limit=limit, now=moment)
                if notice is not None:
                    await self._send_usage_notice(notice)
                action = await self._prepare_action(user_id, effective_limit=limit, now=moment)
                if action is None:
                    continue
                actions += 1
                await self._execute(action, now=moment)
        return actions


@dataclass(frozen=True)
class _DeviceQuotaRevokeTicket:
    user_id: int
    association_id: int
    task_id: int
    cycle_id: int
    ledger_id: int
    action_id: int | None = None


@dataclass(frozen=True)
class _PendingDeviceNotice:
    notification_id: int
    attempt: int
    telegram_user_id: int
    text: str


class DeviceQuotaRevokeGuard:
    def __init__(self, quota: DeviceQuotaService, gate: RecoveryGate | None = None) -> None:
        self.quota = quota
        self.gate = gate

    async def __call__(
        self,
        session: AsyncSession,
        association: DeviceAssociation,
        cycle_id: int,
        ledger_id: int,
        now: datetime,
    ) -> bool:
        scope = await session.get(DeviceTaskScope, association.task_id)
        task = await session.get(QuotaTask, association.task_id)
        cycle = await session.scalar(
            select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
        )
        ledger = await session.scalar(
            select(DeviceCycleLedger).where(DeviceCycleLedger.id == ledger_id).with_for_update()
        )
        moment = ensure_utc(now)
        if (
            scope is None
            or scope.org_id != self.quota.org_id
            or task is None
            or task.status != TaskStatus.RUNNING.value
            or cycle is None
            or cycle.task_id != association.task_id
            or cycle.status != CycleStatus.VERIFIED.value
            or not ensure_utc(cycle.started_at) <= moment < ensure_utc(cycle.reset_at)
            or ledger is None
            or ledger.user_id != association.user_id
            or ledger.task_id != association.task_id
            or ledger.cycle_id != cycle.id
        ):
            return False
        if self.gate is not None and not await self.gate.is_enabled(session):
            return False
        if await self.quota.last_day_allowed(session, cycle, moment):
            return False
        if ledger.quota_locked_at is None:
            return False
        if ledger.quota_unlocked_at is not None:
            ledger.quota_unlocked_at = None
            ledger.updated_at = moment
        return True


class DeviceQuotaActionService:
    """Device-ledger enforcement and durable user-notification delivery."""

    _THRESHOLDS = (50, 80, 100)
    _MAX_BATCH = 500
    _RETRY_SECONDS = 60
    _SEND_TIMEOUT_SECONDS = 35

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        quota: DeviceQuotaService,
        revocation: DeviceRevocationService,
        *,
        user_notify_callback: Callable[[int, str], Awaitable[None]] | None = None,
        alert_callback: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = utcnow,
        gate: RecoveryGate | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.quota = quota
        self.revocation = revocation
        if self.revocation.quota_revoke_check is None:
            self.revocation.quota_revoke_check = DeviceQuotaRevokeGuard(quota, gate)
        self.user_notify_callback = user_notify_callback
        self.alert_callback = alert_callback
        self.clock = clock
        self.gate = gate
        self._lock = asyncio.Lock()

    async def run_once(self, *, now: datetime | None = None, limit: int = 50) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > self._MAX_BATCH:
            raise EligibilityError(f"limit 必须在 1 到 {self._MAX_BATCH} 之间")
        moment = ensure_utc(now or self.clock())
        async with self._lock:
            processed = await self.revocation.reconcile_pending(limit=limit)
            targets = await self._current_ledgers(moment, limit)
            tickets: list[_DeviceQuotaRevokeTicket] = []
            for item in targets:
                ticket = await self._process_ledger(item, moment)
                if ticket is not None:
                    tickets.append(ticket)
                processed += 1

            for ticket in tickets:
                status = "PENDING"
                try:
                    if ticket.action_id is not None:
                        status = (await self.revocation.reconcile_revoke(ticket.action_id)).status
                    else:
                        result = await self.revocation.deauth_quota(
                            ticket.user_id,
                            ticket.association_id,
                            ticket.cycle_id,
                            ticket.ledger_id,
                        )
                        status = result.status if result is not None else "NO_ACTIVE_DEVICE"
                except AuthenticationCircuitOpen:
                    await self._set_revoke_status(ticket.ledger_id, status="UNKNOWN", now=moment)
                    raise
                except EligibilityError:
                    status = "NOT_ELIGIBLE"
                except Exception:
                    status = "UNKNOWN"
                    log.warning(
                        "device_quota_revoke_failed",
                        association_id=ticket.association_id,
                        cycle_id=ticket.cycle_id,
                    )
                await self._set_revoke_status(ticket.ledger_id, status=status, now=moment)
                processed += 1

            await self._queue_reauthorization_notices(moment, limit)
            processed += await self._deliver_pending_notifications(moment, limit)
            return processed

    async def _current_ledgers(self, now: datetime, limit: int) -> list[tuple[int, int, int, int]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DeviceCycleLedger.id,
                        DeviceCycleLedger.user_id,
                        DeviceCycleLedger.task_id,
                        DeviceCycleLedger.cycle_id,
                    )
                    .join(DeviceQuotaCycle, DeviceQuotaCycle.id == DeviceCycleLedger.cycle_id)
                    .join(QuotaTask, QuotaTask.id == DeviceCycleLedger.task_id)
                    .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceCycleLedger.task_id)
                    .where(
                        DeviceTaskScope.org_id == self.quota.org_id,
                        DeviceQuotaCycle.status == CycleStatus.VERIFIED.value,
                        DeviceQuotaCycle.started_at <= now,
                        DeviceQuotaCycle.reset_at > now,
                    )
                    .order_by(DeviceCycleLedger.id)
                    .limit(limit)
                )
            ).all()
            return [(row.id, row.user_id, row.task_id, row.cycle_id) for row in rows]

    async def _process_ledger(
        self,
        target: tuple[int, int, int, int],
        now: datetime,
    ) -> _DeviceQuotaRevokeTicket | None:
        ledger_id, user_id, task_id, cycle_id = target
        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                task = await session.scalar(select(QuotaTask).where(QuotaTask.id == task_id).with_for_update())
                cycle = await session.scalar(
                    select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
                )
                ledger = await session.scalar(
                    select(DeviceCycleLedger).where(DeviceCycleLedger.id == ledger_id).with_for_update()
                )
                if (
                    scope is None
                    or scope.org_id != self.quota.org_id
                    or user is None
                    or task is None
                    or cycle is None
                    or ledger is None
                    or ledger.user_id != user.id
                    or ledger.task_id != task.id
                    or ledger.cycle_id != cycle.id
                    or cycle.task_id != task.id
                    or cycle.status != CycleStatus.VERIFIED.value
                    or not ensure_utc(cycle.started_at) <= now < ensure_utc(cycle.reset_at)
                ):
                    return None

                limit = await self.quota.effective_limit(session, user.id, cycle.id)
                used = ledger.confirmed_used_usd
                if used is None:
                    return None
                used = as_decimal(used)
                last_day = await self.quota.last_day_allowed(session, cycle, now)
                lock_reached = await self.quota.mark_quota_lock_if_reached(session, ledger, cycle, task, now)
                sticky_lock = ledger.quota_locked_at is not None

                association = await session.scalar(
                    select(DeviceAssociation)
                    .where(
                        DeviceAssociation.user_id == user.id,
                        DeviceAssociation.task_id == task.id,
                        DeviceAssociation.org_id == self.quota.org_id,
                        DeviceAssociation.ended_at.is_(None),
                    )
                    .order_by(DeviceAssociation.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                revoke_action = None
                if association is not None and association.device_id is not None:
                    revoke_action = await session.scalar(
                        select(DeviceAction)
                        .where(
                            DeviceAction.association_id == association.id,
                            DeviceAction.kind == "REVOKE",
                        )
                        .order_by(DeviceAction.id.desc())
                        .limit(1)
                        .with_for_update()
                    )

                revoke_status = "NOT_REACHED"
                ticket: _DeviceQuotaRevokeTicket | None = None
                write_enabled = self.gate is None or await self.gate.is_enabled(session)
                should_enforce_lock = write_enabled and task.status == TaskStatus.RUNNING.value and not last_day and (
                    lock_reached or sticky_lock
                )
                if used >= limit:
                    if last_day:
                        revoke_status = "EXEMPT_LAST_DAY"
                    elif task.status != TaskStatus.RUNNING.value:
                        revoke_status = "TASK_STOPPED"
                    elif association is None:
                        revoke_status = "NO_ACTIVE_DEVICE"
                    elif revoke_action is not None and revoke_action.status == "SUCCEEDED":
                        revoke_status = "SUCCEEDED"
                    elif revoke_action is not None and revoke_action.status in {"PENDING", "UNKNOWN"}:
                        revoke_status = revoke_action.status
                        if (
                            revoke_action.attempt_count == 0
                            and association.state == "PENDING_REVOKE"
                            and association.end_reason == "QUOTA"
                        ):
                            ticket = _DeviceQuotaRevokeTicket(
                                user_id=user.id,
                                association_id=association.id,
                                task_id=task.id,
                                cycle_id=cycle.id,
                                ledger_id=ledger.id,
                            )
                    elif association.state == "ACTIVE" and should_enforce_lock:
                        auth_id = await session.scalar(
                            select(DeviceAction.id).where(
                                DeviceAction.association_id == association.id,
                                DeviceAction.kind == "AUTH",
                                DeviceAction.status == "SUCCEEDED",
                                DeviceAction.target_device_id == association.device_id,
                            )
                        )
                        if auth_id is not None:
                            revoke_status = "PENDING"
                            ticket = _DeviceQuotaRevokeTicket(
                                user_id=user.id,
                                association_id=association.id,
                                task_id=task.id,
                                cycle_id=cycle.id,
                                ledger_id=ledger.id,
                            )
                        else:
                            revoke_status = "IDENTITY_UNCONFIRMED"
                elif sticky_lock and task.status == TaskStatus.RUNNING.value and not last_day:
                    revoke_status = "PENDING"
                    if association is not None and association.state == "ACTIVE":
                        ticket = _DeviceQuotaRevokeTicket(
                            user_id=user.id,
                            association_id=association.id,
                            task_id=task.id,
                            cycle_id=cycle.id,
                            ledger_id=ledger.id,
                        )

                if user.status == UserStatus.ACTIVE.value and used is not None:
                    events: list[tuple[str, int | None]] = []
                    if limit > 0:
                        if used * 100 >= limit * 50:
                            events.append(("THRESHOLD_50", 50))
                        if used * 100 >= limit * 80:
                            events.append(("THRESHOLD_80", 80))
                    if used >= limit:
                        events.append(("THRESHOLD_100", 100))
                    for event, threshold in events:
                        existing = await session.scalar(
                            select(DeviceNotification.id).where(
                                DeviceNotification.ledger_id == ledger.id,
                                DeviceNotification.event == event,
                            )
                        )
                        if existing is not None:
                            continue
                        payload: dict[str, Any] = {
                            "cycle_id": cycle.id,
                            "used_usd": str(used),
                            "effective_limit_usd": str(limit),
                            "quality": ledger.quality,
                        }
                        if threshold is not None:
                            payload["threshold_percent"] = threshold
                        if event == "THRESHOLD_100":
                            payload["revoke_status"] = revoke_status
                            payload["quota_locked"] = ledger.quota_locked_at is not None
                        session.add(
                            DeviceNotification(
                                ledger_id=ledger.id,
                                event=event,
                                status="PENDING",
                                attempt_count=0,
                                next_retry_at=None,
                                sent_at=None,
                                created_at=now,
                                updated_at=now,
                                payload=payload,
                                last_error_code=None,
                            )
                        )
                    if events:
                        await session.flush()
                        for event, _threshold in events:
                            if event == "THRESHOLD_100":
                                await audit(
                                    session,
                                    actor_telegram_id=None,
                                    actor_type="SYSTEM",
                                    action="DEVICE_QUOTA_THRESHOLD_REACHED",
                                    target_type="DEVICE_CYCLE_LEDGER",
                                    target_id=str(ledger.id),
                                    result="PENDING_REVOCATION" if revoke_status in {"PENDING", "UNKNOWN"} else revoke_status,
                                    parameters_summary={
                                        "cycle_id": cycle.id,
                                        "used_usd": str(used),
                                        "effective_limit_usd": str(limit),
                                    },
                                )
                                break
                return ticket

    async def _set_revoke_status(self, ledger_id: int, *, status: str, now: datetime) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                ledger = await session.scalar(
                    select(DeviceCycleLedger).where(DeviceCycleLedger.id == ledger_id).with_for_update()
                )
                if ledger is None:
                    return
                notice = await session.scalar(
                    select(DeviceNotification).where(
                        DeviceNotification.ledger_id == ledger_id,
                        DeviceNotification.event == "THRESHOLD_100",
                    ).with_for_update()
                )
                if notice is None or notice.status != "PENDING":
                    return
                payload = dict(notice.payload)
                payload["revoke_status"] = status
                notice.payload = payload
                notice.updated_at = now

    async def _queue_reauthorization_notices(self, now: datetime, limit: int) -> None:
        quota_revoke = exists(
            select(1).where(
                DeviceAction.association_id == DeviceAssociation.id,
                DeviceAction.kind == "REVOKE",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == DeviceAssociation.device_id,
                DeviceAction.completed_at.is_not(None),
                DeviceAction.completed_at <= DeviceAssociation.ended_at,
            )
        )
        async with self.session_factory() as session:
            candidates = (
                await session.execute(
                    select(
                        User.id.label("user_id"),
                        DeviceQuotaCycle.id.label("cycle_id"),
                        DeviceQuotaCycle.task_id.label("task_id"),
                    )
                    .select_from(DeviceAssociation)
                    .join(User, User.id == DeviceAssociation.user_id)
                    .join(DeviceQuotaCycle, DeviceQuotaCycle.task_id == DeviceAssociation.task_id)
                    .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceQuotaCycle.task_id)
                    .where(
                        DeviceTaskScope.org_id == self.quota.org_id,
                        DeviceAssociation.org_id == self.quota.org_id,
                        DeviceAssociation.state == "ENDED",
                        DeviceAssociation.end_reason == "QUOTA",
                        DeviceAssociation.ended_at.is_not(None),
                        DeviceQuotaCycle.started_at <= now,
                        DeviceQuotaCycle.reset_at > now,
                        User.binding_status == "BOUND",
                        User.status == UserStatus.ACTIVE.value,
                        quota_revoke,
                    )
                    .distinct()
                    .order_by(DeviceQuotaCycle.id, User.id)
                    .limit(limit)
                )
            ).all()

        for row in candidates:
            await self._queue_user_auth_notices(row.user_id, row.task_id, row.cycle_id, now)

    async def _queue_user_auth_notices(self, user_id: int, task_id: int, cycle_id: int, now: datetime) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                cycle = await session.scalar(
                    select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
                )
                task = await session.scalar(select(QuotaTask).where(QuotaTask.id == task_id))
                if (
                    scope is None
                    or scope.org_id != self.quota.org_id
                    or user is None
                    or user.status != UserStatus.ACTIVE.value
                    or user.binding_status != "BOUND"
                    or cycle is None
                    or task is None
                    or cycle.status != CycleStatus.VERIFIED.value
                    or not ensure_utc(cycle.started_at) <= now < ensure_utc(cycle.reset_at)
                ):
                    return
                if not await self.quota._user_in_scope(session, scope, user.id):
                    return
                active_association = await session.scalar(
                    select(DeviceAssociation.id)
                    .where(DeviceAssociation.user_id == user.id, DeviceAssociation.ended_at.is_(None))
                    .limit(1)
                )
                if active_association is not None:
                    return
                latest_association = await session.scalar(
                    select(DeviceAssociation)
                    .where(
                        DeviceAssociation.user_id == user.id,
                        DeviceAssociation.org_id == self.quota.org_id,
                    )
                    .order_by(DeviceAssociation.id.desc())
                    .limit(1)
                )
                if latest_association is None or latest_association.end_reason != "QUOTA":
                    return
                current_auth = await session.scalar(
                    select(DeviceAssociation.id)
                    .join(DeviceAction, DeviceAction.association_id == DeviceAssociation.id)
                    .where(
                        DeviceAssociation.user_id == user.id,
                        DeviceAssociation.task_id == task.id,
                        DeviceAssociation.org_id == self.quota.org_id,
                        DeviceAssociation.started_at >= cycle.started_at,
                        DeviceAssociation.started_at < cycle.reset_at,
                        DeviceAction.kind == "AUTH",
                        DeviceAction.status == "SUCCEEDED",
                        DeviceAction.target_device_id == DeviceAssociation.device_id,
                    )
                    .limit(1)
                )
                last_day_allowed = await self.quota.last_day_allowed(session, cycle, now)
                if current_auth is not None and not last_day_allowed:
                    return
                ledger = await self.quota._locked_ledger(session, user.id, cycle)
                context = DeviceTaskContext(
                    task_id=task.id,
                    name=task.name,
                    org_id=self.quota.org_id,
                    limit_usd=as_decimal(task.limit_usd),
                    enabled=task.status == TaskStatus.RUNNING.value,
                )
                try:
                    await self.quota.auth_quota_check(session, user, context, now)
                except EligibilityError:
                    return
                events: list[str] = []
                prior_quota_revoke = await session.scalar(
                    select(DeviceAssociation.id)
                    .where(
                        DeviceAssociation.user_id == user.id,
                        DeviceAssociation.task_id == task.id,
                        DeviceAssociation.org_id == self.quota.org_id,
                        DeviceAssociation.end_reason == "QUOTA",
                        DeviceAssociation.ended_at < cycle.started_at,
                    )
                    .limit(1)
                )
                if prior_quota_revoke is not None and not last_day_allowed:
                    events.append("AUTH_AVAILABLE_NEW_CYCLE")
                has_quota_revoke = await session.scalar(
                    select(DeviceAssociation.id)
                    .where(
                        DeviceAssociation.user_id == user.id,
                        DeviceAssociation.task_id == task.id,
                        DeviceAssociation.org_id == self.quota.org_id,
                        DeviceAssociation.end_reason == "QUOTA",
                        DeviceAssociation.ended_at.is_not(None),
                    )
                    .limit(1)
                )
                if has_quota_revoke is not None and last_day_allowed:
                    events.append("AUTH_AVAILABLE_LAST_DAY")
                for event in events:
                    existing = await session.scalar(
                        select(DeviceNotification.id).where(
                            DeviceNotification.ledger_id == ledger.id,
                            DeviceNotification.event == event,
                        )
                    )
                    if existing is None:
                        session.add(
                            DeviceNotification(
                                ledger_id=ledger.id,
                                event=event,
                                status="PENDING",
                                attempt_count=0,
                                next_retry_at=None,
                                sent_at=None,
                                created_at=now,
                                updated_at=now,
                                payload={"cycle_id": cycle.id, "reset_at": ensure_utc(cycle.reset_at).isoformat()},
                                last_error_code=None,
                            )
                        )

    async def _deliver_pending_notifications(self, now: datetime, limit: int) -> int:
        if self.user_notify_callback is None:
            return 0
        async with self.session_factory() as session:
            notification_ids = list(
                (
                    await session.scalars(
                        select(DeviceNotification.id)
                        .join(DeviceCycleLedger, DeviceCycleLedger.id == DeviceNotification.ledger_id)
                        .join(DeviceQuotaCycle, DeviceQuotaCycle.id == DeviceCycleLedger.cycle_id)
                        .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceCycleLedger.task_id)
                        .where(
                            DeviceTaskScope.org_id == self.quota.org_id,
                            DeviceNotification.status == "PENDING",
                            or_(DeviceNotification.next_retry_at.is_(None), DeviceNotification.next_retry_at <= now),
                        )
                        .order_by(DeviceNotification.created_at, DeviceNotification.id)
                        .limit(limit)
                    )
                ).all()
            )
        sent = 0
        for notification_id in notification_ids:
            pending = await self._claim_notification(notification_id, now)
            if pending is None:
                continue
            try:
                async with asyncio.timeout(self._SEND_TIMEOUT_SECONDS):
                    await self.user_notify_callback(pending.telegram_user_id, pending.text)
            except Exception:
                await self._finish_notification(pending.notification_id, pending.attempt, now, error=True)
                continue
            await self._finish_notification(pending.notification_id, pending.attempt, now, error=False)
            sent += 1
        return sent

    async def _claim_notification(self, notification_id: int, now: datetime) -> _PendingDeviceNotice | None:
        async with self.session_factory() as session:
            async with session.begin():
                hint = await session.execute(
                    select(
                        DeviceNotification.ledger_id,
                        DeviceCycleLedger.user_id,
                        DeviceCycleLedger.task_id,
                        DeviceCycleLedger.cycle_id,
                    )
                    .join(DeviceCycleLedger, DeviceCycleLedger.id == DeviceNotification.ledger_id)
                    .where(DeviceNotification.id == notification_id)
                )
                identity = hint.first()
                if identity is None:
                    return None
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == identity.task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == identity.user_id).with_for_update())
                cycle = await session.scalar(
                    select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == identity.cycle_id).with_for_update()
                )
                ledger = await session.scalar(
                    select(DeviceCycleLedger).where(DeviceCycleLedger.id == identity.ledger_id).with_for_update()
                )
                notice = await session.scalar(
                    select(DeviceNotification).where(DeviceNotification.id == notification_id).with_for_update()
                )
                if (
                    scope is None
                    or scope.org_id != self.quota.org_id
                    or user is None
                    or cycle is None
                    or ledger is None
                    or notice is None
                    or notice.status != "PENDING"
                    or (notice.next_retry_at is not None and ensure_utc(notice.next_retry_at) > now)
                ):
                    return None
                if user.binding_status != "BOUND" or user.status != UserStatus.ACTIVE.value:
                    notice.status = "CANCELLED"
                    notice.updated_at = now
                    notice.last_error_code = "user_not_eligible"
                    return None
                if notice.event in {"AUTH_AVAILABLE_NEW_CYCLE", "AUTH_AVAILABLE_LAST_DAY"} and not await self.quota._user_in_scope(
                    session, scope, user.id
                ):
                    notice.status = "CANCELLED"
                    notice.updated_at = now
                    notice.last_error_code = "user_out_of_scope"
                    return None
                if notice.event in {"AUTH_AVAILABLE_NEW_CYCLE", "AUTH_AVAILABLE_LAST_DAY"}:
                    task = await session.get(QuotaTask, cycle.task_id)
                    active = await session.scalar(
                        select(DeviceAssociation.id)
                        .where(DeviceAssociation.user_id == user.id, DeviceAssociation.ended_at.is_(None))
                        .limit(1)
                    )
                    current_auth = await session.scalar(
                        select(DeviceAssociation.id)
                        .join(DeviceAction, DeviceAction.association_id == DeviceAssociation.id)
                        .where(
                            DeviceAssociation.user_id == user.id,
                            DeviceAssociation.task_id == cycle.task_id,
                            DeviceAssociation.org_id == self.quota.org_id,
                            DeviceAssociation.started_at >= cycle.started_at,
                            DeviceAssociation.started_at < cycle.reset_at,
                            DeviceAction.kind == "AUTH",
                            DeviceAction.status == "SUCCEEDED",
                            DeviceAction.target_device_id == DeviceAssociation.device_id,
                        )
                        .limit(1)
                    )
                    valid = (
                        task is not None
                        and active is None
                        and (notice.event == "AUTH_AVAILABLE_LAST_DAY" or current_auth is None)
                        and cycle.status == CycleStatus.VERIFIED.value
                        and ensure_utc(cycle.started_at) <= now < ensure_utc(cycle.reset_at)
                    )
                    if valid and task is not None:
                        context = DeviceTaskContext(
                            task_id=task.id,
                            name=task.name,
                            org_id=self.quota.org_id,
                            limit_usd=as_decimal(task.limit_usd),
                            enabled=task.status == TaskStatus.RUNNING.value,
                        )
                        try:
                            await self.quota.auth_quota_check(session, user, context, now)
                        except EligibilityError:
                            valid = False
                    if notice.event == "AUTH_AVAILABLE_LAST_DAY" and not await self.quota.last_day_allowed(session, cycle, now):
                        valid = False
                    if not valid:
                        notice.status = "CANCELLED"
                        notice.updated_at = now
                        notice.last_error_code = "auth_not_available"
                        return None
                text = self._notification_text(notice, user, cycle, ledger)
                notice.attempt_count += 1
                attempt = notice.attempt_count
                notice.next_retry_at = now + timedelta(seconds=self._RETRY_SECONDS)
                notice.updated_at = now
                notice.last_error_code = None
                return _PendingDeviceNotice(
                    notification_id=notice.id,
                    attempt=attempt,
                    telegram_user_id=user.telegram_user_id,
                    text=text,
                )

    def _notification_text(
        self,
        notice: DeviceNotification,
        user: User,
        cycle: DeviceQuotaCycle,
        ledger: DeviceCycleLedger,
    ) -> str:
        payload = notice.payload if isinstance(notice.payload, dict) else {}
        if notice.event.startswith("THRESHOLD_"):
            used_value = payload.get("used_usd")
            limit_value = payload.get("effective_limit_usd")
            used = Decimal(str(used_value)) if used_value is not None else ledger.confirmed_used_usd
            limit = Decimal(str(limit_value)) if limit_value is not None else None
            if notice.event == "THRESHOLD_100":
                status = str(payload.get("revoke_status", "PENDING"))
                if status == "SUCCEEDED":
                    revoke_text = "设备撤销已确认。"
                elif status == "EXEMPT_LAST_DAY":
                    revoke_text = "当前处于最后24小时放行时段，未自动撤销设备。"
                elif status == "TASK_STOPPED":
                    revoke_text = "限额任务已停止，自动撤销未执行。"
                else:
                    revoke_text = "设备撤销状态尚未确认。"
                return f"本周期已达到有效额度（已确认用量下界 ${used if used is not None else '未知'} / 有效额度 ${limit if limit is not None else '未知'}）。{revoke_text}"
            threshold = int(payload.get("threshold_percent", 0))
            return (
                f"本周期已确认用量至少达到有效额度的 {threshold}% "
                f"（${used if used is not None else '未知'} / ${limit if limit is not None else '未知'}）。"
            )
        if notice.event == "AUTH_AVAILABLE_NEW_CYCLE":
            return f"新周期已开始，可以发送 /auth 授权链接重新授权设备。周期刷新时间：{format_beijing(cycle.reset_at)}。"
        if notice.event == "AUTH_AVAILABLE_LAST_DAY":
            return f"当前账号已满足周期最后24小时放行条件，可以发送 /auth 授权链接重新授权设备。周期刷新时间：{format_beijing(cycle.reset_at)}。"
        return "额度状态已更新。"

    async def _finish_notification(self, notification_id: int, attempt: int, now: datetime, *, error: bool) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                notice = await session.scalar(
                    select(DeviceNotification).where(DeviceNotification.id == notification_id).with_for_update()
                )
                if notice is None or notice.status != "PENDING" or notice.attempt_count != attempt:
                    return
                if error:
                    notice.next_retry_at = now + timedelta(seconds=self._RETRY_SECONDS)
                    notice.last_error_code = "telegram_delivery_failed"
                    notice.updated_at = now
                else:
                    notice.status = "SENT"
                    notice.sent_at = now
                    notice.next_retry_at = None
                    notice.last_error_code = None
                    notice.updated_at = now
