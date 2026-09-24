from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import AuditLog, QuotaAdjustment, User
from reclaude_bot.infrastructure.reclaude.models import Member


async def _bound_pair(factory, gateway, settings):
    """Sync a cycle with two members and bind both to Telegram users 301/302."""
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    gateway.member_rows["u-2"] = Member(user_id="u-2", email="two@example.com", account_id=None, total_usage_usd="0")
    await quota.sync_members(now=now)
    binding = BindingService(factory, gateway)
    await binding.bind(301, "one@example.com", telegram_username="Alice")
    await binding.bind(302, "two@example.com", telegram_username="bob")
    return quota, now


@pytest.mark.asyncio
async def test_transfer_moves_quota_between_bound_users(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    result = await quota.transfer_quota(301, amount=Decimal("100"), recipient_username="BOB", now=now)

    assert result["recipient_email"] == "two@example.com"
    assert result["sender_remaining_usd"] == Decimal("600")
    async with factory() as session:
        sender = await session.scalar(select(User).where(User.telegram_user_id == 301))
        recipient = await session.scalar(select(User).where(User.telegram_user_id == 302))
        adjustments = list((await session.scalars(select(QuotaAdjustment).order_by(QuotaAdjustment.user_id))).all())
        assert [(row.user_id, row.amount_usd) for row in adjustments] == [(sender.id, Decimal("100")), (recipient.id, Decimal("-100"))]
        assert all(row.operator_telegram_id == 301 for row in adjustments)
        audit_row = await session.scalar(select(AuditLog).where(AuditLog.action == "QUOTA_TRANSFER"))
        assert audit_row is not None
        assert audit_row.actor_telegram_id == 301
        assert audit_row.target_id == str(recipient.id)
    sender_status = await quota.get_status(301, now=now)
    assert sender_status["used_usd"] == Decimal("100")
    assert sender_status["remaining_usd"] == Decimal("600")


@pytest.mark.asyncio
async def test_transfer_credit_offsets_recipient_future_usage(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)
    await quota.transfer_quota(301, amount=Decimal("100"), recipient_telegram_id=302, now=now)

    gateway.member_rows["u-2"] = Member(user_id="u-2", email="two@example.com", account_id=None, total_usage_usd="150")
    await quota.sync_members(now=now + timedelta(minutes=1))

    recipient_status = await quota.get_status(302, now=now + timedelta(minutes=1))
    assert recipient_status["used_usd"] == Decimal("50")
    assert recipient_status["remaining_usd"] == Decimal("650")


@pytest.mark.asyncio
async def test_transfer_rejects_amount_above_sender_remaining(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    with pytest.raises(EligibilityError, match="剩余额度不足"):
        await quota.transfer_quota(301, amount=Decimal("800"), recipient_telegram_id=302, now=now)


@pytest.mark.asyncio
async def test_transfer_rejects_non_positive_or_non_finite_amount(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    for amount in (Decimal("0"), Decimal("-5"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(EligibilityError, match="转账金额必须大于 0"):
            await quota.transfer_quota(301, amount=amount, recipient_telegram_id=302, now=now)


@pytest.mark.asyncio
async def test_transfer_rejects_self_transfer(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    with pytest.raises(EligibilityError, match="不能转账给自己"):
        await quota.transfer_quota(301, amount=Decimal("10"), recipient_telegram_id=301, now=now)
    with pytest.raises(EligibilityError, match="不能转账给自己"):
        await quota.transfer_quota(301, amount=Decimal("10"), recipient_username="alice", now=now)


@pytest.mark.asyncio
async def test_transfer_rejects_unknown_or_unbound_recipient(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    with pytest.raises(EligibilityError, match="对方尚未绑定"):
        await quota.transfer_quota(301, amount=Decimal("10"), recipient_username="nobody", now=now)
    with pytest.raises(EligibilityError, match="对方尚未绑定"):
        await quota.transfer_quota(301, amount=Decimal("10"), recipient_telegram_id=999, now=now)


@pytest.mark.asyncio
async def test_transfer_rejects_unbound_sender(app_context):
    factory, gateway, settings = app_context
    quota, now = await _bound_pair(factory, gateway, settings)

    with pytest.raises(EligibilityError, match="您尚未绑定"):
        await quota.transfer_quota(999, amount=Decimal("10"), recipient_telegram_id=302, now=now)


@pytest.mark.asyncio
async def test_record_username_refreshes_the_mention_cache(app_context):
    factory, gateway, settings = app_context
    quota, _ = await _bound_pair(factory, gateway, settings)

    await quota.record_username(301, "AliceRenamed")
    await quota.record_username(301, None)

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 301))
        assert user.telegram_username == "alicerenamed"


@pytest.mark.asyncio
async def test_bind_stores_username_casefolded(app_context):
    factory, gateway, settings = app_context
    quota = QuotaService(factory, gateway, settings)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    await quota.sync_cycle_from_me(now=now)
    await quota.sync_members(now=now)

    await BindingService(factory, gateway).bind(301, "one@example.com", telegram_username="Alice")

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 301))
        assert user.telegram_username == "alice"
