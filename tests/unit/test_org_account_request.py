import json
from pathlib import Path

import httpx
import pytest

from reclaude_bot.infrastructure.reclaude.client import ReclaudeClient


@pytest.mark.parametrize("org_id", [178, 923])
async def test_current_account_request_uses_configured_org_not_local_selection(org_id):
    requests = []
    payload = json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text())

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = ReclaudeClient("https://rec.example.invalid", session_cookie="rc_sid=test-only", org_id=org_id)
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handle))
    try:
        await client.me()
        client.configure_account_id(8123)
        await client.me()
    finally:
        await client.close()
    assert len(requests) == 2
    for request in requests:
        assert request.method == "GET"
        assert request.url.path == "/api/app/me"
        assert dict(request.url.params) == {"org_id": str(org_id)}
