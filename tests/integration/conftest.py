import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reclaude_bot.config import Settings, validate_postgresql_database_url
from reclaude_bot.infrastructure.db import models  # noqa: F401
from reclaude_bot.infrastructure.db.base import Base
from reclaude_bot.infrastructure.db.models import Device, DeviceTaskScope, QuotaTask, User
from reclaude_bot.infrastructure.reclaude.fake import FakeReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import CurrentAccount, Member, MeResponse, SevenDay, UsageSnapshot, WeeklyLimit


@pytest.fixture
def fixed_clock(monkeypatch):
    """Keep integration paths that use implicit timestamps inside the fixture cycle."""
    current = [datetime(2026, 8, 18, tzinfo=UTC)]

    def fixed_now() -> datetime:
        return current[0]

    for target in (
        "reclaude_bot.application.audit.utcnow",
        "reclaude_bot.application.actions.utcnow",
        "reclaude_bot.application.admin.utcnow",
        "reclaude_bot.application.binding.utcnow",
        "reclaude_bot.application.quota.utcnow",
        "reclaude_bot.application.recovery.utcnow",
        "reclaude_bot.application.task.utcnow",
    ):
        monkeypatch.setattr(target, fixed_now)
    return current


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


def postgres_test_url():
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("requires isolated TEST_DATABASE_URL")
    url = make_url(validate_postgresql_database_url(raw))
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("test database name must end with _test")
    return url


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def lifecycle_db(request):
    device_now = datetime(2026, 9, 28, tzinfo=UTC)
    schema = "device_test_" + uuid4().hex
    if request.param == "postgresql":
        url = postgres_test_url()
        admin_engine = create_async_engine(url)
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        admin_engine = None
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(engine.sync_engine, "connect")
        def enable_fks(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            session.add_all([
                User(id=i, telegram_user_id=1000 + i, email=f"{i}@example.invalid", email_normalized=f"{i}@example.invalid",
                     reclaude_user_id=f"legacy-{i}", bound_at=device_now, updated_at=device_now)
                for i in (1, 2)
            ])
            session.add_all([
                QuotaTask(id=i, name=f"task-{i}", name_normalized=f"task-{i}", limit_usd=Decimal("700"), created_at=device_now, updated_at=device_now)
                for i in (1, 2)
            ])
            await session.flush()
            session.add_all([
                DeviceTaskScope(task_id=i, org_id=177 + i, created_at=device_now, updated_at=device_now)
                for i in (1, 2)
            ])
            session.add_all([
                Device(org_id=org, device_id=device, name="test", first_synced_at=device_now, last_synced_at=device_now)
                for org, device in ((178, 44500), (178, 44501), (179, 44502))
            ])
            await session.flush()
            if request.param == "postgresql":
                # Explicit fixture IDs do not advance PostgreSQL sequences.
                # New local users must receive IDs beyond the seeded rows.
                await session.execute(text("SELECT setval(pg_get_serial_sequence('users', 'id'), (SELECT max(id) FROM users))"))
                await session.execute(text("SELECT setval(pg_get_serial_sequence('quota_tasks', 'id'), (SELECT max(id) FROM quota_tasks))"))
        yield factory, request.param
    finally:
        await engine.dispose()
        if admin_engine is not None:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()
