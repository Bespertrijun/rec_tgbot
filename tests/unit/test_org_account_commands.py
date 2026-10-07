from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reclaude_bot.application.device_account_usage import DeviceAccountUsage
from reclaude_bot.bot.commands import admin_commands
from reclaude_bot.bot.handlers import build_admin_router, build_router
from reclaude_bot.infrastructure.reclaude.models import AccountRecord, AccountsResponse
from tests.unit.test_device_command_wiring import command_callbacks, parameters, settings


def account_summary():
    now = datetime(2026, 9, 29, tzinfo=UTC)
    return DeviceAccountUsage(
        email_masked="owner***@example.com", usage_updated_at=now,
        five_hour_utilization=Decimal("35"), five_hour_resets_at=now + timedelta(hours=3),
        seven_day_utilization=Decimal("10"), seven_day_resets_at=now + timedelta(days=4),
        managed_used_usd=Decimal("120"), estimated_total_usd=Decimal("1200"), estimate_reason=None, account_status="bound",
    )


def test_standalone_account_menu_removed():
    commands = {item.command for item in admin_commands()}
    assert "account" not in commands


def test_manual_selection_removed():
    assert "use" not in {item.command for item in admin_commands()}
    assert "use" not in command_callbacks(build_admin_router(settings()))


@pytest.mark.parametrize("chat_type,user_id", [("supergroup", 1), ("private", 55)])
async def test_task_inventory_is_admin_private_only(chat_type, user_id):
    handler = command_callbacks(build_admin_router(settings()))["task"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(type=chat_type), answer=AsyncMock())
    kwargs, deps = parameters(handler, message, "")
    await handler(**kwargs)
    for dep in deps.values():
        assert not dep.mock_calls


@pytest.mark.parametrize("upstream_fails", [False, True])
async def test_status_keeps_personal_quota_and_adds_masked_account(monkeypatch, upstream_fails):
    handler = command_callbacks(build_router(settings()))["status"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=55, username="reader"), answer=AsyncMock())
    monkeypatch.setattr("reclaude_bot.bot.handlers._record_username_safely_from_store", AsyncMock())
    monkeypatch.setattr("reclaude_bot.bot.handlers._find_local_user", AsyncMock(return_value=(7, "reader@example.com")))
    quota = SimpleNamespace(session_factory=object(), status=AsyncMock(return_value=SimpleNamespace(
        association_state="ACTIVE", pending_action_kind=None, last_sampled_at=datetime(2026, 9, 29, tzinfo=UTC),
        used_usd=Decimal("25"), effective_limit_usd=Decimal("700"), remaining_usd=Decimal("675"),
        device_id=44500, task_id=3, task_name="default", quality="VERIFIED", quota_locked=False, reset_at=None,
    )))
    usage = SimpleNamespace(get_account_usage=AsyncMock(return_value=account_summary()))
    if upstream_fails:
        usage.get_account_usage.side_effect = RuntimeError("secret upstream response")
    await handler(message=message, device_quota=quota, device_account_usage=usage)
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "$25.00" in text and "$700.00" in text and "$675.00" in text
    assert "reader@example.com" not in text and "secret upstream response" not in text
    quota.status.assert_awaited_once_with(7)
    usage.get_account_usage.assert_awaited_once_with(3)
    if upstream_fails:
        assert "暂时不可用" in text
    else:
        assert "owner***@example.com" in text and "35.0%" in text and "10.0%" in text
        assert "1200.00" in text and "快照" in text
        assert "已绑定" in text or "bound" in text


async def test_unbound_user_cannot_query_account_summary(monkeypatch):
    handler = command_callbacks(build_router(settings()))["status"]
    monkeypatch.setattr("reclaude_bot.bot.handlers._record_username_safely_from_store", AsyncMock())
    monkeypatch.setattr("reclaude_bot.bot.handlers._find_local_user", AsyncMock(return_value=None))
    message = SimpleNamespace(from_user=SimpleNamespace(id=55, username=None), answer=AsyncMock())
    quota = SimpleNamespace(session_factory=object(), status=AsyncMock())
    usage = SimpleNamespace(get_account_usage=AsyncMock())
    await handler(message=message, device_quota=quota, device_account_usage=usage)
    quota.status.assert_not_called()
    usage.get_account_usage.assert_not_called()
    assert "/bind" in message.answer.await_args.args[0]


@pytest.mark.parametrize("command_name", ["task", "account"])
@pytest.mark.parametrize("upstream_fails", [False, True])
async def test_task_includes_inventory_even_without_tasks(command_name, upstream_fails):
    handler = command_callbacks(build_admin_router(settings()))[command_name]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    kwargs, deps = parameters(handler, message, "")
    deps["task"].list_tasks.return_value = []
    deps["recovery"].list_accounts.return_value = SimpleNamespace(
        me=SimpleNamespace(current_account=SimpleNamespace(status="bound")), selected_account_id="4949",
        accounts=AccountsResponse(items=[AccountRecord(account_id=4949, account_email="full<owner>@example.com", lifecycle="bound", health="healthy")]),
    )
    if upstream_fails:
        deps["recovery"].list_accounts.side_effect = RuntimeError("secret payload")
    await handler(**kwargs)
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "暂无" in text and "任务" in text
    assert "secret payload" not in text
    deps["recovery"].list_accounts.assert_awaited_once()
    if upstream_fails:
        assert "账号" in text and ("失败" in text or "不可用" in text)
    else:
        assert "full&lt;owner&gt;@example.com" in text and "healthy" in text and "4949" in text


async def test_account_alias_reuses_task_and_cannot_expose_inventory_in_groups():
    callbacks = command_callbacks(build_admin_router(settings()))
    assert callbacks["account"] is callbacks["task"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type="supergroup"), answer=AsyncMock())
    kwargs, deps = parameters(callbacks["account"], message, "")
    await callbacks["account"](**kwargs)
    for dep in deps.values():
        assert not dep.mock_calls


async def test_large_inventory_is_split_without_losing_accounts():
    handler = command_callbacks(build_admin_router(settings()))["task"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    kwargs, deps = parameters(handler, message, "")
    deps["task"].list_tasks.return_value = []
    deps["recovery"].list_accounts.return_value = SimpleNamespace(
        me=SimpleNamespace(current_account=SimpleNamespace(status="bound")), selected_account_id=None,
        accounts=AccountsResponse(items=[AccountRecord(account_id=i + 1, account_email=f"owner{i}@example.com", lifecycle="bound", health="healthy") for i in range(150)]),
    )
    await handler(**kwargs)
    messages = [call.args[0] for call in message.answer.await_args_list]
    assert len(messages) > 1 and all(len(text) <= 4000 for text in messages)
    text = "\n".join(messages)
    for i in range(150):
        assert text.count(f"owner{i}@example.com") == 1


async def test_named_task_details_survive_account_failure():
    handler = command_callbacks(build_admin_router(settings()))["task"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    kwargs, deps = parameters(handler, message, "vip")
    deps["task"].resolve.return_value = "vip"
    deps["task"].snapshot.return_value = SimpleNamespace(name="vip", enabled=True, limit_usd=Decimal("700"), scope_mode="ALL")
    kwargs["jobs"] = SimpleNamespace(status=lambda: dict(loop_running=True, started_at=None, last_tick_started=None,
        last_tick_finished=None, last_tick_error=None, last_result_count=3))
    deps["recovery"].gate.get_state.return_value = SimpleNamespace(selected_account_id="4949", write_enabled=True, reason="ready")
    deps["recovery"].list_accounts.side_effect = RuntimeError("private error")
    deps["device_cycle"].current.return_value = SimpleNamespace(status="VERIFIED", weekly_percent=Decimal("10"), reset_at=None,
                                                                             opening_limit_usd=Decimal("575"), quota_source_estimate_usd=Decimal("2400"),
                                                                             quota_reason="PREVIOUS_ROUND_ESTIMATE", estimated_total_usd=Decimal("2700"),
                                                                             estimate_snapshot_at=None, estimate_blocked=False)
    await handler(**kwargs)
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "vip" in text and "RUNNING" in text and "$700.00" in text and "VERIFIED" in text
    assert "本轮起始每人额度：$575.00" in text and "上一轮预估 $2400.00" in text
    assert "本轮已保存预估：≈$2700.00" in text
    assert "private error" not in text


async def test_status_without_task_preserves_personal_information(monkeypatch):
    handler = command_callbacks(build_router(settings()))["status"]
    message = SimpleNamespace(from_user=SimpleNamespace(id=55, username=None), answer=AsyncMock())
    monkeypatch.setattr("reclaude_bot.bot.handlers._record_username_safely_from_store", AsyncMock())
    monkeypatch.setattr("reclaude_bot.bot.handlers._find_local_user", AsyncMock(return_value=(7, "reader@example.com")))
    quota = SimpleNamespace(session_factory=object(), status=AsyncMock(return_value=SimpleNamespace(
        association_state=None, pending_action_kind=None, last_sampled_at=None, used_usd=None, effective_limit_usd=None,
        remaining_usd=None, device_id=None, task_id=None, task_name=None, quality="UNKNOWN", quota_locked=False, reset_at=None,
    )))
    usage = SimpleNamespace(get_account_usage=AsyncMock())
    await handler(message=message, device_quota=quota, device_account_usage=usage)
    usage.get_account_usage.assert_not_called()
    text = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "未配置" in text and "r***@example.com" in text
