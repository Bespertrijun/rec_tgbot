from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, UpstreamError
from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient

ME_PAYLOAD = {
    "current_account": {
        "status": "bound",
        "email_masked": "owner***@example.com",
        "usage_updated_at": "2026-08-18T05:00:00Z",
        "usage_snapshot": {
            "limits": [
                {
                    "group": "weekly",
                    "kind": "weekly_all",
                    "scope": None,
                    "percent": "42.50",
                    "resets_at": "2026-08-25T05:00:00Z",
                    "is_active": True,
                }
            ],
            "seven_day": {"utilization": "42.50", "resets_at": "2026-08-25T05:00:00Z"},
        },
    }
}


async def _replace_transport(client: ReclaudeClient, handler) -> None:
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_password_login_persists_set_cookie_and_verifies_me(tmp_path: Path) -> None:
    calls: list[str] = []
    password = "correct horse battery staple"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            assert request.headers.get("authorization") is None
            assert request.headers.get("cookie") is None
            assert json.loads(request.content) == {"email": "owner@example.com", "password": password}
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"set-cookie": "rc_sid=fresh-session; Path=/; HttpOnly"},
                request=request,
            )
        assert request.url.path == "/api/app/me"
        assert "rc_sid=fresh-session" in request.headers.get("cookie", "")
        return httpx.Response(200, json=ME_PAYLOAD, request=request)

    jar = tmp_path / "cookies.json"
    client = ReclaudeClient(
        "https://example.test",
        login_email="owner@example.com",
        login_password=SecretStr(password),
        cookie_jar_path=jar,
    )
    await _replace_transport(client, handler)
    try:
        me = await client.authenticate()
        assert me.current_account.status == "bound"
        assert calls == ["/api/auth/login", "/api/app/me"]
        assert oct(jar.stat().st_mode & 0o777) == "0o600"
        assert json.loads(jar.read_text()) == {"rc_sid": "fresh-session"}
        assert password not in jar.read_text()
        assert password not in repr(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_cookie_recovery_logs_in_once_and_clears_circuit(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/app/me" and calls.count("/api/app/me") == 1:
            return httpx.Response(401, request=request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"set-cookie": "rc_sid=recovered-session; Path=/"},
                request=request,
            )
        return httpx.Response(200, json=ME_PAYLOAD, request=request)

    jar = tmp_path / "cookies.json"
    client = ReclaudeClient(
        "https://example.test",
        session_cookie="rc_sid=expired-session",
        login_email="owner@example.com",
        login_password="correct-password",
        cookie_jar_path=jar,
    )
    await _replace_transport(client, handler)
    # Replacing the transport in this test does not alter the client's loaded-session marker.
    try:
        me = await client.authenticate()
        assert me.current_account.status == "bound"
        assert calls == ["/api/app/me", "/api/auth/login", "/api/app/me"]
        assert client.circuit_open is False
        assert json.loads(jar.read_text()) == {"rc_sid": "recovered-session"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrong_password_is_safe_and_not_retried(caplog, tmp_path: Path) -> None:
    calls = 0
    password = "do-not-log-this-password"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "invalid credentials"}, request=request)

    client = ReclaudeClient(
        "https://example.test",
        login_email="owner@example.com",
        login_password=password,
        cookie_jar_path=tmp_path / "cookies.json",
    )
    await _replace_transport(client, handler)
    try:
        with pytest.raises(UpstreamError) as caught:
            await client.authenticate()
        assert str(caught.value) == (
            "Reclaude login credentials are invalid, please fix the login information and restart docker"
        )
        assert calls == 1
        assert password not in str(caught.value)
        assert password not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mfa_response_fails_closed_without_cookie_or_retry(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"step": "mfa_required"}, request=request)

    jar = tmp_path / "cookies.json"
    client = ReclaudeClient(
        "https://example.test",
        login_email="owner@example.com",
        login_password="correct-password",
        cookie_jar_path=jar,
    )
    await _replace_transport(client, handler)
    try:
        with pytest.raises(UpstreamError, match="requires MFA"):
            await client.authenticate()
        assert calls == 1
        assert not jar.exists()
        assert client.circuit_open is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_password_login_fails_closed_when_cookie_cannot_be_persisted(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"set-cookie": "rc_sid=fresh-session; Path=/; HttpOnly"},
            request=request,
        )

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise PermissionError("cookie volume is read-only")

    jar = tmp_path / "cookies.json"
    client = ReclaudeClient(
        "https://example.test",
        login_email="owner@example.com",
        login_password="correct-password",
        cookie_jar_path=jar,
    )
    await _replace_transport(client, handler)
    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        with pytest.raises(UpstreamError, match="could not be persisted"):
            await client.authenticate()
        assert calls == ["/api/auth/login"]
        assert client.circuit_open is True
        assert not jar.exists()
        with pytest.raises(AuthenticationCircuitOpen):
            await client.me()
    finally:
        await client.close()


def test_login_credentials_must_be_complete_and_secret_is_masked() -> None:
    password = "settings-password"
    with pytest.raises(ValidationError) as caught:
        Settings(RECLAUDE_LOGIN_PASSWORD=SecretStr(password))
    assert password not in str(caught.value)
    settings = Settings(RECLAUDE_LOGIN_EMAIL="owner@example.com", RECLAUDE_LOGIN_PASSWORD=SecretStr(password))
    assert settings.reclaude_login_password.get_secret_value() == password
    assert password not in repr(settings)
    with pytest.raises(ValueError, match="configured together"):
        ReclaudeClient("https://example.test", login_password=password, session_cookie="rc_sid=compat")


@pytest.mark.asyncio
async def test_write_request_does_not_implicitly_login(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(204, request=request)

    client = ReclaudeClient(
        "https://example.test",
        login_email="owner@example.com",
        login_password="correct-password",
        cookie_jar_path=tmp_path / "cookies.json",
        account_id=4949,
    )
    await _replace_transport(client, handler)
    try:
        with pytest.raises(AuthenticationCircuitOpen):
            await client.assign("u-1")
        assert calls == []
    finally:
        await client.close()
