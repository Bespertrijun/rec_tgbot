import json
from decimal import Decimal
from pathlib import Path

import pytest

from reclaude_bot.domain.billing import fable_total_usd

FIXTURE = Path(__file__).parents[1] / "fixtures" / "devices" / "usage_fable_30d.json"


def breakdown(*entries):
    return {"breakdown": [{"model": model, "total_usd": amount} for model, amount in entries]}


def test_observed_mixed_model_response_reconciles_and_only_doubles_fable():
    payload = json.loads(FIXTURE.read_text())
    total = Decimal(payload["overview"]["total_usd"])
    fable = fable_total_usd(total, payload["models"])
    assert payload["range"] == "30d"
    assert fable == Decimal("798.2841590000")
    assert total + fable == Decimal("1761.2861949000")


def test_all_fable_models_contribute_and_other_models_are_unchanged():
    models = breakdown(
        ("claude-fable-5-1", "10.1234567890"),
        ("claude-fable-6-20261110", "20.0000000001"),
        ("claude-fable", "3"),
        ("claude-opus-5-5", "60"),
    )
    assert fable_total_usd(Decimal("93.1234567891"), models) == Decimal("33.1234567891")


@pytest.mark.parametrize("model", ["claude-opus-5-5", "some-claude-fable-5-1", "claude-fabulous-5-1", "claude-fableish", "fable"])
def test_near_matches_do_not_receive_the_fable_multiplier(model):
    assert fable_total_usd(Decimal("12"), breakdown((model, "12"))) == Decimal("0")


def test_ignores_percentages_and_time_series_for_accounting():
    models = breakdown(("claude-fable-5-1", "0.1"), ("claude-opus-5-5", "0.2"))
    models["breakdown"][0]["percent"] = 0
    models["timeseries"] = [{"per_model_usd": {"claude-fable-5-1": 99999.12345}}]
    assert fable_total_usd(Decimal("0.3"), models) == Decimal("0.1")


@pytest.mark.parametrize("models", [None, {}, {"breakdown": None}, {"breakdown": []}, breakdown(("claude-fable-5-1", "0"))])
def test_zero_total_needs_no_model_history(models):
    assert fable_total_usd(Decimal("0"), models) == Decimal("0")


@pytest.mark.parametrize("models", [
    None,
    {},
    [],
    {"breakdown": None},
    {"breakdown": {}},
    {"breakdown": []},
    {"breakdown": [None]},
    {"breakdown": [{"model": "claude-fable-5-1"}]},
    breakdown(("claude-opus-5-5", "9")),
    breakdown(("claude-fable-5-1", "11")),
    breakdown(("claude-fable-5-1", "5"), ("claude-fable-5-1", "5")),
    breakdown(("claude-opus-5-5", "5"), ("claude-opus-5-5", "5")),
])
def test_missing_malformed_incomplete_or_duplicate_breakdown_is_rejected(models):
    with pytest.raises(ValueError):
        fable_total_usd(Decimal("10"), models)


@pytest.mark.parametrize("model", [None, True, 1, "", " ", " claude-fable-5-1", "claude-fable-5-1 "])
def test_invalid_model_names_are_rejected(model):
    with pytest.raises(ValueError):
        fable_total_usd(Decimal("10"), breakdown((model, "10")))


@pytest.mark.parametrize("amount", [None, True, False, {}, [], "", "bad", "NaN", "sNaN", "Infinity", "-Infinity", "-1", "100000000", "0.00000000001"])
def test_invalid_model_amounts_are_rejected(amount):
    with pytest.raises(ValueError):
        fable_total_usd(Decimal("10"), breakdown(("claude-fable-5-1", amount)))


@pytest.mark.parametrize("amount", [True, None, Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("100000000"), Decimal("0.00000000001")])
def test_invalid_aggregate_is_rejected(amount):
    with pytest.raises(ValueError):
        fable_total_usd(amount, breakdown(("claude-fable-5-1", "10")))


def test_ten_decimal_places_and_storage_boundary_are_preserved():
    total = Decimal("99999999.9999999999")
    assert fable_total_usd(total, breakdown(("claude-fable-5-1", str(total)))) == total
