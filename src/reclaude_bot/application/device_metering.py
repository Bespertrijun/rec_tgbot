from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.device_usage import DeviceUsageCollectionResult
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceQuotaCycle,
    DeviceResampleJob,
    DeviceTaskScope,
    DeviceUsageSnapshot,
    User,
)

_ALLOWED_ASSOCIATION_STATES = frozenset({"ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"})
_CYCLE_STATUSES = frozenset({"VERIFIED", "EXPIRED"})
_MAX_DUE_LIMIT = 500
_MAX_SEQUENCE = 2_147_483_647
log = structlog.get_logger(__name__)


class _Collector(Protocol):
    async def collect(self, association_id: int, *, sample_key: str) -> DeviceUsageCollectionResult: ...


class _Ledger(Protocol):
    async def ensure(self, association_id: int, cycle_id: int) -> object: ...

    async def apply(self, association_id: int, cycle_id: int, snapshot_id: int) -> object: ...


@dataclass(frozen=True)
class DeviceMeteringResult:
    job_id: int
    status: Literal["PENDING", "COMPLETED", "CANCELLED"]
    snapshot_id: int | None
    error_code: str | None


@dataclass(frozen=True)
class _AssociationHint:
    user_id: int
    task_id: int


@dataclass(frozen=True)
class _AssociationIdentity:
    association_id: int
    user_id: int
    task_id: int
    org_id: int
    device_id: int
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class _Claim:
    job_id: int
    association_id: int
    attempt: int
    claimed_at: datetime


@dataclass(frozen=True)
class _SnapshotFact:
    id: int
    org_id: int
    device_id: int
    range: str
    sampled_at: datetime


@dataclass(frozen=True)
class _CycleFact:
    id: int
    started_at: datetime
    reset_at: datetime
    status: str


@dataclass(frozen=True)
class _CyclePlan:
    usable: tuple[_CycleFact, ...]
    unresolved: bool


class _RetryJob(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code


class DeviceMeteringService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        collector: _Collector,
        ledger: _Ledger,
        org_id: int,
        *,
        clock: Callable[[], datetime] = utcnow,
        retry_seconds: int | float = 60,
        lease_seconds: int | float = 120,
        timeout_seconds: int | float = 30,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.retry_seconds = self._seconds("retry_seconds", retry_seconds)
        self.lease_seconds = self._seconds("lease_seconds", lease_seconds)
        self.timeout_seconds = self._seconds("timeout_seconds", timeout_seconds)
        if self.timeout_seconds >= self.lease_seconds:
            raise EligibilityError("timeout_seconds 必须小于 lease_seconds")
        self.session_factory = factory
        self.collector = collector
        self.ledger = ledger
        self.org_id = org_id
        self.clock = clock

    async def enqueue(
        self,
        association_id: int,
        *,
        sequence: int,
        run_after: datetime | None = None,
    ) -> int:
        self._positive_id(association_id, "设备关联 ID")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence > _MAX_SEQUENCE
        ):
            raise EligibilityError("sequence 必须是有效的非负整数")
        now = self._now()
        due_at = self._aware_datetime(run_after) if run_after is not None else now
        hint = await self._association_hint(association_id)

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    identity = await self._locked_identity(session, association_id, hint)
                    existing = await session.scalar(
                        select(DeviceResampleJob)
                        .where(
                            DeviceResampleJob.association_id == association_id,
                            DeviceResampleJob.sequence == sequence,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        return existing.id

                    job = DeviceResampleJob(
                        association_id=identity.association_id,
                        sequence=sequence,
                        run_after=due_at,
                        status="PENDING",
                        attempt_count=0,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(job)
                    await session.flush()
                    return job.id
        except IntegrityError:
            async with self.session_factory() as session:
                existing_id = await session.scalar(
                    select(DeviceResampleJob.id).where(
                        DeviceResampleJob.association_id == association_id,
                        DeviceResampleJob.sequence == sequence,
                    )
                )
            if existing_id is not None:
                return existing_id
            raise EligibilityError("补采任务入队发生并发冲突，请重试") from None

    async def run_job(self, job_id: int) -> DeviceMeteringResult:
        self._positive_id(job_id, "补采任务 ID")
        now = self._now()
        claim, existing_result = await self._claim(job_id, now)
        if existing_result is not None:
            if existing_result.status == "COMPLETED":
                snapshot = await self._find_snapshot(job_id)
                if snapshot is not None and snapshot.range == "all":
                    return DeviceMeteringResult(
                        job_id=job_id,
                        status=existing_result.status,
                        snapshot_id=snapshot.id,
                        error_code=None,
                    )
            return existing_result
        assert claim is not None

        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._process_claim(claim)
        except AuthenticationCircuitOpen:
            await self._persist_retry(claim, "authentication_circuit_open")
            raise
        except _RetryJob as exc:
            return await self._persist_retry(claim, exc.error_code)
        except TimeoutError:
            return await self._persist_retry(claim, "metering_timeout")
        except EligibilityError:
            return await self._persist_retry(claim, "metering_rejected")
        except Exception:
            return await self._persist_retry(claim, "metering_failed")

    async def run_due(self, limit: int = 50) -> tuple[DeviceMeteringResult, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > _MAX_DUE_LIMIT:
            raise EligibilityError(f"limit 必须在 1 到 {_MAX_DUE_LIMIT} 之间")
        now = self._now()
        async with self.session_factory() as session:
            job_ids = list(
                (
                    await session.scalars(
                        select(DeviceResampleJob.id)
                        .join(DeviceAssociation, DeviceAssociation.id == DeviceResampleJob.association_id)
                        .where(
                            DeviceAssociation.org_id == self.org_id,
                            DeviceResampleJob.status == "PENDING",
                            DeviceResampleJob.run_after <= now,
                        )
                        .order_by(DeviceResampleJob.run_after, DeviceResampleJob.id)
                        .limit(limit)
                    )
                ).all()
            )

        results: list[DeviceMeteringResult] = []
        for job_id in job_ids:
            results.append(await self.run_job(job_id))
        return tuple(results)

    async def _claim(
        self,
        job_id: int,
        now: datetime,
    ) -> tuple[_Claim | None, DeviceMeteringResult | None]:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.execute(
                    select(DeviceResampleJob, DeviceAssociation.org_id)
                    .join(DeviceAssociation, DeviceAssociation.id == DeviceResampleJob.association_id)
                    .where(DeviceResampleJob.id == job_id)
                    .with_for_update(of=DeviceResampleJob)
                )
                result = row.first()
                if result is None:
                    raise EligibilityError("补采任务不存在")
                job, association_org_id = result
                if association_org_id != self.org_id:
                    raise EligibilityError("补采任务不属于当前 Reclaude 组织")
                if job.status != "PENDING":
                    return None, DeviceMeteringResult(
                        job_id=job.id,
                        status=cast(Literal["PENDING", "COMPLETED", "CANCELLED"], job.status),
                        snapshot_id=None,
                        error_code=job.last_error_code,
                    )
                if ensure_utc(job.run_after) > now:
                    return None, DeviceMeteringResult(
                        job_id=job.id,
                        status="PENDING",
                        snapshot_id=None,
                        error_code=job.last_error_code,
                    )
                job.attempt_count += 1
                job.run_after = now + timedelta(seconds=self.lease_seconds)
                job.updated_at = now
                job.last_error_code = None
                await session.flush()
                return (
                    _Claim(
                        job_id=job.id,
                        association_id=job.association_id,
                        attempt=job.attempt_count,
                        claimed_at=now,
                    ),
                    None,
                )

    async def _process_claim(self, claim: _Claim) -> DeviceMeteringResult:
        identity = await self._read_identity(claim.association_id)
        sample_key = self._sample_key(claim.job_id)
        snapshot = await self._find_snapshot(claim.job_id)
        if snapshot is not None and not self._snapshot_matches(snapshot, identity):
            raise _RetryJob("sample_key_conflict")

        if snapshot is None:
            initial_plan = await self._cycle_plan(identity, claim.claimed_at)
            if not initial_plan.usable:
                raise _RetryJob("cycle_unavailable")
            await self._ensure_cycles(identity, initial_plan.usable)
            if initial_plan.unresolved:
                raise _RetryJob("cycle_unresolved")

            collected = await self.collector.collect(identity.association_id, sample_key=sample_key)
            if collected.status != "SUCCEEDED" or collected.snapshot_id is None:
                raise _RetryJob("usage_collection_failed")
            snapshot = await self._find_snapshot(claim.job_id)
            if snapshot is None or snapshot.id != collected.snapshot_id:
                raise _RetryJob("snapshot_not_persisted")
            if not self._snapshot_matches(snapshot, identity):
                raise _RetryJob("sample_key_conflict")

        snapshot_at = ensure_utc(snapshot.sampled_at)
        plan = await self._cycle_plan(identity, snapshot_at)
        if not plan.usable:
            raise _RetryJob("cycle_unavailable" if not plan.unresolved else "cycle_unresolved")
        await self._ensure_cycles(identity, plan.usable)
        for cycle in plan.usable:
            await self.ledger.apply(identity.association_id, cycle.id, snapshot.id)
        if plan.unresolved:
            raise _RetryJob("cycle_unresolved")

        return await self._persist_complete(claim, snapshot.id)

    async def _ensure_cycles(
        self,
        identity: _AssociationIdentity,
        cycles: tuple[_CycleFact, ...],
    ) -> None:
        for cycle in cycles:
            await self.ledger.ensure(identity.association_id, cycle.id)

    async def _cycle_plan(self, identity: _AssociationIdentity, cutoff: datetime) -> _CyclePlan:
        moment = ensure_utc(cutoff)
        statement = select(
            DeviceQuotaCycle.id,
            DeviceQuotaCycle.started_at,
            DeviceQuotaCycle.reset_at,
            DeviceQuotaCycle.status,
        ).where(
            DeviceQuotaCycle.task_id == identity.task_id,
            DeviceQuotaCycle.started_at <= moment,
            DeviceQuotaCycle.reset_at > identity.started_at,
        )
        if identity.ended_at is not None:
            statement = statement.where(DeviceQuotaCycle.started_at < identity.ended_at)
        statement = statement.order_by(DeviceQuotaCycle.started_at, DeviceQuotaCycle.id)
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()

        cycles = tuple(
            _CycleFact(
                id=row.id,
                started_at=ensure_utc(row.started_at),
                reset_at=ensure_utc(row.reset_at),
                status=row.status,
            )
            for row in rows
        )
        usable = tuple(cycle for cycle in cycles if cycle.status in _CYCLE_STATUSES)
        unresolved = any(cycle.status not in _CYCLE_STATUSES for cycle in cycles)
        return _CyclePlan(usable=usable, unresolved=unresolved)

    async def _read_identity(self, association_id: int) -> _AssociationIdentity:
        async with self.session_factory() as session:
            hint = await session.get(DeviceAssociation, association_id)
            if hint is None:
                raise EligibilityError("设备关联不存在")
            return await self._validate_identity(session, association_id, _AssociationHint(hint.user_id, hint.task_id))

    async def _association_hint(self, association_id: int) -> _AssociationHint:
        async with self.session_factory() as session:
            association = await session.get(DeviceAssociation, association_id)
            if association is None:
                raise EligibilityError("设备关联不存在")
            return _AssociationHint(user_id=association.user_id, task_id=association.task_id)

    async def _locked_identity(
        self,
        session: AsyncSession,
        association_id: int,
        hint: _AssociationHint,
    ) -> _AssociationIdentity:
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == hint.task_id).with_for_update()
        )
        user = await session.scalar(select(User).where(User.id == hint.user_id).with_for_update())
        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == association_id).with_for_update()
        )
        actions = await self._actions(session, association_id, lock=True)
        return self._identity_from_rows(association_id, hint, scope, user, association, actions)

    async def _validate_identity(
        self,
        session: AsyncSession,
        association_id: int,
        hint: _AssociationHint,
    ) -> _AssociationIdentity:
        scope = await session.scalar(select(DeviceTaskScope).where(DeviceTaskScope.task_id == hint.task_id))
        user = await session.scalar(select(User).where(User.id == hint.user_id))
        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == association_id)
        )
        actions = await self._actions(session, association_id, lock=False)
        return self._identity_from_rows(association_id, hint, scope, user, association, actions)

    async def _actions(
        self,
        session: AsyncSession,
        association_id: int,
        *,
        lock: bool,
    ) -> tuple[DeviceAction, ...]:
        statement = (
            select(DeviceAction)
            .where(DeviceAction.association_id == association_id)
            .order_by(DeviceAction.id)
        )
        if lock:
            statement = statement.with_for_update()
        rows = await session.scalars(statement)
        return tuple(rows.all())

    def _identity_from_rows(
        self,
        association_id: int,
        hint: _AssociationHint,
        scope: DeviceTaskScope | None,
        user: User | None,
        association: DeviceAssociation | None,
        actions: tuple[DeviceAction, ...],
    ) -> _AssociationIdentity:
        if scope is None or user is None or association is None:
            raise EligibilityError("设备关联归属记录不完整")
        if association.user_id != hint.user_id or association.task_id != hint.task_id:
            raise EligibilityError("设备关联归属在核对期间发生变化")
        if (
            scope.org_id != self.org_id
            or association.org_id != self.org_id
            or association.org_id != scope.org_id
        ):
            raise EligibilityError("设备关联不属于当前 Reclaude 组织")
        if association.state not in _ALLOWED_ASSOCIATION_STATES or association.device_id is None:
            raise EligibilityError("设备关联没有已确认的设备目标")
        if not any(
            action.kind == "AUTH"
            and action.status == "SUCCEEDED"
            and action.target_device_id == association.device_id
            for action in actions
        ):
            raise EligibilityError("设备关联缺少匹配的成功授权记录")
        return _AssociationIdentity(
            association_id=association.id,
            user_id=association.user_id,
            task_id=association.task_id,
            org_id=association.org_id,
            device_id=association.device_id,
            started_at=ensure_utc(association.started_at),
            ended_at=ensure_utc(association.ended_at) if association.ended_at is not None else None,
        )

    async def _find_snapshot(self, job_id: int) -> _SnapshotFact | None:
        sample_key = self._sample_key(job_id)
        async with self.session_factory() as session:
            row = await session.execute(
                select(
                    DeviceUsageSnapshot.id,
                    DeviceUsageSnapshot.org_id,
                    DeviceUsageSnapshot.device_id,
                    DeviceUsageSnapshot.range,
                    DeviceUsageSnapshot.sampled_at,
                ).where(DeviceUsageSnapshot.sample_key == sample_key)
            )
            fact = row.first()
            if fact is None:
                return None
            return _SnapshotFact(
                id=fact.id,
                org_id=fact.org_id,
                device_id=fact.device_id,
                range=fact.range,
                sampled_at=ensure_utc(fact.sampled_at),
            )

    def _snapshot_matches(self, snapshot: _SnapshotFact, identity: _AssociationIdentity) -> bool:
        return (
            snapshot.org_id == self.org_id
            and snapshot.org_id == identity.org_id
            and snapshot.device_id == identity.device_id
            and snapshot.range == "all"
        )

    async def _persist_complete(self, claim: _Claim, snapshot_id: int) -> DeviceMeteringResult:
        now = self._now()
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.execute(
                    select(DeviceResampleJob, DeviceAssociation.org_id)
                    .join(DeviceAssociation, DeviceAssociation.id == DeviceResampleJob.association_id)
                    .where(DeviceResampleJob.id == claim.job_id)
                    .with_for_update(of=DeviceResampleJob)
                )
                found = row.first()
                if found is None or found[1] != self.org_id:
                    return DeviceMeteringResult(
                        claim.job_id, "PENDING", snapshot_id, "identity_changed"
                    )
                job = found[0]
                if job.status != "PENDING" or job.attempt_count != claim.attempt:
                    status = cast(Literal["PENDING", "COMPLETED", "CANCELLED"], job.status)
                    return DeviceMeteringResult(
                        claim.job_id,
                        status,
                        snapshot_id,
                        None if status == "COMPLETED" else "lease_lost",
                    )
                job.status = "COMPLETED"
                job.completed_at = now
                job.last_error_code = None
                job.updated_at = now
                await session.flush()
                return DeviceMeteringResult(claim.job_id, "COMPLETED", snapshot_id, None)

    async def _persist_retry(self, claim: _Claim, error_code: str) -> DeviceMeteringResult:
        now = self._now()
        safe_code = error_code[:80]
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.execute(
                    select(DeviceResampleJob, DeviceAssociation.org_id)
                    .join(DeviceAssociation, DeviceAssociation.id == DeviceResampleJob.association_id)
                    .where(DeviceResampleJob.id == claim.job_id)
                    .with_for_update(of=DeviceResampleJob)
                )
                found = row.first()
                if found is None or found[1] != self.org_id:
                    return DeviceMeteringResult(claim.job_id, "PENDING", None, "identity_changed")
                job = found[0]
                if job.status != "PENDING" or job.attempt_count != claim.attempt:
                    return DeviceMeteringResult(
                        claim.job_id,
                        cast(Literal["PENDING", "COMPLETED", "CANCELLED"], job.status),
                        None,
                        None if job.status != "PENDING" else "lease_lost",
                    )
                job.run_after = now + timedelta(seconds=self.retry_seconds)
                job.last_error_code = safe_code
                job.updated_at = now
                return DeviceMeteringResult(claim.job_id, "PENDING", None, safe_code)

    @staticmethod
    def _sample_key(job_id: int) -> str:
        return f"device-job-{job_id}"

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

    @staticmethod
    def _aware_datetime(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise EligibilityError("run_after 必须是带时区的时间")
        return ensure_utc(value)

    def _now(self) -> datetime:
        try:
            return ensure_utc(self.clock())
        except (TypeError, ValueError, OverflowError):
            raise EligibilityError("当前时间无效") from None
