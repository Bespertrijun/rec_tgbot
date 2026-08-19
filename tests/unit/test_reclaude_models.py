import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from reclaude_bot.infrastructure.reclaude.models import MembersResponse, MeResponse

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_members_preserve_decimal_and_distinguish_ids() -> None:
    parsed = MembersResponse.model_validate(json.loads((FIXTURES / "members.json").read_text()))
    assert parsed.items[0].id == 101
    assert parsed.items[0].user_id == "u-1001"
    assert str(parsed.items[0].total_usage_usd) == "712.1697491500"
    assert parsed.items[1].account_id is None


def test_me_requires_nested_usage_and_unique_weekly_limit() -> None:
    payload = json.loads((FIXTURES / "me.json").read_text())
    parsed = MeResponse.model_validate(payload)
    assert parsed.weekly_all().percent == Decimal("42.50")
    assert parsed.weekly_all().resets_at.tzinfo is not None

    duplicate = json.loads(json.dumps(payload))
    duplicate["current_account"]["usage_snapshot"]["limits"].append(duplicate["current_account"]["usage_snapshot"]["limits"][0])
    with pytest.raises(ValueError, match="expected one weekly_all"):
        MeResponse.model_validate(duplicate).weekly_all()


def test_members_missing_items_is_not_silently_empty() -> None:
    with pytest.raises(ValidationError):
        MembersResponse.model_validate({"total": 1})
