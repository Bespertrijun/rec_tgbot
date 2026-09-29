import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.device_context import SingleOrgAccountSource, SingleOrgTaskService
from reclaude_bot.domain.errors import AuthenticationCircuitOpen, EligibilityError
from reclaude_bot.infrastructure.reclaude.models import AccountsResponse, MeResponse


def accounts(*ids):
    return AccountsResponse.model_validate({"items": [
        {"id": i + 1, "account_id": account_id, "org_id": 178, "lifecycle": "bound", "health": "healthy"}
        for i, account_id in enumerate(ids)
    ]})


@pytest.fixture
def gateway():
    me = MeResponse.model_validate(json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text()))
    return SimpleNamespace(
        account_id=4949, org_id=178, me=AsyncMock(return_value=me), authenticate=AsyncMock(),
        members=AsyncMock(), assign=AsyncMock(), revoke=AsyncMock(),
        accounts=AsyncMock(return_value=accounts(8123)),
    )


@pytest.mark.parametrize("account_id", [4949, "4949", None])
async def test_single_org_uses_live_bound_id_without_mutating_local_selection(gateway, account_id):
    gateway.account_id = account_id
    source = SingleOrgAccountSource(gateway, 178)
    result = await source.get_usage(178)
    assert result.org_id == 178
    assert result.account_id == "8123"
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
        gateway.accounts.return_value = accounts(4950)
        return me

    gateway.me.side_effect = switched_read
    with pytest.raises(EligibilityError):
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    gateway.me.assert_awaited_once()
    assert gateway.account_id == 4949


async def test_auth_circuit_error_is_not_hidden_or_retried(gateway):
    failure = AuthenticationCircuitOpen("operator recovery required")
    gateway.me.side_effect = failure
    with pytest.raises(AuthenticationCircuitOpen) as caught:
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    assert caught.value is failure
    gateway.me.assert_awaited_once()
    gateway.authenticate.assert_not_called()


@pytest.mark.parametrize("ids", [(), (8123, 8124), (8123, 8123), (None,), ("bad-id",), (0,)])
async def test_ambiguous_or_invalid_bound_account_never_uses_cached_id(gateway, ids):
    gateway.accounts.return_value = accounts(*ids)
    with pytest.raises(EligibilityError):
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    assert gateway.account_id == 4949


@pytest.mark.parametrize("changes", [{"org_id": 179}, {"health": "banned"}, {"health": None}, {"lifecycle": "unbound"}])
async def test_wrong_org_or_unavailable_bound_account_rejected(gateway, changes):
    gateway.accounts.return_value.items[0] = gateway.accounts.return_value.items[0].model_copy(update=changes)
    with pytest.raises(EligibilityError):
        await SingleOrgAccountSource(gateway, 178).get_usage(178)


async def test_inventory_failure_does_not_fall_back_to_cached_account(gateway):
    gateway.accounts.side_effect = TimeoutError("unavailable")
    with pytest.raises(TimeoutError):
        await SingleOrgAccountSource(gateway, 178).get_usage(178)
    assert gateway.account_id == 4949


async def test_unbound_inventory_history_is_not_a_second_current_account(gateway):
    gateway.accounts.return_value = accounts(8123, 7000)
    gateway.accounts.return_value.items[1].lifecycle = "unbound"
    result = await SingleOrgAccountSource(gateway, 178).get_usage(178)
    assert result.account_id == "8123"
