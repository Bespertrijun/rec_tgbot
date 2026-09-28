from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import (
    DeviceAction,
    DeviceAssociation,
    DeviceCycleLedger,
    DeviceQuotaCycle,
    DeviceTaskScope,
    DeviceUsageSegment,
    DeviceUsageSnapshot,
    User,
)

_MONEY_LIMIT = Decimal("100000000")
_ZERO = Decimal("0.0000000000")


@dataclass(frozen=True)
class DeviceLedgerResult:
    ledger_id: int
    segment_id: int
    confirmed_used_usd: Decimal | None
    quality: str


@dataclass(frozen=True)
class _AssociationHint:
    user_id: int
    task_id: int


@dataclass(frozen=True)
class _LockedContext:
    scope: DeviceTaskScope
    user: User
    association: DeviceAssociation
    auth_action: DeviceAction
    actions: tuple[DeviceAction, ...]
    cycle: DeviceQuotaCycle
    now: datetime
    cycle_started_at: datetime
    reset_at: datetime
    association_started_at: datetime
    association_ended_at: datetime | None
    segment_started_at: datetime


class DeviceLedgerService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        org_id: int,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.session_factory = factory
        self.org_id = org_id
        self.clock = clock

    async def ensure(self, association_id: int, cycle_id: int) -> DeviceLedgerResult:
        self._validate_id(association_id, "设备关联 ID")
        self._validate_id(cycle_id, "设备周期 ID")
        hint = await self._association_hint(association_id)
        now = self._now()

        async with self.session_factory() as session:
            async with session.begin():
                context = await self._locked_context(session, association_id, cycle_id, hint, now)
                ledger, segment, segments, segment_created, segment_changed = await self._locked_ledger_segment(
                    session, context
                )
                has_gap = await self._has_missing_segment(session, context, segments)
                ledger_changed = self._refresh_ledger(ledger, segments, has_gap, now)
                if segment_created or segment_changed or ledger_changed:
                    await self._write_audit(
                        session,
                        context,
                        ledger,
                        segment,
                        action=(
                            "DEVICE_USAGE_SEGMENT_CREATED"
                            if segment_created
                            else "DEVICE_USAGE_SEGMENT_RECONCILED"
                            if segment_changed
                            else "DEVICE_USAGE_LEDGER_REVIEW_REQUIRED"
                        ),
                    )
                return self._result(ledger, segment)

    async def apply(self, association_id: int, cycle_id: int, snapshot_id: int) -> DeviceLedgerResult:
        self._validate_id(association_id, "设备关联 ID")
        self._validate_id(cycle_id, "设备周期 ID")
        self._validate_id(snapshot_id, "用量快照 ID")
        hint = await self._association_hint(association_id)
        now = self._now()

        async with self.session_factory() as session:
            async with session.begin():
                context = await self._locked_context(session, association_id, cycle_id, hint, now)
                ledger, segment, segments, segment_created, segment_changed = await self._locked_ledger_segment(
                    session, context
                )
                snapshot = await session.get(DeviceUsageSnapshot, snapshot_id)
                if snapshot is None:
                    raise EligibilityError("设备用量快照不存在")
                self._validate_snapshot(snapshot, context, segment, now)

                if await self._superseded_at_snapshot(session, context, snapshot):
                    raise EligibilityError("用量快照采集时设备已属于后续关联")

                revoke_confirmed = self._confirmed_revoke_before_reset(context)
                has_successor = await self._has_successor_association(session, context)

                sampled_at = ensure_utc(snapshot.sampled_at)
                ended_before_sample = (
                    context.association_ended_at is not None
                    and sampled_at > context.association_ended_at
                )
                ended_sample_is_confirmed = ended_before_sample and revoke_confirmed
                normal_window = (
                    context.cycle.status == "VERIFIED"
                    and now < context.reset_at
                    and sampled_at < context.reset_at
                    and not (ended_before_sample and not ended_sample_is_confirmed)
                )
                delayed_final_window = (
                    (context.cycle.status == "EXPIRED" or now >= context.reset_at)
                    and revoke_confirmed
                    and not has_successor
                )

                if not normal_window and not delayed_final_window:
                    if segment.quality != "NEEDS_REVIEW":
                        segment.quality = "NEEDS_REVIEW"
                        segment_changed = True
                    ledger_changed = self._refresh_ledger(
                        ledger,
                        segments,
                        await self._has_missing_segment(session, context, segments),
                        now,
                    )
                    if segment_created or segment_changed or ledger_changed:
                        await self._write_audit(
                            session,
                            context,
                            ledger,
                            segment,
                            action=(
                                "DEVICE_USAGE_SEGMENT_CREATED"
                                if segment_created
                                else "DEVICE_USAGE_BOUNDARY_REVIEW"
                            ),
                            snapshot_id=snapshot.id,
                        )
                    return self._result(ledger, segment)

                zero_origin = await self._has_verified_zero_origin(session, context)
                snapshot_changed = self._apply_sample(segment, snapshot, zero_origin)
                segment_changed = segment_changed or snapshot_changed
                has_gap = await self._has_missing_segment(session, context, segments)
                ledger_changed = self._refresh_ledger(ledger, segments, has_gap, now)
                if segment_created or segment_changed or ledger_changed:
                    await self._write_audit(
                        session,
                        context,
                        ledger,
                        segment,
                        action=(
                            "DEVICE_USAGE_SEGMENT_CREATED"
                            if segment_created
                            else "DEVICE_USAGE_APPLIED"
                            if snapshot_changed
                            else "DEVICE_USAGE_LEDGER_REVIEW_REQUIRED"
                        ),
                        snapshot_id=snapshot.id,
                    )
                return self._result(ledger, segment)

    async def import_initial_usage(
        self,
        session: AsyncSession,
        association_id: int,
        cycle_id: int,
        snapshot: DeviceUsageSnapshot,
        imported_used_usd: Decimal,
        operator_id: int,
    ) -> DeviceLedgerResult:
        self._validate_id(association_id, "设备关联 ID")
        self._validate_id(cycle_id, "设备周期 ID")
        self._validate_id(operator_id, "管理员 ID")
        imported = self._stored_money(imported_used_usd)
        baseline = self._stored_money(snapshot.total_usd)
        now = self._now()

        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == association_id).with_for_update()
        )
        if association is None or association.org_id != self.org_id or association.ended_at is not None:
            raise EligibilityError("待导入的设备关联已变化")
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == association.task_id).with_for_update()
        )
        user = await session.scalar(select(User).where(User.id == association.user_id).with_for_update())
        cycle = await session.scalar(
            select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
        )
        if (
            scope is None
            or scope.org_id != self.org_id
            or user is None
            or association.state != "ACTIVE"
            or association.device_id is None
            or cycle is None
            or cycle.task_id != association.task_id
            or cycle.status != "VERIFIED"
            or not ensure_utc(cycle.started_at) <= now < ensure_utc(cycle.reset_at)
            or not ensure_utc(cycle.started_at) <= ensure_utc(snapshot.sampled_at) < ensure_utc(cycle.reset_at)
            or ensure_utc(snapshot.sampled_at) < ensure_utc(association.started_at)
            or snapshot.org_id != self.org_id
            or snapshot.device_id != association.device_id
            or snapshot.range != "all"
        ):
            raise EligibilityError("设备消费导入周期或基线来源已变化")

        current_cycle_id = await session.scalar(
            select(DeviceQuotaCycle.id)
            .where(
                DeviceQuotaCycle.task_id == association.task_id,
                DeviceQuotaCycle.status == "VERIFIED",
                DeviceQuotaCycle.started_at <= now,
                DeviceQuotaCycle.reset_at > now,
            )
            .order_by(DeviceQuotaCycle.reset_at.desc())
            .limit(1)
            .with_for_update()
        )
        if current_cycle_id != cycle.id:
            raise EligibilityError("设备周期在基线查询期间发生变化，请重新执行 /authuser")

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
            raise EligibilityError("设备消费导入缺少成功授权记录")

        prior_association = await session.scalar(
            select(DeviceAssociation.id)
            .where(DeviceAssociation.user_id == user.id, DeviceAssociation.id != association.id)
            .limit(1)
            .with_for_update()
        )
        existing_ledger = await session.scalar(
            select(DeviceCycleLedger.id)
            .where(
                DeviceCycleLedger.user_id == user.id,
                DeviceCycleLedger.cycle_id == cycle.id,
            )
            .with_for_update()
        )
        if prior_association is not None or existing_ledger is not None:
            raise EligibilityError("本次导入仅支持当前周期内没有既有关联或账本历史的首次关联")

        session.add(snapshot)
        await session.flush()
        ledger = DeviceCycleLedger(
            user_id=user.id,
            cycle_id=cycle.id,
            task_id=association.task_id,
            confirmed_used_usd=imported,
            quality="VERIFIED",
            quota_locked_at=None,
            quota_unlocked_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(ledger)
        await session.flush()
        segment = DeviceUsageSegment(
            association_id=association.id,
            user_id=user.id,
            task_id=association.task_id,
            ledger_id=ledger.id,
            started_at=ensure_utc(association.started_at),
            ended_at=None,
            baseline_total_usd=baseline,
            baseline_captured_at=ensure_utc(snapshot.sampled_at),
            latest_total_usd=baseline,
            latest_sampled_at=ensure_utc(snapshot.sampled_at),
            confirmed_used_usd=imported,
            imported_used_usd=imported,
            quality="VERIFIED",
        )
        session.add(segment)
        await session.flush()
        self._refresh_ledger(ledger, [segment], False, now)
        await audit(
            session,
            actor_telegram_id=operator_id,
            actor_type="ADMIN",
            action="DEVICE_USAGE_IMPORTED",
            target_type="DEVICE_CYCLE_LEDGER",
            target_id=str(ledger.id),
            result=ledger.quality,
            parameters_summary={
                "cycle_id": cycle.id,
                "association_id": association.id,
                "device_id": association.device_id,
                "baseline_total_usd": str(baseline),
                "imported_used_usd": str(imported),
                "sampled_at": ensure_utc(snapshot.sampled_at).isoformat(),
            },
        )
        return self._result(ledger, segment)

    async def _association_hint(self, association_id: int) -> _AssociationHint:
        async with self.session_factory() as session:
            association = await session.get(DeviceAssociation, association_id)
            if association is None:
                raise EligibilityError("设备关联不存在")
            return _AssociationHint(user_id=association.user_id, task_id=association.task_id)

    async def _locked_context(
        self,
        session: AsyncSession,
        association_id: int,
        cycle_id: int,
        hint: _AssociationHint,
        now: datetime,
    ) -> _LockedContext:
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == hint.task_id).with_for_update()
        )
        user = await session.scalar(select(User).where(User.id == hint.user_id).with_for_update())
        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == association_id).with_for_update()
        )
        if scope is None or user is None or association is None:
            raise EligibilityError("设备关联归属记录不完整")
        if association.user_id != hint.user_id or association.task_id != hint.task_id:
            raise EligibilityError("设备关联归属在核对期间发生变化")
        if scope.org_id != self.org_id or association.org_id != self.org_id or association.org_id != scope.org_id:
            raise EligibilityError("设备关联不属于当前 Reclaude 组织")
        if association.state not in {"ACTIVE", "PENDING_REVOKE", "UNKNOWN", "ENDED"}:
            raise EligibilityError("设备关联状态没有已确认的设备授权")
        if association.device_id is None:
            raise EligibilityError("设备关联缺少已确认的设备 ID")

        actions = tuple(
            (
                await session.scalars(
                    select(DeviceAction)
                    .where(DeviceAction.association_id == association.id)
                    .order_by(DeviceAction.id)
                    .with_for_update()
                )
            ).all()
        )
        auth_action = next(
            (
                action
                for action in reversed(actions)
                if action.kind == "AUTH"
                and action.status == "SUCCEEDED"
                and action.target_device_id == association.device_id
            ),
            None,
        )
        if auth_action is None:
            raise EligibilityError("设备关联缺少匹配的成功授权记录")

        cycle = await session.scalar(
            select(DeviceQuotaCycle).where(DeviceQuotaCycle.id == cycle_id).with_for_update()
        )
        if cycle is None or cycle.task_id != association.task_id:
            raise EligibilityError("设备周期不属于当前任务")
        if cycle.status not in {"VERIFIED", "EXPIRED"}:
            raise EligibilityError("设备周期状态尚未确认")

        cycle_started_at = ensure_utc(cycle.started_at)
        reset_at = ensure_utc(cycle.reset_at)
        association_started_at = ensure_utc(association.started_at)
        association_ended_at = ensure_utc(association.ended_at) if association.ended_at is not None else None
        segment_started_at = max(cycle_started_at, association_started_at)
        overlap_end = min(reset_at, association_ended_at) if association_ended_at is not None else reset_at
        if reset_at <= cycle_started_at or segment_started_at >= overlap_end:
            raise EligibilityError("设备关联与该周期没有有效时间交集")

        return _LockedContext(
            scope=scope,
            user=user,
            association=association,
            auth_action=auth_action,
            actions=actions,
            cycle=cycle,
            now=now,
            cycle_started_at=cycle_started_at,
            reset_at=reset_at,
            association_started_at=association_started_at,
            association_ended_at=association_ended_at,
            segment_started_at=segment_started_at,
        )

    async def _locked_ledger_segment(
        self,
        session: AsyncSession,
        context: _LockedContext,
    ) -> tuple[DeviceCycleLedger, DeviceUsageSegment, list[DeviceUsageSegment], bool, bool]:
        ledger = await session.scalar(
            select(DeviceCycleLedger)
            .where(
                DeviceCycleLedger.user_id == context.user.id,
                DeviceCycleLedger.cycle_id == context.cycle.id,
            )
            .with_for_update()
        )
        if ledger is None:
            ledger = DeviceCycleLedger(
                user_id=context.user.id,
                cycle_id=context.cycle.id,
                task_id=context.association.task_id,
                confirmed_used_usd=None,
                quality="UNKNOWN",
                quota_locked_at=None,
                quota_unlocked_at=None,
                created_at=context.now,
                updated_at=context.now,
            )
            session.add(ledger)
            await session.flush()
        elif ledger.task_id != context.association.task_id:
            raise EligibilityError("设备周期账本任务范围不匹配")

        segments = list(
            (
                await session.scalars(
                    select(DeviceUsageSegment)
                    .where(DeviceUsageSegment.ledger_id == ledger.id)
                    .order_by(DeviceUsageSegment.id)
                    .with_for_update()
                )
            ).all()
        )
        for existing in segments:
            if existing.user_id != ledger.user_id or existing.task_id != ledger.task_id:
                raise EligibilityError("设备关联段账本归属不匹配")

        segment = next((item for item in segments if item.association_id == context.association.id), None)
        segment_created = False
        segment_changed = False
        if segment is None:
            known_zero_origin = await self._has_verified_zero_origin(session, context)
            segment = DeviceUsageSegment(
                association_id=context.association.id,
                user_id=context.user.id,
                task_id=context.association.task_id,
                ledger_id=ledger.id,
                started_at=context.segment_started_at,
                ended_at=self._segment_end(context),
                baseline_total_usd=_ZERO if known_zero_origin else None,
                baseline_captured_at=context.association_started_at if known_zero_origin else None,
                latest_total_usd=None,
                latest_sampled_at=None,
                confirmed_used_usd=None,
                imported_used_usd=None,
                quality="UNKNOWN",
            )
            session.add(segment)
            await session.flush()
            segments.append(segment)
            segment_created = True
        else:
            if (
                segment.user_id != context.user.id
                or segment.task_id != context.association.task_id
                or segment.started_at is None
                or ensure_utc(segment.started_at) != context.segment_started_at
            ):
                raise EligibilityError("现有设备关联段与周期身份不匹配")
            segment_changed = self._close_segment_if_needed(segment, context)

        return ledger, segment, segments, segment_created, segment_changed

    async def _has_verified_zero_origin(self, session: AsyncSession, context: _LockedContext) -> bool:
        if (
            context.auth_action.result_reused is not False
            or not context.cycle_started_at <= context.association_started_at < context.reset_at
        ):
            return False
        earlier = await session.scalar(
            select(DeviceAssociation.id)
            .where(
                DeviceAssociation.org_id == context.association.org_id,
                DeviceAssociation.device_id == context.association.device_id,
                DeviceAssociation.id != context.association.id,
                or_(
                    DeviceAssociation.started_at < context.association.started_at,
                    and_(
                        DeviceAssociation.started_at == context.association.started_at,
                        DeviceAssociation.id < context.association.id,
                    ),
                ),
            )
            .limit(1)
        )
        return earlier is None

    @staticmethod
    def _segment_end(context: _LockedContext) -> datetime | None:
        end_candidates: list[datetime] = []
        if context.association_ended_at is not None:
            end_candidates.append(context.association_ended_at)
        if context.cycle.status == "EXPIRED" or context.now >= context.reset_at:
            end_candidates.append(context.reset_at)
        return min(end_candidates) if end_candidates else None

    def _close_segment_if_needed(self, segment: DeviceUsageSegment, context: _LockedContext) -> bool:
        end_at = self._segment_end(context)
        if end_at is None:
            return False
        existing_end = ensure_utc(segment.ended_at) if segment.ended_at is not None else None
        if existing_end is not None and existing_end <= end_at:
            return False
        segment.ended_at = end_at
        return True

    async def _has_missing_segment(
        self,
        session: AsyncSession,
        context: _LockedContext,
        segments: list[DeviceUsageSegment],
    ) -> bool:
        auth_evidence = exists(
            select(1).where(
                DeviceAction.association_id == DeviceAssociation.id,
                DeviceAction.kind == "AUTH",
                DeviceAction.status == "SUCCEEDED",
                DeviceAction.target_device_id == DeviceAssociation.device_id,
            )
        )
        association_ids = list(
            (
                await session.scalars(
                    select(DeviceAssociation.id).where(
                        DeviceAssociation.user_id == context.user.id,
                        DeviceAssociation.task_id == context.association.task_id,
                        DeviceAssociation.org_id == self.org_id,
                        DeviceAssociation.device_id.is_not(None),
                        DeviceAssociation.started_at < context.reset_at,
                        or_(
                            DeviceAssociation.ended_at.is_(None),
                            DeviceAssociation.ended_at > context.cycle_started_at,
                        ),
                        auth_evidence,
                    )
                )
            ).all()
        )
        covered = {segment.association_id for segment in segments}
        return any(association_id not in covered for association_id in association_ids)

    def _refresh_ledger(
        self,
        ledger: DeviceCycleLedger,
        segments: list[DeviceUsageSegment],
        has_gap: bool,
        now: datetime,
    ) -> bool:
        if not segments:
            raise EligibilityError("设备周期账本缺少关联段")
        confirmed: list[Decimal] = []
        unknown_count = 0
        for segment in segments:
            if segment.confirmed_used_usd is None:
                unknown_count += 1
                continue
            amount = self._stored_money(segment.confirmed_used_usd)
            confirmed.append(amount)

        calculated: Decimal | None = None
        if confirmed:
            calculated = sum(confirmed, _ZERO)
            if calculated >= _MONEY_LIMIT:
                raise EligibilityError("用户周期累计用量超过账本可表示范围")

        review_needed = has_gap or any(segment.quality == "NEEDS_REVIEW" for segment in segments)
        if unknown_count and confirmed:
            review_needed = True
        all_verified = not has_gap and unknown_count == 0 and all(
            segment.quality == "VERIFIED" for segment in segments
        )

        previous = ledger.confirmed_used_usd
        if previous is not None:
            previous_amount = self._stored_money(previous)
            if calculated is None or calculated < previous_amount:
                calculated = previous_amount
                review_needed = True

        if review_needed:
            quality = "NEEDS_REVIEW"
        elif all_verified and calculated is not None:
            quality = "VERIFIED"
        elif calculated is None:
            quality = "UNKNOWN"
        elif all(segment.quality == "UNKNOWN" for segment in segments):
            quality = "UNKNOWN"
        else:
            quality = "NEEDS_REVIEW"

        if calculated is not None and calculated >= _MONEY_LIMIT:
            raise EligibilityError("用户周期累计用量超过账本可表示范围")
        changed = ledger.confirmed_used_usd != calculated or ledger.quality != quality
        if changed:
            ledger.confirmed_used_usd = calculated
            ledger.quality = quality
            ledger.updated_at = now
        return changed

    def _validate_snapshot(
        self,
        snapshot: DeviceUsageSnapshot,
        context: _LockedContext,
        segment: DeviceUsageSegment,
        now: datetime,
    ) -> None:
        if (
            snapshot.org_id != self.org_id
            or snapshot.device_id != context.association.device_id
            or snapshot.range != "all"
        ):
            raise EligibilityError("设备用量快照组织、设备或范围不匹配")
        total = self._stored_money(snapshot.total_usd)
        sampled_at = ensure_utc(snapshot.sampled_at)
        if sampled_at < ensure_utc(segment.started_at) or sampled_at > now:
            raise EligibilityError("设备用量快照时间超出关联段或当前时刻")
        if total < 0:
            raise EligibilityError("设备用量快照金额无效")

    @staticmethod
    def _stored_money(value: Decimal) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value >= _MONEY_LIMIT:
            raise EligibilityError("设备用量金额超出有效范围")
        try:
            normalized = value.quantize(Decimal("0.0000000001"))
        except (InvalidOperation, ValueError, OverflowError):
            raise EligibilityError("设备用量金额精度无效") from None
        if normalized != value:
            raise EligibilityError("设备用量金额精度无效")
        return normalized

    async def _has_successor_association(self, session: AsyncSession, context: _LockedContext) -> bool:
        return await session.scalar(
            select(DeviceAssociation.id)
            .where(
                DeviceAssociation.org_id == context.association.org_id,
                DeviceAssociation.device_id == context.association.device_id,
                DeviceAssociation.id != context.association.id,
                or_(
                    DeviceAssociation.started_at > context.association.started_at,
                    and_(
                        DeviceAssociation.started_at == context.association.started_at,
                        DeviceAssociation.id > context.association.id,
                    ),
                ),
            )
            .limit(1)
        ) is not None

    async def _superseded_at_snapshot(
        self,
        session: AsyncSession,
        context: _LockedContext,
        snapshot: DeviceUsageSnapshot,
    ) -> bool:
        sampled_at = ensure_utc(snapshot.sampled_at)
        successor = await session.scalar(
            select(DeviceAssociation.id)
            .where(
                DeviceAssociation.org_id == context.association.org_id,
                DeviceAssociation.device_id == context.association.device_id,
                DeviceAssociation.id != context.association.id,
                DeviceAssociation.started_at <= sampled_at,
                or_(
                    DeviceAssociation.started_at > context.association.started_at,
                    and_(
                        DeviceAssociation.started_at == context.association.started_at,
                        DeviceAssociation.id > context.association.id,
                    ),
                ),
            )
            .limit(1)
        )
        return successor is not None

    @staticmethod
    def _confirmed_revoke_before_reset(context: _LockedContext) -> bool:
        association_ended = context.association_ended_at
        if (
            context.association.state != "ENDED"
            or association_ended is None
            or association_ended > context.reset_at
        ):
            return False
        return any(
            action.kind == "REVOKE"
            and action.status == "SUCCEEDED"
            and action.target_device_id == context.association.device_id
            and action.completed_at is not None
            and ensure_utc(action.completed_at) <= context.reset_at
            for action in context.actions
        )

    def _apply_sample(
        self,
        segment: DeviceUsageSegment,
        snapshot: DeviceUsageSnapshot,
        zero_origin: bool,
    ) -> bool:
        sampled_at = ensure_utc(snapshot.sampled_at)
        total = self._stored_money(snapshot.total_usd)
        current_at = ensure_utc(segment.latest_sampled_at) if segment.latest_sampled_at is not None else None
        if current_at is not None and sampled_at < current_at:
            return False
        if current_at is not None and sampled_at == current_at:
            if segment.latest_total_usd == total:
                return False
            if segment.quality != "NEEDS_REVIEW":
                segment.quality = "NEEDS_REVIEW"
                return True
            return False

        previous = (
            self._stored_money(segment.confirmed_used_usd)
            if segment.confirmed_used_usd is not None
            else None
        )
        imported = (
            self._stored_money(segment.imported_used_usd)
            if segment.imported_used_usd is not None
            else _ZERO
        )
        baseline = (
            self._stored_money(segment.baseline_total_usd)
            if segment.baseline_total_usd is not None
            else None
        )

        segment.latest_total_usd = total
        segment.latest_sampled_at = sampled_at

        if baseline is None:
            segment.baseline_total_usd = total
            segment.baseline_captured_at = sampled_at
            segment.confirmed_used_usd = _ZERO
            segment.quality = "NEEDS_REVIEW"
            return True

        candidate = imported + total - baseline
        if candidate < 0 or (previous is not None and candidate < previous):
            segment.quality = "NEEDS_REVIEW"
            return True

        if previous is None or candidate > previous:
            segment.confirmed_used_usd = candidate

        verified_origin = segment.imported_used_usd is not None or (baseline == _ZERO and zero_origin)
        if segment.quality != "NEEDS_REVIEW" and verified_origin:
            segment.quality = "VERIFIED"
        elif segment.quality != "NEEDS_REVIEW":
            segment.quality = "NEEDS_REVIEW"
        return True

    async def _write_audit(
        self,
        session: AsyncSession,
        context: _LockedContext,
        ledger: DeviceCycleLedger,
        segment: DeviceUsageSegment,
        *,
        action: str,
        snapshot_id: int | None = None,
    ) -> None:
        await audit(
            session,
            actor_telegram_id=None,
            actor_type="SYSTEM",
            action=action,
            target_type="DEVICE_CYCLE_LEDGER",
            target_id=str(ledger.id),
            result=ledger.quality,
            parameters_summary={
                "org_id": self.org_id,
                "task_id": context.association.task_id,
                "cycle_id": context.cycle.id,
                "association_id": context.association.id,
                "segment_id": segment.id,
                "snapshot_id": snapshot_id,
                "confirmed_used_usd": (
                    str(ledger.confirmed_used_usd) if ledger.confirmed_used_usd is not None else None
                ),
            },
        )

    @staticmethod
    def _result(ledger: DeviceCycleLedger, segment: DeviceUsageSegment) -> DeviceLedgerResult:
        return DeviceLedgerResult(
            ledger_id=ledger.id,
            segment_id=segment.id,
            confirmed_used_usd=ledger.confirmed_used_usd,
            quality=ledger.quality,
        )

    @staticmethod
    def _validate_id(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    def _now(self) -> datetime:
        try:
            return ensure_utc(self.clock())
        except (TypeError, ValueError, OverflowError):
            raise EligibilityError("当前时间无效") from None
