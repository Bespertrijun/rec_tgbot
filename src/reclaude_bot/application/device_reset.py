from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_account_notifications import DeviceAccountNotificationService
from reclaude_bot.application.device_context import SingleOrgTaskService
from reclaude_bot.application.device_cycle import DeviceCycleEvidence, DeviceCycleService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc, is_last_24h, same_cycle_reset
from reclaude_bot.infrastructure.db.models import (
    AuditLog,
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceTaskMember,
    DeviceTaskScope,
    DeviceUsageSnapshot,
    QuotaTask,
    ServiceState,
    User,
)
from reclaude_bot.infrastructure.reclaude.models import DeviceUsage as ReclaudeDeviceUsage

_MONEY_LIMIT = Decimal("100000000")
_MONEY_QUANTUM = Decimal("0.0000000001")


class _DeviceResetGateway(Protocol):
    account_id: int | str | None

    async def device_usage(self, device_id: int, org_id: int, range: str = "all") -> ReclaudeDeviceUsage: ...


@dataclass(frozen=True)
class DeviceTaskResetResult:
    task_id: int
    cycle_id: int
    reset_at: datetime
    device_count: int
    user_count: int


@dataclass(frozen=True)
class _AssociationVersion:
    association_id: int
    user_id: int
    device_id: int
    started_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class _ResetPlan:
    task_id: int
    task_name: str
    task_status: str
    task_limit_usd: Decimal
    task_updated_at: datetime
    scope_mode: str
    scope_updated_at: datetime
    scope_member_ids: tuple[int, ...]
    covered_user_ids: tuple[int, ...]
    selected_account_id: str
    write_enabled: bool
    cycle_id: int
    cycle_started_at: datetime
    cycle_reset_at: datetime
    cycle_status: str
    cycle_account_id: str | None
    associations: tuple[_AssociationVersion, ...]


@dataclass(frozen=True)
class _DeviceBaseline:
    association_id: int
    device_id: int
    total_usd: Decimal
    sampled_at: datetime
    payload: dict[str, object]


class DeviceTaskResetService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: _DeviceResetGateway,
        cycle_service: DeviceCycleService,
        org_id: int,
        *,
        clock=utcnow,
        account_notifications: DeviceAccountNotificationService | None = None,
    ) -> None:
        self.session_factory = factory
        self.gateway = gateway
        self.cycle_service = cycle_service
        self.task_service = SingleOrgTaskService(factory, org_id)
        self.org_id = self.task_service.org_id
        self.ledger = DeviceLedgerService(factory, self.org_id, clock=clock)
        self.clock = clock
        self.account_notifications = account_notifications

    async def reset(
        self,
        task_name: str,
        operator_id: int | None,
        *,
        operation_key: str,
        target_account_id: str | None = None,
    ) -> DeviceTaskResetResult:
        if operator_id is not None and (isinstance(operator_id, bool) or not isinstance(operator_id, int) or operator_id <= 0):
            raise EligibilityError("管理员 ID 无效")
        if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > 128:
            raise EligibilityError("reset operation_key 必须是 1 到 128 个非空字符")
        if target_account_id is not None:
            target_account_id = self._validated_account_id(target_account_id)

        context = await self.task_service.resolve_task(task_name)
        existing = await self._find_operation(operation_key)
        if existing is not None:
            if existing.task_id != context.task_id:
                raise EligibilityError("该 Telegram 消息标识已用于其他任务重置")
            return existing

        plan = await self._read_plan(context.task_id, self._now(), lock=False)
        evidence = await self.cycle_service.fetch_fresh_evidence()
        target = target_account_id or plan.selected_account_id
        self._validate_evidence(evidence, target)
        self._ensure_gateway_account(target)
        cut_at = evidence.received_at
        if evidence.reset_at <= cut_at:
            raise EligibilityError("Reclaude 周期已重置，不能使用该响应重置任务")

        baselines: list[_DeviceBaseline] = []
        for association in plan.associations:
            if self._now() >= evidence.reset_at:
                raise EligibilityError("采集设备基线时当前 Reclaude 周期已重置，请重试")
            self._ensure_gateway_account(target)
            usage = await self.gateway.device_usage(association.device_id, self.org_id, range="all")
            self._ensure_gateway_account(target)
            sampled_at = self._now()
            total_usd, payload = self._validate_device_usage(usage)
            if sampled_at < cut_at or sampled_at >= evidence.reset_at:
                raise EligibilityError("设备基线采集时间超出重置周期")
            baselines.append(
                _DeviceBaseline(
                    association_id=association.association_id,
                    device_id=association.device_id,
                    total_usd=total_usd,
                    sampled_at=sampled_at,
                    payload=payload,
                )
            )

        # Recheck the live source after all network baselines.  The inventory
        # can change independently of the local gateway routing hint while a
        # multi-device reset is in flight; committing in that case would mix
        # baselines from two upstream accounts.
        final_evidence = await self.cycle_service.fetch_fresh_evidence()
        self._validate_evidence(final_evidence, target)
        if not same_cycle_reset(evidence.reset_at, final_evidence.reset_at):
            raise EligibilityError("重置期间 Reclaude 周期发生变化，不能提交任务重置")
        if any(item.sampled_at >= final_evidence.reset_at for item in baselines):
            raise EligibilityError("采集设备基线时当前 Reclaude 周期已重置，请重试")

        return await self._commit_reset(
            plan,
            final_evidence,
            cut_at,
            tuple(baselines),
            operator_id,
            operation_key,
            target,
        )

    async def _commit_reset(
        self,
        plan: _ResetPlan,
        evidence: DeviceCycleEvidence,
        cut_at: datetime,
        baselines: tuple[_DeviceBaseline, ...],
        operator_id: int | None,
        operation_key: str,
        target_account_id: str,
    ) -> DeviceTaskResetResult:
        self._ensure_gateway_account(target_account_id)
        now = self._now()
        if evidence.reset_at <= now:
            raise EligibilityError("Reclaude 周期已重置，不能提交任务重置")
        baselines_by_association = {item.association_id: item for item in baselines}
        if set(baselines_by_association) != {item.association_id for item in plan.associations}:
            raise EligibilityError("活动设备基线不完整，不能提交任务重置")

        async with self.session_factory() as session:
            async with session.begin():
                service_state = await session.get(ServiceState, 1, with_for_update=True)
                if service_state is None:
                    raise EligibilityError("尚未确认 Reclaude 绑定账号，不能重置任务周期")

                prior = await self._find_operation(operation_key, session=session)
                if prior is not None:
                    if prior.task_id != plan.task_id:
                        raise EligibilityError("该 Telegram 消息标识已用于其他任务重置")
                    return prior

                current = await self._read_plan(plan.task_id, now, lock=True, session=session)
                if not self._same_plan(plan, current):
                    raise EligibilityError("任务、成员范围、账号、设备或周期在重置期间发生变化，请重试")
                if (
                    service_state.selected_account_id is None
                    or str(service_state.selected_account_id).strip() != plan.selected_account_id
                    or not evidence.source_valid
                    or evidence.account_id is None
                    or str(evidence.account_id).strip() != target_account_id
                    or ensure_utc(evidence.reset_at) <= cut_at
                ):
                    raise EligibilityError("当前 Reclaude 账号或周期证据已变化，不能重置任务")
                self._ensure_gateway_account(target_account_id)

                latest = await session.scalar(
                    select(DeviceQuotaCycle)
                    .where(DeviceQuotaCycle.id == plan.cycle_id)
                    .with_for_update()
                )
                original_reset_at: datetime | None = None
                if latest is not None:
                    original_reset_at = ensure_utc(latest.reset_at)
                    if ensure_utc(latest.started_at) > cut_at:
                        raise EligibilityError("当前本地周期晚于重置切点，不能关闭该周期")
                    if original_reset_at > cut_at:
                        latest.reset_at = cut_at
                    latest.status = "EXPIRED"
                    latest.last_day_allow = False
                    await session.flush()

                cycle = DeviceQuotaCycle(
                    task_id=plan.task_id,
                    started_at=cut_at,
                    reset_at=ensure_utc(evidence.reset_at),
                    created_at=now,
                    status="VERIFIED",
                    account_id=target_account_id,
                    weekly_percent=evidence.percent,
                    last_day_allow=bool(
                        evidence.source_valid
                        and is_last_24h(cut_at, evidence.reset_at)
                        and evidence.percent < Decimal("100")
                    ),
                    last_day_checked_at=ensure_utc(evidence.request_started_at),
                )
                session.add(cycle)
                await session.flush()

                for association in current.associations:
                    baseline = baselines_by_association[association.association_id]
                    snapshot = DeviceUsageSnapshot(
                        org_id=self.org_id,
                        device_id=baseline.device_id,
                        sample_key=f"device-reset-{uuid4().hex}",
                        range="all",
                        total_usd=baseline.total_usd,
                        sampled_at=baseline.sampled_at,
                        payload=baseline.payload,
                    )
                    await self.ledger.initialize_reset_baseline(
                        session,
                        association.association_id,
                        cycle.id,
                        snapshot,
                    )

                if self.account_notifications is not None and target_account_id != plan.selected_account_id:
                    affected_user_ids = await self._affected_user_ids(session, plan, current)
                    await self.account_notifications.queue_reset_success(
                        session,
                        task_id=plan.task_id,
                        cycle_id=cycle.id,
                        generation_key=operation_key,
                        task_name=plan.task_name,
                        account_id=target_account_id,
                        previous_account_id=plan.selected_account_id,
                        task_limit_usd=plan.task_limit_usd,
                        reset_at=cycle.reset_at,
                        affected_user_ids=affected_user_ids,
                    )

                # The new identity becomes durable in the same transaction as
                # the new cycle and all device baselines.  A manual reset of
                # the same account must preserve the caller's closed-latch
                # reason.
                if target_account_id != plan.selected_account_id:
                    service_state.selected_account_id = target_account_id
                    service_state.reason = "account_reconciled"
                service_state.updated_at = now

                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="DEVICE_TASK_RESET",
                    target_type="QUOTA_TASK",
                    target_id=str(plan.task_id),
                    result="SUCCESS",
                    parameters_summary={
                        "operation_key": operation_key,
                        "cycle_id": cycle.id,
                        "previous_cycle_id": plan.cycle_id,
                        "previous_reset_at": original_reset_at.isoformat() if original_reset_at else None,
                        "cut_at": cut_at.isoformat(),
                        "reset_at": ensure_utc(evidence.reset_at).isoformat(),
                        "account_id": target_account_id,
                        "device_count": len(current.associations),
                        "user_count": len(current.covered_user_ids),
                    },
                )
                return DeviceTaskResetResult(
                    task_id=plan.task_id,
                    cycle_id=cycle.id,
                    reset_at=ensure_utc(evidence.reset_at),
                    device_count=len(current.associations),
                    user_count=len(current.covered_user_ids),
                )

    async def _read_plan(
        self,
        task_id: int,
        now: datetime,
        *,
        lock: bool,
        session: AsyncSession | None = None,
    ) -> _ResetPlan:
        if session is None:
            async with self.session_factory() as owned:
                return await self._read_plan(task_id, now, lock=lock, session=owned)

        task_query = select(QuotaTask).where(QuotaTask.id == task_id)
        scope_query = select(DeviceTaskScope).where(DeviceTaskScope.task_id == task_id)
        state_query = select(ServiceState).where(ServiceState.id == 1)
        if lock:
            task_query = task_query.with_for_update()
            scope_query = scope_query.with_for_update()
            state_query = state_query.with_for_update()
        task = await session.scalar(task_query)
        scope = await session.scalar(scope_query)
        service_state = await session.scalar(state_query)
        if task is None or scope is None or scope.org_id != self.org_id:
            raise EligibilityError("任务未配置到当前 Reclaude 组织")
        if service_state is None or service_state.selected_account_id is None:
            raise EligibilityError("尚未确认 Reclaude 绑定账号，不能重置任务周期")

        if scope.scope_mode not in {"ALL", "ALLOWLIST", "EXCLUDE"}:
            raise EligibilityError("设备任务成员范围配置无效")
        listed_user_ids = tuple(
            (
                await session.scalars(
                    select(DeviceTaskMember.user_id)
                    .where(DeviceTaskMember.task_id == task_id)
                    .order_by(DeviceTaskMember.user_id)
                )
            ).all()
        )
        all_user_ids = tuple((await session.scalars(select(User.id).order_by(User.id))).all())
        if scope.scope_mode == "ALL":
            covered_user_ids = all_user_ids
        elif scope.scope_mode == "ALLOWLIST":
            covered_user_ids = listed_user_ids
        else:
            excluded = set(listed_user_ids)
            covered_user_ids = tuple(user_id for user_id in all_user_ids if user_id not in excluded)

        # If the bot was stopped until the upstream cycle expired, the latest
        # historical cycle is still needed to identify the old account and
        # establish the new generation.
        cycle_query = (
            select(DeviceQuotaCycle)
            .where(DeviceQuotaCycle.task_id == task_id)
            .order_by(DeviceQuotaCycle.reset_at.desc(), DeviceQuotaCycle.id.desc())
            .limit(1)
        )
        if lock:
            cycle_query = cycle_query.with_for_update()
        cycle = await session.scalar(cycle_query)
        if cycle is None or cycle.status not in {"VERIFIED", "NEEDS_REVIEW", "EXPIRED"}:
            raise EligibilityError("当前没有可重置的本地设备周期")
        if (
            cycle.account_id is not None
            and str(cycle.account_id).strip() != str(service_state.selected_account_id).strip()
        ):
            raise EligibilityError("当前本地周期与已选 Reclaude 账号不匹配")

        unresolved = await session.scalar(
            select(DeviceAction.id)
            .join(DeviceAssociation, DeviceAssociation.id == DeviceAction.association_id)
            .where(
                DeviceAssociation.task_id == task_id,
                DeviceAssociation.org_id == self.org_id,
                DeviceAction.status.in_(("PENDING", "UNKNOWN")),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise EligibilityError("任务存在未确认的授权或撤销，先完成核对再重置")

        association_query = (
            select(DeviceAssociation)
            .where(
                DeviceAssociation.task_id == task_id,
                DeviceAssociation.org_id == self.org_id,
                DeviceAssociation.ended_at.is_(None),
            )
            .order_by(DeviceAssociation.id)
        )
        if lock:
            association_query = association_query.with_for_update()
        active_associations = list((await session.scalars(association_query)).all())
        association_versions: list[_AssociationVersion] = []
        for association in active_associations:
            if association.state != "ACTIVE" or association.device_id is None:
                raise EligibilityError("任务存在未确认的设备关联，不能重置")
            auth_action_id = await session.scalar(
                select(DeviceAction.id)
                .where(
                    DeviceAction.association_id == association.id,
                    DeviceAction.kind == "AUTH",
                    DeviceAction.status == "SUCCEEDED",
                    DeviceAction.target_device_id == association.device_id,
                )
                .order_by(DeviceAction.id.desc())
                .limit(1)
            )
            if auth_action_id is None:
                raise EligibilityError("活动设备关联缺少成功授权记录，不能重置")
            association_versions.append(
                _AssociationVersion(
                    association_id=association.id,
                    user_id=association.user_id,
                    device_id=association.device_id,
                    started_at=ensure_utc(association.started_at),
                    updated_at=ensure_utc(association.updated_at),
                )
            )

        return _ResetPlan(
            task_id=task.id,
            task_name=task.name,
            task_status=task.status,
            task_limit_usd=Decimal(task.limit_usd),
            task_updated_at=ensure_utc(task.updated_at),
            scope_mode=scope.scope_mode,
            scope_updated_at=ensure_utc(scope.updated_at),
            scope_member_ids=listed_user_ids,
            covered_user_ids=covered_user_ids,
            selected_account_id=str(service_state.selected_account_id).strip(),
            write_enabled=bool(service_state.write_enabled),
            cycle_id=cycle.id,
            cycle_started_at=ensure_utc(cycle.started_at),
            cycle_reset_at=ensure_utc(cycle.reset_at),
            cycle_status=cycle.status,
            cycle_account_id=cycle.account_id,
            associations=tuple(association_versions),
        )

    async def _find_operation(
        self,
        operation_key: str,
        *,
        task_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> DeviceTaskResetResult | None:
        if session is None:
            async with self.session_factory() as owned:
                return await self._find_operation(operation_key, task_id=task_id, session=owned)
        entries = list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.action == "DEVICE_TASK_RESET")
                    .order_by(AuditLog.id.desc())
                )
            ).all()
        )
        for entry in entries:
            summary = entry.parameters_summary if isinstance(entry.parameters_summary, dict) else {}
            if summary.get("operation_key") != operation_key:
                continue
            if task_id is not None and entry.target_id != str(task_id):
                raise EligibilityError("该 Telegram 消息标识已用于其他任务重置")
            try:
                return DeviceTaskResetResult(
                    task_id=int(entry.target_id),
                    cycle_id=int(summary["cycle_id"]),
                    reset_at=ensure_utc(datetime.fromisoformat(str(summary["reset_at"]))),
                    device_count=int(summary["device_count"]),
                    user_count=int(summary["user_count"]),
                )
            except (KeyError, TypeError, ValueError):
                raise EligibilityError("已有重置操作记录不完整，不能安全重试") from None
        return None

    async def _affected_user_ids(
        self,
        session: AsyncSession,
        previous: _ResetPlan,
        current: _ResetPlan,
    ) -> tuple[int, ...]:
        ledger_user_ids = set(
            (
                await session.scalars(
                    select(DeviceCycleLedger.user_id).where(
                        DeviceCycleLedger.task_id == previous.task_id,
                        DeviceCycleLedger.cycle_id == previous.cycle_id,
                    )
                )
            ).all()
        )
        active_user_ids = {association.user_id for association in current.associations}
        return tuple(sorted(set(current.covered_user_ids) | ledger_user_ids | active_user_ids))

    @staticmethod
    def _same_plan(previous: _ResetPlan, current: _ResetPlan) -> bool:
        return (
            previous.task_id == current.task_id
            and previous.task_status == current.task_status
            and previous.task_limit_usd == current.task_limit_usd
            and previous.task_updated_at == current.task_updated_at
            and previous.scope_mode == current.scope_mode
            and previous.scope_updated_at == current.scope_updated_at
            and previous.scope_member_ids == current.scope_member_ids
            and previous.covered_user_ids == current.covered_user_ids
            and previous.selected_account_id == current.selected_account_id
            and previous.write_enabled == current.write_enabled
            and previous.cycle_id == current.cycle_id
            and previous.cycle_started_at == current.cycle_started_at
            and previous.cycle_reset_at == current.cycle_reset_at
            and previous.cycle_status == current.cycle_status
            and previous.cycle_account_id == current.cycle_account_id
            and previous.associations == current.associations
        )

    @staticmethod
    def _validate_evidence(evidence: DeviceCycleEvidence, selected_account_id: str) -> None:
        if (
            not evidence.source_valid
            or evidence.account_id is None
            or str(evidence.account_id).strip() != selected_account_id
        ):
            raise EligibilityError("当前 Reclaude 账号或周周期数据未通过验证")

    def _ensure_gateway_account(self, expected_account_id: str) -> None:
        actual_account_id = self.gateway.account_id
        if actual_account_id is None or str(actual_account_id).strip() != expected_account_id:
            raise EligibilityError("重置期间 Reclaude 账号发生变化")

    @staticmethod
    def _validated_account_id(account_id: str) -> str:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id.strip()) > 128 or not account_id.strip().isdigit():
            raise EligibilityError("Reclaude 账号 ID 无效")
        if int(account_id.strip()) <= 0:
            raise EligibilityError("Reclaude 账号 ID 无效")
        return account_id.strip()

    @staticmethod
    def _validate_device_usage(usage: ReclaudeDeviceUsage) -> tuple[Decimal, dict[str, object]]:
        if not isinstance(usage, ReclaudeDeviceUsage) or usage.range != "all":
            raise EligibilityError("设备全量用量响应无效")
        total = usage.overview.total_usd
        if not isinstance(total, Decimal) or not total.is_finite() or total < 0 or total >= _MONEY_LIMIT:
            raise EligibilityError("设备基线用量金额无效")
        try:
            normalized = total.quantize(_MONEY_QUANTUM)
        except (InvalidOperation, ValueError, OverflowError):
            raise EligibilityError("设备基线用量精度无效") from None
        if normalized != total:
            raise EligibilityError("设备基线用量最多支持 10 位小数")
        payload = usage.model_dump(mode="json")
        if not isinstance(payload, dict):
            raise EligibilityError("设备基线用量内容无效")
        return normalized, payload

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
