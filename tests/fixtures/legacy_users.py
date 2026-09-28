"""Seed historical REC membership for the old services' transitional regressions.

The production /bind now creates local identities only. These tests exercise old
quota/recovery code until those paths are replaced; membership must be explicit
fixture data, never an implicit requirement of the new binding service.
"""

from sqlalchemy import select

from reclaude_bot.application.binding import BindingService, normalize_email
from reclaude_bot.infrastructure.db.models import CycleBaseline, UpstreamMember, User


class LegacyMemberUserFixture:
    def __init__(self, session_factory, gateway=None):
        self.session_factory = session_factory
        self.binding = BindingService(session_factory, gateway)

    async def bind(self, telegram_user_id, email, **kwargs):
        user = await self.binding.bind(telegram_user_id, email, **kwargs)
        async with self.session_factory.begin() as session:
            member = await session.scalar(select(UpstreamMember).where(UpstreamMember.email_normalized == normalize_email(email)))
            assert member is not None, "legacy service fixture requires a seeded historical member"
            row = await session.get(User, user.id)
            row.reclaude_user_id = member.reclaude_user_id
            user.reclaude_user_id = member.reclaude_user_id
            baselines = (await session.scalars(select(CycleBaseline).where(CycleBaseline.reclaude_user_id == member.reclaude_user_id).order_by(CycleBaseline.cycle_id))).all()
            for baseline in baselines:
                baseline.user_id = row.id
                row.baseline_status = user.baseline_status = baseline.status
        return user
