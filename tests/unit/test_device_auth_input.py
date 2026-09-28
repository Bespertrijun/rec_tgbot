from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from reclaude_bot.application.device import parse_device_auth_link, validate_device_auth_description
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.reclaude.models import DeviceAuthDescription

NOW = datetime(2026, 9, 28, tzinfo=UTC)
STATE = "private-state-123"
LINK = f"https://www.recode.cat/cli/auth?state={STATE}"


@pytest.mark.parametrize("host", ["www.recode.cat", "www.reclaude.ai", "WWW.RECODE.CAT"])
async def test_allowed_links_normalize_hosts_and_hide_state(host):
    request = parse_device_auth_link(f" https://{host}/cli/auth?extra=value&state={STATE}\n")
    assert request.state == STATE
    assert request.fingerprint == sha256(STATE.encode()).hexdigest()
    assert STATE not in repr(request)
    assert "https" not in repr(request)
    with pytest.raises(FrozenInstanceError):
        request.state = "changed"


@pytest.mark.parametrize("query,state", [
    ("state=a%2Bb", "a+b"),
    ("state=a%2520b", "a%20b"),
    ("%73tate=abc", "abc"),
    ("state=%E4%B8%AD%E6%96%87", "中文"),
    ("state=a%26b%3Dc", "a&b=c"),
])
def test_state_decoded_once_without_inventing_token_alphabet(query, state):
    request = parse_device_auth_link("https://www.recode.cat/cli/auth?" + query)
    assert request.state == state
    assert request.fingerprint == sha256(state.encode()).hexdigest()


@pytest.mark.parametrize("link", [
    "", None, 123,
    "http://www.recode.cat/cli/auth?state=secret",
    "https://www.recode.cat.evil.test/cli/auth?state=secret",
    "https://evil.test@www.recode.cat/cli/auth?state=secret",
    "https://www.recode.cat@evil.test/cli/auth?state=secret",
    "https://www.recode.cat:443/cli/auth?state=secret",
    "https://www.recode.cat:/cli/auth?state=secret",
    "https://www.recode.cat:bad/cli/auth?state=secret",
    "https://www.recode.cat./cli/auth?state=secret",
    "https://www.recode.cat/cli/auth/?state=secret",
    "https://www.recode.cat/cli/%61uth?state=secret",
    "https://www.recode.cat/cli/auth?state=secret#",
    "https://www.recode.cat/cli/auth?state=secret#state=other",
    "https://www.recode.cat/cli/auth?state=secret\\other",
    "https://www.recode.cat/cli/auth?state=sec\tret",
    "https://www.recode.cat/cli/auth?state=sec\x7fret",
    "https://www.recode.cat/cli/auth?state=secret&state=other",
    "https://www.recode.cat/cli/auth?state=secret&%73tate=other",
    "https://www.recode.cat/cli/auth?state=&state=secret",
    "https://www.recode.cat/cli/auth?state=",
    "https://www.recode.cat/cli/auth?STATE=secret",
    "https://www.recode.cat/cli/auth?state=sec+ret",
    "https://www.recode.cat/cli/auth?state=sec%00ret",
    "https://www.recode.cat/cli/auth?state=sec%C2%A0ret",
    "https://www.recode.cat/cli/auth?state=secret%",
    "https://www.recode.cat/cli/auth?state=secret%G0",
    "https://www.recode.cat/cli/auth?state=secret%FF",
    "https://www.recode.cat/cli/auth?state=secret&unused=%FF",
    "https://www.recode.cat/cli/auth?state=secret&unused=%",
    "https://[www.recode.cat/cli/auth?state=secret",
])
def test_invalid_link_is_rejected_without_echoing_input(link):
    with pytest.raises(EligibilityError) as error:
        parse_device_auth_link(link)
    assert str(error.value) == "授权链接无效"
    assert error.value.__suppress_context__


def test_input_size_boundaries():
    prefix = "https://www.recode.cat/cli/auth?state="
    assert len(parse_device_auth_link(prefix + "x" * 2048).state) == 2048
    with pytest.raises(EligibilityError):
        parse_device_auth_link(prefix + "x" * 2049)
    query = LINK + "&extra=" + "x" * (8192 - len(LINK) - len("&extra="))
    assert parse_device_auth_link(query).state == STATE
    with pytest.raises(EligibilityError):
        parse_device_auth_link(query + "x")
    assert parse_device_auth_link(LINK + "&x=y" * 31).state == STATE
    with pytest.raises(EligibilityError):
        parse_device_auth_link(LINK + "&x=y" * 32)


def description(**changes):
    fields = dict(state=STATE, device_name="new laptop", created_at=NOW, expires_at=NOW + timedelta(minutes=10))
    fields.update(changes)
    return DeviceAuthDescription.model_validate(fields)


def test_description_time_boundaries_and_timezones():
    request = parse_device_auth_link(LINK)
    value = description()
    validate_device_auth_description(request, value, now=NOW)
    validate_device_auth_description(request, value, now=NOW.replace(tzinfo=None))
    validate_device_auth_description(request, value, now=NOW.astimezone(timezone(timedelta(hours=8))))
    validate_device_auth_description(request, value, now=value.expires_at - timedelta(microseconds=1))
    with pytest.raises(EligibilityError):
        validate_device_auth_description(request, value, now=value.expires_at)


@pytest.mark.parametrize("changes", [
    {"state": "other-secret"},
    {"created_at": NOW + timedelta(seconds=1)},
    {"expires_at": NOW},
    {"expires_at": NOW - timedelta(seconds=1)},
    {"created_at": NOW - timedelta(seconds=1), "expires_at": NOW - timedelta(seconds=2)},
    {"device_name": "   "},
    {"device_name": "name\x00secret"},
    {"device_name": "name\nsecret"},
])
def test_description_mismatch_expiry_and_invalid_name_fail_safely(changes):
    with pytest.raises(EligibilityError) as error:
        validate_device_auth_description(parse_device_auth_link(LINK), description(**changes), now=NOW)
    assert STATE not in str(error.value)
    assert "secret" not in str(error.value)


def test_description_wrong_types_fail_safely():
    for request, value, now in [(None, description(), NOW), (parse_device_auth_link(LINK), None, NOW), (parse_device_auth_link(LINK), description(), None)]:
        with pytest.raises(EligibilityError):
            validate_device_auth_description(request, value, now=now)
