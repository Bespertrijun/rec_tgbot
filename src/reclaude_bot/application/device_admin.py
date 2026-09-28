from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device import DeviceAuthorizationResult, DeviceAuthorizationService, QuotaCheck
from reclaude_bot.application.device_context import DeviceTaskContext, SingleOrgTaskService
from reclaude_bot.application.device_ledger import DeviceLedgerService
from reclaude_bot.application.device_sampling import SamplingCallback, invoke_sampling_callback
from reclaude_bot.domain.enums import TaskStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import as_decimal, ensure_utc
from reclaude_bot.infrastructure.db.models import (
    Device,
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceTaskScope,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
    QuotaTask,
    User,
)
from reclaude_bot.infrastructure.reclaude.models import DeviceRecord, DeviceUsage

_MONEY_LIMIT = Decimal("100000000")
_MONEY_QUANTUM = Decimal("0.0000000001")


class _DeviceAdminGateway(Protocol):
    async def list_devices(self) -> list[DeviceRecord]: ...
    async def device_usage(self, device_id: int, org_id: int, range: str = "all") -> DeviceUsage: ...


@dataclass(frozen=True)
class DeviceListEntry:
    device_id: int
    org_id: int
    name: str
    revoked_at: datetime | None
    owner_user_id: int | None
    owner_email: str | None
    association_id: int | None
    association_state: str | None


class DeviceAdminService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: _DeviceAdminGateway,
        org_id: int,
        quota_check: QuotaCheck,
        *,
        clock: Callable[[], datetime] = utcnow,
        on_authorized: SamplingCallback | None = None,
        before_authorize: Callable[[str | None], Awaitable[object]] | None = None,
        ledger: DeviceLedgerService | None = None,
    ) -> None:
        self.session_factory = factory
        self.gateway = gateway
        self.task_service = SingleOrgTaskService(factory, org_id)
        self.org_id = self.task_service.org_id
        self.quota_check = quota_check
        self.clock = clock
        self.on_authorized = on_authorized
        self.before_authorize = before_authorize
        self.ledger = ledger or DeviceLedgerService(factory, self.org_id, clock=clock)

    async def authuser(
        self,
        user_id: int,
        device_id: int,
        operator_id: int,
        *,
        task_name: str | None = None,
        used_usd: Decimal | None = None,
    ) -> DeviceAuthorizationResult:
        amount = self._validate_import_amount(used_usd) if used_usd is not None else None
        if amount is None:
            result = await self._authuser(user_id, device_id, operator_id, task_name=task_name)
        else:
            result = await self._authuser_with_import(
                user_id,
                device_id,
                operator_id,
                amount,
                task_name=task_name,
            )
        if result.status == "SUCCEEDED" and self.on_authorized is not None:
            await invoke_sampling_callback(
                self.on_authorized,
                result.association_id,
                phase="after_admin_authorized",
            )
        return result

    async def _authuser_with_import(
        self,
        user_id: int,
        device_id: int,
        operator_id: int,
        used_usd: Decimal,
        *,
        task_name: str | None = None,
    ) -> DeviceAuthorizationResult:
        self._positive_id(user_id, "用户 ID")
        self._positive_id(device_id, "设备 ID")
        if isinstance(operator_id, bool) or not isinstance(operator_id, int) or operator_id <= 0:
            raise EligibilityError("管理员 ID 无效")

        context = await self.task_service.resolve_task(task_name)
        if self.before_authorize is not None:
            await self.before_authorize(context.name)

        async with self.session_factory() as session:
            async with session.begin():
                context, existing = await self._check_qualification(
                    session,
                    context,
                    user_id,
                    device_id,
                    allow_existing_over_limit=True,
                )
                if existing is not None:
                    return await self._require_matching_import(session, existing, used_usd)
                cycle = await self._current_import_cycle(session, context.task_id, self._now())
                await self._ensure_import_history_is_empty(session, user_id, cycle.id)
                cycle_id = cycle.id

        records = await self.gateway.list_devices()
        matching = [record for record in records if record.id == device_id and record.org_id == self.org_id]
        if len(matching) != 1 or matching[0].revoked_at is not None:
            raise EligibilityError("当前组织中找不到可关联的有效设备")
        device_record = matching[0]

        association_started_at = self._now()
        usage = await self.gateway.device_usage(device_id, self.org_id, range="all")
        baseline_total, payload = self._validate_usage_baseline(usage)
        sampled_at = self._now()

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    context, current_existing = await self._check_qualification(
                        session,
                        context,
                        user_id,
                        device_id,
                        allow_existing_over_limit=True,
                    )
                    if current_existing is not None:
                        return await self._require_matching_import(session, current_existing, used_usd)

                    cycle = await self._current_import_cycle(session, context.task_id, sampled_at)
                    if cycle.id != cycle_id:
                        raise EligibilityError("设备周期在基线查询期间发生变化，请重新执行 /authuser")
                    await self._ensure_import_history_is_empty(session, user_id, cycle.id)
                    await self._locked_scope(session, context)
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    if user is None:
                        raise EligibilityError("用户不存在")
                    active_device = await session.scalar(
                        select(DeviceAssociation.id)
                        .where(
                            DeviceAssociation.org_id == self.org_id,
                            DeviceAssociation.device_id == device_id,
                            DeviceAssociation.ended_at.is_(None),
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if active_device is not None:
                        raise EligibilityError("该设备已有关联用户")

                    now = self._now()
                    device = await session.scalar(
                        select(Device)
                        .where(Device.org_id == self.org_id, Device.device_id == device_id)
                        .with_for_update()
                    )
                    if device is None:
                        device = Device(
                            org_id=self.org_id,
                            device_id=device_id,
                            name=device_record.name,
                            first_synced_at=now,
                            last_synced_at=now,
                            revoked_at=None,
                        )
                        session.add(device)
                        await session.flush()
                    else:
                        device.name = device_record.name
                        device.last_synced_at = now
                        device.revoked_at = None

                    association = DeviceAssociation(
                        user_id=user_id,
                        task_id=context.task_id,
                        org_id=self.org_id,
                        device_id=device_id,
                        state="ACTIVE",
                        started_at=association_started_at,
                        ended_at=None,
                        end_reason=None,
                        updated_at=now,
                    )
                    session.add(association)
                    await session.flush()
                    action = DeviceAction(
                        association_id=association.id,
                        kind="AUTH",
                        status="SUCCEEDED",
                        idempotency_key=uuid4().hex,
                        target_device_id=device_id,
                        result_reused=None,
                        attempt_count=0,
                        next_retry_at=None,
                        last_error_code=None,
                        created_at=association_started_at,
                        updated_at=sampled_at,
                        completed_at=sampled_at,
                    )
                    session.add(action)
                    await session.flush()
                    snapshot = DeviceUsageSnapshot(
                        org_id=self.org_id,
                        device_id=device_id,
                        sample_key=f"admin-import-{uuid4().hex}",
                        range="all",
                        total_usd=baseline_total,
                        sampled_at=sampled_at,
                        payload=payload,
                    )
                    await self.ledger.import_initial_usage(
                        session,
                        association.id,
                        cycle.id,
                        snapshot,
                        used_usd,
                        operator_id,
                    )
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_AUTHUSER_ASSOCIATED",
                        target_type="DEVICE_ASSOCIATION",
                        target_id=str(association.id),
                        parameters_summary={
                            "user_id": user.id,
                            "task_id": context.task_id,
                            "org_id": self.org_id,
                            "device_id": device_id,
                            "used_imported": True,
                        },
                    )
                    return DeviceAuthorizationService._result(association, action)
        except IntegrityError:
            async with self.session_factory() as session:
                async with session.begin():
                    current = await self._active_association_for_import(session, user_id, device_id)
                    if current is not None:
                        return await self._require_matching_import(session, current, used_usd)
            raise EligibilityError("设备或用户已被并发关联，请重新查询") from None

    async def _authuser(
        self,
        user_id: int,
        device_id: int,
        operator_id: int,
        *,
        task_name: str | None = None,
    ) -> DeviceAuthorizationResult:
        self._positive_id(user_id, "用户 ID")
        self._positive_id(device_id, "设备 ID")
        if isinstance(operator_id, bool) or not isinstance(operator_id, int):
            raise EligibilityError("管理员 ID 无效")

        context = await self.task_service.resolve_task(task_name)
        if self.before_authorize is not None:
            await self.before_authorize(context.name)
        async with self.session_factory() as session:
            async with session.begin():
                context, existing = await self._check_qualification(session, context, user_id, device_id)

        records = await self.gateway.list_devices()
        matching = [record for record in records if record.id == device_id and record.org_id == self.org_id]
        if len(matching) != 1 or matching[0].revoked_at is not None:
            raise EligibilityError("当前组织中找不到可关联的有效设备")
        device_record = matching[0]

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    context, current_existing = await self._check_qualification(session, context, user_id, device_id)
                    if existing is not None and current_existing != existing:
                        raise EligibilityError("原设备关联已结束或发生变化，请重新核对后重试")
                    existing = current_existing
                    if existing is not None:
                        association_id, action_id = existing
                        association = await session.get(DeviceAssociation, association_id)
                        action = await session.get(DeviceAction, action_id)
                        if association is None or action is None:
                            raise EligibilityError("设备关联记录已变化，请重新查询")
                        if (
                            association.user_id != user_id
                            or association.task_id != context.task_id
                            or association.org_id != self.org_id
                            or association.device_id != device_id
                            or association.state != "ACTIVE"
                            or association.ended_at is not None
                            or action.association_id != association.id
                            or action.kind != "AUTH"
                            or action.status != "SUCCEEDED"
                            or action.target_device_id != device_id
                        ):
                            raise EligibilityError("当前设备关联已变化，请重新核对后重试")
                        return DeviceAuthorizationService._result(association, action)

                    await self._locked_scope(session, context)
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    if user is None:
                        raise EligibilityError("用户不存在")
                    active_device = await session.scalar(
                        select(DeviceAssociation.id)
                        .where(
                            DeviceAssociation.org_id == self.org_id,
                            DeviceAssociation.device_id == device_id,
                            DeviceAssociation.ended_at.is_(None),
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if active_device is not None:
                        raise EligibilityError("该设备已有关联用户")

                    now = self._now()
                    device = await session.scalar(
                        select(Device)
                        .where(Device.org_id == self.org_id, Device.device_id == device_id)
                        .with_for_update()
                    )
                    if device is None:
                        device = Device(
                            org_id=self.org_id,
                            device_id=device_id,
                            name=device_record.name,
                            first_synced_at=now,
                            last_synced_at=now,
                            revoked_at=None,
                        )
                        session.add(device)
                        await session.flush()
                    else:
                        device.name = device_record.name
                        device.last_synced_at = now
                        device.revoked_at = None

                    association = DeviceAssociation(
                        user_id=user_id,
                        task_id=context.task_id,
                        org_id=self.org_id,
                        device_id=device_id,
                        state="ACTIVE",
                        started_at=now,
                        ended_at=None,
                        end_reason=None,
                        updated_at=now,
                    )
                    session.add(association)
                    await session.flush()
                    action = DeviceAction(
                        association_id=association.id,
                        kind="AUTH",
                        status="SUCCEEDED",
                        idempotency_key=uuid4().hex,
                        target_device_id=device_id,
                        result_reused=None,
                        attempt_count=0,
                        next_retry_at=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=now,
                    )
                    session.add(action)
                    await session.flush()
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_AUTHUSER_ASSOCIATED",
                        target_type="DEVICE_ASSOCIATION",
                        target_id=str(association.id),
                        parameters_summary={
                            "user_id": user.id,
                            "task_id": context.task_id,
                            "org_id": self.org_id,
                            "device_id": device_id,
                        },
                    )
                    return DeviceAuthorizationService._result(association, action)
        except IntegrityError:
            raise EligibilityError("设备或用户已被并发关联，请重新查询") from None

    async def list_devices(self) -> tuple[DeviceListEntry, ...]:
        records = await self.gateway.list_devices()
        local_records = [record for record in records if record.org_id == self.org_id]
        device_ids = tuple(sorted({record.id for record in local_records}))
        owners: dict[int, tuple[DeviceAssociation, User]] = {}
        if device_ids:
            async with self.session_factory() as session:
                async with session.begin():
                    rows = (
                        await session.execute(
                            select(DeviceAssociation, User)
                            .join(User, User.id == DeviceAssociation.user_id)
                            .where(
                                DeviceAssociation.org_id == self.org_id,
                                DeviceAssociation.device_id.in_(device_ids),
                                DeviceAssociation.ended_at.is_(None),
                            )
                            .order_by(DeviceAssociation.device_id, DeviceAssociation.id)
                        )
                    ).all()
                    for linked_association, linked_user in rows:
                        device_id = cast(int, linked_association.device_id)
                        if device_id in owners:
                            raise EligibilityError("本地设备关联状态不一致，无法确定当前用户")
                        owners[device_id] = (linked_association, linked_user)

        entries: list[DeviceListEntry] = []
        for record in local_records:
            owner = owners.get(record.id)
            owner_association = owner[0] if owner is not None else None
            owner_user = owner[1] if owner is not None else None
            entries.append(
                DeviceListEntry(
                    device_id=record.id,
                    org_id=record.org_id,
                    name=record.name,
                    revoked_at=record.revoked_at,
                    owner_user_id=owner_user.id if owner_user is not None else None,
                    owner_email=owner_user.email if owner_user is not None else None,
                    association_id=owner_association.id if owner_association is not None else None,
                    association_state=owner_association.state if owner_association is not None else None,
                )
            )
        entries.sort(key=lambda item: item.device_id)
        return tuple(entries)

    async def _check_qualification(
        self,
        session: AsyncSession,
        context: DeviceTaskContext,
        user_id: int,
        device_id: int,
        *,
        allow_existing_over_limit: bool = False,
    ) -> tuple[DeviceTaskContext, tuple[int, int] | None]:
        scope = await self._locked_scope(session, context)
        user = DeviceAuthorizationService._require_eligible_user(
            await session.scalar(select(User).where(User.id == user_id).with_for_update())
        )
        if not await DeviceAuthorizationService._scope_allows_user(session, scope, user_id):
            raise EligibilityError("用户不在当前设备任务范围内")

        open_associations = list(
            (
                await session.scalars(
                    select(DeviceAssociation)
                    .where(DeviceAssociation.user_id == user_id, DeviceAssociation.ended_at.is_(None))
                    .order_by(DeviceAssociation.id)
                    .with_for_update()
                )
            ).all()
        )
        existing: tuple[int, int] | None = None
        if open_associations:
            if len(open_associations) != 1:
                raise EligibilityError("用户存在多个未结束设备关联")
            association = open_associations[0]
            if (
                association.task_id != context.task_id
                or association.org_id != self.org_id
                or association.state != "ACTIVE"
                or association.device_id != device_id
            ):
                raise EligibilityError("用户已有其他未结束设备关联")
            action = await session.scalar(
                select(DeviceAction)
                .where(
                    DeviceAction.association_id == association.id,
                    DeviceAction.kind == "AUTH",
                    DeviceAction.status == "SUCCEEDED",
                    DeviceAction.target_device_id == device_id,
                )
                .order_by(DeviceAction.id.desc())
                .limit(1)
                .with_for_update()
            )
            if action is None:
                raise EligibilityError("当前设备关联缺少成功记录，不能按幂等结果返回")
            existing = (association.id, action.id)

        active_device_query = select(DeviceAssociation.id).where(
            DeviceAssociation.org_id == self.org_id,
            DeviceAssociation.device_id == device_id,
            DeviceAssociation.ended_at.is_(None),
        )
        if existing is not None:
            active_device_query = active_device_query.where(DeviceAssociation.id != existing[0])
        active_device = await session.scalar(active_device_query.limit(1).with_for_update())
        if active_device is not None:
            raise EligibilityError("该设备已有关联用户")

        current_context = await self._current_task_context(session, context)
        if existing is None or not allow_existing_over_limit:
            await self.quota_check(session, user, current_context, self._now())
        return current_context, existing

    async def _current_import_cycle(
        self,
        session: AsyncSession,
        task_id: int,
        now: datetime,
    ) -> DeviceQuotaCycle:
        cycle = await session.scalar(
            select(DeviceQuotaCycle)
            .where(
                DeviceQuotaCycle.task_id == task_id,
                DeviceQuotaCycle.status == "VERIFIED",
                DeviceQuotaCycle.started_at <= now,
                DeviceQuotaCycle.reset_at > now,
            )
            .order_by(DeviceQuotaCycle.reset_at.desc())
            .limit(1)
            .with_for_update()
        )
        if cycle is None:
            raise EligibilityError("当前设备周期尚未核实，不能导入已用金额")
        return cycle

    async def _ensure_import_history_is_empty(
        self,
        session: AsyncSession,
        user_id: int,
        cycle_id: int,
    ) -> None:
        prior_association = await session.scalar(
            select(DeviceAssociation.id).where(DeviceAssociation.user_id == user_id).limit(1).with_for_update()
        )
        existing_ledger = await session.scalar(
            select(DeviceCycleLedger.id)
            .where(DeviceCycleLedger.user_id == user_id, DeviceCycleLedger.cycle_id == cycle_id)
            .limit(1)
            .with_for_update()
        )
        if prior_association is not None or existing_ledger is not None:
            raise EligibilityError("--used 仅支持当前周期没有设备关联或账本历史的首次关联")

    async def _require_matching_import(
        self,
        session: AsyncSession,
        existing: tuple[int, int],
        used_usd: Decimal,
    ) -> DeviceAuthorizationResult:
        association_id, action_id = existing
        association = await session.get(DeviceAssociation, association_id, with_for_update=True)
        action = await session.get(DeviceAction, action_id, with_for_update=True)
        if association is None or action is None:
            raise EligibilityError("现有设备关联已变化，请重新查询")
        amounts = list(
            (
                await session.scalars(
                    select(DeviceUsageSegment.imported_used_usd)
                    .where(
                        DeviceUsageSegment.association_id == association.id,
                        DeviceUsageSegment.imported_used_usd.is_not(None),
                    )
                    .order_by(DeviceUsageSegment.id)
                    .with_for_update()
                )
            ).all()
        )
        if len(amounts) != 1 or amounts[0] != used_usd:
            raise EligibilityError("该设备关联已存在；只有原导入金额完全相同的请求才可幂等返回")
        if (
            association.state != "ACTIVE"
            or association.ended_at is not None
            or action.kind != "AUTH"
            or action.status != "SUCCEEDED"
            or action.target_device_id != association.device_id
        ):
            raise EligibilityError("已导入的设备关联已变化，不能重置消费或重新导入")
        return DeviceAuthorizationService._result(association, action)

    async def _active_association_for_import(
        self,
        session: AsyncSession,
        user_id: int,
        device_id: int,
    ) -> tuple[int, int] | None:
        association = await session.scalar(
            select(DeviceAssociation)
            .where(
                DeviceAssociation.user_id == user_id,
                DeviceAssociation.device_id == device_id,
                DeviceAssociation.org_id == self.org_id,
                DeviceAssociation.ended_at.is_(None),
            )
            .with_for_update()
        )
        if association is None:
            return None
        action = await session.scalar(
            select(DeviceAction)
            .where(
                DeviceAction.association_id == association.id,
                DeviceAction.kind == "AUTH",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == device_id,
            )
            .order_by(DeviceAction.id.desc())
            .limit(1)
            .with_for_update()
        )
        return (association.id, action.id) if action is not None else None

    @staticmethod
    def _validate_import_amount(value: Decimal) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value >= _MONEY_LIMIT:
            raise EligibilityError("导入的已用金额必须是小于 100000000 的有限非负金额")
        try:
            normalized = value.quantize(_MONEY_QUANTUM)
        except (InvalidOperation, ValueError, OverflowError):
            raise EligibilityError("导入的已用金额精度无效") from None
        if normalized != value:
            raise EligibilityError("导入的已用金额最多支持 10 位小数")
        return normalized

    @staticmethod
    def _validate_usage_baseline(usage: DeviceUsage) -> tuple[Decimal, dict[str, object]]:
        if not isinstance(usage, DeviceUsage) or usage.range != "all":
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

    async def _locked_scope(self, session: AsyncSession, context: DeviceTaskContext) -> DeviceTaskScope:
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == context.task_id).with_for_update()
        )
        if scope is None or scope.org_id != self.org_id or context.org_id != self.org_id:
            raise EligibilityError("任务未配置到当前 Reclaude 组织")
        return scope

    async def _current_task_context(
        self,
        session: AsyncSession,
        context: DeviceTaskContext,
    ) -> DeviceTaskContext:
        task = await session.get(QuotaTask, context.task_id)
        if task is None:
            raise EligibilityError("设备限额任务不存在")
        return DeviceTaskContext(
            task_id=task.id,
            name=task.name,
            org_id=self.org_id,
            limit_usd=as_decimal(task.limit_usd),
            enabled=task.status == TaskStatus.RUNNING.value,
        )

    @staticmethod
    def _positive_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
