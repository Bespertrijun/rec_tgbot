from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_context import DeviceTaskContext
from reclaude_bot.domain.enums import TaskStatus, UserStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc, is_last_24h
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaAdjustment,
    DeviceQuotaCycle,
    DeviceTaskMember,
    DeviceTaskScope,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
    QuotaTask,
    ServiceState,
    User,
)

_MONEY_LIMIT = Decimal("100000000")
_ZERO = Decimal("0.0000000000")


@dataclass(frozen=True)
class DeviceQuotaStatus:
    ledger_id: int | None
    cycle_id: int | None
    used_usd: Decimal | None
    effective_limit_usd: Decimal | None
    remaining_usd: Decimal | None
    quality: str
    quota_locked: bool
    reset_at: datetime | None
    task_id: int | None = None
    task_name: str | None = None
    device_id: int | None = None
    last_sampled_at: datetime | None = None
    quota_unlocked_at: datetime | None = None
    association_state: str | None = None
    pending_action_kind: str | None = None


@dataclass(frozen=True)
class DeviceTransferResult:
    transfer_key: str
    amount_usd: Decimal
    sender_ledger_id: int
    recipient_ledger_id: int
    sender_remaining_usd: Decimal
    recipient_effective_limit_usd: Decimal


class DeviceQuotaService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        org_id: int,
        *,
        clock=utcnow,
        max_cycle_age_seconds: int | float = 90,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.session_factory = factory
        self.org_id = org_id
        self.clock = clock
        self.max_cycle_age_seconds = self._age_seconds(max_cycle_age_seconds)

    async def auth_quota_check(
        self,
        session: AsyncSession,
        user: User,
        context: DeviceTaskContext,
        now: datetime,
    ) -> None:
        moment = ensure_utc(now)
        if context.org_id != self.org_id:
            raise EligibilityError("任务不属于当前 Reclaude 组织")
        scope = await session.get(DeviceTaskScope, context.task_id)
        if scope is None or scope.org_id != self.org_id:
            raise EligibilityError("任务未配置到当前 Reclaude 组织")

        cycle = await self._current_cycle(session, context.task_id, moment, lock=True)
        if cycle is None or cycle.status != "VERIFIED":
            raise EligibilityError("当前周期来源尚未核实，暂不能授权")
        if not await self._cycle_evidence_is_fresh(session, cycle, moment):
            raise EligibilityError("周期或账号来源数据已过期，请稍后重试授权")

        ledger = await session.scalar(
            select(DeviceCycleLedger)
            .where(
                DeviceCycleLedger.user_id == user.id,
                DeviceCycleLedger.cycle_id == cycle.id,
            )
            .with_for_update()
        )
        limit = await self.effective_limit(session, user.id, cycle.id)
        history = await self._has_cycle_auth_history(session, user.id, cycle)

        if self._last_day_allowed(cycle, moment):
            if ledger is not None and ledger.quota_locked_at is not None and ledger.quota_unlocked_at is None:
                ledger.quota_unlocked_at = moment
                ledger.updated_at = moment
                await audit(
                    session,
                    actor_telegram_id=None,
                    actor_type="SYSTEM",
                    action="DEVICE_QUOTA_LAST_DAY_UNLOCKED",
                    target_type="DEVICE_CYCLE_LEDGER",
                    target_id=str(ledger.id),
                    parameters_summary={"cycle_id": cycle.id, "task_id": context.task_id},
                )
            return

        if ledger is None:
            if history:
                raise EligibilityError("本周期设备用量尚未同步，请稍后重试授权")
            if _ZERO >= limit:
                raise EligibilityError("本周期有效额度为零，暂不能授权")
            return

        if ledger.quota_locked_at is not None:
            raise EligibilityError("本周期额度锁定，暂不能授权")

        confirmed_used = ledger.confirmed_used_usd
        if confirmed_used is None:
            if history:
                raise EligibilityError("本周期设备用量尚未同步，请稍后重试授权")
            used = _ZERO
        else:
            used = self._money(confirmed_used)
            if ledger.quality != "VERIFIED":
                if used >= limit:
                    raise EligibilityError("已确认用量达到额度，暂不能授权")
                raise EligibilityError("本周期设备用量待核对，暂不能授权")

        if used >= limit:
            raise EligibilityError("已达到本周期有效额度，暂不能授权")

    async def quota_check(
        self,
        session: AsyncSession,
        user: User,
        context: DeviceTaskContext,
        now: datetime,
    ) -> None:
        await self.auth_quota_check(session, user, context, now)

    async def effective_limit(self, session: AsyncSession, user_id: int, cycle_id: int) -> Decimal:
        cycle = await session.get(DeviceQuotaCycle, cycle_id)
        if cycle is None:
            raise EligibilityError("设备周期不存在")
        scope = await session.get(DeviceTaskScope, cycle.task_id)
        if scope is None or scope.org_id != self.org_id:
            raise EligibilityError("设备周期不属于当前组织")
        task = await session.get(QuotaTask, cycle.task_id)
        user = await session.get(User, user_id)
        if task is None or user is None:
            raise EligibilityError("额度用户或任务不存在")

        ledger_id = await session.scalar(
            select(DeviceCycleLedger.id).where(
                DeviceCycleLedger.user_id == user_id,
                DeviceCycleLedger.cycle_id == cycle_id,
            )
        )
        adjustments: Sequence[DeviceQuotaAdjustment] = ()
        if ledger_id is not None:
            adjustments = (
                await session.scalars(
                    select(DeviceQuotaAdjustment).where(DeviceQuotaAdjustment.ledger_id == ledger_id)
                )
            ).all()
        amount = self._money(task.limit_usd) + sum(
            (self._money(item.amount_usd, allow_negative=True) for item in adjustments), _ZERO
        )
        return max(_ZERO, amount)

    async def effective_limit_for(
        self,
        user_id: int,
        cycle_id: int,
    ) -> Decimal:
        async with self.session_factory() as session:
            return await self.effective_limit(session, user_id, cycle_id)

    async def status(
        self,
        user_id: int,
        *,
        task_id: int | None = None,
        now: datetime | None = None,
    ) -> DeviceQuotaStatus:
        self._positive_id(user_id, "用户 ID")
        if task_id is not None:
            self._positive_id(task_id, "任务 ID")
        moment = ensure_utc(now or self.clock())
        async with self.session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise EligibilityError("用户不存在")
            association_state = await self._current_association(session, user_id)
            resolved_task_id = task_id or await self._status_task_id(session, user_id)
            if resolved_task_id is None:
                return DeviceQuotaStatus(
                    None,
                    None,
                    None,
                    None,
                    None,
                    "UNKNOWN",
                    False,
                    None,
                    device_id=association_state[0],
                    association_state=association_state[1],
                    pending_action_kind=association_state[2],
                )
            scope = await session.get(DeviceTaskScope, resolved_task_id)
            task = await session.get(QuotaTask, resolved_task_id)
            if scope is None or scope.org_id != self.org_id or task is None:
                raise EligibilityError("任务不属于当前 Reclaude 组织")

            cycle = await self._current_cycle(session, task.id, moment, lock=False)
            if cycle is None:
                cycle = await session.scalar(
                    select(DeviceQuotaCycle)
                    .where(DeviceQuotaCycle.task_id == task.id)
                    .order_by(DeviceQuotaCycle.reset_at.desc())
                    .limit(1)
                )
            if cycle is None:
                return DeviceQuotaStatus(
                    None,
                    None,
                    _ZERO,
                    self._money(task.limit_usd),
                    self._money(task.limit_usd),
                    "UNKNOWN",
                    False,
                    None,
                    task_id=task.id,
                    task_name=task.name,
                    device_id=association_state[0],
                    association_state=association_state[1],
                    pending_action_kind=association_state[2],
                )

            ledger = await session.scalar(
                select(DeviceCycleLedger).where(
                    DeviceCycleLedger.user_id == user_id,
                    DeviceCycleLedger.cycle_id == cycle.id,
                )
            )
            history = await self._has_cycle_auth_history(session, user_id, cycle)
            limit = await self.effective_limit(session, user_id, cycle.id)
            if ledger is None:
                used = None if history else _ZERO
                quality = "NEEDS_REVIEW" if history else "UNKNOWN"
                ledger_id = None
                locked = False
                unlocked_at = None
            else:
                ledger_id = ledger.id
                quality = ledger.quality
                used = (
                    self._money(ledger.confirmed_used_usd)
                    if ledger.confirmed_used_usd is not None
                    else (None if history else _ZERO)
                )
                if ledger.confirmed_used_usd is None and not history:
                    quality = "UNKNOWN"
                locked = ledger.quota_locked_at is not None and ledger.quota_unlocked_at is None
                unlocked_at = ensure_utc(ledger.quota_unlocked_at) if ledger.quota_unlocked_at is not None else None
            remaining = max(_ZERO, limit - used) if used is not None else None
            return DeviceQuotaStatus(
                ledger_id=ledger_id,
                cycle_id=cycle.id,
                used_usd=used,
                effective_limit_usd=limit,
                remaining_usd=remaining,
                quality=quality,
                quota_locked=locked,
                reset_at=ensure_utc(cycle.reset_at),
                task_id=task.id,
                task_name=task.name,
                device_id=association_state[0],
                last_sampled_at=await self._last_sampled_at(session, ledger_id),
                quota_unlocked_at=unlocked_at,
                association_state=association_state[1],
                pending_action_kind=association_state[2],
            )

    async def adjust(
        self,
        user_id: int,
        cycle_id: int,
        amount: Decimal,
        reason: str,
        operator_id: int,
        *,
        operation_key: str,
    ) -> DeviceQuotaStatus:
        self._positive_id(user_id, "用户 ID")
        self._positive_id(cycle_id, "周期 ID")
        self._positive_id(operator_id, "管理员 ID")
        resolved_amount = self._money(amount, allow_negative=True)
        if not isinstance(reason, str) or not reason.strip():
            raise EligibilityError("调整原因不能为空")
        key = self._operation_key("adjust", operation_key)
        cycle_task_id = await self._cycle_task_id(cycle_id)
        now = self._now()

        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == cycle_task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                cycle = await session.scalar(
                    select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
                )
                if scope is None or scope.org_id != self.org_id or cycle is None or cycle.task_id != cycle_task_id:
                    raise EligibilityError("周期不属于当前 Reclaude 组织")
                if user is None:
                    raise EligibilityError("用户不存在")
                ledger = await self._locked_ledger(session, user.id, cycle)
                existing = await session.scalar(
                    select(DeviceQuotaAdjustment).where(DeviceQuotaAdjustment.operation_key == key)
                )
                if existing is not None:
                    if (
                        existing.ledger_id != ledger.id
                        or self._money(existing.amount_usd, allow_negative=True) != resolved_amount
                        or existing.reason != reason.strip()
                    ):
                        raise EligibilityError("operation_key 已用于不同的额度调整")
                else:
                    current = await self.effective_limit(session, user.id, cycle.id)
                    if current + resolved_amount < 0:
                        raise EligibilityError("额度调整不能使有效额度为负数")
                    session.add(
                        DeviceQuotaAdjustment(
                            ledger_id=ledger.id,
                            amount_usd=resolved_amount,
                            reason=reason.strip(),
                            operator_telegram_id=operator_id,
                            operation_key=key,
                            transfer_key=None,
                            created_at=now,
                        )
                    )
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_QUOTA_ADJUSTMENT",
                        target_type="DEVICE_CYCLE_LEDGER",
                        target_id=str(ledger.id),
                        parameters_summary={"cycle_id": cycle.id, "amount_usd": str(resolved_amount), "reason": reason.strip()},
                    )
        return await self.status(user_id, task_id=cycle_task_id, now=now)

    async def transfer(
        self,
        sender_user_id: int,
        recipient_user_id: int,
        cycle_id: int,
        amount: Decimal,
        *,
        operation_key: str,
    ) -> DeviceTransferResult:
        self._positive_id(sender_user_id, "转出用户 ID")
        self._positive_id(recipient_user_id, "转入用户 ID")
        self._positive_id(cycle_id, "周期 ID")
        if sender_user_id == recipient_user_id:
            raise EligibilityError("不能转账给自己")
        transfer_amount = self._money(amount)
        if transfer_amount <= 0:
            raise EligibilityError("转账金额必须大于零")
        raw_key = self._operation_key("transfer", operation_key)
        transfer_key = sha256(raw_key.encode()).hexdigest()
        out_key = sha256(f"{raw_key}:out".encode()).hexdigest()
        in_key = sha256(f"{raw_key}:in".encode()).hexdigest()
        cycle_task_id = await self._cycle_task_id(cycle_id)
        now = self._now()
        user_ids = sorted((sender_user_id, recipient_user_id))

        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == cycle_task_id).with_for_update()
                )
                users = list(
                    (
                        await session.scalars(
                            select(User).where(User.id.in_(user_ids)).order_by(User.id).with_for_update()
                        )
                    ).all()
                )
                cycle = await session.scalar(
                    select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
                )
                if (
                    scope is None
                    or scope.org_id != self.org_id
                    or cycle is None
                    or cycle.task_id != cycle_task_id
                ):
                    raise EligibilityError("周期不属于当前 Reclaude 组织")
                if (
                    cycle.status != "VERIFIED"
                    or not ensure_utc(cycle.started_at) <= now < ensure_utc(cycle.reset_at)
                ):
                    raise EligibilityError("仅可在当前已核实周期内转赠额度")
                if len(users) != 2:
                    raise EligibilityError("转账用户不存在")
                by_id = {user.id: user for user in users}
                sender = by_id[sender_user_id]
                recipient = by_id[recipient_user_id]
                for user in (sender, recipient):
                    if user.binding_status != "BOUND" or user.status != UserStatus.ACTIVE.value:
                        raise EligibilityError("转账双方必须是已绑定且未禁用的用户")
                    if not await self._user_in_scope(session, scope, user.id):
                        raise EligibilityError("转账双方必须属于当前任务范围")

                ledgers = {
                    user.id: await self._locked_ledger(session, user.id, cycle)
                    for user in sorted(users, key=lambda item: item.id)
                }
                existing_rows = list(
                    (
                        await session.scalars(
                            select(DeviceQuotaAdjustment).where(
                                DeviceQuotaAdjustment.operation_key.in_((out_key, in_key))
                            )
                        )
                    ).all()
                )
                if existing_rows:
                    existing_transfer = True
                    out_row = next((row for row in existing_rows if row.operation_key == out_key), None)
                    in_row = next((row for row in existing_rows if row.operation_key == in_key), None)
                    if (
                        len(existing_rows) != 2
                        or out_row is None
                        or in_row is None
                        or out_row.ledger_id != ledgers[sender.id].id
                        or in_row.ledger_id != ledgers[recipient.id].id
                        or out_row.transfer_key != transfer_key
                        or in_row.transfer_key != transfer_key
                        or out_row.amount_usd != -transfer_amount
                        or in_row.amount_usd != transfer_amount
                    ):
                        raise EligibilityError("operation_key 已用于不同的转账")
                else:
                    existing_transfer = False
                    sender_ledger = ledgers[sender.id]
                    recipient_ledger = ledgers[recipient.id]
                    if sender_ledger.quota_locked_at is not None:
                        raise EligibilityError("额度已锁定，不能转出额度")
                    sender_limit = await self.effective_limit(session, sender.id, cycle.id)
                    recipient_limit = await self.effective_limit(session, recipient.id, cycle.id)
                    sender_usage = await self._transfer_usage(session, sender.id, cycle, sender_ledger, sender_limit)
                    remaining = max(_ZERO, sender_limit - sender_usage)
                    if transfer_amount > remaining:
                        raise EligibilityError("转账金额超过当前可转出额度")
                    if recipient_limit + transfer_amount >= _MONEY_LIMIT:
                        raise EligibilityError("转入后有效额度超过可表示范围")
                    session.add_all(
                        [
                            DeviceQuotaAdjustment(
                                ledger_id=sender_ledger.id,
                                amount_usd=-transfer_amount,
                                reason="额度转出",
                                operator_telegram_id=sender.telegram_user_id,
                                operation_key=out_key,
                                transfer_key=transfer_key,
                                created_at=now,
                            ),
                            DeviceQuotaAdjustment(
                                ledger_id=recipient_ledger.id,
                                amount_usd=transfer_amount,
                                reason="额度转入",
                                operator_telegram_id=sender.telegram_user_id,
                                operation_key=in_key,
                                transfer_key=transfer_key,
                                created_at=now,
                            ),
                        ]
                    )
                    await audit(
                        session,
                        actor_telegram_id=sender.telegram_user_id,
                        actor_type="USER",
                        action="DEVICE_QUOTA_TRANSFER",
                        target_type="DEVICE_CYCLE_LEDGER",
                        target_id=str(sender_ledger.id),
                        parameters_summary={
                            "cycle_id": cycle.id,
                            "recipient_ledger_id": recipient_ledger.id,
                            "amount_usd": str(transfer_amount),
                            "transfer_key": transfer_key,
                        },
                    )
                sender_limit = await self.effective_limit(session, sender.id, cycle.id)
                recipient_limit = await self.effective_limit(session, recipient.id, cycle.id)
                if existing_transfer:
                    sender_confirmed = ledgers[sender.id].confirmed_used_usd
                    sender_usage = self._money(sender_confirmed) if sender_confirmed is not None else _ZERO
                else:
                    sender_usage = await self._transfer_usage(
                        session,
                        sender.id,
                        cycle,
                        ledgers[sender.id],
                        sender_limit,
                        allow_locked=True,
                    )
                remaining = max(_ZERO, sender_limit - sender_usage)
                return DeviceTransferResult(
                    transfer_key=transfer_key,
                    amount_usd=transfer_amount,
                    sender_ledger_id=ledgers[sender.id].id,
                    recipient_ledger_id=ledgers[recipient.id].id,
                    sender_remaining_usd=remaining,
                    recipient_effective_limit_usd=recipient_limit,
                )

    async def mark_quota_lock_if_reached(
        self,
        session: AsyncSession,
        ledger: DeviceCycleLedger,
        cycle: DeviceQuotaCycle,
        task: QuotaTask,
        now: datetime,
    ) -> bool:
        moment = ensure_utc(now)
        if (
            task.status != TaskStatus.RUNNING.value
            or task.id != cycle.task_id
            or cycle.status != "VERIFIED"
            or not cycle.started_at <= moment < cycle.reset_at
            or await self.last_day_allowed(session, cycle, moment)
            or ledger.confirmed_used_usd is None
        ):
            return False
        used = self._money(ledger.confirmed_used_usd)
        limit = await self.effective_limit(session, ledger.user_id, cycle.id)
        if used < limit:
            return False
        if ledger.quota_locked_at is None:
            ledger.quota_locked_at = moment
            ledger.quota_unlocked_at = None
            ledger.updated_at = moment
        return True

    async def _transfer_usage(
        self,
        session: AsyncSession,
        user_id: int,
        cycle: DeviceQuotaCycle,
        ledger: DeviceCycleLedger,
        limit: Decimal,
        *,
        allow_locked: bool = False,
    ) -> Decimal:
        history = await self._has_cycle_auth_history(session, user_id, cycle)
        if ledger.confirmed_used_usd is None:
            if history:
                raise EligibilityError("本周期设备用量待同步，暂不能转出")
            return _ZERO
        used = self._money(ledger.confirmed_used_usd)
        if ledger.quality != "VERIFIED":
            if used >= limit:
                return used
            raise EligibilityError("本周期设备用量待核对，暂不能转出")
        if ledger.quota_locked_at is not None and not allow_locked:
            raise EligibilityError("额度已锁定，不能转出额度")
        return used

    async def _locked_ledger(
        self,
        session: AsyncSession,
        user_id: int,
        cycle: DeviceQuotaCycle,
    ) -> DeviceCycleLedger:
        ledger = await session.scalar(
            select(DeviceCycleLedger)
            .where(DeviceCycleLedger.user_id == user_id, DeviceCycleLedger.cycle_id == cycle.id)
            .with_for_update()
        )
        if ledger is None:
            now = self._now()
            ledger = DeviceCycleLedger(
                user_id=user_id,
                cycle_id=cycle.id,
                task_id=cycle.task_id,
                confirmed_used_usd=None,
                quality="UNKNOWN",
                quota_locked_at=None,
                quota_unlocked_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(ledger)
            await session.flush()
        elif ledger.task_id != cycle.task_id:
            raise EligibilityError("设备周期账本任务范围不匹配")
        return ledger

    async def _cycle_task_id(self, cycle_id: int) -> int:
        async with self.session_factory() as session:
            task_id = await session.scalar(
                select(DeviceQuotaCycle.task_id).where(DeviceQuotaCycle.id == cycle_id)
            )
            if task_id is None:
                raise EligibilityError("设备周期不存在")
            return task_id

    async def _current_cycle(
        self,
        session: AsyncSession,
        task_id: int,
        now: datetime,
        *,
        lock: bool,
    ) -> DeviceQuotaCycle | None:
        statement = (
            select(DeviceQuotaCycle)
            .where(
                DeviceQuotaCycle.task_id == task_id,
                DeviceQuotaCycle.started_at <= ensure_utc(now),
                DeviceQuotaCycle.reset_at > ensure_utc(now),
            )
            .order_by(DeviceQuotaCycle.reset_at.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def _cycle_evidence_is_fresh(
        self,
        session: AsyncSession,
        cycle: DeviceQuotaCycle,
        now: datetime,
    ) -> bool:
        if (
            cycle.last_day_checked_at is None
            or cycle.account_id is None
            or not cycle.account_id.strip()
            or cycle.weekly_percent is None
            or not cycle.weekly_percent.is_finite()
            or cycle.weekly_percent < 0
            or cycle.weekly_percent >= _MONEY_LIMIT
        ):
            return False
        checked_at = ensure_utc(cycle.last_day_checked_at)
        age = ensure_utc(now) - checked_at
        if age < timedelta(0) or age > timedelta(seconds=self.max_cycle_age_seconds):
            return False
        state = await session.get(ServiceState, 1)
        return bool(
            state is not None
            and not (state.reason.startswith("account_reconcile_") and state.reason != "account_reconciled")
            and state.selected_account_id is not None
            and str(state.selected_account_id).strip() == cycle.account_id.strip()
        )

    async def _status_task_id(self, session: AsyncSession, user_id: int) -> int | None:
        association = await session.scalar(
            select(DeviceAssociation.task_id)
            .where(DeviceAssociation.user_id == user_id, DeviceAssociation.org_id == self.org_id)
            .order_by(DeviceAssociation.ended_at.is_not(None), DeviceAssociation.id.desc())
            .limit(1)
        )
        if association is not None:
            return association
        ledger_task = await session.scalar(
            select(DeviceCycleLedger.task_id)
            .join(DeviceQuotaCycle, DeviceQuotaCycle.id == DeviceCycleLedger.cycle_id)
            .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceCycleLedger.task_id)
            .where(DeviceCycleLedger.user_id == user_id, DeviceTaskScope.org_id == self.org_id)
            .order_by(DeviceQuotaCycle.reset_at.desc())
            .limit(1)
        )
        if ledger_task is not None:
            return ledger_task
        return await session.scalar(
            select(DeviceTaskScope.task_id).where(DeviceTaskScope.org_id == self.org_id).limit(1)
        )

    async def _current_association(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> tuple[int | None, str | None, str | None]:
        association = await session.scalar(
            select(DeviceAssociation)
            .where(
                DeviceAssociation.user_id == user_id,
                DeviceAssociation.org_id == self.org_id,
                DeviceAssociation.ended_at.is_(None),
            )
            .order_by(DeviceAssociation.id.desc())
            .limit(1)
        )
        if association is None:
            return None, None, None
        action_kind = await session.scalar(
            select(DeviceAction.kind)
            .where(
                DeviceAction.association_id == association.id,
                DeviceAction.status.in_(("PENDING", "UNKNOWN")),
            )
            .order_by(DeviceAction.id.desc())
            .limit(1)
        )
        return association.device_id, association.state, action_kind

    async def _last_sampled_at(self, session: AsyncSession, ledger_id: int | None) -> datetime | None:
        if ledger_id is None:
            return None
        value = await session.scalar(
            select(DeviceUsageSnapshot.sampled_at)
            .join(
                DeviceUsageSegment,
                and_(
                    DeviceUsageSegment.ledger_id == ledger_id,
                    DeviceUsageSegment.latest_sampled_at == DeviceUsageSnapshot.sampled_at,
                ),
            )
            .where(DeviceUsageSnapshot.org_id == self.org_id, DeviceUsageSnapshot.range == "all")
            .order_by(DeviceUsageSnapshot.sampled_at.desc())
            .limit(1)
        )
        return ensure_utc(value) if value is not None else None

    async def _has_cycle_auth_history(
        self,
        session: AsyncSession,
        user_id: int,
        cycle: DeviceQuotaCycle,
    ) -> bool:
        auth_evidence = exists(
            select(1).where(
                DeviceAction.association_id == DeviceAssociation.id,
                DeviceAction.kind == "AUTH",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == DeviceAssociation.device_id,
            )
        )
        return (
            await session.scalar(
                select(DeviceAssociation.id)
                .where(
                    DeviceAssociation.user_id == user_id,
                    DeviceAssociation.task_id == cycle.task_id,
                    DeviceAssociation.org_id == self.org_id,
                    DeviceAssociation.device_id.is_not(None),
                    DeviceAssociation.started_at < cycle.reset_at,
                    or_(
                        DeviceAssociation.ended_at.is_(None),
                        DeviceAssociation.ended_at > cycle.started_at,
                    ),
                    auth_evidence,
                )
                .limit(1)
            )
            is not None
        )

    async def _user_in_scope(self, session: AsyncSession, scope: DeviceTaskScope, user_id: int) -> bool:
        if scope.scope_mode == "ALL":
            return True
        listed = await session.scalar(
            select(DeviceTaskMember.user_id).where(
                DeviceTaskMember.task_id == scope.task_id,
                DeviceTaskMember.user_id == user_id,
            )
        )
        if scope.scope_mode == "ALLOWLIST":
            return listed is not None
        if scope.scope_mode == "EXCLUDE":
            return listed is None
        raise EligibilityError("任务成员范围配置无效")

    @staticmethod
    def _last_day_allowed(cycle: DeviceQuotaCycle, now: datetime) -> bool:
        percent = cycle.weekly_percent
        return bool(
            cycle.status == "VERIFIED"
            and cycle.account_id is not None
            and cycle.last_day_allow is True
            and percent is not None
            and percent.is_finite()
            and Decimal("0") <= percent < Decimal("100")
            and is_last_24h(now, cycle.reset_at)
        )

    async def last_day_allowed(
        self,
        session: AsyncSession,
        cycle: DeviceQuotaCycle,
        now: datetime,
    ) -> bool:
        return bool(
            self._last_day_allowed(cycle, now)
            and await self._cycle_evidence_is_fresh(session, cycle, ensure_utc(now))
        )

    @staticmethod
    def _money(value: Decimal, *, allow_negative: bool = False) -> Decimal:
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value >= _MONEY_LIMIT
            or value <= -_MONEY_LIMIT
        ):
            raise EligibilityError("额度金额无效")
        if not allow_negative and value < 0:
            raise EligibilityError("额度金额不能为负数")
        try:
            normalized = value.quantize(Decimal("0.0000000001"))
        except (InvalidOperation, ValueError, OverflowError):
            raise EligibilityError("额度金额精度无效") from None
        if normalized != value:
            raise EligibilityError("额度金额精度无效")
        return normalized

    @staticmethod
    def _operation_key(purpose: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise EligibilityError("operation_key 必须是 1 到 128 个字符的非空字符串")
        return sha256(f"device-{purpose}:{value}".encode()).hexdigest()

    @staticmethod
    def _positive_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    def _now(self) -> datetime:
        try:
            return ensure_utc(self.clock())
        except (TypeError, ValueError, OverflowError):
            raise EligibilityError("当前时间无效") from None

    @staticmethod
    def _age_seconds(value: int | float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EligibilityError("max_cycle_age_seconds 必须是有限的非负数")
        try:
            seconds = float(value)
        except OverflowError:
            raise EligibilityError("max_cycle_age_seconds 必须是有限的非负数") from None
        if not math.isfinite(seconds) or seconds < 0:
            raise EligibilityError("max_cycle_age_seconds 必须是有限的非负数")
        return seconds
