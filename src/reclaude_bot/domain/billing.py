from __future__ import annotations

from decimal import Decimal, InvalidOperation

_ZERO = Decimal("0.0000000000")
_MONEY_LIMIT = Decimal("100000000")
_MONEY_QUANTUM = Decimal("0.0000000001")


def _money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int | float):
        raise ValueError("invalid model usage amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount >= _MONEY_LIMIT:
            raise ValueError("model usage amount is outside storage range")
        normalized = amount.quantize(_MONEY_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError("invalid model usage amount") from exc
    if normalized != amount:
        raise ValueError("model usage amount exceeds storage precision")
    return normalized


def fable_total_usd(total_usd: Decimal, models: object) -> Decimal:
    """Return the Fable API amount to add for subscription billing at twice its API rate.

    Require complete, reconciled model totals: missing detail must not be treated
    as zero Fable usage. Percentages and floating-point time series are not used.
    """
    total = _money(total_usd)
    if models is None and total == _ZERO:
        return _ZERO
    if not isinstance(models, dict):
        raise ValueError("model usage breakdown is missing")
    breakdown = models.get("breakdown")
    if breakdown is None and total == _ZERO:
        return _ZERO
    if not isinstance(breakdown, list):
        raise ValueError("model usage breakdown is invalid")

    seen: set[str] = set()
    breakdown_total = _ZERO
    fable_total = _ZERO
    for item in breakdown:
        if not isinstance(item, dict):
            raise ValueError("model usage entry is invalid")
        model = item.get("model")
        if not isinstance(model, str) or not model or model.strip() != model or model in seen:
            raise ValueError("model usage name is invalid or duplicated")
        seen.add(model)
        amount = _money(item.get("total_usd"))
        breakdown_total += amount
        if model == "claude-fable" or model.startswith("claude-fable-"):
            fable_total += amount

    if breakdown_total != total:
        raise ValueError("model usage breakdown does not match the aggregate")
    return fable_total
