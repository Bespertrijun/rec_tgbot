from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.account_usage_refresh import AccountUsageRefreshService
from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.device_context import OrgAccountSource, OrgAccountUsage
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc, estimate_window_total, same_cycle_reset
from reclaude_bot.infrastructure.db.models import (
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceTaskScope,
    ServiceState,
)

_MONEY_LIMIT = Decimal("100000000")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class DeviceAccountUsage:
    """Account windows plus an estimate from known device-ledger spend."""

    email_masked: str
    usage_updated_at: datetime
    five_hour_utilization: Decimal | None
    five_hour_resets_at: datetime | None
    seven_day_utilization: Decimal | None
    seven_day_resets_at: datetime | None
    managed_used_usd: Decimal | None
    estimated_total_usd: Decimal | None
    estimate_reason: str | None
    account_status: str | None = None
    account_id: str | None = None
    cycle_id: int | None = None
    cycle_started_at: datetime | None = None
    cycle_reset_at: datetime | None = None

    @property
    def seven_day_estimated_total(self) -> Decimal | None:
        """Compatibility name used by the legacy account summary formatter."""

        return self.estimated_total_usd

    @property
    def managed_device_used_usd(self) -> Decimal | None:
        return self.managed_used_usd


@dataclass(frozen=True)
class _CycleIdentity:
    id: int
    started_at: datetime
    reset_at: datetime
    status: str
    account_id: str | None


@dataclass(frozen=True)
class _SourceFact:
    account_id: str | None
    account_status: str
    email_masked: str
    usage_updated_at: datetime
    five_hour_utilization: Decimal | None
    five_hour_resets_at: datetime | None
    seven_day_utilization: Decimal | None
    seven_day_resets_at: datetime
    weekly_reset_at: datetime
    source_reason: str | None


@dataclass(frozen=True)
class _LocalFact:
    cycle: _CycleIdentity | None
    managed_used_usd: Decimal | None
    selected_account_id: str | None
    cycle_changed: bool


class DeviceAccountUsageService:
    """Read one account snapshot and estimate known spend for one local cycle."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        source: OrgAccountSource,
        org_id: int,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_snapshot_age_seconds: int | float | None = None,
        refresh: AccountUsageRefreshService | None = None,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        # Retain the first draft's keyword for callers while intentionally applying
        # no age cutoff to known ledger spend.
        del max_snapshot_age_seconds
        self.session_factory = factory
        self.source = source
        self.org_id = org_id
        self.clock = clock
        self.refresh = refresh

    async def get_account_usage(
        self,
        task_id: int,
        *,
        now: datetime | None = None,
    ) -> DeviceAccountUsage:
        self._positive_id(task_id, "任务 ID")
        request_started_at = self._now(now)
        before = await self._current_cycle(task_id, request_started_at)

        if self.refresh is not None:
            await self.refresh.refresh_if_due(now=request_started_at)
        usage = await self.source.get_usage(self.org_id)
        response_received_at = self._now()
        source = self._source_fact(usage, response_received_at)
        local = await self._read_local(task_id, response_received_at, before)

        reason: str | None = None
        if local.cycle_changed:
            reason = "账号或周期变化"
        elif local.cycle is None:
            reason = "无有效设备周期"
        elif local.cycle.status != "VERIFIED":
            reason = "账号周期未核实"
        elif source.source_reason is not None:
            reason = source.source_reason
        elif local.selected_account_id is None or source.account_id != local.selected_account_id:
            reason = "账号或周期变化"
        elif local.cycle.account_id is None or source.account_id != local.cycle.account_id:
            reason = "账号或周期变化"
        elif not same_cycle_reset(source.weekly_reset_at, local.cycle.reset_at):
            reason = "账号或周期变化"
        elif local.managed_used_usd is None:
            reason = "无可用消费金额"
        elif local.managed_used_usd <= _ZERO:
            reason = "本周期托管设备消费为零"
        elif source.seven_day_utilization is None or source.seven_day_utilization <= _ZERO:
            reason = "7天使用率无效"

        estimated = None
        if reason is None:
            estimated = estimate_window_total(local.managed_used_usd, source.seven_day_utilization)
            if estimated is None or not estimated.is_finite() or estimated < _ZERO:
                reason = "预估数据无效"

        return DeviceAccountUsage(
            email_masked=source.email_masked,
            usage_updated_at=source.usage_updated_at,
            five_hour_utilization=source.five_hour_utilization,
            five_hour_resets_at=source.five_hour_resets_at,
            seven_day_utilization=source.seven_day_utilization,
            seven_day_resets_at=source.seven_day_resets_at,
            managed_used_usd=local.managed_used_usd,
            estimated_total_usd=estimated,
            estimate_reason=reason,
            account_status=source.account_status,
            account_id=source.account_id,
            cycle_id=local.cycle.id if local.cycle is not None else None,
            cycle_started_at=local.cycle.started_at if local.cycle is not None else None,
            cycle_reset_at=local.cycle.reset_at if local.cycle is not None else None,
        )

    async def get(self, task_id: int, *, now: datetime | None = None) -> DeviceAccountUsage:
        return await self.get_account_usage(task_id, now=now)

    async def _current_cycle(self, task_id: int, moment: datetime) -> _CycleIdentity | None:
        async with self.session_factory() as session:
            scope = await session.scalar(
                select(DeviceTaskScope).where(
                    DeviceTaskScope.task_id == task_id,
                    DeviceTaskScope.org_id == self.org_id,
                )
            )
            if scope is None:
                raise EligibilityError("任务未配置到当前 Reclaude 组织")
            cycle = await self._current_cycle_row(session, task_id, moment)
            return self._cycle_identity(cycle)

    async def _read_local(
        self,
        task_id: int,
        moment: datetime,
        expected: _CycleIdentity | None,
    ) -> _LocalFact:
        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(
                        DeviceTaskScope.task_id == task_id,
                        DeviceTaskScope.org_id == self.org_id,
                    )
                )
                if scope is None:
                    raise EligibilityError("任务未配置到当前 Reclaude 组织")

                cycle = await self._current_cycle_row(session, task_id, moment, lock=True)
                identity = self._cycle_identity(cycle)
                if identity != expected:
                    return _LocalFact(
                        cycle=identity,
                        managed_used_usd=None,
                        selected_account_id=self._normalize_account_id(
                            await self._read_selected_account(session)
                        ),
                        cycle_changed=True,
                    )

                selected_before = self._normalize_account_id(await self._read_selected_account(session))
                if cycle is None:
                    return _LocalFact(
                        cycle=None,
                        managed_used_usd=None,
                        selected_account_id=selected_before,
                        cycle_changed=False,
                    )

                total = await self._aggregate_cycle(session, cycle, moment)

                # The control-plane account or local reset may change while the
                # read-only ledger query is in progress. Re-read both before returning.
                after_cycle = await self._current_cycle_row(session, task_id, moment, lock=True)
                after_identity = self._cycle_identity(after_cycle)
                selected_after = self._normalize_account_id(await self._read_selected_account(session))
                if after_identity != identity or selected_after != selected_before:
                    return _LocalFact(
                        cycle=after_identity,
                        managed_used_usd=None,
                        selected_account_id=selected_after,
                        cycle_changed=True,
                    )
                return _LocalFact(
                    cycle=identity,
                    managed_used_usd=total,
                    selected_account_id=selected_after,
                    cycle_changed=False,
                )

    async def _aggregate_cycle(
        self,
        session: AsyncSession,
        cycle: DeviceQuotaCycle,
        moment: datetime,
    ) -> Decimal | None:
        del moment
        values = list(
            (
                await session.scalars(
                    select(DeviceCycleLedger.confirmed_used_usd)
                    .join(DeviceQuotaCycle, DeviceQuotaCycle.id == DeviceCycleLedger.cycle_id)
                    .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceCycleLedger.task_id)
                    .where(
                        DeviceCycleLedger.cycle_id == cycle.id,
                        DeviceCycleLedger.task_id == cycle.task_id,
                        DeviceQuotaCycle.task_id == cycle.task_id,
                        DeviceTaskScope.org_id == self.org_id,
                    )
                )
            ).all()
        )
        total: Decimal | None = None
        for value in values:
            amount = self._valid_money(value)
            if amount is None:
                continue
            total = amount if total is None else total + amount
            if not total.is_finite() or total >= _MONEY_LIMIT:
                return None
        return total

    def _source_fact(self, usage: OrgAccountUsage, received_at: datetime) -> _SourceFact:
        if not isinstance(usage, OrgAccountUsage) or usage.org_id != self.org_id:
            raise EligibilityError("Reclaude 用量响应组织不匹配")
        try:
            me = usage.me
            weekly = me.weekly_all()
            account = me.current_account
            if weekly.resets_at is None:
                raise ValueError
            weekly_reset = ensure_utc(weekly.resets_at)
            seven_day = account.usage_snapshot.seven_day
            seven_day_reset = ensure_utc(seven_day.resets_at)
            usage_updated_at = ensure_utc(account.usage_updated_at)
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise EligibilityError("Reclaude 周期或使用率数据无效") from None

        account_id = self._account_id(usage.account_id)
        weekly_percent = self._finite_nonnegative(weekly.percent)
        seven_percent = self._finite_nonnegative(seven_day.utilization)
        five_hour = account.usage_snapshot.five_hour
        five_percent = self._finite_nonnegative(five_hour.utilization) if five_hour is not None else None
        five_reset = None
        if five_hour is not None and five_hour.resets_at is not None:
            five_reset = ensure_utc(five_hour.resets_at)

        source_reason: str | None = None
        if received_at - usage_updated_at < timedelta(0):
            source_reason = "账号快照时间无效"
        elif weekly_reset <= received_at or seven_day_reset <= received_at:
            source_reason = "账号周期已刷新"
        elif not same_cycle_reset(weekly_reset, seven_day_reset):
            source_reason = "账号 7天窗口与周期不一致"
        elif account.status.strip().casefold() != "bound" or weekly.is_active is not True or account_id is None:
            source_reason = "账号来源未核实"
        elif weekly_percent is None or seven_percent is None:
            source_reason = "7天使用率无效"
        elif weekly_percent != seven_percent:
            source_reason = "账号 7天窗口与周期不一致"

        return _SourceFact(
            account_id=account_id,
            account_status=account.status,
            email_masked=account.email_masked,
            usage_updated_at=usage_updated_at,
            five_hour_utilization=five_percent,
            five_hour_resets_at=five_reset,
            seven_day_utilization=seven_percent,
            seven_day_resets_at=seven_day_reset,
            weekly_reset_at=weekly_reset,
            source_reason=source_reason,
        )

    @staticmethod
    def _cycle_identity(cycle: DeviceQuotaCycle | None) -> _CycleIdentity | None:
        if cycle is None:
            return None
        return _CycleIdentity(
            id=cycle.id,
            started_at=ensure_utc(cycle.started_at),
            reset_at=ensure_utc(cycle.reset_at),
            status=cycle.status,
            account_id=DeviceAccountUsageService._account_id(cycle.account_id),
        )

    @staticmethod
    async def _read_selected_account(session: AsyncSession) -> str | None:
        return await session.scalar(select(ServiceState.selected_account_id).where(ServiceState.id == 1))

    @staticmethod
    async def _current_cycle_row(
        session: AsyncSession,
        task_id: int,
        moment: datetime,
        *,
        lock: bool = False,
    ) -> DeviceQuotaCycle | None:
        statement = (
            select(DeviceQuotaCycle)
            .where(
                DeviceQuotaCycle.task_id == task_id,
                DeviceQuotaCycle.started_at <= moment,
                DeviceQuotaCycle.reset_at > moment,
            )
            .order_by(DeviceQuotaCycle.reset_at.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    @staticmethod
    def _valid_money(value: object) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return amount if amount.is_finite() and _ZERO <= amount < _MONEY_LIMIT else None

    @staticmethod
    def _finite_nonnegative(value: object) -> Decimal | None:
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return amount if amount.is_finite() and _ZERO <= amount < _MONEY_LIMIT else None

    @staticmethod
    def _normalize_account_id(value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _account_id(value: object) -> str | None:
        return DeviceAccountUsageService._normalize_account_id(value)

    @staticmethod
    def _positive_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    def _now(self, value: datetime | None = None) -> datetime:
        try:
            return ensure_utc(value if value is not None else self.clock())
        except (TypeError, ValueError, OverflowError):
            raise EligibilityError("当前时间无效") from None


DeviceAccountUsageSnapshot = DeviceAccountUsage
