from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.application.device_context import OrgAccountSource, OrgAccountUsage, SingleOrgTaskService
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.application.recovery import RecoveryGate
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import DeviceQuotaCycle, DeviceTaskScope, QuotaTask, ServiceState

log = structlog.get_logger(__name__)

ReconcileStatus = Literal["INITIALIZED", "UNCHANGED", "RESET"]


@dataclass(frozen=True)
class AccountReconcileResult:
    status: ReconcileStatus
    account_id: str
    previous_account_id: str | None
    cycle_id: int | None
    resumed_task: bool


class DeviceAccountReconcileService:
    """Discover the org account and rotate the device task on replacement.

    The account inventory is the source of truth.  ``ServiceState`` keeps the
    last successfully reconciled identity so a failed baseline collection can
    be retried after a restart without treating the new account as committed.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: object,
        source: OrgAccountSource,
        cycle_service: DeviceCycleService,
        reset_service: DeviceTaskResetService,
        org_id: int,
        *,
        clock=utcnow,
        gate: RecoveryGate | None = None,
        task_service: QuotaTaskService | None = None,
        account_notifications: DeviceAccountNotificationService | None = None,
    ) -> None:
        self.session_factory = factory
        self.gateway = gateway
        self.source = source
        self.cycle_service = cycle_service
        self.reset_service = reset_service
        self.org_id = org_id
        self.clock = clock
        self.gate = gate or RecoveryGate(factory)
        self.task_service = task_service or QuotaTaskService(factory, org_id=org_id)
        self.account_notifications = account_notifications
        self._lock = asyncio.Lock()

    async def reconcile(
        self,
        task_name: str | None = None,
        *,
        operator_id: int | None = None,
    ) -> AccountReconcileResult:
        async with self._lock:
            try:
                context = await SingleOrgTaskService(self.session_factory, self.org_id).resolve_task(task_name)
                usage = await self.cycle_service.fetch_usage()
                account_id = self._validate_usage(usage)
            except Exception:
                await self._disable_after_failure()
                raise

            state, latest, task_status = await self._snapshot(context.task_id)
            selected = self._normalize_id(state.selected_account_id if state is not None else None)
            cycle_account = self._normalize_id(latest.account_id if latest is not None else None)

            # A cycle that already records the live account is authoritative
            # over a stale selected_account_id left by an older process.
            if cycle_account == account_id:
                previous = selected if selected != account_id else None
                await self._persist_identity(account_id, expected=selected, reason="account_reconciled")
                self._configure_gateway(account_id)
                resumed = await self._resume_running_task(task_status)
                return AccountReconcileResult("INITIALIZED" if previous else "UNCHANGED", account_id, previous, latest.id if latest else None, resumed)

            if latest is None:
                previous = selected
                await self._persist_identity(account_id, expected=selected, reason="account_reconciled")
                self._configure_gateway(account_id)
                resumed = await self._resume_running_task(task_status)
                return AccountReconcileResult("INITIALIZED", account_id, previous, None, resumed)

            previous = cycle_account or selected
            if previous is None:
                # A legacy cycle without an account cannot safely be attributed
                # to either side of a replacement.  Establish the live source
                # and let the ordinary cycle sync repair its evidence.
                await self._persist_identity(account_id, expected=selected, reason="account_reconciled")
                self._configure_gateway(account_id)
                resumed = await self._resume_running_task(task_status)
                return AccountReconcileResult("INITIALIZED", account_id, None, latest.id, resumed)

            await self.gate.disable("account_reconcile_pending")
            if selected != previous:
                await self._persist_identity(previous, expected=selected, reason="account_reconcile_pending", write_enabled=False)
            self._configure_gateway(account_id)
            operation_key = f"auto-account-reset:{context.task_id}:{latest.id}:{previous}:{account_id}"
            try:
                reset = await self.reset_service.reset(
                    context.name,
                    operator_id,
                    operation_key=operation_key,
                    target_account_id=account_id,
                )
            except Exception as exc:
                if self.account_notifications is not None:
                    try:
                        await self.account_notifications.queue_reset_failure(
                            task_id=context.task_id,
                            cycle_id=latest.id,
                            generation_key=operation_key,
                            task_name=context.name,
                            account_id=account_id,
                            previous_account_id=previous,
                            error_text=type(exc).__name__,
                        )
                    except Exception:
                        log.warning("device_account_reset_failure_notification_failed", error_type=type(exc).__name__)
                self._restore_gateway(previous)
                raise

            self._configure_gateway(account_id)
            resumed = await self._resume_running_task(task_status)
            return AccountReconcileResult("RESET", account_id, previous, reset.cycle_id, resumed)

    async def prepare(self, task_name: str | None = None) -> AccountReconcileResult:
        """Reconcile the identity and refresh cycle evidence before writes."""

        result = await self.reconcile(task_name)
        await self.cycle_service.sync(task_name)
        return result

    async def is_configured(self, task_name: str | None = None) -> bool:
        async with self.session_factory() as session:
            statement = select(DeviceTaskScope.task_id).where(DeviceTaskScope.org_id == self.org_id)
            if task_name and task_name.strip():
                statement = statement.join(QuotaTask, QuotaTask.id == DeviceTaskScope.task_id).where(
                    QuotaTask.name_normalized == task_name.strip().casefold()
                )
            return await session.scalar(statement.limit(1)) is not None

    async def _snapshot(self, task_id: int) -> tuple[ServiceState | None, DeviceQuotaCycle | None, str]:
        async with self.session_factory() as session:
            state = await session.get(ServiceState, 1)
            task = await session.get(QuotaTask, task_id)
            latest = await session.scalar(
                select(DeviceQuotaCycle)
                .where(DeviceQuotaCycle.task_id == task_id)
                .order_by(DeviceQuotaCycle.reset_at.desc(), DeviceQuotaCycle.id.desc())
                .limit(1)
            )
            return state, latest, task.status if task is not None else "STOPPED"

    async def _persist_identity(
        self,
        account_id: str,
        *,
        expected: str | None,
        reason: str,
        write_enabled: bool | None = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(ServiceState, 1, with_for_update=True)
                if state is None:
                    state = ServiceState(id=1, write_enabled=False, reason=reason, updated_at=self._now())
                    session.add(state)
                    await session.flush()
                current = self._normalize_id(state.selected_account_id)
                if current not in {expected, account_id}:
                    raise EligibilityError("Reclaude 账号在自动核对期间再次变化，请重试")
                state.selected_account_id = account_id
                state.reason = reason
                if write_enabled is not None:
                    state.write_enabled = write_enabled
                state.updated_at = self._now()

    async def _resume_running_task(self, task_status: str) -> bool:
        if task_status != "RUNNING":
            return False
        return await self.task_service.enable_latch(reason="account_reconciled")

    async def _disable_after_failure(self) -> None:
        try:
            await self.gate.disable("account_reconcile_failed")
        except Exception:
            pass

    def _validate_usage(self, usage: OrgAccountUsage) -> str:
        if not isinstance(usage, OrgAccountUsage) or usage.org_id != self.org_id:
            raise EligibilityError("Reclaude 用量响应组织不匹配")
        account_id = self._normalize_id(usage.account_id)
        if account_id is None:
            raise EligibilityError("Reclaude 当前绑定账号无有效 ID")
        # Reuse the cycle validator so account reconciliation cannot commit an
        # identity whose cycle evidence would immediately be unusable.
        self.cycle_service._validate_usage(usage, self._now())
        return account_id

    def _configure_gateway(self, account_id: str) -> None:
        configure = getattr(self.gateway, "configure_account_id", None)
        if configure is None:
            raise EligibilityError("Reclaude 网关不支持账号路由配置")
        configure(account_id)

    def _restore_gateway(self, account_id: str) -> None:
        try:
            self._configure_gateway(account_id)
        except Exception:
            pass

    @staticmethod
    def _normalize_id(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
