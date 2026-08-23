from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import QuotaTaskMember, ServiceState, UpstreamMember
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway

ALL = "ALL"
ALLOWLIST = "ALLOWLIST"


@dataclass(frozen=True)
class TaskSnapshot:
    enabled: bool
    scope_mode: str
    member_ids: tuple[str, ...]
    missing_member_ids: tuple[str, ...]
    selected_account_id: str | None
    reason: str
    updated_at: datetime | None
    updated_by: int | None


class QuotaTaskService:
    """Durable operator state for the quota loop and its member scope."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: ReclaudeGateway | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway

    async def ensure_state(self) -> ServiceState:
        async with self.session_factory() as session:
            async with session.begin():
                return await self._ensure_state(session)

    async def get_state(self) -> ServiceState | None:
        async with self.session_factory() as session:
            return await session.get(ServiceState, 1)

    async def start(self, operator_id: int | None = None) -> bool:
        """Atomically enable the durable task and the internal write latch."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                if state.selected_account_id is None:
                    raise EligibilityError("尚未选择 Reclaude 账号，请先使用 /use account_id")
                now = utcnow()
                changed = not state.quota_task_enabled or not state.write_enabled
                state.quota_task_enabled = True
                state.write_enabled = True
                state.reason = "quota_task_started"
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="QUOTA_TASK_STARTED",
                    target_type="SERVICE",
                    target_id="1",
                    result="SUCCESS" if changed else "NOOP",
                )
                return changed

    async def stop(self, operator_id: int | None = None, *, reason: str = "quota_task_stopped") -> bool:
        """Atomically disable the durable task and its internal write latch."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                changed = state.quota_task_enabled or state.write_enabled
                state.quota_task_enabled = False
                state.write_enabled = False
                state.reason = reason
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="QUOTA_TASK_STOPPED",
                    target_type="SERVICE",
                    target_id="1",
                    result="SUCCESS" if changed else "NOOP",
                    parameters_summary={"reason": reason},
                )
                return changed

    async def force_stop(self, reason: str) -> None:
        """Emergency/internal stop used by startup validation and 401 recovery."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                state.quota_task_enabled = False
                state.write_enabled = False
                state.reason = reason
                state.updated_at = now
                state.quota_task_updated_at = now

    async def enable_latch(self, *, reason: str = "quota_task_resumed") -> bool:
        """Re-open only the internal latch after persisted-task startup validation."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                if not state.quota_task_enabled:
                    return False
                state.write_enabled = True
                state.reason = reason
                state.updated_at = utcnow()
                return True

    async def persist_selected_account(self, account_id: int | str, operator_id: int | None = None) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                state.selected_account_id = str(account_id)
                # Account selection never opens the write latch.
                state.write_enabled = False
                state.reason = "account_selected_task_stopped"
                state.updated_at = now
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="SELECT_ACCOUNT",
                    target_type="SERVICE",
                    target_id="1",
                    parameters_summary={"account_id": str(account_id)},
                )

    async def add_members(self, member_ids: list[str], operator_id: int) -> tuple[str, ...]:
        values = self._normalize_ids(member_ids)
        if len(values) == 1 and values[0].casefold() == "all":
            await self.reset_all(operator_id)
            return ()
        if any(value.casefold() == "all" for value in values):
            raise EligibilityError("/addtaskmember all 只能单独使用")
        available = await self._latest_member_ids()
        unknown = [value for value in values if value not in available]
        if unknown:
            raise EligibilityError(f"未知上游成员 ID：{', '.join(unknown)}")

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                existing = set(
                    (
                        await session.scalars(
                            select(QuotaTaskMember.reclaude_user_id).where(QuotaTaskMember.reclaude_user_id.in_(values))
                        )
                    ).all()
                )
                now = utcnow()
                for value in values:
                    if value not in existing:
                        session.add(QuotaTaskMember(reclaude_user_id=value, added_by=operator_id, added_at=now))
                state.quota_task_scope_mode = ALLOWLIST
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_ADD",
                    target_type="SERVICE",
                    target_id="1",
                    parameters_summary={"member_ids": values, "scope_mode": ALLOWLIST},
                )
        return tuple(values)

    async def reset_all(self, operator_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                await session.execute(delete(QuotaTaskMember))
                now = utcnow()
                state.quota_task_scope_mode = ALL
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_RESET",
                    target_type="SERVICE",
                    target_id="1",
                    parameters_summary={"scope_mode": ALL},
                )

    async def delete_members(self, member_ids: list[str], operator_id: int) -> tuple[str, ...]:
        values = self._normalize_ids(member_ids)
        if any(value.casefold() == "all" for value in values):
            raise EligibilityError("/deletetaskmember all 不支持，请使用 /addtaskmember all")
        async with self.session_factory() as session:
            state = await session.get(ServiceState, 1)
            if state is None or state.quota_task_scope_mode != ALLOWLIST:
                raise EligibilityError("当前任务范围为 ALL；请先使用 /addtaskmember <reclaude_user_id> 创建白名单")
            configured = set(
                (
                    await session.scalars(select(QuotaTaskMember.reclaude_user_id))
                ).all()
            )
        available = await self._latest_member_ids()
        unknown = [value for value in values if value not in available and value not in configured]
        if unknown:
            raise EligibilityError(f"未知上游成员 ID：{', '.join(unknown)}")

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                await session.execute(delete(QuotaTaskMember).where(QuotaTaskMember.reclaude_user_id.in_(values)))
                now = utcnow()
                state.quota_task_scope_mode = ALLOWLIST
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_TASK_SCOPE_DELETE",
                    target_type="SERVICE",
                    target_id="1",
                    parameters_summary={"member_ids": values, "scope_mode": ALLOWLIST},
                )
        return tuple(values)

    async def snapshot(self) -> TaskSnapshot:
        async with self.session_factory() as session:
            state = await session.get(ServiceState, 1)
            rows = list((await session.scalars(select(QuotaTaskMember).order_by(QuotaTaskMember.reclaude_user_id.asc()))).all())
            upstream_ids = set((await session.scalars(select(UpstreamMember.reclaude_user_id))).all())
        if state is None:
            return TaskSnapshot(False, ALL, (), (), None, "startup_recovery_required", None, None)
        mode = state.quota_task_scope_mode if state.quota_task_scope_mode in (ALL, ALLOWLIST) else ALL
        member_ids = tuple(row.reclaude_user_id for row in rows)
        missing = tuple(value for value in member_ids if value not in upstream_ids)
        return TaskSnapshot(
            enabled=bool(state.quota_task_enabled),
            scope_mode=mode,
            member_ids=member_ids,
            missing_member_ids=missing,
            selected_account_id=state.selected_account_id,
            reason=state.reason,
            updated_at=state.quota_task_updated_at or state.updated_at,
            updated_by=state.quota_task_updated_by,
        )

    async def member_ids(self) -> tuple[str, ...]:
        async with self.session_factory() as session:
            state = await session.get(ServiceState, 1)
            if state is None or state.quota_task_scope_mode != ALLOWLIST:
                return ()
            return tuple((await session.scalars(select(QuotaTaskMember.reclaude_user_id))).all())

    async def is_enabled(self) -> bool:
        async with self.session_factory() as session:
            state = await session.get(ServiceState, 1)
            return bool(state and state.quota_task_enabled)

    @staticmethod
    async def _ensure_state(session: AsyncSession, *, with_for_update: bool = False) -> ServiceState:
        state = await session.get(ServiceState, 1, with_for_update=with_for_update)
        if state is None:
            now = utcnow()
            state = ServiceState(
                id=1,
                write_enabled=False,
                quota_task_enabled=False,
                quota_task_scope_mode=ALL,
                reason="startup_recovery_required",
                updated_at=now,
                quota_task_updated_at=now,
            )
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


__all__ = ["ALL", "ALLOWLIST", "QuotaTaskService", "TaskSnapshot"]
