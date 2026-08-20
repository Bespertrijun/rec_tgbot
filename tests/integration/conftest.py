from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.db.base import Base
from reclaude_bot.infrastructure.reclaude.fake import FakeReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import CurrentAccount, Member, MeResponse, SevenDay, UsageSnapshot, WeeklyLimit


@pytest_asyncio.fixture
async def app_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    reset = datetime(2026, 8, 25, tzinfo=UTC)
    me = MeResponse(
        current_account=CurrentAccount(
            status="bound",
            email_masked="owner***@example.com",
            usage_updated_at=datetime(2026, 8, 18, tzinfo=UTC),
            usage_snapshot=UsageSnapshot(
                limits=[WeeklyLimit(group="weekly", kind="weekly_all", scope=None, percent="10", resets_at=reset, is_active=True)],
                seven_day=SevenDay(utilization="10", resets_at=reset),
            ),
        ),
    )
    gateway = FakeReclaudeGateway(
        [Member(user_id="u-1", email="one@example.com", account_id=None, total_usage_usd="0")],
        me,
        accounts=[{"id": 4949, "email_masked": me.current_account.email_masked}],
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_ADMIN_IDS=[1],
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        RECLAUDE_ACCOUNT_EMAIL_MASKED="owner***@example.com",
        BASELINE_CAPTURE_WINDOW_SECONDS=60,
    )
    yield factory, gateway, settings
    await engine.dispose()
