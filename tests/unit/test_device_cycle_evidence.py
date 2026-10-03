import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_context import OrgAccountUsage
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.infrastructure.reclaude.models import MeResponse

NOW = datetime(2026, 8, 18, tzinfo=UTC)


@pytest.mark.parametrize("percent", ["0", "10"])
async def test_inactive_weekly_window_is_valid_cycle_evidence(percent):
    me = MeResponse.model_validate(json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text()))
    me.current_account.usage_updated_at = NOW
    weekly = me.weekly_all()
    weekly.is_active = False
    weekly.percent = percent
    me.current_account.usage_snapshot.seven_day.utilization = percent
    me.current_account.usage_snapshot.seven_day.resets_at = weekly.resets_at
    usage = OrgAccountUsage(org_id=178, account_id="7055", me=me)
    source = SimpleNamespace(get_usage=AsyncMock(return_value=usage))
    service = DeviceCycleService(None, source, 178, clock=lambda: NOW)
    evidence = await service.fetch_fresh_evidence()
    assert evidence.source_valid
    assert DeviceAccountUsageService(None, source, 178)._source_fact(usage, NOW).source_reason is None

    # Removing the activity requirement must not accept future snapshots.
    me.current_account.usage_updated_at = NOW + timedelta(seconds=1)
    assert not (await service.fetch_fresh_evidence()).source_valid
