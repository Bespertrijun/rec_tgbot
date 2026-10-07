from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_context import DeviceTaskContext, OrgAccountSource, OrgAccountUsage, SingleOrgTaskService
from reclaude_bot.application.device_round_quota import apply_round_quota, round_quota_summary
from reclaude_bot.domain.enums import CycleStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import as_decimal, ensure_utc, is_last_24h, same_cycle_reset
from reclaude_bot.infrastructure.db.models import DeviceQuotaCycle, DeviceTaskScope, QuotaTask
from reclaude_bot.infrastructure.reclaude.models import MeResponse

_MAX_MONEY_VALUE = Decimal("100000000")


@dataclass(frozen=True)
class _CycleEvidence:
    account_id: str | None
    reset_at: datetime
    percent: Decimal
    source_valid: bool


@dataclass(frozen=True)
class DeviceCycleEvidence:
    account_id: str | None
    reset_at: datetime
    percent: Decimal
    source_valid: bool
    request_started_at: datetime
    received_at: datetime


class DeviceCycleService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        source: OrgAccountSource,
        org_id: int,
        *,
        max_snapshot_age_seconds: int | float = 90,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if (
            isinstance(org_id, bool)
            or not isinstance(org_id, int)
            or org_id <= 0
        ):
            raise EligibilityError("组织 ID 必须是正整数")
        if (
            isinstance(max_snapshot_age_seconds, bool)
            or not isinstance(max_snapshot_age_seconds, (int, float))
            or not math.isfinite(max_snapshot_age_seconds)
            or max_snapshot_age_seconds < 0
        ):
            raise ValueError("max_snapshot_age_seconds must be finite and non-negative")
        self.session_factory = factory
        self.source = source
        self.task_service = SingleOrgTaskService(factory, org_id)
        self.org_id = self.task_service.org_id
        self.max_snapshot_age_seconds = float(max_snapshot_age_seconds)
        self.clock = clock

    async def sync(self, task_name: str | None = None) -> DeviceQuotaCycle:
        context = await self.task_service.resolve_task(task_name)
        evidence = await self.fetch_fresh_evidence()
        request_started_at = evidence.request_started_at
        received_at = evidence.received_at

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    task = await session.get(QuotaTask, context.task_id, with_for_update=True)
                    scope = await self._locked_scope(session, context)
                    if task is None or scope.task_id != task.id:
                        raise EligibilityError("设备限额任务配置已变化")

                    latest = await session.scalar(
                        select(DeviceQuotaCycle)
                        .where(DeviceQuotaCycle.task_id == context.task_id)
                        .order_by(DeviceQuotaCycle.reset_at.desc())
                        .limit(1)
                        .with_for_update()
                    )
                    if latest is not None and latest.last_day_checked_at is not None:
                        if ensure_utc(latest.last_day_checked_at) > request_started_at:
                            return latest

                    if (
                        latest is not None
                        and same_cycle_reset(evidence.reset_at, latest.reset_at)
                    ):
                        if latest.account_id != evidence.account_id:
                            latest.estimate_blocked = True
                            latest.status = CycleStatus.NEEDS_REVIEW.value
                            latest.last_day_allow = False
                            latest.last_day_checked_at = request_started_at
                            await self._write_audit(session, context, latest, "DEVICE_CYCLE_SOURCE_MISMATCH")
                            return latest

                        if latest.weekly_percent is not None and evidence.percent < latest.weekly_percent:
                            latest.estimate_blocked = True
                        latest.weekly_percent = evidence.percent
                        latest.status = self._cycle_status(evidence)
                        latest.last_day_allow = self._last_day_allow(evidence, received_at)
                        latest.last_day_checked_at = request_started_at
                        await self._write_audit(session, context, latest, "DEVICE_CYCLE_SYNCED")
                        return latest

                    if latest is not None and ensure_utc(latest.reset_at) > received_at:
                        latest.estimate_blocked = True
                        latest.status = CycleStatus.NEEDS_REVIEW.value
                        latest.last_day_allow = False
                        latest.weekly_percent = None
                        latest.last_day_checked_at = request_started_at
                        await self._write_audit(session, context, latest, "DEVICE_CYCLE_RESET_DRIFT")
                        return latest

                    if latest is not None and evidence.reset_at <= ensure_utc(latest.reset_at):
                        raise EligibilityError("Reclaude 周期重置时间早于已保存周期")

                    started_at = evidence.reset_at - timedelta(days=7)
                    if latest is not None:
                        started_at = max(started_at, ensure_utc(latest.reset_at))
                    if started_at > received_at:
                        raise EligibilityError("Reclaude 周期开始时间晚于当前时间")

                    if latest is not None:
                        await session.execute(
                            update(DeviceQuotaCycle)
                            .where(
                                DeviceQuotaCycle.task_id == context.task_id,
                                DeviceQuotaCycle.reset_at < evidence.reset_at,
                                DeviceQuotaCycle.status != CycleStatus.EXPIRED.value,
                            )
                            .values(status=CycleStatus.EXPIRED.value)
                        )

                    cycle = DeviceQuotaCycle(
                        task_id=context.task_id,
                        started_at=started_at,
                        reset_at=evidence.reset_at,
                        created_at=received_at,
                        status=self._cycle_status(evidence),
                        account_id=evidence.account_id,
                        weekly_percent=evidence.percent,
                        last_day_allow=self._last_day_allow(evidence, received_at),
                        last_day_checked_at=request_started_at,
                    )
                    apply_round_quota(task, latest, cycle, received_at)
                    session.add(cycle)
                    await session.flush()
                    await self._write_audit(session, context, cycle, "DEVICE_CYCLE_SYNCED")
                    return cycle
        except IntegrityError:
            raise EligibilityError("设备周期同步发生并发冲突，请重新查询") from None

    async def fetch_fresh_evidence(self) -> DeviceCycleEvidence:
        """Fetch and validate live account/cycle evidence without mutating local cycles."""

        request_started_at = self._now()
        usage = await self.source.get_usage(self.org_id)
        received_at = self._now()
        evidence = self._validate_usage(usage, received_at)
        return DeviceCycleEvidence(
            account_id=evidence.account_id,
            reset_at=evidence.reset_at,
            percent=evidence.percent,
            source_valid=evidence.source_valid,
            request_started_at=request_started_at,
            received_at=received_at,
        )

    async def current(self, task_name: str | None = None) -> DeviceQuotaCycle | None:
        context = await self.task_service.resolve_task(task_name)
        now = self._now()
        async with self.session_factory() as session:
            scope = await session.scalar(
                select(DeviceTaskScope).where(DeviceTaskScope.task_id == context.task_id)
            )
            if scope is None or scope.org_id != self.org_id:
                raise EligibilityError("任务未配置到当前 Reclaude 组织")
            return await session.scalar(
                select(DeviceQuotaCycle)
                .where(
                    DeviceQuotaCycle.task_id == context.task_id,
                    DeviceQuotaCycle.started_at <= now,
                    DeviceQuotaCycle.reset_at > now,
                )
                .order_by(DeviceQuotaCycle.reset_at.desc())
                .limit(1)
            )

    async def _locked_scope(self, session: AsyncSession, context: DeviceTaskContext) -> DeviceTaskScope:
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == context.task_id).with_for_update()
        )
        if scope is None or scope.org_id != self.org_id or context.org_id != self.org_id:
            raise EligibilityError("任务未配置到当前 Reclaude 组织")
        return scope

    def _validate_usage(self, usage: OrgAccountUsage, received_at: datetime) -> _CycleEvidence:
        if not isinstance(usage, OrgAccountUsage):
            raise EligibilityError("Reclaude 用量响应无效")
        if isinstance(usage.org_id, bool) or not isinstance(usage.org_id, int) or usage.org_id != self.org_id:
            raise EligibilityError("Reclaude 用量响应组织不匹配")
        if not isinstance(usage.me, MeResponse):
            raise EligibilityError("Reclaude 用量响应无效")
        if usage.account_id is not None and (
            not isinstance(usage.account_id, str) or not usage.account_id.strip() or len(usage.account_id) > 128
        ):
            raise EligibilityError("Reclaude 账号来源无效")
        try:
            weekly = usage.me.weekly_all()
            if weekly.resets_at is None:
                raise ValueError
            reset_at = ensure_utc(weekly.resets_at)
            percent = as_decimal(weekly.percent)
            usage_updated_at = ensure_utc(usage.me.current_account.usage_updated_at)
        except (AttributeError, TypeError, ValueError, OverflowError, InvalidOperation):
            raise EligibilityError("Reclaude 周期或使用率数据无效") from None
        if not percent.is_finite() or percent < 0 or percent >= _MAX_MONEY_VALUE:
            raise EligibilityError("Reclaude 周使用率超出有效范围")
        if reset_at <= received_at:
            raise EligibilityError("Reclaude 周期重置时间已到或无效")

        age = received_at - usage_updated_at
        source_valid = (
            usage.account_id is not None
            and usage.me.current_account.status.strip().casefold() == "bound"
            and age >= timedelta(0)
        )
        return _CycleEvidence(
            account_id=usage.account_id,
            reset_at=reset_at,
            percent=percent,
            source_valid=source_valid,
        )

    @staticmethod
    def _cycle_status(evidence: _CycleEvidence | DeviceCycleEvidence) -> str:
        return CycleStatus.VERIFIED.value if evidence.source_valid else CycleStatus.NEEDS_REVIEW.value

    @staticmethod
    def _last_day_allow(evidence: _CycleEvidence | DeviceCycleEvidence, now: datetime) -> bool:
        return bool(evidence.source_valid and is_last_24h(now, evidence.reset_at) and evidence.percent < Decimal("100"))

    async def _write_audit(
        self,
        session: AsyncSession,
        context: DeviceTaskContext,
        cycle: DeviceQuotaCycle,
        action: str,
    ) -> None:
        await audit(
            session,
            actor_telegram_id=None,
            actor_type="SYSTEM",
            action=action,
            target_type="DEVICE_QUOTA_CYCLE",
            target_id=str(cycle.id),
            result=cycle.status,
            parameters_summary={"task_id": context.task_id, "org_id": self.org_id, **round_quota_summary(cycle)},
        )

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
