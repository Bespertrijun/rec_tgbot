from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceResampleJob,
    DeviceTaskScope,
)

log = structlog.get_logger(__name__)

SamplingCallback = Callable[[int], Awaitable[object]]
_CALLBACK_TIMEOUT_SECONDS = 35.0
_AFTER_REVOKE_DELAYS = (
    (2, 0),
    (3, 60),
    (4, 300),
    (5, 1800),
    (6, 7200),
    (7, 86400),
)
_ACTIVE_STATES = frozenset({"ACTIVE", "PENDING_REVOKE", "UNKNOWN"})
_METERING_STATES = frozenset({"ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"})
_MAX_LIMIT = 500


class _Metering(Protocol):
    async def enqueue(
        self,
        association_id: int,
        *,
        sequence: int,
        run_after: datetime | None = None,
    ) -> int: ...

    async def run_job(self, job_id: int) -> object: ...

    async def run_due(self, limit: int = 50) -> tuple[object, ...]: ...


@dataclass(frozen=True)
class _SamplingAssociation:
    association_id: int
    user_id: int
    task_id: int
    org_id: int
    device_id: int
    state: str
    started_at: datetime
    ended_at: datetime | None
    revoke_confirmed: bool


@dataclass(frozen=True)
class _JobState:
    sequence: int
    status: str
    created_at: datetime


class DeviceSamplingService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        metering: _Metering,
        org_id: int,
        *,
        clock: Callable[[], datetime] = utcnow,
        poll_seconds: int | float = 60,
        operation_timeout_seconds: int | float = 35,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.poll_seconds = self._seconds("poll_seconds", poll_seconds)
        self.operation_timeout_seconds = self._seconds("operation_timeout_seconds", operation_timeout_seconds)
        self.session_factory = factory
        self.metering = metering
        self.org_id = org_id
        self.clock = clock

    async def after_authorized(self, association_id: int) -> object:
        self._positive_id(association_id, "设备关联 ID")
        async with asyncio.timeout(self.operation_timeout_seconds):
            job_id = await self.metering.enqueue(association_id, sequence=0, run_after=self._now())
            return await self.metering.run_job(job_id)

    async def before_revoke(self, association_id: int) -> object:
        self._positive_id(association_id, "设备关联 ID")
        async with asyncio.timeout(self.operation_timeout_seconds):
            job_id = await self.metering.enqueue(association_id, sequence=1, run_after=self._now())
            return await self.metering.run_job(job_id)

    async def after_revoked(self, association_id: int) -> object:
        self._positive_id(association_id, "设备关联 ID")
        async with asyncio.timeout(self.operation_timeout_seconds):
            ended_at = await self._confirmed_ended_at(association_id)
            if ended_at is None:
                raise EligibilityError("设备关联没有已确认的撤销结果")
            immediate_job_id: int | None = None
            for sequence, delay_seconds in _AFTER_REVOKE_DELAYS:
                job_id = await self.metering.enqueue(
                    association_id,
                    sequence=sequence,
                    run_after=ended_at + timedelta(seconds=delay_seconds),
                )
                if sequence == 2:
                    immediate_job_id = job_id
            if immediate_job_id is None:
                raise EligibilityError("撤销后采样任务计划无效")
            return await self.metering.run_job(immediate_job_id)

    async def tick(self, limit: int = 50) -> tuple[object, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > _MAX_LIMIT:
            raise EligibilityError(f"limit 必须在 1 到 {_MAX_LIMIT} 之间")
        now = self._now()
        associations, jobs = await self._tick_state()
        for association in associations:
            association_jobs = jobs.get(association.association_id, ())
            sequences = {job.sequence for job in association_jobs}
            missing_initial = 0 not in sequences
            if missing_initial:
                await self._enqueue_from_tick(association.association_id, sequence=0, run_after=now)
                association_jobs = (*association_jobs, _JobState(0, "PENDING", now))

            if association.state == "ENDED":
                if association.revoke_confirmed:
                    ended_at = association.ended_at
                    if ended_at is not None:
                        for sequence, delay_seconds in _AFTER_REVOKE_DELAYS:
                            await self._enqueue_from_tick(
                                association.association_id,
                                sequence=sequence,
                                run_after=ended_at + timedelta(seconds=delay_seconds),
                            )
                continue

            if association.state not in _ACTIVE_STATES or missing_initial:
                continue
            if any(job.status == "PENDING" for job in association_jobs):
                continue
            last_created_at = max((job.created_at for job in association_jobs), default=None)
            if last_created_at is None or now - last_created_at < timedelta(seconds=self.poll_seconds):
                continue
            last_sequence = max((job.sequence for job in association_jobs), default=7)
            sequence = max(last_sequence, 7) + 1
            await self._enqueue_from_tick(association.association_id, sequence=sequence, run_after=now)

        return await self.metering.run_due(limit=limit)

    async def _confirmed_ended_at(self, association_id: int) -> datetime | None:
        async with self.session_factory() as session:
            association = await session.scalar(
                select(DeviceAssociation)
                .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceAssociation.task_id)
                .where(
                    DeviceAssociation.id == association_id,
                    DeviceAssociation.org_id == self.org_id,
                    DeviceTaskScope.org_id == self.org_id,
                    DeviceAssociation.state == "ENDED",
                    DeviceAssociation.ended_at.is_not(None),
                    DeviceAssociation.device_id.is_not(None),
                )
            )
            if association is None or association.ended_at is None:
                return None
            association_ended_at = association.ended_at
            actions = list(
                (
                    await session.scalars(
                        select(DeviceAction).where(
                            DeviceAction.association_id == association.id,
                            DeviceAction.kind == "REVOKE",
                            DeviceAction.status == "SUCCEEDED",
                            DeviceAction.target_device_id == association.device_id,
                        )
                    )
                ).all()
            )
            ended_at = ensure_utc(association_ended_at)
            if not any(
                action.completed_at is not None and ensure_utc(action.completed_at) <= ended_at
                for action in actions
            ):
                return None
            auth_id = await session.scalar(
                select(DeviceAction.id).where(
                    DeviceAction.association_id == association.id,
                    DeviceAction.kind == "AUTH",
                    DeviceAction.status == "SUCCEEDED",
                    DeviceAction.target_device_id == association.device_id,
                )
            )
            return ended_at if auth_id is not None else None

    async def _tick_state(self) -> tuple[tuple[_SamplingAssociation, ...], dict[int, tuple[_JobState, ...]]]:
        auth_evidence = exists(
            select(1).where(
                DeviceAction.association_id == DeviceAssociation.id,
                DeviceAction.kind == "AUTH",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == DeviceAssociation.device_id,
            )
        )
        revoke_evidence = exists(
            select(1).where(
                DeviceAction.association_id == DeviceAssociation.id,
                DeviceAction.kind == "REVOKE",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == DeviceAssociation.device_id,
                DeviceAction.completed_at.is_not(None),
                DeviceAction.completed_at <= DeviceAssociation.ended_at,
            )
        )
        statement = (
            select(
                DeviceAssociation.id,
                DeviceAssociation.user_id,
                DeviceAssociation.task_id,
                DeviceAssociation.org_id,
                DeviceAssociation.device_id,
                DeviceAssociation.state,
                DeviceAssociation.started_at,
                DeviceAssociation.ended_at,
                revoke_evidence.label("revoke_confirmed"),
            )
            .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceAssociation.task_id)
            .where(
                DeviceAssociation.org_id == self.org_id,
                DeviceTaskScope.org_id == self.org_id,
                DeviceAssociation.state.in_(_METERING_STATES),
                DeviceAssociation.device_id.is_not(None),
                auth_evidence,
            )
            .order_by(DeviceAssociation.id)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
            associations = tuple(
                _SamplingAssociation(
                    association_id=row.id,
                    user_id=row.user_id,
                    task_id=row.task_id,
                    org_id=row.org_id,
                    device_id=row.device_id,
                    state=row.state,
                    started_at=ensure_utc(row.started_at),
                    ended_at=ensure_utc(row.ended_at) if row.ended_at is not None else None,
                    revoke_confirmed=bool(row.revoke_confirmed),
                )
                for row in rows
            )
            association_ids = tuple(item.association_id for item in associations)
            if not association_ids:
                return associations, {}
            job_rows = (
                await session.execute(
                    select(
                        DeviceResampleJob.association_id,
                        DeviceResampleJob.sequence,
                        DeviceResampleJob.status,
                        DeviceResampleJob.created_at,
                    )
                    .where(DeviceResampleJob.association_id.in_(association_ids))
                    .order_by(DeviceResampleJob.association_id, DeviceResampleJob.sequence)
                )
            ).all()

        grouped: dict[int, list[_JobState]] = {association_id: [] for association_id in association_ids}
        for row in job_rows:
            grouped[row.association_id].append(
                _JobState(
                    sequence=row.sequence,
                    status=row.status,
                    created_at=ensure_utc(row.created_at),
                )
            )
        return associations, {association_id: tuple(rows) for association_id, rows in grouped.items()}

    async def _enqueue_from_tick(
        self,
        association_id: int,
        *,
        sequence: int,
        run_after: datetime,
    ) -> int | None:
        try:
            return await self.metering.enqueue(association_id, sequence=sequence, run_after=run_after)
        except EligibilityError:
            log.warning(
                "device_sampling_enqueue_rejected",
                association_id=association_id,
                sequence=sequence,
            )
            return None

    @staticmethod
    def _positive_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    @staticmethod
    def _seconds(name: str, value: int | float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EligibilityError(f"{name} 必须是有限的正数")
        try:
            seconds = float(value)
        except OverflowError:
            raise EligibilityError(f"{name} 必须是有限的正数") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise EligibilityError(f"{name} 必须是有限的正数")
        return seconds

    def _now(self) -> datetime:
        try:
            return ensure_utc(self.clock())
        except (TypeError, ValueError, OverflowError):
            raise EligibilityError("当前时间无效") from None


async def invoke_sampling_callback(
    callback: SamplingCallback | None,
    association_id: int,
    *,
    phase: str,
) -> None:
    if callback is None:
        return
    try:
        async with asyncio.timeout(_CALLBACK_TIMEOUT_SECONDS):
            await callback(association_id)
    except Exception:
        log.warning(
            "device_sampling_callback_failed",
            association_id=association_id,
            phase=phase,
        )
