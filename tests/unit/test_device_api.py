"""Observed REC device contracts; all traffic stays in MockTransport."""

import asyncio
import json
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError, UpstreamError
from reclaude_bot.infrastructure.reclaude.client import DeviceApiError, ReclaudeClient

STATE = "test-state-not-a-real-authorization"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "devices" / "usage_all.json"
DESCRIPTION = {
    "state": STATE,
    "device_name": "test workstation",
    "created_at": 1790519269086,
    "expires_at": 1790519869086,
    "hostname": "test-host",
    "os": "windows",
    "arch": "amd64",
    "client_ip_masked": "192.0.2.0/24",
    "client_user_agent": "Go-http-client/1.1",
    "machine_summary": {"cpu_cores": 16},
}
DEVICE = {
    "id": 44500,
    "org_id": 178,
    "name": "test workstation",
    "created_at": 1790519785401,
    "revoked_at": None,
    "last_used_at": 1790519993935,
    "client_platform": "windows/amd64",
    "client_version": "v1.4.0",
}
APPROVAL = {"device_id": 44500, "ok": True, "reused": False, "user_email": "operator@example.invalid"}


@asynccontextmanager
async def client_for(handler, **kwargs):
    client = ReclaudeClient("https://rec.example.invalid", session_cookie="rc_sid=test-only", **kwargs)
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        yield client
    finally:
        await client.close()


async def test_describe_uses_fixed_endpoint_and_ms_times_without_logging_state():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=DESCRIPTION)

    with capture_logs() as logs:
        async with client_for(handler) as client:
            result = await client.describe_device_auth(STATE)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/cli/auth/describe"
    assert json.loads(requests[0].content) == {"state": STATE}
    assert result.state == STATE
    assert result.created_at == datetime.fromtimestamp(1790519269.086, UTC)
    assert (result.expires_at - result.created_at).total_seconds() == 600
    assert STATE not in repr(result)
    assert STATE not in repr(logs)


async def test_describe_preserves_observed_client_metadata():
    async with client_for(lambda _: httpx.Response(200, json=DESCRIPTION)) as client:
        result = await client.describe_device_auth(STATE)
    data = result.model_dump()
    assert data.get("client_ip_masked") == DESCRIPTION["client_ip_masked"]
    assert data.get("client_user_agent") == DESCRIPTION["client_user_agent"]


async def test_approve_sends_org_and_preserves_exact_device_and_reused():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={**APPROVAL, "reused": True})

    async with client_for(handler, org_id=999) as client:
        result = await client.approve_device_auth(STATE, "test workstation", 178)
        assert client.org_id == 999
    assert requests[0].url.path == "/api/cli/auth/approve"
    assert json.loads(requests[0].content) == {"state": STATE, "device_name": "test workstation", "org_id": 178}
    assert result.device_id == 44500
    assert result.reused is True
    assert result.user_email == "operator@example.invalid"


@pytest.mark.parametrize("ok", [False, 1, "true", None])
@pytest.mark.parametrize("operation", ["approve", "revoke"])
async def test_write_operations_require_boolean_true(ok, operation):
    payload = {**APPROVAL, "ok": ok}
    async with client_for(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(UpstreamError):
            if operation == "approve":
                await client.approve_device_auth(STATE, "test workstation", 178)
            else:
                await client.revoke_device(44500)


@pytest.mark.parametrize("field,value", [("device_id", True), ("device_id", 0), ("device_id", "44500"), ("reused", 0), ("reused", "false")])
async def test_approve_rejects_malformed_identity(field, value):
    async with client_for(lambda _: httpx.Response(200, json={**APPROVAL, field: value})) as client:
        with pytest.raises(UpstreamError):
            await client.approve_device_auth(STATE, "test workstation", 178)


async def test_list_and_revoke_follow_observed_contracts():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[DEVICE] if request.method == "GET" else {"ok": True})

    async with client_for(handler) as client:
        devices = await client.list_devices()
        result = await client.revoke_device(devices[0].id)
    assert len(devices) == 1
    assert devices[0].org_id == 178
    assert devices[0].revoked_at is None
    assert devices[0].last_used_at.tzinfo is UTC
    assert result.ok is True
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/app/devices"), ("POST", "/api/app/devices/44500/revoke")
    ]


@pytest.mark.parametrize("payload", [{"items": [DEVICE]}, None, [{}], [{**DEVICE, "id": True}]])
async def test_bad_device_list_is_not_an_empty_success(payload):
    async with client_for(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(UpstreamError):
            await client.list_devices()


async def test_usage_uses_exact_aggregate_and_retains_historical_data():
    payload = json.loads(FIXTURE.read_text())
    async with client_for(lambda _: httpx.Response(200, json=payload)) as client:
        result = await client.device_usage(15685, 178)
    assert result.overview.total_usd == Decimal("5839.0072946000")
    assert result.overview.model_dump()["heatmap"] == payload["overview"]["heatmap"]
    assert result.model_dump()["models"] == json.loads(FIXTURE.read_text(), parse_float=Decimal)["models"]


@pytest.mark.parametrize("granularity", ["day", "week", None])
async def test_usage_preserves_model_granularity_for_archival(granularity):
    payload = json.loads(FIXTURE.read_text())
    payload["models_granularity"] = granularity
    async with client_for(lambda _: httpx.Response(200, json=payload)) as client:
        result = await client.device_usage(15685, 178)
    assert result.model_dump().get("models_granularity") == granularity
    assert "heatmap" not in result.model_dump()
    assert result.overview.model_dump()["heatmap"] == payload["overview"]["heatmap"]


async def test_org_queries_do_not_mutate_shared_org_and_7d_is_not_heatmap_sum():
    requests = []

    def handler(request):
        requests.append(request)
        requested_range = request.url.params["range"]
        payload = json.loads(FIXTURE.read_text())
        payload["range"] = requested_range
        payload["overview"]["total_usd"] = "625.1690941000" if requested_range == "7d" else "5839.0072946000"
        return httpx.Response(200, json=payload)

    async with client_for(handler, org_id=999) as client:
        all_usage, week_usage = await asyncio.gather(client.device_usage(15685, 178), client.device_usage(44500, 179, "7d"))
        assert client.org_id == 999
    assert all_usage.overview.total_usd == Decimal("5839.0072946000")
    assert week_usage.overview.total_usd == Decimal("625.1690941000")
    assert {tuple(sorted(dict(r.url.params).items())) for r in requests} == {
        tuple(sorted({"device_id": "15685", "org_id": "178", "range": "all"}.items())),
        tuple(sorted({"device_id": "44500", "org_id": "179", "range": "7d"}.items())),
    }


@pytest.mark.parametrize("overview", [{}, {"total_usd": None}, {"total_usd": True}, {"total_usd": "NaN"}, {"total_usd": "Infinity"}, {"total_usd": "-0.01"}])
async def test_invalid_money_never_becomes_zero(overview):
    async with client_for(lambda _: httpx.Response(200, json={"range": "all", "overview": overview})) as client:
        with pytest.raises(UpstreamError):
            await client.device_usage(15685, 178)


@pytest.mark.parametrize("payload", [{"overview": {"total_usd": "10"}}, {"range": "7d", "overview": {"total_usd": "10"}}])
async def test_missing_or_wrong_range_is_not_fabricated(payload):
    async with client_for(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(UpstreamError):
            await client.device_usage(15685, 178)


@pytest.mark.parametrize("status", [200, 400, 409, 500])
async def test_capacity_error_is_structured_safe_and_not_retried(status):
    calls = []
    payload = {
        "code": "client.device_limit_reached", "type": "client", "layer": "admin", "retryable": False,
        "message": STATE, "detail": {"current": 4, "max": 4, "scope": "account", "state": STATE},
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    with capture_logs() as logs:
        async with client_for(handler, max_retries=3) as client:
            with pytest.raises(DeviceApiError) as caught:
                await client.approve_device_auth(STATE, "test workstation", 178)
    assert len(calls) == 1
    assert caught.value.code == "client.device_limit_reached"
    # A confirmed capacity rejection must not strand the authorization slot
    # as if the approve request had timed out after possibly succeeding.
    assert caught.value.outcome_unknown is (status >= 500)
    assert caught.value.status == status
    assert caught.value.retryable is False
    assert (caught.value.current, caught.value.max, caught.value.scope) == (4, 4, "account")
    assert STATE not in str(caught.value)
    assert STATE not in repr(logs)


@pytest.mark.parametrize("operation,code,retryable", [
    ("approve", "client.device_limit_reached", True),
    ("approve", "client.device_limit_reached", None),
    ("approve", "client.device_limit_reached", 0),
    ("approve", "client.device_limit_reached", "false"),
    ("approve", "client.other_error", False),
    ("revoke", "client.device_limit_reached", False),
    ("describe", "client.device_limit_reached", False),
])
async def test_only_confirmed_approve_capacity_error_is_definitive(operation, code, retryable):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"code": code, "retryable": retryable})

    async with client_for(handler) as client:
        with pytest.raises(DeviceApiError) as caught:
            if operation == "approve":
                await client.approve_device_auth(STATE, "test workstation", 178)
            elif operation == "revoke":
                await client.revoke_device(44500)
            else:
                await client.describe_device_auth(STATE)
    assert caught.value.outcome_unknown is True
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["approve", "revoke"])
async def test_post_timeout_is_unknown_not_retried_and_does_not_leak_state(operation):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(STATE, request=request)

    with capture_logs() as logs:
        async with client_for(handler, max_retries=3) as client:
            with pytest.raises(DeviceApiError) as caught:
                if operation == "approve":
                    await client.approve_device_auth(STATE, "test workstation", 178)
                else:
                    await client.revoke_device(44500)
    assert len(calls) == 1
    assert caught.value.outcome_unknown is True
    assert STATE not in "".join(traceback.format_exception(caught.value))
    assert STATE not in repr(logs)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"ok":true}'])
async def test_unusable_approval_response_is_unknown(body):
    async with client_for(lambda _: httpx.Response(200, content=body)) as client:
        with pytest.raises(DeviceApiError) as caught:
            await client.approve_device_auth(STATE, "test workstation", 178)
    assert caught.value.outcome_unknown is True


async def test_device_401_preserves_existing_circuit_and_alert_once():
    calls = []
    alerts = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"message": STATE})

    async def alert():
        alerts.append(True)

    async with client_for(handler, auth_alert_callback=alert) as client:
        with pytest.raises(AuthenticationCircuitOpen):
            await client.approve_device_auth(STATE, "test workstation", 178)
        with pytest.raises(AuthenticationCircuitOpen):
            await client.list_devices()
    assert len(calls) == len(alerts) == 1


async def test_get_transient_error_reuses_existing_retry_policy():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=[DEVICE])

    async with client_for(handler, max_retries=1) as client:
        assert len(await client.list_devices()) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("bad_id", [True, 0, -1, "178", 1.5])
async def test_invalid_ids_never_reach_upstream(bad_id):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with client_for(handler) as client:
        with pytest.raises(EligibilityError):
            await client.revoke_device(bad_id)
        with pytest.raises(EligibilityError):
            await client.approve_device_auth(STATE, "test workstation", bad_id)
        with pytest.raises(EligibilityError):
            await client.device_usage(15685, bad_id)
        with pytest.raises(EligibilityError):
            await client.device_usage(bad_id, 178)
    assert not calls


@pytest.mark.parametrize("bad_range", ["30d", None, [], {}])
async def test_unsupported_range_is_a_controlled_error_before_network(bad_range):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async with client_for(handler) as client:
        with pytest.raises(EligibilityError):
            await client.device_usage(15685, 178, bad_range)
    assert not calls
