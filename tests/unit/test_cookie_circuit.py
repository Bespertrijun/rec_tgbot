import asyncio

import httpx
import pytest

from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError, UpstreamError
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient


def _replace_transport(client: ReclaudeClient, handler) -> None:
    client._client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_first_401_opens_circuit_and_prevents_second_request(tmp_path) -> None:
    calls = 0
    alerts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, request=request)

    async def alert() -> None:
        nonlocal alerts
        alerts += 1

    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test", cookie_jar_path=tmp_path / "cookies.json", auth_alert_callback=alert)
    await client._client.aclose()
    _replace_transport(client, handler)
    with pytest.raises(AuthenticationCircuitOpen):
        await client.me()
    with pytest.raises(AuthenticationCircuitOpen):
        await client.me()
    assert calls == 1
    assert alerts == 1
    await client.close()


@pytest.mark.asyncio
async def test_cookie_jar_takes_precedence_but_invalid_jar_falls_back_to_environment(tmp_path) -> None:
    jar = tmp_path / "cookies.json"
    jar.write_text('{"rc_sid": "jar-value"}')
    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=env-value", cookie_jar_path=jar)
    assert client._client.cookies.get("rc_sid") == "jar-value"
    assert oct(jar.stat().st_mode & 0o777) == "0o600"
    await client.close()

    jar.write_text("{}")
    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=env-value", cookie_jar_path=jar)
    assert client._client.cookies.get("rc_sid") == "env-value"
    assert jar.read_text() == '{"rc_sid": "env-value"}'
    await client.close()


def test_client_requires_a_valid_cookie(tmp_path) -> None:
    with pytest.raises(ValueError, match="session cookie"):
        ReclaudeClient("https://example.test", cookie_jar_path=tmp_path / "missing.json")


@pytest.mark.asyncio
async def test_concurrent_requests_stop_after_first_401(tmp_path) -> None:
    calls = 0
    alerts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, request=request)

    async def alert() -> None:
        nonlocal alerts
        alerts += 1

    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test", cookie_jar_path=tmp_path / "cookies.json", auth_alert_callback=alert)
    await client._client.aclose()
    _replace_transport(client, handler)
    results = await asyncio.gather(client.me(), client.me(), return_exceptions=True)
    assert all(isinstance(item, AuthenticationCircuitOpen) for item in results)
    assert calls == 1
    assert alerts == 1
    with pytest.raises(AuthenticationCircuitOpen):
        await client.me()
    assert calls == 1
    assert alerts == 1
    await client.close()


@pytest.mark.asyncio
async def test_get_retries_but_write_request_does_not(tmp_path) -> None:
    calls = 0
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        paths.append(request.url.path)
        return httpx.Response(503, request=request)

    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test", cookie_jar_path=tmp_path / "cookies.json", max_retries=2)
    await client._client.aclose()
    _replace_transport(client, handler)
    with pytest.raises(UpstreamError):
        await client.members()
    assert calls == 3

    calls = 0
    paths.clear()
    client.configure_account_id(8123)
    with pytest.raises(UpstreamError):
        await client.assign("u-1")
    assert calls == 1
    assert paths == ["/api/app/orgs/178/accounts/8123/assignments"]
    await client.close()


@pytest.mark.asyncio
async def test_assign_requires_recovery_account_configuration(tmp_path) -> None:
    client = ReclaudeClient("https://example.test", session_cookie="rc_sid=test", cookie_jar_path=tmp_path / "cookies.json")
    with pytest.raises(EligibilityError, match="尚未完成恢复配置"):
        await client.assign("u-1")
    await client.close()
