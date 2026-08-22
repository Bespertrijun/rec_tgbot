from __future__ import annotations

import re
from pathlib import Path

import pytest

from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.db.database import create_session_factory
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient
from reclaude_bot.infrastructure.reclaude.constants import DEFAULT_RECLAUDE_USER_AGENT

TEST_DATABASE_URL = "postgresql+asyncpg://test:test@localhost/test"


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg,arg-type]


def test_default_user_agent_is_a_linux_chrome_browser_ua() -> None:
    settings = _settings(DATABASE_URL=TEST_DATABASE_URL)
    assert settings.reclaude_user_agent == DEFAULT_RECLAUDE_USER_AGENT
    assert re.fullmatch(
        r"Mozilla/5\.0 \(X11; Linux x86_64\) AppleWebKit/537\.36 "
        r"\(KHTML, like Gecko\) Chrome/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ Safari/537\.36",
        settings.reclaude_user_agent,
    )


def test_numeric_admin_ids_env_value_is_a_single_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456789")

    settings = _settings(DATABASE_URL=TEST_DATABASE_URL)

    assert settings.telegram_admin_ids == [123456789]


def test_admin_ids_accept_comma_separated_values() -> None:
    settings = Settings.model_validate({"DATABASE_URL": TEST_DATABASE_URL, "TELEGRAM_ADMIN_IDS": "123456789, 987654321"})

    assert settings.telegram_admin_ids == [123456789, 987654321]


@pytest.mark.asyncio
async def test_direct_client_uses_the_same_default_user_agent() -> None:
    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test")
    try:
        assert client._client.headers["user-agent"] == DEFAULT_RECLAUDE_USER_AGENT
    finally:
        await client.close()


def test_database_url_is_required() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        _settings()


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+aiosqlite:///:memory:",
        "postgresql://user:password@localhost/reclaude",
        "postgresql+asyncpg://user:password@/reclaude",
    ),
)
def test_database_url_must_be_async_postgresql(database_url: str) -> None:
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        _settings(DATABASE_URL=database_url)


def test_database_url_accepts_async_postgresql_without_echoing_password() -> None:
    password = "not-for-logs"
    settings = _settings(DATABASE_URL=f"postgresql+asyncpg://bot:{password}@localhost/reclaude")
    assert settings.database_url.endswith("@localhost/reclaude")
    with pytest.raises(ValueError) as caught:
        _settings(DATABASE_URL=f"postgresql+asyncpg://bot:{password}@/reclaude")
    assert password not in str(caught.value)


def test_database_factory_rejects_bypassed_sqlite_settings() -> None:
    class BypassedSettings:
        database_url = "sqlite+aiosqlite:///:memory:"

    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        create_session_factory(BypassedSettings())  # type: ignore[arg-type]


def test_compose_uses_project_local_persistent_bind_mounts() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert "RECLAUDE_COOKIE_JAR_PATH: /var/lib/reclaude-bot/cookies/cookies.json" in compose
    assert "- ./data/postgres:/var/lib/postgresql/data" in compose
    assert "- ./data/cookies:/var/lib/reclaude-bot/cookies" in compose
    assert "postgres-data:" not in compose
    assert "reclaude-cookies:" not in compose
    assert "\nvolumes:\n" not in compose
