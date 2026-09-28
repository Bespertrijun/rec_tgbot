from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.device_context import SingleOrgTaskService
from reclaude_bot.application.task import ALL, ALLOWLIST, EXCLUDE
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import DeviceTaskMember, DeviceTaskScope, User


@dataclass(frozen=True)
class DeviceTaskMemberSnapshot:
    task_id: int
    name: str
    org_id: int
    scope_mode: str
    member_ids: tuple[int, ...]
    covered_user_ids: tuple[int, ...]


class DeviceTaskMemberService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], org_id: int) -> None:
        self.session_factory = session_factory
        self.task_service = SingleOrgTaskService(session_factory, org_id)
        self.org_id = self.task_service.org_id

    async def snapshot(self, name: str | None = None) -> DeviceTaskMemberSnapshot:
        context = await self.task_service.resolve_task(name)
        async with self.session_factory() as session:
            async with session.begin():
                scope = await self._locked_scope(session, context.task_id)
                member_ids = tuple(
                    (
                        await session.scalars(
                            select(DeviceTaskMember.user_id)
                            .where(DeviceTaskMember.task_id == context.task_id)
                            .order_by(DeviceTaskMember.user_id)
                        )
                    ).all()
                )
                if scope.scope_mode == ALL:
                    covered_user_ids = tuple((await session.scalars(select(User.id).order_by(User.id))).all())
                elif scope.scope_mode == ALLOWLIST:
                    covered_user_ids = member_ids
                elif scope.scope_mode == EXCLUDE:
                    covered_user_ids = tuple(
                        (
                            await session.scalars(
                                select(User.id).where(User.id.not_in(member_ids)).order_by(User.id)
                            )
                        ).all()
                    )
                else:
                    raise EligibilityError("设备任务成员范围配置无效")
                return DeviceTaskMemberSnapshot(
                    task_id=context.task_id,
                    name=context.name,
                    org_id=self.org_id,
                    scope_mode=scope.scope_mode,
                    member_ids=member_ids,
                    covered_user_ids=covered_user_ids,
                )

    async def add_members(self, name: str | None, user_ids: list[int], operator_id: int) -> tuple[int, ...]:
        values = self._normalize_user_ids(user_ids)
        context = await self.task_service.resolve_task(name)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await self._locked_scope(session, context.task_id)
                    await self._validate_users(session, values)
                    now = utcnow()

                    if scope.scope_mode == ALL:
                        await session.execute(delete(DeviceTaskMember).where(DeviceTaskMember.task_id == context.task_id))
                        await self._insert_members(session, context.task_id, values, operator_id, now)
                        scope.scope_mode = ALLOWLIST
                    elif scope.scope_mode == ALLOWLIST:
                        existing = set(
                            (
                                await session.scalars(
                                    select(DeviceTaskMember.user_id).where(
                                        DeviceTaskMember.task_id == context.task_id,
                                        DeviceTaskMember.user_id.in_(values),
                                    )
                                )
                            ).all()
                        )
                        await self._insert_members(
                            session,
                            context.task_id,
                            tuple(user_id for user_id in values if user_id not in existing),
                            operator_id,
                            now,
                        )
                    elif scope.scope_mode == EXCLUDE:
                        await session.execute(
                            delete(DeviceTaskMember).where(
                                DeviceTaskMember.task_id == context.task_id,
                                DeviceTaskMember.user_id.in_(values),
                            )
                        )
                    else:
                        raise EligibilityError("设备任务成员范围配置无效")

                    scope.updated_at = now
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_TASK_MEMBERS_ADD",
                        target_type="DEVICE_TASK_SCOPE",
                        target_id=str(context.task_id),
                        parameters_summary={
                            "name": context.name,
                            "org_id": self.org_id,
                            "user_ids": list(values),
                            "scope_mode": scope.scope_mode,
                        },
                    )
        except IntegrityError:
            raise EligibilityError("本地任务成员范围已变化或用户不存在，请重新查询后重试") from None
        return values

    async def delete_members(self, name: str | None, user_ids: list[int], operator_id: int) -> tuple[int, ...]:
        values = self._normalize_user_ids(user_ids)
        context = await self.task_service.resolve_task(name)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    scope = await self._locked_scope(session, context.task_id)
                    await self._validate_users(session, values)
                    now = utcnow()

                    if scope.scope_mode == ALL:
                        await session.execute(delete(DeviceTaskMember).where(DeviceTaskMember.task_id == context.task_id))
                        await self._insert_members(session, context.task_id, values, operator_id, now)
                        scope.scope_mode = EXCLUDE
                    elif scope.scope_mode == EXCLUDE:
                        existing = set(
                            (
                                await session.scalars(
                                    select(DeviceTaskMember.user_id).where(
                                        DeviceTaskMember.task_id == context.task_id,
                                        DeviceTaskMember.user_id.in_(values),
                                    )
                                )
                            ).all()
                        )
                        await self._insert_members(
                            session,
                            context.task_id,
                            tuple(user_id for user_id in values if user_id not in existing),
                            operator_id,
                            now,
                        )
                    elif scope.scope_mode == ALLOWLIST:
                        await session.execute(
                            delete(DeviceTaskMember).where(
                                DeviceTaskMember.task_id == context.task_id,
                                DeviceTaskMember.user_id.in_(values),
                            )
                        )
                    else:
                        raise EligibilityError("设备任务成员范围配置无效")

                    scope.updated_at = now
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_TASK_MEMBERS_DELETE",
                        target_type="DEVICE_TASK_SCOPE",
                        target_id=str(context.task_id),
                        parameters_summary={
                            "name": context.name,
                            "org_id": self.org_id,
                            "user_ids": list(values),
                            "scope_mode": scope.scope_mode,
                        },
                    )
        except IntegrityError:
            raise EligibilityError("本地任务成员范围已变化或用户不存在，请重新查询后重试") from None
        return values

    async def reset_all(self, name: str | None, operator_id: int) -> None:
        context = await self.task_service.resolve_task(name)
        async with self.session_factory() as session:
            async with session.begin():
                scope = await self._locked_scope(session, context.task_id)
                await session.execute(delete(DeviceTaskMember).where(DeviceTaskMember.task_id == context.task_id))
                now = utcnow()
                scope.scope_mode = ALL
                scope.updated_at = now
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="DEVICE_TASK_MEMBERS_RESET_ALL",
                    target_type="DEVICE_TASK_SCOPE",
                    target_id=str(context.task_id),
                    parameters_summary={"name": context.name, "org_id": self.org_id, "scope_mode": ALL},
                )

    async def _locked_scope(self, session: AsyncSession, task_id: int) -> DeviceTaskScope:
        scope = await session.scalar(
            select(DeviceTaskScope).where(DeviceTaskScope.task_id == task_id).with_for_update()
        )
        if scope is None or scope.org_id != self.org_id:
            raise EligibilityError("任务未配置到当前 Reclaude 组织")
        return scope

    @staticmethod
    def _normalize_user_ids(user_ids: list[int]) -> tuple[int, ...]:
        if not isinstance(user_ids, list) or not user_ids:
            raise EligibilityError("请至少提供一个本地用户 ID")
        if any(isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0 for user_id in user_ids):
            raise EligibilityError("本地用户 ID 必须是正整数")
        return tuple(sorted(set(user_ids)))

    @staticmethod
    async def _validate_users(session: AsyncSession, user_ids: tuple[int, ...]) -> None:
        existing = set((await session.scalars(select(User.id).where(User.id.in_(user_ids)))).all())
        missing = [user_id for user_id in user_ids if user_id not in existing]
        if missing:
            raise EligibilityError(f"本地用户不存在：{', '.join(str(user_id) for user_id in missing)}")

    @staticmethod
    async def _insert_members(
        session: AsyncSession,
        task_id: int,
        user_ids: tuple[int, ...],
        operator_id: int,
        now,
    ) -> None:
        session.add_all(
            DeviceTaskMember(task_id=task_id, user_id=user_id, added_by=operator_id, added_at=now)
            for user_id in user_ids
        )
