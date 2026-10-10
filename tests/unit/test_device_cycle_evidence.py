import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.device_account_usage import DeviceAccountUsageService
from reclaude_bot.application.device_context import OrgAccountUsage
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_reset import DeviceTaskResetService
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

    # Snapshots beyond the shared five-minute clock tolerance remain invalid.
    me.current_account.usage_updated_at = NOW + timedelta(seconds=301)
    assert not (await service.fetch_fresh_evidence()).source_valid


@pytest.mark.parametrize("ahead, valid", [(2.620, True), (300, True), (300.001, False)])
async def test_snapshot_clock_skew_has_same_boundary_for_cycle_reset_and_summary(ahead, valid):
    from reclaude_bot.domain.errors import EligibilityError

    me = MeResponse.model_validate(json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text()))
    me.current_account.usage_updated_at = NOW + timedelta(seconds=ahead)
    usage = OrgAccountUsage(org_id=178, account_id="7055", me=me)
    source = SimpleNamespace(get_usage=AsyncMock(return_value=usage))
    evidence = await DeviceCycleService(None, source, 178, clock=lambda: NOW).fetch_fresh_evidence()
    assert evidence.source_valid is valid
    summary = DeviceAccountUsageService(None, source, 178)._source_fact(usage, NOW)
    if valid:
        DeviceTaskResetService._validate_evidence(evidence, "7055")
        assert summary.source_reason is None
    else:
        with pytest.raises(EligibilityError, match="快照时间"):
            DeviceTaskResetService._validate_evidence(evidence, "7055")
        assert summary.source_reason == "账号快照时间无效"


async def test_valid_cycle_does_not_request_rollover_refresh():
    me = MeResponse.model_validate(json.loads((Path(__file__).parents[1] / "fixtures" / "me.json").read_text()))
    usage = OrgAccountUsage(org_id=178, account_id="7055", me=me)
    source = SimpleNamespace(get_usage=AsyncMock(return_value=usage))
    refresh = SimpleNamespace(refresh_if_due=AsyncMock())
    service = DeviceCycleService(None, source, 178, clock=lambda: NOW, refresh=refresh)
    await service.fetch_fresh_evidence()
    refresh.refresh_if_due.assert_not_awaited()
    source.get_usage.assert_awaited_once_with(178)
