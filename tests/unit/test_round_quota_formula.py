from datetime import UTC, datetime
from decimal import Decimal

import pytest

from reclaude_bot.application.device_round_quota import apply_round_quota
from reclaude_bot.infrastructure.db.models import DeviceQuotaCycle, QuotaTask


@pytest.mark.parametrize("estimate,expected", [
    ("2400", "575"), ("2700", "650"), ("100", "0"), ("50", "0"), ("101.03", "0.25"),
    (None, "700"), ("0", "700"), ("NaN", "700"), ("Infinity", "700"), ("100000000", "700"),
])
def test_round_allocation_uses_total_estimate_with_safe_fallback(estimate, expected):
    task = QuotaTask(limit_usd=Decimal("700"))
    old = DeviceQuotaCycle(id=1, estimated_total_usd=Decimal(estimate) if estimate is not None else None)
    new = DeviceQuotaCycle()
    apply_round_quota(task, old, new, datetime.now(UTC))
    assert new.opening_limit_usd == task.limit_usd == Decimal(expected)
    assert new.quota_source_cycle_id == 1
    assert new.quota_reason == ("NO_VALID_ESTIMATE" if expected == "700" else "PREVIOUS_ROUND_ESTIMATE")


def test_initial_cycle_preserves_configured_limit():
    task = QuotaTask(limit_usd=Decimal("800"))
    cycle = DeviceQuotaCycle()
    apply_round_quota(task, None, cycle, datetime.now(UTC))
    assert cycle.opening_limit_usd == 800
    assert cycle.quota_source_cycle_id is None and cycle.quota_reason == "NO_VALID_ESTIMATE"
