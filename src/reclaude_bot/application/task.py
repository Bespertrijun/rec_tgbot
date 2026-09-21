from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.domain.enums import TaskStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import DEFAULT_QUOTA_LIMIT, as_decimal
from reclaude_bot.infrastructure.db.models import QuotaTask, QuotaTaskMember, RuntimeSetting, ServiceState, UpstreamMember
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway

ALL = "ALL"
ALLOWLIST = "ALLOWLIST"
EXCLUDE = "EXCLUDE"

_NAME_MAX_LENGTH = 32
_RESERVED_NAMES = frozenset({"all"})


@dataclass(frozen=True)
class TaskSnapshot:
    id: int
    name: str
    enabled: bool
    scope_mode: str
    limit_usd: Decimal
    member_ids: tuple[str, ...]
    missing_member_ids: tuple[str, ...]
    updated_at: datetime | None
    updated_by: int | None


class QuotaTaskService:
    """Durable per-task lifecycle, limits, and member scopes for the quota loop."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: ReclaudeGateway | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway

    async def resolve(self, name: str | None) -> str:
        """Resolve an optional task name to an existing task's display name."""
        async with self.session_factory() as session:
            if name and name.strip():
                actual = await session.scalar(select(QuotaTask.name).where(QuotaTask.name_normalized == name.strip().casefold()))
                if actual is None:
                    raise EligibilityError(f"任务不存在：{name.strip()}，请使用 /task 查看现有任务")
                return actual
            names = list((await session.scalars(select(QuotaTask.name).order_by(QuotaTask.name_normalized.asc()))).all())
        if not names:
            raise EligibilityError("暂无限额任务，请先使用 /newtask 创建")
        if len(names) > 1:
            raise EligibilityError(f"存在多个任务，请指定名称：{', '.join(names)}")
        return names[0]

    async def resolve_members_args(self, values: list[str], *, usage: str) -> tuple[str, list[str]]:
        """Split ``[task?, member...]`` args; the name is optional only when unambiguous."""
        if not values:
            raise EligibilityError(usage)
        first = values[0]
        async with self.session_factory() as session:
            exists = await session.scalar(select(QuotaTask.id).where(QuotaTask.name_normalized == first.casefold()))
        if exists is not None:
            rest = values[1:]
            if not rest:
                raise EligibilityError("请至少提供一个 reclaude_user_id")
            return first, rest
        return await self.resolve(None), values

    async def create_task(self, name: str, limit: Decimal | None, operator_id: int) -> TaskSnapshot:
        normalized = self._normalize_name(name)
        display = name.strip()
        async with self.session_factory() as session:
            async with session.begin():
                existing = await session.scalar(select(QuotaTask).where(QuotaTask.name_normalized == normalized))
                if existing is not None:
                    raise EligibilityError(f"任务已存在：{existing.name}")
                if limit is None:
                    setting = await session.get(RuntimeSetting, 1)
                    limit = as_decimal(setting.quota_limit_usd) if setting is not None else DEFAULT_QUOTA_LIMIT
                else:
                    limit = as_decimal(limit)
                if limit < 0:
                    raise EligibilityError("额度不能为负数")
                now = utcnow()
                task = QuotaTask(
                    name=display,
                    name_normalized=normalized,
                    status=TaskStatus.STOPPED.value,
                    scope_mode=ALL,
                    limit_usd=limit,
                    created_by=operator_id,
                    updated_by=operator_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(task)
                await session.flush()
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_CREATED",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": display, "limit_usd": str(limit)},
                )
        return await self.snapshot(display)

    async def delete_task(self, name: str, operator_id: int) -> None:
        resolved = await self.resolve(name)
        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                now = utcnow()
                await session.execute(delete(QuotaTaskMember).where(QuotaTaskMember.task_id == task.id))
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_DELETED",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": task.name},
                )
                await session.delete(task)
                await session.flush()
                await self._refresh_latch(session, now)

    async def list_tasks(self) -> list[TaskSnapshot]:
        async with self.session_factory() as session:
            tasks = list((await session.scalars(select(QuotaTask).order_by(QuotaTask.name_normalized.asc()))).all())
            rows = list((await session.scalars(select(QuotaTaskMember).order_by(QuotaTaskMember.reclaude_user_id.asc()))).all())
            upstream_ids = set((await session.scalars(select(UpstreamMember.reclaude_user_id))).all())
            members_by_task: dict[int, list[str]] = {}
            for row in rows:
                members_by_task.setdefault(row.task_id, []).append(row.reclaude_user_id)
            return [self._snapshot(task, tuple(members_by_task.get(task.id, ())), upstream_ids) for task in tasks]

    async def snapshot(self, name: str | None = None) -> TaskSnapshot:
        resolved = await self.resolve(name)
        async with self.session_factory() as session:
            task = await self._get_task(session, resolved)
            rows = tuple(
                (
                    await session.scalars(
                        select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id).order_by(QuotaTaskMember.reclaude_user_id.asc())
                    )
                ).all()
            )
            upstream_ids = set((await session.scalars(select(UpstreamMember.reclaude_user_id))).all())
            return self._snapshot(task, rows, upstream_ids)

    async def start(self, name: str | None, operator_id: int | None = None) -> bool:
        """Mark a task RUNNING and open the global write latch."""

        resolved = await self.resolve(name)
        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                state = await self._ensure_state(session, with_for_update=True)
                if state.selected_account_id is None:
                    raise EligibilityError("尚未选择 Reclaude 账号，请先使用 /use account_id")
                now = utcnow()
                changed = task.status != TaskStatus.RUNNING.value
                task.status = TaskStatus.RUNNING.value
                task.updated_at = now
                task.updated_by = operator_id
                await self._refresh_latch(session, now)
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="QUOTA_TASK_STARTED",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    result="SUCCESS" if changed else "NOOP",
                    parameters_summary={"name": task.name},
                )
                return changed

    async def stop(self, name: str | None, operator_id: int | None = None, *, reason: str = "quota_task_stopped") -> bool:
        """Mark a task STOPPED; the global latch closes when no task keeps running."""

        resolved = await self.resolve(name)
        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                now = utcnow()
                changed = task.status != TaskStatus.STOPPED.value
                task.status = TaskStatus.STOPPED.value
                task.updated_at = now
                task.updated_by = operator_id
                await self._refresh_latch(session, now)
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="QUOTA_TASK_STOPPED",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    result="SUCCESS" if changed else "NOOP",
                    parameters_summary={"name": task.name, "reason": reason},
                )
                return changed

    async def enable_latch(self, *, reason: str = "quota_task_resumed") -> bool:
        """Re-open the global write latch when at least one task is RUNNING."""

        async with self.session_factory() as session:
            async with session.begin():
                if not await self._any_running(session):
                    return False
                state = await self._ensure_state(session, with_for_update=True)
                state.write_enabled = True
                state.reason = reason
                state.updated_at = utcnow()
                return True

    async def any_enabled(self) -> bool:
        async with self.session_factory() as session:
            return await self._any_running(session)

    async def sync_enabled(self) -> bool:
        async with self.session_factory() as session:
            state = await self._ensure_state(session)
            return bool(state.sync_enabled)

    async def set_sync_enabled(self, enabled: bool, operator_id: int | None = None) -> bool:
        """Flip the durable usage-sync switch; returns True when it changed."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                changed = bool(state.sync_enabled) != enabled
                state.sync_enabled = enabled
                state.updated_at = utcnow()
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="USAGE_SYNC_STARTED" if enabled else "USAGE_SYNC_STOPPED",
                    target_type="SERVICE",
                    target_id="1",
                    result="SUCCESS" if changed else "NOOP",
                )
                return changed

    async def set_limit(self, name: str | None, amount: Decimal, operator_id: int) -> tuple[str, Decimal]:
        resolved = await self.resolve(name)
        amount = as_decimal(amount)
        if amount < 0:
            raise EligibilityError("额度不能为负数")
        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                old = as_decimal(task.limit_usd)
                now = utcnow()
                task.limit_usd = amount
                task.updated_at = now
                task.updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="SET_TASK_QUOTA",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": task.name, "old_limit_usd": str(old), "new_limit_usd": str(amount)},
                )
                return task.name, amount

    async def add_members(self, name: str | None, member_ids: list[str], operator_id: int) -> tuple[str, ...]:
        resolved = await self.resolve(name)
        values = self._normalize_ids(member_ids)
        if len(values) == 1 and values[0].casefold() == "all":
            await self.reset_all(resolved, operator_id)
            return ()
        if any(value.casefold() == "all" for value in values):
            raise EligibilityError("/addtaskmember all 只能单独使用")
        async with self.session_factory() as session:
            task = await self._get_task(session, resolved)
            configured = set((await session.scalars(select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id))).all())
        available = await self._latest_member_ids()
        unknown = [value for value in values if value not in available and value not in configured]
        if unknown:
            raise EligibilityError(f"未知上游成员 ID：{', '.join(unknown)}")

        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                now = utcnow()
                if task.scope_mode == EXCLUDE:
                    # An EXCLUDE task covers everyone not listed; adding re-includes the IDs.
                    await session.execute(delete(QuotaTaskMember).where(QuotaTaskMember.task_id == task.id, QuotaTaskMember.reclaude_user_id.in_(values)))
                else:
                    existing = set(
                        (
                            await session.scalars(
                                select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id, QuotaTaskMember.reclaude_user_id.in_(values))
                            )
                        ).all()
                    )
                    for value in values:
                        if value not in existing:
                            session.add(QuotaTaskMember(task_id=task.id, reclaude_user_id=value, added_by=operator_id, added_at=now))
                    task.scope_mode = ALLOWLIST
                task.updated_at = now
                task.updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_ADD",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": task.name, "member_ids": values, "scope_mode": task.scope_mode},
                )
        return tuple(values)

    async def reset_all(self, name: str | None, operator_id: int) -> None:
        resolved = await self.resolve(name)
        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                await session.execute(delete(QuotaTaskMember).where(QuotaTaskMember.task_id == task.id))
                now = utcnow()
                task.scope_mode = ALL
                task.updated_at = now
                task.updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_RESET",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": task.name, "scope_mode": ALL},
                )

    async def delete_members(self, name: str | None, member_ids: list[str], operator_id: int) -> tuple[str, ...]:
        resolved = await self.resolve(name)
        values = self._normalize_ids(member_ids)
        if any(value.casefold() == "all" for value in values):
            raise EligibilityError("/deletetaskmember all 不支持，请使用 /addtaskmember <任务名> all")
        async with self.session_factory() as session:
            task = await self._get_task(session, resolved)
            configured = set((await session.scalars(select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id))).all())
        available = await self._latest_member_ids()
        unknown = [value for value in values if value not in available and value not in configured]
        if unknown:
            raise EligibilityError(f"未知上游成员 ID：{', '.join(unknown)}")

        async with self.session_factory() as session:
            async with session.begin():
                task = await self._get_task(session, resolved, with_for_update=True)
                now = utcnow()
                if task.scope_mode == ALLOWLIST:
                    await session.execute(delete(QuotaTaskMember).where(QuotaTaskMember.task_id == task.id, QuotaTaskMember.reclaude_user_id.in_(values)))
                else:
                    # ALL or EXCLUDE: deleting excludes the IDs while everyone else stays covered.
                    existing = set(
                        (
                            await session.scalars(
                                select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.task_id == task.id, QuotaTaskMember.reclaude_user_id.in_(values))
                            )
                        ).all()
                    )
                    for value in values:
                        if value not in existing:
                            session.add(QuotaTaskMember(task_id=task.id, reclaude_user_id=value, added_by=operator_id, added_at=now))
                    task.scope_mode = EXCLUDE
                task.updated_at = now
                task.updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_DELETE",
                    target_type="QUOTA_TASK",
                    target_id=str(task.id),
                    parameters_summary={"name": task.name, "member_ids": values, "scope_mode": task.scope_mode},
                )
        return tuple(values)

    @staticmethod
    def _snapshot(task: QuotaTask, member_ids: tuple[str, ...], upstream_ids: set[str]) -> TaskSnapshot:
        return TaskSnapshot(
            id=task.id,
            name=task.name,
            enabled=task.status == TaskStatus.RUNNING.value,
            scope_mode=task.scope_mode if task.scope_mode in (ALL, ALLOWLIST, EXCLUDE) else ALL,
            limit_usd=as_decimal(task.limit_usd),
            member_ids=member_ids,
            missing_member_ids=tuple(value for value in member_ids if value not in upstream_ids),
            updated_at=task.updated_at,
            updated_by=task.updated_by,
        )

    async def _get_task(self, session: AsyncSession, name: str, *, with_for_update: bool = False) -> QuotaTask:
        query = select(QuotaTask).where(QuotaTask.name_normalized == name.strip().casefold())
        if with_for_update:
            query = query.with_for_update()
        task = await session.scalar(query)
        if task is None:
            raise EligibilityError(f"任务不存在：{name.strip()}，请使用 /task 查看现有任务")
        return task

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = name.strip()
        if not value or len(value) > _NAME_MAX_LENGTH or any(char.isspace() for char in value):
            raise EligibilityError("任务名称需为 1-32 个非空白字符")
        if value.casefold() in _RESERVED_NAMES:
            raise EligibilityError(f"任务名称 {value} 为保留字")
        return value.casefold()

    @staticmethod
    async def _any_running(session: AsyncSession) -> bool:
        return bool(await session.scalar(select(func.count(QuotaTask.id)).where(QuotaTask.status == TaskStatus.RUNNING.value)))

    async def _refresh_latch(self, session: AsyncSession, now: datetime) -> None:
        """Open the global write latch while any task runs; close it otherwise."""

        state = await self._ensure_state(session, with_for_update=True)
        if await self._any_running(session):
            if not state.write_enabled:
                state.write_enabled = True
                state.reason = "quota_task_started"
                state.updated_at = now
        elif state.write_enabled:
            state.write_enabled = False
            state.reason = "quota_task_stopped"
            state.updated_at = now

    @staticmethod
    async def _ensure_state(session: AsyncSession, *, with_for_update: bool = False) -> ServiceState:
        state = await session.get(ServiceState, 1, with_for_update=with_for_update)
        if state is None:
            now = utcnow()
            state = ServiceState(id=1, write_enabled=False, reason="startup_recovery_required", updated_at=now)
            session.add(state)
            await session.flush()
        return state

    async def _latest_member_ids(self) -> set[str]:
        if self.gateway is None:
            raise EligibilityError("Reclaude 成员同步尚未初始化")
        try:
            response = await self.gateway.members()
        except Exception as exc:
            raise EligibilityError("无法读取最新 Reclaude 成员列表") from exc
        return {str(member.user_id) for member in response.items}

    @staticmethod
    def _normalize_ids(member_ids: list[str]) -> list[str]:
        values = [str(value).strip() for value in member_ids if str(value).strip()]
        if not values:
            raise EligibilityError("请至少提供一个 reclaude_user_id")
        if any(len(value) > 128 or any(char.isspace() for char in value) for value in values):
            raise EligibilityError("reclaude_user_id 格式无效")
        if len(values) != len(set(values)):
            raise EligibilityError("成员 ID 不能重复")
        return values


__all__ = ["ALL", "ALLOWLIST", "EXCLUDE", "QuotaTaskService", "TaskSnapshot"]
