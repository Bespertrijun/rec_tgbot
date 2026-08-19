from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.domain.enums import UserStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, User


class AdminService:
    """Administrative state changes; Telegram-ID authorization remains in handlers."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], quota: QuotaService | None = None, actions: Any | None = None) -> None:
        self.session_factory = session_factory
        self.quota = quota
        self.actions = actions

    async def set_banned(self, user_id: int, operator_id: int, banned: bool) -> User:
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id, with_for_update=True)
                if user is None:
                    raise EligibilityError("用户不存在")
                user.status = UserStatus.BANNED.value if banned else UserStatus.ACTIVE.value
                user.updated_at = utcnow()
                from reclaude_bot.application.audit import audit

                await audit(session, actor_telegram_id=operator_id, actor_type="ADMIN", action="BAN" if banned else "UNBAN", target_type="USER", target_id=str(user.id))
                return user

    async def set_quota(self, amount: Decimal, operator_id: int) -> Decimal:
        if self.quota is None:
            raise EligibilityError("额度服务尚未初始化")
        value = await self.quota.set_quota(amount, operator_id)
        if self.actions is not None:
            await self.actions.reconcile_cached()
        return value

    async def list_users(self) -> list[User]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(User).order_by(User.id.asc()))).all())

    async def recent_audit(self, limit: int = 50) -> list[AuditLog]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit))).all())
