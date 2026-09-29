from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.infrastructure.db.models import ServiceState
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway


class AccountUsageRefreshService:
    """Refresh the configured organization's snapshot at most once per three hours."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], gateway: ReclaudeGateway) -> None:
        self.factory = factory
        self.gateway = gateway

    async def refresh_if_due(self, *, now: datetime | None = None) -> bool:
        moment = ensure_utc(now or utcnow())
        # Claim atomically and commit before the request. Failed requests (including
        # 429), concurrent queries, and process restarts must all respect the cooldown.
        async with self.factory() as session, session.begin():
            claimed = await session.scalar(
                update(ServiceState)
                .where(
                    ServiceState.id == 1,
                    or_(
                        ServiceState.account_usage_refresh_attempted_at.is_(None),
                        ServiceState.account_usage_refresh_attempted_at <= moment - timedelta(hours=3),
                    ),
                )
                .values(account_usage_refresh_attempted_at=moment)
                .returning(ServiceState.id)
            )
        if claimed is None:
            return False
        await self.gateway.refresh_account_usage()
        return True
