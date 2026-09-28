from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device import DeviceAuthorizationResult
from reclaude_bot.application.device_sampling import SamplingCallback, invoke_sampling_callback
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import AuditLog, Device, DeviceAction, DeviceAssociation, DeviceTaskScope, User
from reclaude_bot.infrastructure.reclaude.models import DeviceRecord, DeviceRevokeResponse

log = structlog.get_logger(__name__)


class _DeviceRevocationGateway(Protocol):
    account_id: int | str | None

    async def list_devices(self) -> list[DeviceRecord]: ...
    async def revoke_device(self, device_id: int) -> DeviceRevokeResponse: ...


@dataclass(frozen=True)
class _AssociationTarget:
    association_id: int
    task_id: int
    org_id: int
    explicit: bool


@dataclass(frozen=True)
class _RevocationReservation:
    association_id: int
    action_id: int
    task_id: int
    device_id: int
    user_id: int
    operator_id: int | None
    end_reason: str
    quota_cycle_id: int | None = None
    quota_ledger_id: int | None = None


QuotaRevokeCheck = Callable[
    [AsyncSession, DeviceAssociation, int, int, datetime],
    Awaitable[bool],
]


class DeviceRevocationService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: _DeviceRevocationGateway,
        org_id: int,
        *,
        clock=utcnow,
        before_revoke: SamplingCallback | None = None,
        after_revoked: SamplingCallback | None = None,
        quota_revoke_check: QuotaRevokeCheck | None = None,
    ) -> None:
        if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
            raise EligibilityError("组织 ID 必须是正整数")
        self.session_factory = factory
        self.gateway = gateway
        self.org_id = org_id
        self.clock = clock
        self.before_revoke = before_revoke
        self.after_revoked = after_revoked
        self.quota_revoke_check = quota_revoke_check

    async def deauth(
        self,
        user_id: int,
        *,
        association_id: int | None = None,
        operator_id: int | None = None,
        quota_cycle_id: int | None = None,
        quota_ledger_id: int | None = None,
    ) -> DeviceAuthorizationResult | None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise EligibilityError("本地用户 ID 无效")
        if association_id is not None and (
            isinstance(association_id, bool) or not isinstance(association_id, int) or association_id <= 0
        ):
            raise EligibilityError("设备关联 ID 无效")
        if (quota_cycle_id is None) != (quota_ledger_id is None):
            raise EligibilityError("自动额度撤销必须同时指定周期和账本")
        if quota_cycle_id is not None:
            self._positive_id(quota_cycle_id, "设备周期 ID")
            self._positive_id(quota_ledger_id, "设备账本 ID")
            if operator_id is not None:
                raise EligibilityError("自动额度撤销不能指定用户或管理员操作者")

        target = await self._discover_target(user_id, association_id)
        if target is None:
            await self._confirm_no_current_association(user_id)
            return None

        end_reason = "QUOTA" if quota_cycle_id is not None else None
        reserved = await self._reserve(
            user_id,
            target,
            operator_id,
            end_reason=end_reason,
            quota_cycle_id=quota_cycle_id,
            quota_ledger_id=quota_ledger_id,
        )
        if reserved is None:
            return None
        if isinstance(reserved, DeviceAuthorizationResult):
            return await self._after_revoked_result(reserved)

        if self.before_revoke is not None:
            await invoke_sampling_callback(
                self.before_revoke,
                reserved.association_id,
                phase="before_revoke",
            )

        try:
            result = await self._arm_revoke(reserved)
        except Exception:
            return await self._mark_unknown_with_callback(reserved, "REVOKE_PREPOST_UNKNOWN")
        if result is not None:
            return await self._after_revoked_result(result)

        try:
            response = await self.gateway.revoke_device(reserved.device_id)
        except AuthenticationCircuitOpen:
            await self._mark_unknown_with_callback(reserved, "REVOKE_AUTH_CIRCUIT_OPEN")
            raise
        except Exception:
            return await self._mark_unknown_with_callback(reserved, "REVOKE_OUTCOME_UNKNOWN")

        if not isinstance(response, DeviceRevokeResponse) or response.ok is not True:
            return await self._mark_unknown_with_callback(reserved, "REVOKE_INVALID_RESPONSE")
        result = await self._finalize_success(reserved)
        return await self._after_revoked_result(result)

    async def deauth_quota(
        self,
        user_id: int,
        association_id: int,
        cycle_id: int,
        ledger_id: int,
    ) -> DeviceAuthorizationResult | None:
        return await self.deauth(
            user_id,
            association_id=association_id,
            quota_cycle_id=cycle_id,
            quota_ledger_id=ledger_id,
        )

    async def reconcile_revoke(self, action_id: int) -> DeviceAuthorizationResult:
        if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id <= 0:
            raise EligibilityError("设备动作 ID 无效")
        reservation_or_result = await self._prepare_reconciliation(action_id)
        if isinstance(reservation_or_result, DeviceAuthorizationResult):
            return await self._after_revoked_result(reservation_or_result)
        reservation = reservation_or_result

        account_before = self.gateway.account_id
        try:
            records = await self.gateway.list_devices()
        except AuthenticationCircuitOpen:
            await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_AUTH_CIRCUIT")
            raise
        except Exception:
            return await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_LIST_FAILED")
        if self.gateway.account_id != account_before:
            return await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_ACCOUNT_CHANGED")
        if not isinstance(records, list) or any(not isinstance(record, DeviceRecord) for record in records):
            return await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_INVALID_LIST")

        matches = [record for record in records if record.id == reservation.device_id]
        if len(matches) > 1:
            return await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_DUPLICATE_TARGET")
        if matches:
            record = matches[0]
            if record.org_id != self.org_id:
                return await self._mark_unknown_with_callback(reservation, "REVOKE_RECONCILE_ORG_CONFLICT")
            if record.revoked_at is None:
                return await self._mark_unknown_with_callback(reservation, "REVOKE_STILL_ACTIVE")
        result = await self._finalize_success(reservation)
        return await self._after_revoked_result(result)

    async def reconcile_pending(self, *, limit: int = 50) -> int:
        """Reconcile every armed unresolved REVOKE in this organization, across cycles."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 500:
            raise EligibilityError("limit 必须在 1 到 500 之间")
        async with self.session_factory() as session:
            action_ids = list(
                (
                    await session.scalars(
                        select(DeviceAction.id)
                        .join(DeviceAssociation, DeviceAssociation.id == DeviceAction.association_id)
                        .join(DeviceTaskScope, DeviceTaskScope.task_id == DeviceAssociation.task_id)
                        .where(
                            DeviceTaskScope.org_id == self.org_id,
                            DeviceAssociation.org_id == self.org_id,
                            DeviceAction.kind == "REVOKE",
                            DeviceAction.status.in_(("PENDING", "UNKNOWN")),
                            DeviceAction.attempt_count == 1,
                        )
                        .order_by(DeviceAction.updated_at, DeviceAction.id)
                        .limit(limit)
                    )
                ).all()
            )

        reconciled = 0
        for action_id in action_ids:
            try:
                await self.reconcile_revoke(action_id)
            except AuthenticationCircuitOpen:
                raise
            except EligibilityError:
                log.warning("device_revoke_reconciliation_rejected", action_id=action_id)
            except Exception:
                log.warning("device_revoke_reconciliation_failed", action_id=action_id)
            reconciled += 1
        return reconciled

    async def _mark_unknown_with_callback(
        self,
        reservation: _RevocationReservation,
        error_code: str,
    ) -> DeviceAuthorizationResult:
        result = await self._mark_unknown(reservation, error_code)
        return await self._after_revoked_result(result)

    async def _after_revoked_result(self, result: DeviceAuthorizationResult) -> DeviceAuthorizationResult:
        if result.status == "SUCCEEDED" and self.after_revoked is not None:
            await invoke_sampling_callback(
                self.after_revoked,
                result.association_id,
                phase="after_revoked",
            )
        return result

    async def _prepare_reconciliation(
        self,
        action_id: int,
    ) -> _RevocationReservation | DeviceAuthorizationResult:
        async with self.session_factory() as session:
            action_hint = await session.get(DeviceAction, action_id)
            if action_hint is None or action_hint.kind != "REVOKE":
                raise EligibilityError("指定动作不是有效的设备撤销动作")
            association_hint = await session.get(DeviceAssociation, action_hint.association_id)
            if association_hint is None:
                raise EligibilityError("撤销动作缺少设备关联记录")
            task_id = association_hint.task_id
            user_id = association_hint.user_id

        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                association = await session.scalar(
                    select(DeviceAssociation).where(DeviceAssociation.id == association_hint.id).with_for_update()
                )
                action = await session.scalar(
                    select(DeviceAction).where(DeviceAction.id == action_id).with_for_update()
                )
                if user is None or association is None or action is None or action.association_id != association.id:
                    raise EligibilityError("撤销动作归属记录不完整")
                if (
                    scope is None
                    or scope.org_id != self.org_id
                    or association.org_id != self.org_id
                    or association.task_id != task_id
                    or association.user_id != user_id
                    or action.kind != "REVOKE"
                ):
                    raise EligibilityError("撤销动作不属于当前组织或关联")

                if action.status in ("SUCCEEDED", "FAILED"):
                    if action.target_device_id is None or association.device_id != action.target_device_id:
                        raise EligibilityError("终态撤销动作的设备目标不匹配")
                    return self._result(association, action)
                if action.status == "PENDING" and action.attempt_count == 0:
                    return self._result(association, action)
                if action.status not in ("PENDING", "UNKNOWN") or action.attempt_count != 1:
                    return self._result(association, action)
                if action.target_device_id is None or association.device_id != action.target_device_id:
                    raise EligibilityError("撤销动作目标与设备关联不匹配")
                if association.ended_at is not None:
                    return self._result(association, action)
                if association.state not in ("PENDING_REVOKE", "UNKNOWN"):
                    raise EligibilityError("撤销关联状态不允许自动核对")

                operator_id, end_reason = await self._original_revoke_actor(session, user, association, action)
                return _RevocationReservation(
                    association_id=association.id,
                    action_id=action.id,
                    task_id=association.task_id,
                    device_id=action.target_device_id,
                    user_id=user.id,
                    operator_id=operator_id,
                    end_reason=end_reason,
                )

    async def _original_revoke_actor(
        self,
        session: AsyncSession,
        user: User,
        association: DeviceAssociation,
        action: DeviceAction,
    ) -> tuple[int | None, str]:
        entries = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "DEVICE_REVOKE_RESERVED",
                        AuditLog.target_type == "DEVICE_ACTION",
                        AuditLog.target_id == str(action.id),
                    )
                )
            ).all()
        )
        if len(entries) != 1:
            raise EligibilityError("撤销动作缺少唯一有效的原始审计，不能自动核对")
        entry = entries[0]
        details = entry.parameters_summary
        association_id = details.get("association_id") if isinstance(details, dict) else None
        org_id = details.get("org_id") if isinstance(details, dict) else None
        device_id = details.get("device_id") if isinstance(details, dict) else None
        if (
            entry.result != "PENDING"
            or not isinstance(details, dict)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in (association_id, org_id, device_id))
            or association_id != association.id
            or org_id != self.org_id
            or device_id != action.target_device_id
        ):
            raise EligibilityError("撤销动作原始审计无效，不能自动核对")
        if entry.actor_type == "USER":
            if (
                isinstance(entry.actor_telegram_id, bool)
                or not isinstance(entry.actor_telegram_id, int)
                or entry.actor_telegram_id != user.telegram_user_id
                or details.get("end_reason") not in {None, "USER_DEAUTH"}
            ):
                raise EligibilityError("撤销动作用户审计不匹配，不能自动核对")
            return None, "USER_DEAUTH"
        if (
            entry.actor_type == "ADMIN"
            and isinstance(entry.actor_telegram_id, int)
            and not isinstance(entry.actor_telegram_id, bool)
            and entry.actor_telegram_id > 0
            and details.get("end_reason") in {None, "ADMIN_DEAUTH"}
        ):
            return entry.actor_telegram_id, "ADMIN_DEAUTH"
        if (
            entry.actor_type == "SYSTEM"
            and entry.actor_telegram_id is None
            and details.get("end_reason") == "QUOTA"
            and association.end_reason == "QUOTA"
        ):
            return None, "QUOTA"
        raise EligibilityError("撤销动作原始操作者无效，不能自动核对")

    async def _discover_target(self, user_id: int, association_id: int | None) -> _AssociationTarget | None:
        async with self.session_factory() as session:
            if await session.get(User, user_id) is None:
                raise EligibilityError("用户不存在")
            if association_id is not None:
                association = await session.get(DeviceAssociation, association_id)
                if association is None or association.user_id != user_id:
                    raise EligibilityError("指定设备关联不属于该用户")
                if association.org_id != self.org_id:
                    raise EligibilityError("指定设备关联不属于当前 Reclaude 组织")
                return _AssociationTarget(
                    association_id=association.id,
                    task_id=association.task_id,
                    org_id=association.org_id,
                    explicit=True,
                )

            association = await session.scalar(
                select(DeviceAssociation)
                .where(DeviceAssociation.user_id == user_id, DeviceAssociation.ended_at.is_(None))
                .order_by(DeviceAssociation.id)
                .limit(1)
            )
            if association is None:
                return None
            if association.org_id != self.org_id:
                raise EligibilityError("当前设备关联不属于配置的 Reclaude 组织")
            return _AssociationTarget(
                association_id=association.id,
                task_id=association.task_id,
                org_id=association.org_id,
                explicit=False,
            )

    async def _confirm_no_current_association(self, user_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                if user is None:
                    raise EligibilityError("用户不存在")
                association_id = await session.scalar(
                    select(DeviceAssociation.id)
                    .where(DeviceAssociation.user_id == user_id, DeviceAssociation.ended_at.is_(None))
                    .limit(1)
                )
                if association_id is not None:
                    raise EligibilityError("设备关联刚发生变化，请重新执行 deauth")
        return None

    async def _reserve(
        self,
        user_id: int,
        target: _AssociationTarget,
        operator_id: int | None,
        *,
        end_reason: str | None = None,
        quota_cycle_id: int | None = None,
        quota_ledger_id: int | None = None,
    ) -> _RevocationReservation | DeviceAuthorizationResult | None:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await session.scalar(
                        select(DeviceTaskScope).where(DeviceTaskScope.task_id == target.task_id).with_for_update()
                    )
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    association = await session.scalar(
                        select(DeviceAssociation)
                        .where(DeviceAssociation.id == target.association_id)
                        .with_for_update()
                    )
                    if user is None:
                        raise EligibilityError("用户不存在")
                    if (
                        scope is None
                        or scope.org_id != self.org_id
                        or target.org_id != self.org_id
                        or association is None
                        or association.user_id != user_id
                        or association.task_id != target.task_id
                        or association.org_id != self.org_id
                    ):
                        raise EligibilityError("设备关联已变化或不属于当前组织")

                    actions = list(
                        (
                            await session.scalars(
                                select(DeviceAction)
                                .where(DeviceAction.association_id == association.id, DeviceAction.kind == "REVOKE")
                                .order_by(DeviceAction.id.desc())
                                .with_for_update()
                            )
                        ).all()
                    )
                    if association.ended_at is not None:
                        if actions:
                            return self._result(association, actions[0])
                        if target.explicit:
                            raise EligibilityError("指定设备关联已经结束")
                        return None

                    if actions:
                        action = actions[0]
                        if (
                            action.status == "FAILED"
                            and action.last_error_code == "QUOTA_RECHECK_FAILED"
                        ):
                            actions = []
                        else:
                            if action.target_device_id != association.device_id or association.device_id is None:
                                return await self._mark_unknown_in_transaction(
                                    session,
                                    user,
                                    association,
                                    action,
                                    "REVOKE_TARGET_MISMATCH",
                                    operator_id,
                                )
                            if action.status == "PENDING" and action.attempt_count > 0:
                                action.status = "UNKNOWN"
                                action.completed_at = None
                                action.last_error_code = "REVOKE_OUTCOME_UNKNOWN"
                                action.updated_at = self._now()
                                association.state = "UNKNOWN"
                                association.updated_at = action.updated_at
                                await self._audit_revoke(session, user, association, action, operator_id, "DEVICE_REVOKE_UNKNOWN")
                            if (
                                end_reason == "QUOTA"
                                and action.status == "PENDING"
                                and action.attempt_count == 0
                                and association.state == "PENDING_REVOKE"
                                and association.end_reason == "QUOTA"
                                and quota_cycle_id is not None
                                and quota_ledger_id is not None
                            ):
                                eligible = self.quota_revoke_check is not None and await self.quota_revoke_check(
                                    session,
                                    association,
                                    quota_cycle_id,
                                    quota_ledger_id,
                                    self._now(),
                                )
                                if eligible:
                                    return _RevocationReservation(
                                        association_id=association.id,
                                        action_id=action.id,
                                        task_id=association.task_id,
                                        device_id=association.device_id,
                                        user_id=user_id,
                                        operator_id=None,
                                        end_reason="QUOTA",
                                        quota_cycle_id=quota_cycle_id,
                                        quota_ledger_id=quota_ledger_id,
                                    )
                                now = self._now()
                                action.status = "FAILED"
                                action.completed_at = now
                                action.last_error_code = "QUOTA_RECHECK_FAILED"
                                action.updated_at = now
                                association.state = "ACTIVE"
                                association.updated_at = now
                                await self._audit_revoke(
                                    session,
                                    user,
                                    association,
                                    action,
                                    None,
                                    "DEVICE_REVOKE_QUOTA_RECHECK_FAILED",
                                )
                                association.end_reason = None
                                return self._result(association, action)
                            return self._result(association, action)

                    if association.state != "ACTIVE" or association.device_id is None:
                        raise EligibilityError("只有已确认且有设备 ID 的关联才能撤销")

                    now = self._now()
                    resolved_end_reason = end_reason or ("ADMIN_DEAUTH" if operator_id is not None else "USER_DEAUTH")
                    if resolved_end_reason == "QUOTA":
                        if (
                            quota_cycle_id is None
                            or quota_ledger_id is None
                            or self.quota_revoke_check is None
                            or not await self.quota_revoke_check(
                                session,
                                association,
                                quota_cycle_id,
                                quota_ledger_id,
                                now,
                            )
                        ):
                            raise EligibilityError("额度撤销条件已变化，未开始撤销")
                    association.state = "PENDING_REVOKE"
                    association.updated_at = now
                    if resolved_end_reason == "QUOTA":
                        association.end_reason = "QUOTA"
                    action = DeviceAction(
                        association_id=association.id,
                        kind="REVOKE",
                        status="PENDING",
                        idempotency_key=uuid4().hex,
                        target_device_id=association.device_id,
                        result_reused=None,
                        attempt_count=0,
                        next_retry_at=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(action)
                    await session.flush()
                    await self._audit_revoke(session, user, association, action, operator_id, "DEVICE_REVOKE_RESERVED")
                    return _RevocationReservation(
                        association_id=association.id,
                        action_id=action.id,
                        task_id=association.task_id,
                        device_id=association.device_id,
                        user_id=user_id,
                        operator_id=operator_id,
                        end_reason=resolved_end_reason,
                        quota_cycle_id=quota_cycle_id,
                        quota_ledger_id=quota_ledger_id,
                    )
        except IntegrityError:
            raise EligibilityError("设备撤销预占发生并发冲突，请重新检查设备状态") from None

    async def _arm_revoke(
        self,
        reservation: _RevocationReservation,
    ) -> DeviceAuthorizationResult | None:
        async with self.session_factory() as session:
            async with session.begin():
                scope = await session.scalar(
                    select(DeviceTaskScope).where(DeviceTaskScope.task_id == reservation.task_id).with_for_update()
                )
                user = await session.scalar(select(User).where(User.id == reservation.user_id).with_for_update())
                association, action = await self._locked_action(session, reservation)
                if scope is None or scope.org_id != self.org_id:
                    return await self._mark_unknown_in_transaction(
                        session, user, association, action, "REVOKE_SCOPE_CHANGED", reservation.operator_id
                    )
                if action.status != "PENDING" or action.attempt_count != 0:
                    if action.status == "PENDING" and action.attempt_count > 0:
                        action.status = "UNKNOWN"
                        action.completed_at = None
                        action.last_error_code = "REVOKE_OUTCOME_UNKNOWN"
                        action.updated_at = self._now()
                        if association.ended_at is None:
                            association.state = "UNKNOWN"
                            association.updated_at = action.updated_at
                        await self._audit_revoke(session, user, association, action, reservation.operator_id, "DEVICE_REVOKE_UNKNOWN")
                    return self._result(association, action)
                if (
                    association.user_id != reservation.user_id
                    or association.task_id != reservation.task_id
                    or association.org_id != self.org_id
                    or association.ended_at is not None
                    or association.state != "PENDING_REVOKE"
                    or association.device_id != reservation.device_id
                    or action.kind != "REVOKE"
                    or action.target_device_id != reservation.device_id
                ):
                    return await self._mark_unknown_in_transaction(
                        session, user, association, action, "REVOKE_TARGET_CHANGED", reservation.operator_id
                    )
                if reservation.end_reason == "QUOTA":
                    if (
                        reservation.quota_cycle_id is None
                        or reservation.quota_ledger_id is None
                        or self.quota_revoke_check is None
                        or not await self.quota_revoke_check(
                            session,
                            association,
                            reservation.quota_cycle_id,
                            reservation.quota_ledger_id,
                            self._now(),
                        )
                    ):
                        now = self._now()
                        action.status = "FAILED"
                        action.completed_at = now
                        action.last_error_code = "QUOTA_RECHECK_FAILED"
                        action.updated_at = now
                        association.state = "ACTIVE"
                        association.updated_at = now
                        await self._audit_revoke(
                            session,
                            user,
                            association,
                            action,
                            None,
                            "DEVICE_REVOKE_QUOTA_RECHECK_FAILED",
                        )
                        association.end_reason = None
                        return self._result(association, action)
                action.attempt_count = 1
                action.updated_at = self._now()
                await self._audit_revoke(session, user, association, action, reservation.operator_id, "DEVICE_REVOKE_ATTEMPTED")
                return None

    async def _locked_action(
        self,
        session: AsyncSession,
        reservation: _RevocationReservation,
    ) -> tuple[DeviceAssociation, DeviceAction]:
        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == reservation.association_id).with_for_update()
        )
        action = await session.scalar(
            select(DeviceAction).where(DeviceAction.id == reservation.action_id).with_for_update()
        )
        if association is None or action is None or action.association_id != association.id:
            raise EligibilityError("设备撤销动作记录不存在")
        return association, action

    async def _mark_unknown(
        self,
        reservation: _RevocationReservation,
        error_code: str,
    ) -> DeviceAuthorizationResult:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await session.scalar(
                        select(DeviceTaskScope).where(DeviceTaskScope.task_id == reservation.task_id).with_for_update()
                    )
                    user = await session.scalar(select(User).where(User.id == reservation.user_id).with_for_update())
                    association, action = await self._locked_action(session, reservation)
                    if action.status == "SUCCEEDED":
                        return self._result(association, action)
                    if scope is not None and scope.org_id == self.org_id:
                        return await self._mark_unknown_in_transaction(
                            session,
                            user,
                            association,
                            action,
                            error_code,
                            reservation.operator_id,
                        )
                    return self._result(association, action)
        except Exception:
            return DeviceAuthorizationResult(
                association_id=reservation.association_id,
                action_id=reservation.action_id,
                status="UNKNOWN",
                device_id=reservation.device_id,
            )

    async def _mark_unknown_in_transaction(
        self,
        session: AsyncSession,
        user: User | None,
        association: DeviceAssociation,
        action: DeviceAction,
        error_code: str,
        operator_id: int | None,
    ) -> DeviceAuthorizationResult:
        now = self._now()
        action.status = "UNKNOWN"
        action.completed_at = None
        action.last_error_code = error_code
        action.updated_at = now
        if association.ended_at is None:
            association.state = "UNKNOWN"
            association.updated_at = now
        await self._audit_revoke(session, user, association, action, operator_id, "DEVICE_REVOKE_UNKNOWN")
        return self._result(association, action)

    async def _finalize_success(
        self,
        reservation: _RevocationReservation,
    ) -> DeviceAuthorizationResult:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await session.scalar(
                        select(DeviceTaskScope).where(DeviceTaskScope.task_id == reservation.task_id).with_for_update()
                    )
                    user = await session.scalar(select(User).where(User.id == reservation.user_id).with_for_update())
                    association, action = await self._locked_action(session, reservation)
                    if action.status == "SUCCEEDED":
                        return self._result(association, action)
                    if (
                        scope is None
                        or scope.org_id != self.org_id
                        or association.user_id != reservation.user_id
                        or association.task_id != reservation.task_id
                        or association.org_id != self.org_id
                        or association.ended_at is not None
                        or association.device_id != reservation.device_id
                        or action.kind != "REVOKE"
                        or action.target_device_id != reservation.device_id
                        or action.attempt_count != 1
                        or action.status not in ("PENDING", "UNKNOWN")
                    ):
                        return await self._mark_unknown_in_transaction(
                            session,
                            user,
                            association,
                            action,
                            "REVOKE_TARGET_CHANGED",
                            reservation.operator_id,
                        )

                    device = await session.scalar(
                        select(Device)
                        .where(Device.org_id == self.org_id, Device.device_id == reservation.device_id)
                        .with_for_update()
                    )
                    if device is None:
                        return await self._mark_unknown_in_transaction(
                            session,
                            user,
                            association,
                            action,
                            "REVOKE_DEVICE_RECORD_MISSING",
                            reservation.operator_id,
                        )

                    now = self._now()
                    device.revoked_at = now
                    association.state = "ENDED"
                    association.ended_at = now
                    association.end_reason = reservation.end_reason
                    association.updated_at = now
                    action.status = "SUCCEEDED"
                    action.completed_at = now
                    action.last_error_code = None
                    action.updated_at = now
                    await session.flush()
                    await self._audit_revoke(session, user, association, action, reservation.operator_id, "DEVICE_REVOKE_SUCCEEDED")
                    return self._result(association, action)
        except Exception:
            return await self._mark_unknown(reservation, "REVOKE_RESULT_PERSIST_FAILED")

    async def _audit_revoke(
        self,
        session: AsyncSession,
        user: User | None,
        association: DeviceAssociation,
        action: DeviceAction,
        operator_id: int | None,
        audit_action: str,
    ) -> None:
        reason = association.end_reason or ("ADMIN_DEAUTH" if operator_id is not None else "USER_DEAUTH")
        system_action = reason == "QUOTA"
        actor_id = (
            None
            if system_action
            else operator_id
            if operator_id is not None
            else (user.telegram_user_id if user is not None else None)
        )
        await audit(
            session,
            actor_telegram_id=actor_id,
            actor_type="SYSTEM" if system_action else "ADMIN" if operator_id is not None else "USER",
            action=audit_action,
            target_type="DEVICE_ACTION",
            target_id=str(action.id),
            result=action.status,
            parameters_summary={
                "association_id": association.id,
                "org_id": self.org_id,
                "device_id": action.target_device_id,
                "end_reason": reason,
            },
        )

    @staticmethod
    def _result(association: DeviceAssociation, action: DeviceAction) -> DeviceAuthorizationResult:
        status = cast(Literal["PENDING", "SUCCEEDED", "FAILED", "UNKNOWN"], action.status)
        return DeviceAuthorizationResult(
            association_id=association.id,
            action_id=action.id,
            status=status,
            device_id=action.target_device_id if action.target_device_id is not None else association.device_id,
        )

    @staticmethod
    def _positive_id(value: int | None, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EligibilityError(f"{label}必须是正整数")

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
