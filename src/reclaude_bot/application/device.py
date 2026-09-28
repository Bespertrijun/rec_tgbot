from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from string import hexdigits
from typing import Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_context import DeviceTaskContext, SingleOrgTaskService
from reclaude_bot.application.device_sampling import SamplingCallback, invoke_sampling_callback
from reclaude_bot.application.task import ALL, ALLOWLIST, EXCLUDE
from reclaude_bot.domain.enums import BindingStatus, TaskStatus, UserStatus
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.domain.quota import as_decimal, ensure_utc
from reclaude_bot.infrastructure.db.models import Device, DeviceAction, DeviceAssociation, DeviceTaskMember, DeviceTaskScope, QuotaTask, User
from reclaude_bot.infrastructure.reclaude.client import DeviceApiError
from reclaude_bot.infrastructure.reclaude.models import DeviceAuthApproval, DeviceAuthDescription

_ALLOWED_HOSTS = frozenset({"www.recode.cat", "www.reclaude.ai"})
_MAX_LINK_LENGTH = 8192
_MAX_STATE_LENGTH = 2048
_MAX_QUERY_FIELDS = 32


@dataclass(frozen=True)
class DeviceAuthRequest:
    state: str = field(repr=False)
    fingerprint: str


DeviceAuthorizationStatus = Literal["PENDING", "SUCCEEDED", "FAILED", "UNKNOWN"]
QuotaCheck = Callable[[AsyncSession, User, DeviceTaskContext, datetime], Awaitable[None]]
DeviceAuthClock = Callable[[], datetime]


class _DeviceGateway(Protocol):
    async def describe_device_auth(self, state: str) -> DeviceAuthDescription: ...
    async def approve_device_auth(self, state: str, device_name: str, org_id: int) -> DeviceAuthApproval: ...


@dataclass(frozen=True)
class DeviceAuthorizationResult:
    association_id: int
    action_id: int
    status: DeviceAuthorizationStatus
    device_id: int | None


@dataclass(frozen=True)
class _Reservation:
    association_id: int
    action_id: int


class DeviceAuthorizationService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        gateway: _DeviceGateway,
        org_id: int,
        quota_check: QuotaCheck,
        *,
        clock: DeviceAuthClock = utcnow,
        on_authorized: SamplingCallback | None = None,
        before_authorize: Callable[[str | None], Awaitable[object]] | None = None,
    ) -> None:
        self.session_factory = factory
        self.gateway = gateway
        self.task_service = SingleOrgTaskService(factory, org_id)
        self.org_id = self.task_service.org_id
        self.quota_check = quota_check
        self.clock = clock
        self.on_authorized = on_authorized
        self.before_authorize = before_authorize

    async def auth(
        self,
        user_id: int,
        link: str,
        *,
        task_name: str | None = None,
    ) -> DeviceAuthorizationResult:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise EligibilityError("本地用户 ID 无效")
        request = parse_device_auth_link(link)
        context = await self.task_service.resolve_task(task_name)
        if self.before_authorize is not None:
            await self.before_authorize(context.name)
        reservation = await self._reserve(user_id, request, context, self._now())
        if isinstance(reservation, DeviceAuthorizationResult):
            return await self._after_authorized(reservation)

        try:
            description = await self.gateway.describe_device_auth(request.state)
            validate_device_auth_description(request, description, now=self._now())
        except Exception as exc:
            await self._finish_before_approve(
                user_id,
                context,
                reservation,
                "AUTH_DESCRIBE_FAILED",
            )
            if isinstance(exc, AuthenticationCircuitOpen):
                raise
            raise EligibilityError("授权请求无法验证或已过期，请重新发起") from None

        try:
            existing = await self._arm_approve(user_id, context, reservation, request, description)
        except Exception as exc:
            await self._finish_before_approve(
                user_id,
                context,
                reservation,
                "AUTH_PREAPPROVE_REJECTED",
            )
            if isinstance(exc, (AuthenticationCircuitOpen, EligibilityError)):
                raise
            raise EligibilityError("授权前资格核查失败，请重新发起") from None
        if existing is not None:
            return await self._after_authorized(existing)

        try:
            approval = await self.gateway.approve_device_auth(request.state, description.device_name.strip(), self.org_id)
        except Exception as exc:
            known_capacity_failure = (
                isinstance(exc, DeviceApiError)
                and exc.code == "client.device_limit_reached"
                and exc.retryable is False
                and exc.outcome_unknown is False
            )
            if known_capacity_failure:
                result = await self._finish_after_approve_failure(
                    user_id, context, reservation, failed=True, error_code="client.device_limit_reached"
                )
                return await self._after_authorized(result)
            result = await self._finish_after_approve_failure(
                user_id,
                context,
                reservation,
                failed=False,
                error_code="AUTH_APPROVE_UNKNOWN",
            )
            if isinstance(exc, AuthenticationCircuitOpen):
                raise
            return await self._after_authorized(result)

        if not self._valid_approval(approval):
            result = await self._finish_after_approve_failure(
                user_id,
                context,
                reservation,
                failed=False,
                error_code="AUTH_APPROVE_INVALID_RESPONSE",
            )
            return await self._after_authorized(result)
        result = await self._finalize_approval(user_id, context, reservation, description, approval)
        return await self._after_authorized(result)

    async def _after_authorized(self, result: DeviceAuthorizationResult) -> DeviceAuthorizationResult:
        if result.status == "SUCCEEDED" and self.on_authorized is not None:
            await invoke_sampling_callback(
                self.on_authorized,
                result.association_id,
                phase="after_authorized",
            )
        return result

    async def _reserve(
        self,
        user_id: int,
        request: DeviceAuthRequest,
        context: DeviceTaskContext,
        now: datetime,
    ) -> _Reservation | DeviceAuthorizationResult:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await self._locked_scope(session, context)
                    user = self._require_eligible_user(
                        await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    )

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
                    action = await session.scalar(
                        select(DeviceAction).where(DeviceAction.idempotency_key == request.fingerprint).with_for_update()
                    )
                    if action is not None:
                        if action.kind != "AUTH":
                            raise EligibilityError("授权请求标识已被其他设备动作占用")
                        association = next(
                            (item for item in open_associations if item.id == action.association_id),
                            None,
                        )
                        if association is None:
                            association = await session.get(DeviceAssociation, action.association_id)
                        if association is None or association.user_id != user_id:
                            raise EligibilityError("该授权请求已由其他用户处理")
                        if association.task_id != context.task_id or association.org_id != self.org_id:
                            raise EligibilityError("该授权请求已用于其他任务，请重新生成授权链接")
                        if action.status == "SUCCEEDED":
                            if (
                                association.state != "ACTIVE"
                                or association.ended_at is not None
                                or action.target_device_id is None
                                or association.device_id != action.target_device_id
                            ):
                                raise EligibilityError("该授权关联已结束或失效，请重新生成授权链接")
                            return self._result(association, action)
                        if action.status == "PENDING" and action.attempt_count > 0:
                            action.status = "UNKNOWN"
                            action.completed_at = None
                            action.last_error_code = "AUTH_ATTEMPT_OUTCOME_UNKNOWN"
                            action.updated_at = now
                            if association.ended_at is None:
                                association.state = "UNKNOWN"
                                association.updated_at = now
                            await self._audit_action(
                                session,
                                user,
                                context,
                                association,
                                action,
                                "DEVICE_AUTH_UNKNOWN",
                            )
                        return self._result(association, action)

                    if open_associations:
                        raise EligibilityError("用户已有未结束设备关联，请先完成 deauth")
                    if not await self._scope_allows_user(session, scope, user_id):
                        raise EligibilityError("用户不在当前设备任务范围内")
                    context = await self._current_task_context(session, context)
                    await self.quota_check(session, user, context, now)

                    association = DeviceAssociation(
                        user_id=user_id,
                        task_id=context.task_id,
                        org_id=self.org_id,
                        device_id=None,
                        state="PENDING_AUTH",
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
                        status="PENDING",
                        idempotency_key=request.fingerprint,
                        target_device_id=None,
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
                    await self._audit_action(session, user, context, association, action, "DEVICE_AUTH_RESERVED")
                    return _Reservation(association_id=association.id, action_id=action.id)
        except IntegrityError:
            raise EligibilityError("设备授权预占发生并发冲突，请检查当前设备状态") from None

    async def _arm_approve(
        self,
        user_id: int,
        context: DeviceTaskContext,
        reservation: _Reservation,
        request: DeviceAuthRequest,
        description: DeviceAuthDescription,
    ) -> DeviceAuthorizationResult | None:
        async with self.session_factory() as session:
            async with session.begin():
                scope = await self._locked_scope(session, context)
                user = self._require_eligible_user(
                    await session.scalar(select(User).where(User.id == user_id).with_for_update())
                )
                association, action = await self._locked_reservation(session, reservation)
                if action.status != "PENDING" or action.attempt_count != 0:
                    if action.status == "PENDING" and action.attempt_count > 0:
                        action.status = "UNKNOWN"
                        action.completed_at = None
                        action.last_error_code = "AUTH_ATTEMPT_OUTCOME_UNKNOWN"
                        action.updated_at = self._now()
                        if association.ended_at is None:
                            association.state = "UNKNOWN"
                            association.updated_at = action.updated_at
                        await self._audit_action(
                            session,
                            user,
                            context,
                            association,
                            action,
                            "DEVICE_AUTH_UNKNOWN",
                        )
                    return self._result(association, action)
                if association.state != "PENDING_AUTH" or association.ended_at is not None:
                    raise EligibilityError("设备授权预占状态已变化")
                if not await self._scope_allows_user(session, scope, user_id):
                    raise EligibilityError("用户不再属于当前设备任务范围")
                other_open = await session.scalar(
                    select(DeviceAssociation.id).where(
                        DeviceAssociation.user_id == user_id,
                        DeviceAssociation.ended_at.is_(None),
                        DeviceAssociation.id != association.id,
                    ).limit(1)
                )
                if other_open is not None:
                    raise EligibilityError("用户已有其他未结束设备关联")
                now = self._now()
                context = await self._current_task_context(session, context)
                await self.quota_check(session, user, context, now)
                validate_device_auth_description(request, description, now=now)
                action.attempt_count = 1
                action.updated_at = now
                await self._audit_action(session, user, context, association, action, "DEVICE_AUTH_APPROVE_ATTEMPTED")
                return None

    async def _finish_before_approve(
        self,
        user_id: int,
        context: DeviceTaskContext,
        reservation: _Reservation,
        error_code: str,
    ) -> None:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await self._locked_scope(session, context)
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    association, action = await self._locked_reservation(session, reservation)
                    now = self._now()
                    if action.status != "PENDING":
                        return
                    if action.attempt_count > 0:
                        action.status = "UNKNOWN"
                        action.last_error_code = "AUTH_ATTEMPT_OUTCOME_UNKNOWN"
                        if association.ended_at is None:
                            association.state = "UNKNOWN"
                            association.updated_at = now
                    else:
                        action.status = "FAILED"
                        action.completed_at = now
                        action.last_error_code = error_code
                        if association.ended_at is None:
                            association.state = "ENDED"
                            association.ended_at = now
                            association.end_reason = error_code
                            association.updated_at = now
                    action.updated_at = now
                    await self._audit_action(
                        session,
                        user,
                        context,
                        association,
                        action,
                        "DEVICE_AUTH_FAILED" if action.status == "FAILED" else "DEVICE_AUTH_UNKNOWN",
                    )
        except Exception:
            return

    async def _finish_after_approve_failure(
        self,
        user_id: int,
        context: DeviceTaskContext,
        reservation: _Reservation,
        *,
        failed: bool,
        error_code: str,
        target_device_id: int | None = None,
        result_reused: bool | None = None,
    ) -> DeviceAuthorizationResult:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await self._locked_scope(session, context)
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    association, action = await self._locked_reservation(session, reservation)
                    if action.status == "SUCCEEDED":
                        return self._result(association, action)
                    now = self._now()
                    action.status = "FAILED" if failed else "UNKNOWN"
                    action.last_error_code = error_code
                    action.updated_at = now
                    if target_device_id is not None:
                        action.target_device_id = target_device_id
                    if result_reused is not None:
                        action.result_reused = result_reused
                    if failed:
                        action.completed_at = now
                        if association.ended_at is None:
                            association.state = "ENDED"
                            association.ended_at = now
                            association.end_reason = error_code
                            association.updated_at = now
                    else:
                        action.completed_at = None
                        if association.ended_at is None:
                            association.state = "UNKNOWN"
                            association.updated_at = now
                    await self._audit_action(
                        session,
                        user,
                        context,
                        association,
                        action,
                        "DEVICE_AUTH_FAILED" if failed else "DEVICE_AUTH_UNKNOWN",
                    )
                    return self._result(association, action)
        except Exception:
            return DeviceAuthorizationResult(
                association_id=reservation.association_id,
                action_id=reservation.action_id,
                status="UNKNOWN",
                device_id=None,
            )

    async def _finalize_approval(
        self,
        user_id: int,
        context: DeviceTaskContext,
        reservation: _Reservation,
        description: DeviceAuthDescription,
        approval: DeviceAuthApproval,
    ) -> DeviceAuthorizationResult:
        device_id = approval.device_id
        reused = approval.reused
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await self._locked_scope(session, context)
                    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                    association, action = await self._locked_reservation(session, reservation)
                    if action.status == "SUCCEEDED":
                        return self._result(association, action)
                    action.target_device_id = device_id
                    action.result_reused = reused
                    action.updated_at = self._now()
                    if action.attempt_count != 1 or action.status not in ("PENDING", "UNKNOWN"):
                        return await self._mark_conflicting_approval(
                            session,
                            user,
                            context,
                            association,
                            action,
                            device_id,
                            reused,
                        )
                    if association.ended_at is not None or association.state not in ("PENDING_AUTH", "UNKNOWN"):
                        return await self._mark_conflicting_approval(
                            session,
                            user,
                            context,
                            association,
                            action,
                            device_id,
                            reused,
                        )

                    active_user_association = await session.scalar(
                        select(DeviceAssociation.id)
                        .where(
                            DeviceAssociation.user_id == user_id,
                            DeviceAssociation.ended_at.is_(None),
                            DeviceAssociation.id != association.id,
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if active_user_association is not None:
                        return await self._mark_conflicting_approval(
                            session,
                            user,
                            context,
                            association,
                            action,
                            device_id,
                            reused,
                        )
                    active_device_association = await session.scalar(
                        select(DeviceAssociation.id)
                        .where(
                            DeviceAssociation.org_id == self.org_id,
                            DeviceAssociation.device_id == device_id,
                            DeviceAssociation.ended_at.is_(None),
                            DeviceAssociation.id != association.id,
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if active_device_association is not None:
                        return await self._mark_conflicting_approval(
                            session,
                            user,
                            context,
                            association,
                            action,
                            device_id,
                            reused,
                        )

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
                            name=description.device_name.strip(),
                            first_synced_at=now,
                            last_synced_at=now,
                            revoked_at=None,
                        )
                        session.add(device)
                        await session.flush()
                    else:
                        device.name = description.device_name.strip()
                        device.last_synced_at = now
                        device.revoked_at = None

                    association.device_id = device_id
                    association.state = "ACTIVE"
                    association.updated_at = now
                    action.status = "SUCCEEDED"
                    action.completed_at = now
                    action.last_error_code = None
                    action.updated_at = now
                    await session.flush()
                    await self._audit_action(session, user, context, association, action, "DEVICE_AUTH_SUCCEEDED")
                    return self._result(association, action)
        except Exception:
            return await self._finish_after_approve_failure(
                user_id,
                context,
                reservation,
                failed=False,
                error_code="AUTH_RESULT_PERSIST_FAILED",
                target_device_id=device_id,
                result_reused=reused,
            )

    async def _mark_conflicting_approval(
        self,
        session: AsyncSession,
        user: User | None,
        context: DeviceTaskContext,
        association: DeviceAssociation,
        action: DeviceAction,
        device_id: int,
        reused: bool,
    ) -> DeviceAuthorizationResult:
        now = self._now()
        action.status = "UNKNOWN"
        action.target_device_id = device_id
        action.result_reused = reused
        action.completed_at = None
        action.last_error_code = "AUTH_DEVICE_CONFLICT"
        action.updated_at = now
        if association.ended_at is None:
            association.state = "UNKNOWN"
            association.updated_at = now
        await self._audit_action(session, user, context, association, action, "DEVICE_AUTH_UNKNOWN")
        return self._result(association, action)

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
    def _require_eligible_user(user: User | None) -> User:
        if user is None or user.binding_status != BindingStatus.BOUND.value:
            raise EligibilityError("用户尚未完成本地绑定")
        if user.status != UserStatus.ACTIVE.value:
            raise EligibilityError("账号已被禁用，无法授权设备")
        return user

    @staticmethod
    async def _scope_allows_user(session: AsyncSession, scope: DeviceTaskScope, user_id: int) -> bool:
        membership = await session.scalar(
            select(DeviceTaskMember.user_id).where(
                DeviceTaskMember.task_id == scope.task_id,
                DeviceTaskMember.user_id == user_id,
            )
        )
        if scope.scope_mode == ALL:
            return True
        if scope.scope_mode == ALLOWLIST:
            return membership is not None
        if scope.scope_mode == EXCLUDE:
            return membership is None
        raise EligibilityError("设备任务成员范围配置无效")

    async def _locked_reservation(
        self,
        session: AsyncSession,
        reservation: _Reservation,
    ) -> tuple[DeviceAssociation, DeviceAction]:
        association = await session.scalar(
            select(DeviceAssociation).where(DeviceAssociation.id == reservation.association_id).with_for_update()
        )
        action = await session.scalar(
            select(DeviceAction).where(DeviceAction.id == reservation.action_id).with_for_update()
        )
        if association is None or action is None or action.association_id != association.id:
            raise EligibilityError("设备授权动作记录不存在")
        return association, action

    async def _audit_action(
        self,
        session: AsyncSession,
        user: User | None,
        context: DeviceTaskContext,
        association: DeviceAssociation,
        action: DeviceAction,
        audit_action: str,
    ) -> None:
        await audit(
            session,
            actor_telegram_id=user.telegram_user_id if user is not None else None,
            actor_type="USER",
            action=audit_action,
            target_type="DEVICE_ACTION",
            target_id=str(action.id),
            result=action.status,
            parameters_summary={
                "task_id": context.task_id,
                "org_id": self.org_id,
                "association_id": association.id,
            },
        )

    @staticmethod
    def _valid_approval(approval: object) -> bool:
        return (
            isinstance(approval, DeviceAuthApproval)
            and approval.ok is True
            and isinstance(approval.device_id, int)
            and not isinstance(approval.device_id, bool)
            and approval.device_id > 0
            and type(approval.reused) is bool
        )

    @staticmethod
    def _result(association: DeviceAssociation, action: DeviceAction) -> DeviceAuthorizationResult:
        allowed_statuses = {"PENDING", "SUCCEEDED", "FAILED", "UNKNOWN"}
        status = cast(DeviceAuthorizationStatus, action.status if action.status in allowed_statuses else "UNKNOWN")
        device_id = action.target_device_id if action.target_device_id is not None else association.device_id
        return DeviceAuthorizationResult(
            association_id=association.id,
            action_id=action.id,
            status=status,
            device_id=device_id,
        )

    def _now(self) -> datetime:
        return ensure_utc(self.clock())


def parse_device_auth_link(link: str) -> DeviceAuthRequest:
    try:
        if not isinstance(link, str):
            raise ValueError
        value = link.strip()
        if not value or len(value) > _MAX_LINK_LENGTH:
            raise ValueError
        value.encode("utf-8")
        if any(character.isspace() or _is_control(character) for character in value):
            raise ValueError
        if "\\" in value or "#" in value or not _has_valid_percent_encoding(value):
            raise ValueError

        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "https" or parsed.path != "/cli/auth" or parsed.fragment:
            raise ValueError
        if "@" in parsed.netloc or parsed.port is not None:
            raise ValueError
        hostname = parsed.hostname
        if hostname is None or hostname.casefold() not in _ALLOWED_HOSTS or parsed.netloc.casefold() != hostname.casefold():
            raise ValueError
        if parsed.query and len(parsed.query.split("&")) > _MAX_QUERY_FIELDS:
            raise ValueError

        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_QUERY_FIELDS,
        )
        states = [item_value for key, item_value in query if key == "state"]
        if len(states) != 1:
            raise ValueError
        state = states[0]
        if (
            not state
            or len(state) > _MAX_STATE_LENGTH
            or any(character.isspace() or _is_control(character) for character in state)
        ):
            raise ValueError
        fingerprint = sha256(state.encode("utf-8")).hexdigest()
        return DeviceAuthRequest(state=state, fingerprint=fingerprint)
    except (TypeError, ValueError, UnicodeError):
        raise EligibilityError("授权链接无效") from None


def validate_device_auth_description(
    request: DeviceAuthRequest,
    description: DeviceAuthDescription,
    *,
    now: datetime,
) -> None:
    if not isinstance(request, DeviceAuthRequest) or not isinstance(description, DeviceAuthDescription):
        raise EligibilityError("设备授权描述无效")
    if description.state != request.state:
        raise EligibilityError("授权请求与设备描述不匹配")

    try:
        current_time = ensure_utc(now)
        created_at = ensure_utc(description.created_at)
        expires_at = ensure_utc(description.expires_at)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise EligibilityError("设备授权描述时间无效") from None
    if created_at > current_time or current_time >= expires_at or created_at >= expires_at:
        raise EligibilityError("设备授权请求已过期或时间无效")

    device_name = description.device_name.strip()
    if not device_name or any(_is_control(character) for character in device_name):
        raise EligibilityError("设备授权描述名称无效")


def _is_control(character: str) -> bool:
    return unicodedata.category(character) == "Cc"


def _has_valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in hexdigits or value[index + 2] not in hexdigits:
            return False
        index += 3
    return True
