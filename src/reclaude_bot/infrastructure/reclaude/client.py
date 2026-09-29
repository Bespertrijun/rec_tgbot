from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog
from pydantic import SecretStr

from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError, UpstreamError

from .constants import DEFAULT_RECLAUDE_USER_AGENT
from .models import (
    AccountsResponse,
    DeviceAuthApproval,
    DeviceAuthDescription,
    DeviceRecord,
    DeviceRevokeResponse,
    DeviceUsage,
    MembersResponse,
    MeResponse,
)

log = structlog.get_logger(__name__)

_RECOGNIZED_SESSION_COOKIE_NAMES = frozenset({"rc_sid", "session", "sessionid", "sid", "auth_token"})
_SAFE_DEVICE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_SAFE_DEVICE_ERROR_SCOPES = frozenset({"account", "org", "organization"})


class DeviceApiError(UpstreamError):
    """Safe device API failure carrying only selected upstream error metadata."""

    def __init__(
        self,
        *,
        operation: str,
        status: int | None = None,
        code: str | None = None,
        retryable: bool | None = None,
        current: int | None = None,
        max: int | None = None,
        scope: str | None = None,
        outcome_unknown: bool = False,
        attempt_count: int | None = None,
    ) -> None:
        self.operation = operation
        self.status = status
        self.code = code if isinstance(code, str) and _SAFE_DEVICE_ERROR_CODE.fullmatch(code) else None
        self.retryable = retryable if isinstance(retryable, bool) else None
        self.current = current if isinstance(current, int) and not isinstance(current, bool) and current >= 0 else None
        self.max = max if isinstance(max, int) and not isinstance(max, bool) and max >= 0 else None
        self.scope = scope if isinstance(scope, str) and scope.casefold() in _SAFE_DEVICE_ERROR_SCOPES else None
        self.outcome_unknown = outcome_unknown
        self.attempt_count = (
            attempt_count
            if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) and attempt_count > 0
            else None
        )

        message = "Reclaude device API request failed"
        if self.status is not None:
            message += f" (HTTP {self.status})"
        if self.code is not None:
            message += f" ({self.code})"
        if self.current is not None and self.max is not None:
            message += f"; capacity {self.current}/{self.max}"
            if self.scope is not None:
                message += f" ({self.scope})"
        super().__init__(message)


class _RequestRetriesExhausted(UpstreamError):
    def __init__(self, attempt_count: int) -> None:
        self.attempt_count = attempt_count
        super().__init__("Reclaude request retries exhausted")


def _positive_device_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EligibilityError(f"Reclaude {label} must be a positive integer")
    return value


def _looks_like_device_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False or payload.get("success") is False:
        return True
    if payload.get("error") not in (None, False, ""):
        return True
    return "code" in payload and any(key in payload for key in ("type", "layer", "detail"))


def _device_error_from_payload(payload: Any, *, operation: str, status: int, outcome_unknown: bool) -> DeviceApiError:
    root = payload if isinstance(payload, dict) else {}
    nested = root.get("error")
    source = nested if isinstance(nested, dict) else root
    code = source.get("code", root.get("code"))
    retryable = source.get("retryable", root.get("retryable"))
    detail = source.get("detail", root.get("detail"))
    details = detail if isinstance(detail, dict) else {}
    scope = details.get("scope")
    return DeviceApiError(
        operation=operation,
        status=status,
        code=code,
        retryable=retryable,
        current=details.get("current"),
        max=details.get("max"),
        scope=scope,
        outcome_unknown=outcome_unknown,
    )


class ReclaudeGateway(Protocol):
    account_id: int | str | None

    async def members(self) -> MembersResponse: ...
    async def me(self) -> MeResponse: ...
    async def authenticate(self) -> MeResponse: ...
    async def accounts(self) -> AccountsResponse: ...
    def configure_account_id(self, account_id: int | str) -> None: ...
    async def assign(self, user_id: str | int) -> httpx.Response | None: ...
    async def revoke(self, user_id: str | int) -> httpx.Response | None: ...


def _is_session_cookie(name: str, value: str | None) -> bool:
    normalized_name = name.casefold()
    return bool(value and value.strip()) and (
        normalized_name in _RECOGNIZED_SESSION_COOKIE_NAMES
        or "session" in normalized_name
        or normalized_name.endswith("sid")
    )


def _has_session_cookie(cookies: dict[str, str]) -> bool:
    return any(_is_session_cookie(name, value) for name, value in cookies.items())


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
        parsed_base_url = httpx.URL(self.base_url)
        if not parsed_base_url.host:
            raise ValueError("Reclaude base URL must include a hostname")
        if parsed_base_url.scheme not in {"http", "https"}:
            raise ValueError("Reclaude base URL must use http or https")
        self._cookie_domain = parsed_base_url.host
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
            self._client.cookies.set(name, value, domain=self._cookie_domain, path="/")
        self._session_available = True
        if self.cookie_jar_path:
            self._persist_cookies()

    @property
    def _has_login_credentials(self) -> bool:
        return bool(self._login_email and self._login_password.get_secret_value())

    def _has_current_session_cookie(self) -> bool:
        if self._client is None:
            return False
        return any(_is_session_cookie(cookie.name, cookie.value) for cookie in self._client.cookies.jar)

    def _canonicalize_response_cookies(self, response: httpx.Response) -> None:
        """Keep the newest recognized session cookie as the only same-name variant."""
        if self._client is None:
            return
        authoritative: dict[str, tuple[str, str, str]] = {}
        for cookie in response.cookies.jar:
            if _is_session_cookie(cookie.name, cookie.value):
                authoritative[cookie.name.casefold()] = (cookie.name, cookie.domain, cookie.path)
        if not authoritative:
            return
        for cookie in list(self._client.cookies.jar):
            selected = authoritative.get(cookie.name.casefold())
            if selected is None or (cookie.name, cookie.domain, cookie.path) == selected:
                continue
            self._client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)

    def _cookie_values(self) -> dict[str, str]:
        if self._client is None:
            return {}
        return {
            str(cookie.name): str(cookie.value)
            for cookie in self._client.cookies.jar
            if isinstance(cookie.value, str) and cookie.value.strip()
        }

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
                    json.dump(self._cookie_values(), handle, sort_keys=True)
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

    async def _request(self, method: str, path: str, *, capture_http_errors: bool = False, **kwargs: Any) -> httpx.Response:
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
                except httpx.HTTPError:
                    if attempt + 1 >= attempts:
                        raise _RequestRetriesExhausted(attempt + 1) from None
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                self._canonicalize_response_cookies(response)
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
                if method_upper == "GET" and response.status_code >= 500:
                    raise _RequestRetriesExhausted(attempt + 1)
                if response.status_code >= 400 and capture_http_errors:
                    log.warning("reclaude_request_error", request_id=request_id, path=path, status=response.status_code)
                    return response
                if response.status_code >= 400:
                    log.warning("reclaude_request_error", request_id=request_id, path=path, status=response.status_code)
                    raise UpstreamError(f"Reclaude returned HTTP {response.status_code}")
                self._persist_cookies()
                log.info("reclaude_request", request_id=request_id, path=path, status=response.status_code, duration_ms=round((time.monotonic() - started) * 1000, 2))
                return response
        raise UpstreamError("request lock exited without response")

    async def _device_request_json(self, method: str, path: str, *, operation: str, **kwargs: Any) -> Any:
        is_post = method.upper() == "POST"
        try:
            response = await self._request(method, path, capture_http_errors=True, **kwargs)
        except AuthenticationCircuitOpen:
            raise
        except UpstreamError as exc:
            raise DeviceApiError(
                operation=operation,
                outcome_unknown=is_post,
                attempt_count=getattr(exc, "attempt_count", None),
            ) from None

        try:
            payload = response.json(parse_float=Decimal)
        except (ValueError, TypeError):
            raise DeviceApiError(
                operation=operation,
                status=response.status_code,
                outcome_unknown=is_post,
            ) from None
        if response.status_code >= 400 or _looks_like_device_error(payload):
            error = _device_error_from_payload(
                payload,
                operation=operation,
                status=response.status_code,
                outcome_unknown=is_post,
            )
            is_explicit_device_limit = (
                is_post
                and path == "/api/cli/auth/approve"
                and (200 <= response.status_code < 300 or 400 <= response.status_code < 500)
                and error.code == "client.device_limit_reached"
                and error.retryable is False
            )
            if is_explicit_device_limit:
                error.outcome_unknown = False
            raise error
        return payload

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
            self._canonicalize_response_cookies(response)

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
                raise UpstreamError(
                    "Reclaude login credentials are invalid, please fix the login information and restart docker"
                )
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
        response = await self._request("GET", "/api/app/me", params={"org_id": self.org_id})
        return MeResponse.model_validate(response.json())

    async def accounts(self) -> AccountsResponse:
        response = await self._request("GET", f"/api/app/orgs/{self.org_id}/accounts")
        try:
            return AccountsResponse.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise UpstreamError("Reclaude accounts response has invalid shape") from exc

    async def describe_device_auth(self, state: str) -> DeviceAuthDescription:
        if not isinstance(state, str) or not state.strip():
            raise EligibilityError("Reclaude authorization state is invalid")
        payload = await self._device_request_json(
            "POST",
            "/api/cli/auth/describe",
            operation="authorization description",
            json={"state": state},
        )
        try:
            description = DeviceAuthDescription.model_validate(payload)
        except (TypeError, ValueError):
            raise DeviceApiError(operation="authorization description", outcome_unknown=True) from None
        if description.state != state:
            raise DeviceApiError(operation="authorization description", outcome_unknown=True) from None
        return description

    async def approve_device_auth(self, state: str, device_name: str, org_id: int) -> DeviceAuthApproval:
        if not isinstance(state, str) or not state.strip():
            raise EligibilityError("Reclaude authorization state is invalid")
        if not isinstance(device_name, str) or not device_name.strip():
            raise EligibilityError("Reclaude device name is invalid")
        validated_org_id = _positive_device_id(org_id, "organization ID")
        payload = await self._device_request_json(
            "POST",
            "/api/cli/auth/approve",
            operation="device authorization",
            json={"device_name": device_name, "org_id": validated_org_id, "state": state},
        )
        try:
            return DeviceAuthApproval.model_validate(payload)
        except (TypeError, ValueError):
            raise DeviceApiError(operation="device authorization", outcome_unknown=True) from None

    async def list_devices(self) -> list[DeviceRecord]:
        payload = await self._device_request_json(
            "GET",
            "/api/app/devices",
            operation="device listing",
        )
        if not isinstance(payload, list):
            raise DeviceApiError(operation="device listing")
        try:
            return [DeviceRecord.model_validate(item) for item in payload]
        except (TypeError, ValueError):
            raise DeviceApiError(operation="device listing") from None

    async def revoke_device(self, device_id: int) -> DeviceRevokeResponse:
        validated_device_id = _positive_device_id(device_id, "device ID")
        payload = await self._device_request_json(
            "POST",
            f"/api/app/devices/{validated_device_id}/revoke",
            operation="device revocation",
        )
        try:
            return DeviceRevokeResponse.model_validate(payload)
        except (TypeError, ValueError):
            raise DeviceApiError(operation="device revocation", outcome_unknown=True) from None

    async def device_usage(self, device_id: int, org_id: int, range: str = "all") -> DeviceUsage:
        validated_device_id = _positive_device_id(device_id, "device ID")
        validated_org_id = _positive_device_id(org_id, "organization ID")
        if not isinstance(range, str) or range not in {"all", "7d"}:
            raise EligibilityError("Reclaude usage range must be 'all' or '7d'")
        payload = await self._device_request_json(
            "GET",
            "/api/app/usage/stats",
            operation="device usage query",
            params={"range": range, "device_id": validated_device_id, "org_id": validated_org_id},
        )
        if not isinstance(payload, dict) or payload.get("range") != range:
            raise DeviceApiError(operation="device usage query")
        try:
            return DeviceUsage.model_validate(payload)
        except (TypeError, ValueError):
            raise DeviceApiError(operation="device usage query") from None

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
