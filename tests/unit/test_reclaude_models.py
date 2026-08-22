import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from reclaude_bot.bot.handlers import build_admin_router
from reclaude_bot.config import Settings
from reclaude_bot.infrastructure.reclaude.models import AccountsResponse, MembersResponse, MeResponse

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
    reset_at = parsed.weekly_all().resets_at
    assert reset_at is not None
    assert reset_at.tzinfo is not None

    duplicate = json.loads(json.dumps(payload))
    duplicate["current_account"]["usage_snapshot"]["limits"].append(duplicate["current_account"]["usage_snapshot"]["limits"][0])
    with pytest.raises(ValueError, match="expected one weekly_all"):
        MeResponse.model_validate(duplicate).weekly_all()


def test_non_target_limit_allows_null_reset_but_weekly_all_does_not() -> None:
    payload = json.loads(json.dumps(json.loads((FIXTURES / "me.json").read_text())))
    payload["current_account"]["usage_snapshot"]["limits"][1]["resets_at"] = None
    parsed = MeResponse.model_validate(payload)
    assert parsed.current_account.usage_snapshot.limits[1].resets_at is None
    assert parsed.weekly_all().resets_at is not None

    payload["current_account"]["usage_snapshot"]["limits"][0]["resets_at"] = None
    with pytest.raises(ValueError, match="weekly_all limit is missing resets_at"):
        MeResponse.model_validate(payload).weekly_all()


def test_members_missing_items_is_not_silently_empty() -> None:
    with pytest.raises(ValidationError):
        MembersResponse.model_validate({"total": 1})


def test_accounts_use_account_id_and_keep_record_id_distinct() -> None:
    parsed = AccountsResponse.model_validate(
        {
            "items": [
                {
                    "id": 7022,
                    "account_email": "owner@example.com",
                    "account_id": 4949,
                    "health": "healthy",
                    "lifecycle": "bound",
                    "org_id": 178,
                    "unexpected": "ignored",
                }
            ]
        }
    )
    record = parsed.items[0]
    assert record.id == 7022
    assert record.account_id == 4949
    assert record.id != record.account_id
    with pytest.raises(ValidationError):
        AccountsResponse.model_validate({"items": [{"account_id": True, "health": "healthy", "lifecycle": "bound"}]})


def test_admin_recovery_commands_share_account_handler() -> None:
    router = build_admin_router(Settings(DATABASE_URL="postgresql+asyncpg://test:test@localhost/test", TELEGRAM_ADMIN_IDS=[1]))
    matching = [handler for handler in router.message.handlers if handler.callback.__name__ == "recovery_enable"]
    assert len(matching) == 1
    filters = matching[0].filters
    assert filters is not None
    assert getattr(filters[0].callback, "commands", None) == ("account", "recovery_enable")
