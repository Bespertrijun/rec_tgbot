from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryGate
from reclaude_bot.config import Settings
from reclaude_bot.domain.enums import CycleStatus, QuotaRevocationStatus, TaskStatus, UserStatus
from reclaude_bot.domain.quota import as_decimal, cycle_used, ensure_utc, is_last_24h
from reclaude_bot.infrastructure.db.models import CycleBaseline, QuotaAdjustment, QuotaRevocation, QuotaTask, QuotaTaskMember, UpstreamMember, User
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway

log = structlog.get_logger(__name__)


class QuotaActionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: ReclaudeGateway,
        quota: QuotaService,
        settings: Settings,
        gate: RecoveryGate | None = None,
        alert_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.quota = quota
        self.settings = settings
        self.gate = gate
        self.alert_callback = alert_callback
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
                if user is None or user.binding_status != "BOUND" or user.status != UserStatus.ACTIVE.value:
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
                targets = [(user.id, coverage[user.reclaude_user_id]) for user in rows]
            actions = 0
            for user_id, limit in targets:
                action = await self._prepare_action(user_id, effective_limit=limit, now=moment)
                if action is None:
                    continue
                actions += 1
                await self._execute(action, now=moment)
            return actions
