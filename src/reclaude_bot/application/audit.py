from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reclaude_bot.infrastructure.db.models import AuditLog


def utcnow() -> datetime:
    return datetime.now(UTC)


async def audit(
    session: AsyncSession,
    *,
    actor_telegram_id: int | None,
    actor_type: str,
    action: str,
    target_type: str,
    target_id: str,
    parameters_summary: dict[str, Any] | None = None,
    result: str = "SUCCESS",
) -> AuditLog:
    row = AuditLog(
        actor_telegram_id=actor_telegram_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        parameters_summary=parameters_summary or {},
        result=result,
        created_at=utcnow(),
    )
    session.add(row)
    return row
