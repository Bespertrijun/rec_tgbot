from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceTaskScope,
    DeviceUsageSnapshot,
    User,
)
from reclaude_bot.infrastructure.reclaude.client import DeviceApiError
from reclaude_bot.infrastructure.reclaude.models import DeviceUsage

log = structlog.get_logger(__name__)

_MONEY_LIMIT = Decimal("100000000")
_MONEY_QUANTUM = Decimal("0.0000000001")
_RANGE = "all"


class _DeviceUsageGateway(Protocol):
    account_id: int | str | None

    async def device_usage(self, device_id: int, org_id: int, range: str = "all") -> DeviceUsage: ...


@dataclass(frozen=True)
class DeviceUsageCollectionResult:
    association_id: int
    device_id: int
    sample_key: str
    status: Literal["SUCCEEDED", "FAILED"]
    snapshot_id: int | None
    error_code: str | None


@dataclass(frozen=True)
class _AssociationIdentity:
    association_id: int
    user_id: int
    task_id: int
    org_id: int
    device_id: int
    started_at: datetime


@dataclass(frozen=True)
class _SnapshotFact:
    id: int
    org_id: int
    device_id: int
    range: str


class DeviceUsageCollector:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: _DeviceUsageGateway,
        org_id: int,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.session_factory = factory
        self.gateway = gateway
        self.org_id = org_id
        self.clock = clock

    async def collect(self, association_id: int, *, sample_key: str | None = None) -> DeviceUsageCollectionResult:
        self._validate_association_id(association_id)
        key = self._sample_key(sample_key)
        identity = await self._lock_and_validate_association(association_id)
        existing = await self._find_snapshot(key)
        if existing is not None:
            return self._existing_result(identity, key, existing)

        try:
            account_before = self.gateway.account_id
        except AuthenticationCircuitOpen:
            raise
        except Exception:
            return self._failed(association_id, identity.device_id, key, "usage_request_failed")

        try:
            sampled_at = ensure_utc(self.clock())
        except Exception:
            return self._failed(association_id, identity.device_id, key, "invalid_sample_time")

        request_failed = False
        usage: object | None = None
        try:
            usage = await self.gateway.device_usage(identity.device_id, self.org_id, range=_RANGE)
        except AuthenticationCircuitOpen:
            raise
        except Exception as exc:
            request_failed = True
            if (
                isinstance(exc, DeviceApiError)
                and exc.operation == "device usage query"
                and exc.attempt_count == 3
            ):
                log.warning(
                    "device_usage_get_retries_exhausted",
                    association_id=identity.association_id,
                    attempt_count=exc.attempt_count,
                    error_code="usage_request_failed",
                )
        try:
            account_after = self.gateway.account_id
        except Exception:
            return self._failed(association_id, identity.device_id, key, "usage_request_failed")

        if account_after != account_before:
            return self._failed(association_id, identity.device_id, key, "account_changed")
        if request_failed:
            return self._failed(association_id, identity.device_id, key, "usage_request_failed")

        try:
            total_usd, payload = self._validate_usage(usage)
        except Exception:
            return self._failed(association_id, identity.device_id, key, "invalid_usage_response")

        try:
            snapshot_id = await self._save_snapshot(identity, key, sampled_at, total_usd, payload)
        except IntegrityError:
            try:
                winner = await self._find_snapshot(key)
            except Exception:
                return self._failed(association_id, identity.device_id, key, "snapshot_persist_failed")
            if winner is None:
                return self._failed(association_id, identity.device_id, key, "snapshot_persist_failed")
            try:
                return self._existing_result(identity, key, winner)
            except EligibilityError:
                return self._failed(association_id, identity.device_id, key, "sample_key_conflict")
        except EligibilityError:
            return self._failed(association_id, identity.device_id, key, "association_changed")
        except Exception:
            return self._failed(association_id, identity.device_id, key, "snapshot_persist_failed")

        return DeviceUsageCollectionResult(
            association_id=association_id,
            device_id=identity.device_id,
            sample_key=key,
            status="SUCCEEDED",
            snapshot_id=snapshot_id,
            error_code=None,
        )

    async def _lock_and_validate_association(self, association_id: int) -> _AssociationIdentity:
        async with self.session_factory() as session:
            hint = await session.get(DeviceAssociation, association_id)
            if hint is None:
                raise EligibilityError("设备关联不存在")
            hint_user_id = hint.user_id
            hint_task_id = hint.task_id

        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == hint_task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == hint_user_id).with_for_update())
                association = await session.scalar(
                    select(DeviceAssociation)
                    .where(DeviceAssociation.id == association_id)
                    .with_for_update()
                )
                if scope is None or user is None or association is None:
                    raise EligibilityError("设备关联归属记录不完整")
                if association.user_id != hint_user_id or association.task_id != hint_task_id:
                    raise EligibilityError("设备关联归属在核对期间发生变化")
                if (
                    scope.org_id != self.org_id
                    or association.org_id != self.org_id
                    or association.org_id != scope.org_id
                ):
                    raise EligibilityError("设备关联不属于当前 Reclaude 组织")
                if association.state not in {"ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"}:
                    raise EligibilityError("设备关联状态尚未确认")
                if association.device_id is None:
                    raise EligibilityError("设备关联缺少已确认的设备 ID")

                auth_action = await session.scalar(
                    select(DeviceAction)
                    .where(
                        DeviceAction.association_id == association.id,
                        DeviceAction.kind == "AUTH",
                        DeviceAction.status == "SUCCEEDED",
                        DeviceAction.target_device_id == association.device_id,
                    )
                    .order_by(DeviceAction.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if auth_action is None:
                    raise EligibilityError("设备关联缺少匹配的成功授权记录")

                return _AssociationIdentity(
                    association_id=association.id,
                    user_id=association.user_id,
                    task_id=association.task_id,
                    org_id=association.org_id,
                    device_id=association.device_id,
                    started_at=ensure_utc(association.started_at),
                )

    async def _save_snapshot(
        self,
        identity: _AssociationIdentity,
        sample_key: str,
        sampled_at: datetime,
        total_usd: Decimal,
        payload: dict[str, object],
    ) -> int:
        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == identity.task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == identity.user_id).with_for_update())
                association = await session.scalar(
                    select(DeviceAssociation)
                    .where(DeviceAssociation.id == identity.association_id)
                    .with_for_update()
                )
                if (
                    scope is None
                    or user is None
                    or association is None
                    or scope.org_id != self.org_id
                    or association.user_id != identity.user_id
                    or association.task_id != identity.task_id
                    or association.org_id != identity.org_id
                    or association.org_id != scope.org_id
                    or association.device_id != identity.device_id
                    or ensure_utc(association.started_at) != identity.started_at
                    or association.state not in {"ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"}
                ):
                    raise EligibilityError("设备关联在采样期间发生变化")

                auth_action = await session.scalar(
                    select(DeviceAction)
                    .where(
                        DeviceAction.association_id == association.id,
                        DeviceAction.kind == "AUTH",
                        DeviceAction.status == "SUCCEEDED",
                        DeviceAction.target_device_id == identity.device_id,
                    )
                    .order_by(DeviceAction.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if auth_action is None:
                    raise EligibilityError("设备关联授权证据在采样期间失效")

                snapshot = DeviceUsageSnapshot(
                    org_id=self.org_id,
                    device_id=identity.device_id,
                    sample_key=sample_key,
                    range=_RANGE,
                    total_usd=total_usd,
                    sampled_at=sampled_at,
                    payload=payload,
                )
                session.add(snapshot)
                await session.flush()
                return snapshot.id

    async def _find_snapshot(self, sample_key: str) -> _SnapshotFact | None:
        async with self.session_factory() as session:
            row = await session.execute(
                select(
                    DeviceUsageSnapshot.id,
                    DeviceUsageSnapshot.org_id,
                    DeviceUsageSnapshot.device_id,
                    DeviceUsageSnapshot.range,
                ).where(DeviceUsageSnapshot.sample_key == sample_key)
            )
            fact = row.first()
            return None if fact is None else _SnapshotFact(*fact)

    def _existing_result(
        self,
        identity: _AssociationIdentity,
        sample_key: str,
        snapshot: _SnapshotFact,
    ) -> DeviceUsageCollectionResult:
        if (
            snapshot.org_id != self.org_id
            or snapshot.org_id != identity.org_id
            or snapshot.device_id != identity.device_id
            or snapshot.range != _RANGE
        ):
            raise EligibilityError("sample_key 已用于其他组织、设备或范围")
        return DeviceUsageCollectionResult(
            association_id=identity.association_id,
            device_id=identity.device_id,
            sample_key=sample_key,
            status="SUCCEEDED",
            snapshot_id=snapshot.id,
            error_code=None,
        )

    @staticmethod
    def _validate_usage(usage: object | None) -> tuple[Decimal, dict[str, object]]:
        if not isinstance(usage, DeviceUsage) or usage.range != _RANGE:
            raise ValueError("invalid usage range")
        total = usage.overview.total_usd
        if not isinstance(total, Decimal) or not total.is_finite() or total < 0 or total >= _MONEY_LIMIT:
            raise ValueError("invalid usage total")
        normalized = total.quantize(_MONEY_QUANTUM)
        if normalized != total:
            raise ValueError("usage total exceeds storage precision")
        payload = usage.model_dump(mode="json")
        if not isinstance(payload, dict):
            raise ValueError("invalid usage payload")
        return normalized, payload

    @staticmethod
    def _validate_association_id(association_id: int) -> None:
        if isinstance(association_id, bool) or not isinstance(association_id, int) or association_id <= 0:
            raise EligibilityError("设备关联 ID 必须是正整数")

    @staticmethod
    def _sample_key(sample_key: str | None) -> str:
        key = uuid4().hex if sample_key is None else sample_key
        if not isinstance(key, str) or not key.strip() or len(key) > 64:
            raise EligibilityError("sample_key 必须是 1 到 64 个字符的非空字符串")
        return key

    @staticmethod
    def _failed(
        association_id: int,
        device_id: int,
        sample_key: str,
        error_code: str,
    ) -> DeviceUsageCollectionResult:
        return DeviceUsageCollectionResult(
            association_id=association_id,
            device_id=device_id,
            sample_key=sample_key,
            status="FAILED",
            snapshot_id=None,
            error_code=error_code,
        )
