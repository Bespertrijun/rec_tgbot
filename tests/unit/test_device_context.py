import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.device_context import SingleOrgAccountSource, SingleOrgTaskService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.infrastructure.reclaude.models import MeResponse


@pytest.fixture
def gateway():
    me = MeResponse.model_validate(json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text()))
    return SimpleNamespace(
        account_id=4949, org_id=178, me=AsyncMock(return_value=me), authenticate=AsyncMock(),
        members=AsyncMock(), assign=AsyncMock(), revoke=AsyncMock(),
    )


@pytest.mark.parametrize("account_id", [4949, "4949", None])
async def test_single_org_reuses_me_without_other_upstream_calls(gateway, account_id):
    gateway.account_id = account_id
    source = SingleOrgAccountSource(gateway, 178)
    result = await source.get_usage(178)
    assert result.org_id == 178
    assert result.account_id == (str(account_id) if account_id is not None else None)
    assert result.me is gateway.me.return_value
    gateway.me.assert_awaited_once_with()
    for method in (gateway.authenticate, gateway.members, gateway.assign, gateway.revoke):
        method.assert_not_called()
    assert gateway.org_id == 178
    assert gateway.account_id == account_id
    with pytest.raises(FrozenInstanceError):
        result.org_id = 179


@pytest.mark.parametrize("org_id", [179, 0, -1, True, "178", None])
async def test_unsupported_org_does_not_query_or_mutate_gateway(gateway, org_id):
    source = SingleOrgAccountSource(gateway, 178)
    with pytest.raises(EligibilityError):
        await source.get_usage(org_id)
    gateway.me.assert_not_called()
    assert (gateway.org_id, gateway.account_id) == (178, 4949)


@pytest.mark.parametrize("org_id", [0, -1, True, "178", None])
def test_invalid_configured_org_is_rejected(gateway, org_id):
    with pytest.raises((ValueError, EligibilityError)):
        SingleOrgAccountSource(gateway, org_id)
    with pytest.raises((ValueError, EligibilityError)):
        SingleOrgTaskService(None, org_id)


async def test_switch_during_read_cannot_misattribute_account(gateway):
    me = gateway.me.return_value

    async def switched_read():
        gateway.account_id = 4950
        return me

    gateway.me.side_effect = switched_read
    with pytest.raises(EligibilityError):
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    gateway.me.assert_awaited_once()
    assert gateway.account_id == 4950


async def test_auth_circuit_error_is_not_hidden_or_retried(gateway):
    failure = AuthenticationCircuitOpen("operator recovery required")
    gateway.me.side_effect = failure
    with pytest.raises(AuthenticationCircuitOpen) as caught:
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    assert caught.value is failure
    gateway.me.assert_awaited_once()
    gateway.authenticate.assert_not_called()
