from __future__ import annotations

import re
from pathlib import Path

import pytest

from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient
from reclaude_bot.infrastructure.reclaude.constants import DEFAULT_RECLAUDE_USER_AGENT


def test_default_user_agent_is_a_linux_chrome_browser_ua() -> None:
    settings = Settings()
    assert settings.reclaude_user_agent == DEFAULT_RECLAUDE_USER_AGENT
    assert re.fullmatch(
        r"Mozilla/5\.0 \(X11; Linux x86_64\) AppleWebKit/537\.36 "
        r"\(KHTML, like Gecko\) Chrome/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ Safari/537\.36",
        settings.reclaude_user_agent,
    )


@pytest.mark.asyncio
async def test_direct_client_uses_the_same_default_user_agent() -> None:
    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test")
    try:
        assert client._client.headers["user-agent"] == DEFAULT_RECLAUDE_USER_AGENT
    finally:
        await client.close()


def test_compose_pins_cookie_jar_to_persistent_volume() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert "RECLAUDE_COOKIE_JAR_PATH: /var/lib/reclaude-bot/cookies/cookies.json" in compose
    assert "- reclaude-cookies:/var/lib/reclaude-bot/cookies" in compose
