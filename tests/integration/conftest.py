import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.config import Settings, validate_postgresql_database_url
from reclaude_bot.infrastructure.db import models  # noqa: F401
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
        accounts=[
            {
                "id": 7022,
                "account_email": "owner@example.com",
                "account_id": 4949,
                "health": "healthy",
                "lifecycle": "bound",
                "org_id": 178,
            }
        ],
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_ADMIN_IDS=[1],
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        BASELINE_CAPTURE_WINDOW_SECONDS=60,
    )
    yield factory, gateway, settings
    await engine.dispose()


@pytest_asyncio.fixture
async def postgres_factories():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL concurrency test requires TEST_DATABASE_URL=postgresql+asyncpg://...")
    try:
        validated_url = validate_postgresql_database_url(database_url)
        parsed_url = make_url(validated_url)
    except ValueError as exc:
        pytest.fail(f"TEST_DATABASE_URL is invalid: {exc}", pytrace=False)
    if not parsed_url.database or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with '_test'", pytrace=False)
    engine = create_async_engine(validated_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    except (OSError, SQLAlchemyError) as exc:
        await engine.dispose()
        pytest.fail(f"PostgreSQL test database unavailable ({type(exc).__name__})", pytrace=False)
    first = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    second = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield first, second
    finally:
        await engine.dispose()
