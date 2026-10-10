import asyncio
from datetime import datetime, timedelta

from sqlalchemy import false, literal, or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from reclaude_bot.application.audit import utcnow
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import ServiceState
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway


class AccountUsageRefreshService:
    """Refresh every three hours for queries, or explicitly for a new reset round."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], gateway: ReclaudeGateway) -> None:
        self.factory = factory
        self.gateway = gateway
        self._lock = asyncio.Lock()

    async def refresh_if_due(
        self, *, now: datetime | None = None, force: bool = False,
        expired_cycle: bool = False, before_reset_at: datetime | None = None,
    ) -> bool:
        async with self._lock:
            moment = ensure_utc(now or utcnow())
            deadline_due: ColumnElement[bool] = false()
            if before_reset_at is not None:
                reset_at = ensure_utc(before_reset_at)
                deadline = reset_at - timedelta(minutes=1)
                if deadline <= moment < reset_at:
                    # Reuse the durable claim to permit one attempt in the
                    # final minute, including across concurrent callers/restarts.
                    deadline_due = ServiceState.account_usage_refresh_attempted_at < deadline
            # Expired windows must recover before reconciliation can run. Keep a
            # durable retry bound even when REC keeps returning an expired window.
            cooldown = timedelta(minutes=5) if expired_cycle else timedelta(hours=3)
            # Claim atomically and commit before the request. Failed requests (including
            # 429), concurrent queries, and process restarts must all respect the cooldown.
            async with self.factory() as session, session.begin():
                claimed = await session.scalar(
                    update(ServiceState)
                    .where(
                        ServiceState.id == 1,
                        or_(
                            literal(force),
                            deadline_due,
                            ServiceState.account_usage_refresh_attempted_at.is_(None),
                            ServiceState.account_usage_refresh_attempted_at <= moment - cooldown,
                        ),
                    )
                    .values(account_usage_refresh_attempted_at=moment)
                    .returning(ServiceState.id)
                )
            if claimed is None:
                return False
            await self.gateway.refresh_account_usage()
            return True
