from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog
from pydantic import SecretStr

from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError, UpstreamError

from .constants import DEFAULT_RECLAUDE_USER_AGENT
from .models import AccountsResponse, MembersResponse, MeResponse

log = structlog.get_logger(__name__)


class ReclaudeGateway(Protocol):
    account_id: int | str | None

    async def members(self) -> MembersResponse: ...
    async def me(self) -> MeResponse: ...
    async def authenticate(self) -> MeResponse: ...
    async def accounts(self) -> AccountsResponse: ...
    def configure_account_id(self, account_id: int | str) -> None: ...
    async def assign(self, user_id: str | int) -> httpx.Response | None: ...
    async def revoke(self, user_id: str | int) -> httpx.Response | None: ...


def _has_session_cookie(cookies: dict[str, str]) -> bool:
    recognized = {"rc_sid", "session", "sessionid", "sid", "auth_token"}
    return any(
        value.strip() and (name.casefold() in recognized or "session" in name.casefold() or name.casefold().endswith("sid"))
        for name, value in cookies.items()
    )


def _parse_cookie_header(session_cookie: str | None) -> dict[str, str]:
    if not session_cookie or not session_cookie.strip():
        return {}
    if "=" not in session_cookie:
        return {"rc_sid": session_cookie.strip()}
    cookies: dict[str, str] = {}
    for fragment in session_cookie.split(";"):
        name, separator, value = fragment.strip().partition("=")
        if separator and name.strip() and value.strip():
            cookies[name.strip()] = value.strip()
    return cookies if _has_session_cookie(cookies) else {}


class ReclaudeClient:
    """Cookie-backed API adapter with a one-way authentication circuit breaker."""

    def __init__(
        self,
        base_url: str,
        session_cookie: str | None = None,
        cookie_jar_path: str | Path | None = None,
        user_agent: str = DEFAULT_RECLAUDE_USER_AGENT,
        timeout: float = 15.0,
        max_retries: int = 2,
        org_id: int = 178,
        account_id: int | str | None = None,
        auth_alert_callback: Callable[[], Awaitable[None]] | None = None,
        *,
        login_email: str | None = None,
        login_password: SecretStr | str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar_path = Path(cookie_jar_path) if cookie_jar_path else None
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.org_id = org_id
        self.account_id: int | str | None = None
        if account_id is not None:
            self.configure_account_id(account_id)
        self.auth_alert_callback = auth_alert_callback
        self._login_email = (login_email or "").strip()
        if isinstance(login_password, SecretStr):
            password_value = login_password.get_secret_value()
        else:
            password_value = login_password or ""
        if bool(self._login_email) != bool(password_value):
            raise ValueError("RECLAUDE_LOGIN_EMAIL and RECLAUDE_LOGIN_PASSWORD must be configured together")
        self._login_password = SecretStr(password_value)
        self._circuit_open = False
        self._alert_sent = False
        self._lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._session_available = False
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers={"User-Agent": user_agent})
        try:
            self._load_cookies(session_cookie)
        except Exception:
            self._client = None  # type: ignore[assignment]
            raise

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def _load_cookies(self, session_cookie: str | None) -> None:
        jar_cookies: dict[str, str] = {}
        jar_valid = False
        if self.cookie_jar_path and self.cookie_jar_path.exists():
            try:
                payload = json.loads(self.cookie_jar_path.read_text())
                if isinstance(payload, dict):
                    jar_cookies = {str(name): str(value) for name, value in payload.items() if isinstance(value, str) and value.strip()}
                    jar_valid = _has_session_cookie(jar_cookies)
                if not jar_valid:
                    log.warning("cookie_jar_invalid", path=str(self.cookie_jar_path))
            except (OSError, ValueError, TypeError) as exc:
                log.warning("cookie_jar_load_failed", error=str(exc))
        cookies = jar_cookies if jar_valid else _parse_cookie_header(session_cookie)
        if not _has_session_cookie(cookies):
            if not self._has_login_credentials:
                raise ValueError("no valid Reclaude session cookie is configured")
            return
        for name, value in cookies.items():
            self._client.cookies.set(name, value)
        self._session_available = True
        if self.cookie_jar_path:
            self._persist_cookies()

    @property
    def _has_login_credentials(self) -> bool:
        return bool(self._login_email and self._login_password.get_secret_value())

    def _has_current_session_cookie(self) -> bool:
        if self._client is None:
            return False
        return _has_session_cookie({str(name): str(value) for name, value in self._client.cookies.items()})

    def _persist_cookies(self, *, required: bool = False) -> None:
        if not self.cookie_jar_path:
            return
        try:
            self.cookie_jar_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.cookie_jar_path.name}.", dir=self.cookie_jar_path.parent)
            temp_path = Path(temp_name)
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(dict(self._client.cookies), handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.cookie_jar_path)
                os.chmod(self.cookie_jar_path, stat.S_IRUSR | stat.S_IWUSR)
            finally:
                temp_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cookie_jar_persist_failed", error=str(exc))
            if required:
                raise UpstreamError("Reclaude session cookie could not be persisted") from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._circuit_open:
            raise AuthenticationCircuitOpen("Reclaude authentication circuit is open")
        if not self._session_available:
            raise AuthenticationCircuitOpen("Reclaude session is unavailable; run recovery authentication")
        request_id = str(uuid4())
        started = time.monotonic()
        method_upper = method.upper()
        attempts = self.max_retries + 1 if method_upper == "GET" else 1
        async with self._lock:
            if self._circuit_open:
                raise AuthenticationCircuitOpen("Reclaude authentication circuit is open")
            if not self._session_available:
                raise AuthenticationCircuitOpen("Reclaude session is unavailable; run recovery authentication")
            for attempt in range(attempts):
                try:
                    response = await self._client.request(method_upper, path, **kwargs)
                except httpx.HTTPError as exc:
                    if attempt + 1 >= attempts:
                        raise UpstreamError(f"Reclaude request failed: {exc}") from exc
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                if response.status_code == 401:
                    self._circuit_open = True
                    self._persist_cookies()
                    log.error("reclaude_auth_circuit_open", request_id=request_id, path=path)
                    if not self._alert_sent and self.auth_alert_callback:
                        self._alert_sent = True
                        try:
                            await self.auth_alert_callback()
                        except Exception as exc:
                            log.error("reclaude_auth_alert_failed", error=str(exc))
                    raise AuthenticationCircuitOpen("Reclaude returned 401; operator recovery required")
                if response.status_code >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                if response.status_code >= 400:
                    log.warning("reclaude_request_error", request_id=request_id, path=path, status=response.status_code)
                    raise UpstreamError(f"Reclaude returned HTTP {response.status_code}")
                self._persist_cookies()
                log.info("reclaude_request", request_id=request_id, path=path, status=response.status_code, duration_ms=round((time.monotonic() - started) * 1000, 2))
                return response
        raise UpstreamError("request lock exited without response")

    async def authenticate(self) -> MeResponse:
        """Validate the current session, or perform one explicit recovery login.

        This method is intentionally called by the recovery workflow only. Ordinary API
        methods never use configured credentials implicitly.
        """

        async with self._auth_lock:
            if self._session_available and not self._circuit_open:
                try:
                    return await self.me()
                except AuthenticationCircuitOpen:
                    # The 401 path opens the circuit; use credentials below when available.
                    pass
            if not self._has_login_credentials:
                raise AuthenticationCircuitOpen("Reclaude session is unavailable and login credentials are not configured")
            return await self._login_and_verify()

    async def _login_and_verify(self) -> MeResponse:
        if not self._has_login_credentials:
            raise AuthenticationCircuitOpen("Reclaude login credentials are not configured")
        client = self._client
        if client is None:
            raise UpstreamError("Reclaude login client is unavailable")

        # Login is a single explicit recovery attempt. Never retry or log its request body.
        async with self._lock:
            client.cookies.clear()
            self._session_available = False
            self._circuit_open = False
            try:
                response = await client.post(
                    "/api/auth/login",
                    json={"email": self._login_email, "password": self._login_password.get_secret_value()},
                )
            except httpx.HTTPError as exc:
                self._circuit_open = True
                raise UpstreamError("Reclaude login request failed; retry recovery later") from exc

            status = response.status_code
            payload: Any = None
            if response.content:
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    payload = None
            if isinstance(payload, dict) and (
                str(payload.get("step", "")).casefold() == "mfa_required" or payload.get("mfa_required") is True
            ):
                self._circuit_open = True
                raise UpstreamError("Reclaude login requires MFA; MFA recovery is not configured")
            if status == 429:
                self._circuit_open = True
                raise UpstreamError("Reclaude login is rate limited; retry recovery later")
            if status in (401, 403):
                self._circuit_open = True
                raise UpstreamError("Reclaude login credentials are invalid")
            if status >= 500:
                self._circuit_open = True
                raise UpstreamError("Reclaude login service is temporarily unavailable")
            if status >= 400:
                self._circuit_open = True
                raise UpstreamError(f"Reclaude login failed with HTTP {status}")
            if not self._has_current_session_cookie():
                self._circuit_open = True
                raise UpstreamError("Reclaude login did not establish a valid session")
            self._session_available = True
            try:
                self._persist_cookies(required=True)
            except UpstreamError:
                self._session_available = False
                self._circuit_open = True
                raise

        self._circuit_open = False
        self._alert_sent = False
        try:
            return await self.me()
        except AuthenticationCircuitOpen as exc:
            raise UpstreamError("Reclaude login session verification failed") from exc
        except (ValueError, TypeError) as exc:
            raise UpstreamError("Reclaude login session verification returned invalid data") from exc

    async def members(self) -> MembersResponse:
        response = await self._request("GET", f"/api/app/orgs/{self.org_id}/members")
        return MembersResponse.model_validate(response.json())

    async def me(self) -> MeResponse:
        response = await self._request("GET", "/api/app/me")
        return MeResponse.model_validate(response.json())

    async def accounts(self) -> AccountsResponse:
        response = await self._request("GET", f"/api/app/orgs/{self.org_id}/accounts")
        try:
            return AccountsResponse.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise UpstreamError("Reclaude accounts response has invalid shape") from exc

    def configure_account_id(self, account_id: int | str) -> None:
        if isinstance(account_id, bool) or (isinstance(account_id, str) and not account_id.strip()):
            raise EligibilityError("Reclaude 账号 ID 无效")
        if not isinstance(account_id, (int, str)):
            raise EligibilityError("Reclaude 账号 ID 无效")
        if isinstance(account_id, int) and account_id <= 0:
            raise EligibilityError("Reclaude 账号 ID 无效")
        if isinstance(account_id, str) and (not account_id.strip().isdigit() or int(account_id.strip()) <= 0):
            raise EligibilityError("Reclaude 账号 ID 无效")
        self.account_id = account_id

    def set_account_id(self, account_id: int | str) -> None:
        """Backward-compatible alias for configuring the recovered account."""
        self.configure_account_id(account_id)

    async def assign(self, user_id: str | int) -> httpx.Response:
        if self.account_id is None:
            raise EligibilityError("Reclaude 账号尚未完成恢复配置")
        return await self._request("POST", f"/api/app/orgs/{self.org_id}/accounts/{self.account_id}/assignments", json={"user_id": user_id})

    async def revoke(self, user_id: str | int) -> httpx.Response:
        return await self._request("DELETE", f"/api/app/orgs/{self.org_id}/assignments/{user_id}")
