from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from reclaude_bot.application.device_cycle import DeviceCycleEvidence
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.domain.errors import EligibilityError

NOW = datetime(2026, 10, 8, 2, tzinfo=UTC)
EVIDENCE = DeviceCycleEvidence(
    account_id="7022", reset_at=NOW + timedelta(days=2), percent=Decimal("0"),
    source_valid=True, request_started_at=NOW, received_at=NOW, usage_updated_at=NOW,
)


def test_zero_usage_is_valid_reset_evidence():
    DeviceTaskResetService._validate_evidence(EVIDENCE, "7022")


@pytest.mark.parametrize("changes, reason", [
    ({"account_id": None, "source_valid": False}, "未返回可核实的绑定账号"),
    ({"account_id": "9999"}, "9999 与本地已选账号 7022 不一致"),
    ({"source_valid": False}, "账号来源未通过验证"),
    ({"source_valid": False, "usage_updated_at": NOW + timedelta(seconds=300, milliseconds=1)}, "晚 300.001 秒"),
])
def test_reset_validation_reports_specific_cause(changes, reason):
    with pytest.raises(EligibilityError, match=reason):
        DeviceTaskResetService._validate_evidence(replace(EVIDENCE, **changes), "7022")
