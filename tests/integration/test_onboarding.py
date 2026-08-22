import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from reclaude_bot.application.onboarding import TOKEN_PREFIX, OnboardingAction, OnboardingAuditAction, OnboardingService
from reclaude_bot.domain.enums import GroupMembershipState, ManagedGroupStatus
from reclaude_bot.domain.errors import OnboardingError
from reclaude_bot.infrastructure.db.models import AuditLog, GroupMembership, ManagedGroup, User

TOKEN_PAYLOAD = re.compile(r"^verify_(-[0-9]+)_([A-Za-z0-9_-]+)$")


async def _add_groups(factory, now: datetime, *chat_ids: int) -> None:
    async with factory() as session:
        async with session.begin():
            for chat_id in chat_ids:
                session.add(
                    ManagedGroup(
                        chat_id=chat_id,
                        title=f"Managed group {chat_id}",
                        status=ManagedGroupStatus.ACTIVE.value,
                        created_at=now,
                        updated_at=now,
                    )
                )


async def _add_bound_user(factory, now: datetime, user_id: int) -> None:
    async with factory() as session:
        async with session.begin():
            session.add(
                User(
                    telegram_user_id=user_id,
                    email=f"user-{user_id}@example.com",
                    email_normalized=f"user-{user_id}@example.com",
                    reclaude_user_id=f"reclaude-{user_id}",
                    binding_status="BOUND",
                    status="ACTIVE",
                    bound_at=now,
                    updated_at=now,
                )
            )


async def _prepare_token(service: OnboardingService, chat_id: int, user_id: int, now: datetime):
    membership = await service.begin_join(chat_id, user_id, real_join=True, joined_at=now)
    assert membership.state == GroupMembershipState.RESTRICT_PENDING.value
    attempt_id = await service.claim_restriction(chat_id, user_id, membership.generation, now=now)
    assert attempt_id is not None
    assert await service.confirm_restriction(
        chat_id,
        user_id,
        membership.generation,
        attempt_id=attempt_id,
        now=now + timedelta(seconds=1),
    ) is not None
    token = await service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=2))
    assert token is not None
    return membership, token


@pytest.mark.asyncio
async def test_verification_payload_round_trip_hash_and_one_time_use(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001234567890
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)

    membership, payload = await _prepare_token(service, chat_id, user_id, now)
    match = TOKEN_PAYLOAD.fullmatch(payload)
    assert match is not None
    assert int(match.group(1)) == chat_id
    assert len(payload) <= 64
    assert all(character.isascii() and (character.isalnum() or character in "_-") for character in payload)

    async with factory() as session:
        stored = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert stored is not None
    assert stored.verification_token_hash == hashlib.sha256(match.group(2).encode("ascii")).hexdigest()

    verified = await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=3))
    assert verified is not None
    assert verified.chat_id == chat_id
    assert await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=4)) is None


@pytest.mark.asyncio
async def test_verification_payload_rejects_missing_prefix_and_chat_id_tampering(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    first_chat_id = -1001
    second_chat_id = -1002
    user_id = 42
    await _add_groups(factory, now, first_chat_id, second_chat_id)
    service = OnboardingService(factory)
    first_membership, first_payload = await _prepare_token(service, first_chat_id, user_id, now)
    _, second_payload = await _prepare_token(service, second_chat_id, user_id, now)

    with pytest.raises(OnboardingError, match="验证链接无效"):
        await service.verify_token(
            user_id,
            first_payload.removeprefix(TOKEN_PREFIX),
            generation=first_membership.generation,
            now=now + timedelta(seconds=3),
        )

    replaced_chat_id = first_payload.replace(str(first_chat_id), str(second_chat_id), 1)
    assert await service.verify_token(user_id, replaced_chat_id, now=now + timedelta(seconds=3)) is None
    assert await service.verify_token(user_id, "verify_not-a-chat_opaque", now=now + timedelta(seconds=3)) is None
    assert await service.verify_token(user_id, second_payload, now=now + timedelta(seconds=3)) is not None


@pytest.mark.asyncio
async def test_verification_payload_binds_user_and_expires(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, 42, now)

    assert await service.verify_token(99, payload, generation=membership.generation, now=now + timedelta(seconds=3)) is None
    assert await service.verify_token(42, payload, generation=membership.generation, now=now + timedelta(minutes=5)) is None
    assert await service.verify_token(42, payload, generation=membership.generation, now=now + timedelta(minutes=5, seconds=1)) is None


@pytest.mark.asyncio
async def test_same_user_tokens_locate_their_respective_groups(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    first_chat_id = -1001
    second_chat_id = -1002
    user_id = 42
    await _add_groups(factory, now, first_chat_id, second_chat_id)
    service = OnboardingService(factory)
    first_membership, first_payload = await _prepare_token(service, first_chat_id, user_id, now)
    second_membership, second_payload = await _prepare_token(service, second_chat_id, user_id, now)

    first = await service.verify_token(user_id, first_payload, generation=first_membership.generation, now=now + timedelta(seconds=3))
    second = await service.verify_token(user_id, second_payload, generation=second_membership.generation, now=now + timedelta(seconds=3))
    assert first is not None and first.chat_id == first_chat_id
    assert second is not None and second.chat_id == second_chat_id


@pytest.mark.asyncio
async def test_old_token_is_invalid_after_left_and_removed_rejoins(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)

    first_membership, first_payload = await _prepare_token(service, chat_id, user_id, now)
    await service.mark_left(chat_id, user_id, first_membership.generation, now=now + timedelta(seconds=3))
    second_membership, second_payload = await _prepare_token(service, chat_id, user_id, now + timedelta(minutes=1))
    assert second_membership.generation == first_membership.generation + 1
    assert await service.verify_token(user_id, first_payload, now=now + timedelta(minutes=1, seconds=3)) is None
    async with factory() as session:
        async with session.begin():
            row = await session.scalar(
                select(GroupMembership).where(
                    GroupMembership.chat_id == chat_id,
                    GroupMembership.telegram_user_id == user_id,
                )
            )
            assert row is not None
            row.state = GroupMembershipState.REMOVE_PENDING.value
    removed = await service.confirm_removal(chat_id, user_id, second_membership.generation, now=now + timedelta(minutes=2))
    assert removed is not None
    assert removed.state == GroupMembershipState.REMOVED.value
    third_membership, third_payload = await _prepare_token(service, chat_id, user_id, now + timedelta(minutes=3))
    assert third_membership.generation == second_membership.generation + 1
    assert await service.verify_token(user_id, second_payload, now=now + timedelta(minutes=3, seconds=3)) is None
    assert await service.verify_token(user_id, third_payload, now=now + timedelta(minutes=3, seconds=3)) is not None


@pytest.mark.asyncio
async def test_confirm_removal_is_explicit_idempotent_and_generation_safe(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, _ = await _prepare_token(service, chat_id, user_id, now)

    assert await service.confirm_removal(chat_id, user_id, membership.generation, now=now + timedelta(seconds=3)) is None
    assert await service.confirm_removal(chat_id, user_id, membership.generation - 1, now=now + timedelta(seconds=3)) is None

    async with factory() as session:
        async with session.begin():
            row = await session.scalar(
                select(GroupMembership).where(
                    GroupMembership.chat_id == chat_id,
                    GroupMembership.telegram_user_id == user_id,
                )
            )
            assert row is not None
            row.state = GroupMembershipState.REMOVE_PENDING.value
            row.removal_requested_at = now
            row.retry_count = 2
            row.last_error = "temporary failure"

    removed = await service.confirm_removal(chat_id, user_id, membership.generation, now=now + timedelta(seconds=4))
    assert removed is not None
    assert removed.state == GroupMembershipState.REMOVED.value
    assert removed.removed_at is not None
    assert removed.deadline is None
    assert removed.verification_token_hash is None
    assert removed.pending_action is None
    assert removed.retry_count == 0
    assert removed.last_attempt_at is None
    assert removed.next_retry_at is None
    assert removed.last_error is None

    repeated = await service.confirm_removal(chat_id, user_id, membership.generation, now=now + timedelta(seconds=5))
    assert repeated is not None
    assert repeated.state == GroupMembershipState.REMOVED.value

    async with factory() as session:
        audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "GROUP_REMOVAL_SUCCESS",
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_confirm_restriction_clears_restrict_retry_and_is_idempotent(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership = await service.begin_join(chat_id, user_id, real_join=True, joined_at=now)

    first_attempt = await service.claim_restriction(chat_id, user_id, membership.generation, now=now)
    assert first_attempt is not None
    failed = await service.fail_restriction(
        chat_id,
        user_id,
        membership.generation,
        "restrict failed",
        attempt_id=first_attempt,
        next_retry_at=now + timedelta(seconds=20),
        now=now + timedelta(seconds=1),
    )
    assert failed is not None
    assert failed.pending_action == OnboardingAction.RESTRICT.value
    assert failed.retry_count == 1

    second_attempt = await service.claim_restriction(
        chat_id,
        user_id,
        membership.generation,
        now=now + timedelta(seconds=20),
    )
    assert second_attempt is not None
    confirmed = await service.confirm_restriction(
        chat_id,
        user_id,
        membership.generation,
        attempt_id=second_attempt,
        now=now + timedelta(seconds=21),
    )
    assert confirmed is not None
    assert confirmed.state == GroupMembershipState.MUTED.value
    assert confirmed.pending_action is None
    assert confirmed.retry_count == 0
    assert confirmed.last_attempt_at is None
    assert confirmed.next_retry_at is None
    assert confirmed.last_error is None
    assert confirmed.action_attempt_id is None
    assert await service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=3)) is not None
    assert await service.confirm_restriction(
        chat_id,
        user_id,
        membership.generation,
        attempt_id="stale",
        now=now + timedelta(seconds=4),
    ) is not None

    async with factory() as session:
        audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.RESTRICT_CONFIRMED.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_confirm_restriction_rejects_conflicting_pending_action_without_cleanup(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership = await service.begin_join(chat_id, user_id, real_join=True, joined_at=now)
    attempt_id = await service.claim_restriction(chat_id, user_id, membership.generation, now=now)
    assert attempt_id is not None

    async with factory() as session:
        async with session.begin():
            row = await session.scalar(
                select(GroupMembership).where(
                    GroupMembership.chat_id == chat_id,
                    GroupMembership.telegram_user_id == user_id,
                )
            )
            assert row is not None
            row.pending_action = OnboardingAction.VERIFICATION_NOTIFICATION.value
            row.action_attempt_id = attempt_id
            row.retry_count = 2
            row.last_error = "verification pending"

    assert (
        await service.confirm_restriction(
            chat_id,
            user_id,
            membership.generation,
            attempt_id=attempt_id,
            now=now + timedelta(seconds=1),
        )
        is None
    )

    async with factory() as session:
        async with session.begin():
            row = await session.scalar(
                select(GroupMembership).where(
                    GroupMembership.chat_id == chat_id,
                    GroupMembership.telegram_user_id == user_id,
                )
            )
            assert row is not None
            assert row.state == GroupMembershipState.RESTRICT_PENDING.value
            assert row.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
            assert row.action_attempt_id == attempt_id
            assert row.retry_count == 2
            row.state = GroupMembershipState.MUTED.value

    muted = await service.confirm_restriction(
        chat_id,
        user_id,
        membership.generation,
        attempt_id="wrong",
        now=now + timedelta(seconds=2),
    )
    assert muted is not None
    assert muted.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
    assert muted.action_attempt_id == attempt_id
    assert muted.retry_count == 2

    async with factory() as session:
        audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.RESTRICT_CONFIRMED.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert audits == []


@pytest.mark.asyncio
async def test_restriction_claim_lease_failure_retry_and_stale_attempts_are_idempotent(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership = await service.begin_join(chat_id, user_id, real_join=True, joined_at=now)

    first_attempt = await service.claim_restriction(chat_id, user_id, membership.generation, now=now)
    assert first_attempt is not None
    assert len(first_attempt) <= 64
    assert first_attempt.isascii()
    assert await service.claim_restriction(chat_id, user_id, membership.generation, now=now + timedelta(seconds=29)) is None

    failed = await service.fail_restriction(
        chat_id,
        user_id,
        membership.generation,
        "restrict failed",
        attempt_id=first_attempt,
        now=now + timedelta(seconds=1),
    )
    assert failed is not None
    assert failed.action_attempt_id is None
    assert failed.pending_action == OnboardingAction.RESTRICT.value
    assert failed.retry_count == 1
    assert failed.next_retry_at == now + timedelta(seconds=31)
    assert failed.last_error == "restrict failed"
    assert (
        await service.fail_restriction(
            chat_id,
            user_id,
            membership.generation,
            "duplicate",
            attempt_id=first_attempt,
            now=now + timedelta(seconds=2),
        )
        is None
    )
    assert await service.claim_restriction(chat_id, user_id, membership.generation, now=now + timedelta(seconds=30)) is None

    second_attempt = await service.claim_restriction(chat_id, user_id, membership.generation, now=now + timedelta(seconds=31))
    assert second_attempt is not None
    assert second_attempt != first_attempt
    async with factory() as session:
        row = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
        failure_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.RESTRICT_FAILURE.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert row is not None
    assert row.action_attempt_id == second_attempt
    assert row.retry_count == 1
    assert row.last_error == "restrict failed"
    assert len(failure_audits) == 1

    assert (
        await service.confirm_restriction(
            chat_id,
            user_id,
            membership.generation,
            attempt_id=first_attempt,
            now=now + timedelta(seconds=32),
        )
        is None
    )
    assert (
        await service.fail_restriction(
            chat_id,
            user_id,
            membership.generation,
            "stale",
            attempt_id=first_attempt,
            now=now + timedelta(seconds=32),
        )
        is None
    )
    confirmed = await service.confirm_restriction(
        chat_id,
        user_id,
        membership.generation,
        attempt_id=second_attempt,
        now=now + timedelta(seconds=32),
    )
    assert confirmed is not None
    assert confirmed.state == GroupMembershipState.MUTED.value
    assert confirmed.action_attempt_id is None


@pytest.mark.asyncio
async def test_deadline_removal_supersedes_failed_restriction_backoff_but_honors_inflight_lease(app_context):
    factory, _, _ = app_context
    joined_at = datetime(2026, 8, 22, 4, 55, tzinfo=UTC)
    failed_chat_id = -1001
    inflight_chat_id = -1002
    user_id = 42
    await _add_groups(factory, joined_at, failed_chat_id, inflight_chat_id)
    service = OnboardingService(factory)

    failed_membership = await service.begin_join(failed_chat_id, user_id, real_join=True, joined_at=joined_at)
    failed_attempt = await service.claim_restriction(failed_chat_id, user_id, failed_membership.generation, now=joined_at)
    assert failed_attempt is not None
    failed = await service.fail_restriction(
        failed_chat_id,
        user_id,
        failed_membership.generation,
        "restrict failed",
        attempt_id=failed_attempt,
        next_retry_at=joined_at + timedelta(hours=1),
        now=joined_at + timedelta(minutes=1),
    )
    assert failed is not None
    assert failed.action_attempt_id is None
    assert failed.next_retry_at == joined_at + timedelta(hours=1)

    removal_attempt = await service.claim_removal(
        failed_chat_id,
        user_id,
        failed_membership.generation,
        now=joined_at + timedelta(minutes=6),
    )
    assert removal_attempt is not None
    assert await service.confirm_restriction(
        failed_chat_id,
        user_id,
        failed_membership.generation,
        attempt_id=failed_attempt,
        now=joined_at + timedelta(minutes=6, seconds=1),
    ) is None

    inflight_membership = await service.begin_join(inflight_chat_id, user_id, real_join=True, joined_at=joined_at)
    inflight_attempt = await service.claim_restriction(
        inflight_chat_id,
        user_id,
        inflight_membership.generation,
        now=joined_at + timedelta(minutes=4, seconds=45),
    )
    assert inflight_attempt is not None
    lease_expires_at = joined_at + timedelta(minutes=5, seconds=15)
    assert await service.claim_removal(
        inflight_chat_id,
        user_id,
        inflight_membership.generation,
        now=lease_expires_at - timedelta(seconds=1),
    ) is None
    assert await service.claim_removal(
        inflight_chat_id,
        user_id,
        inflight_membership.generation,
        now=lease_expires_at,
    ) is not None
    assert await service.confirm_restriction(
        inflight_chat_id,
        user_id,
        inflight_membership.generation,
        attempt_id=inflight_attempt,
        now=lease_expires_at + timedelta(seconds=1),
    ) is None


@pytest.mark.asyncio
async def test_verification_notification_retry_rotates_token_and_clears_metadata_on_verify(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, first_payload = await _prepare_token(service, chat_id, user_id, now)

    async with factory() as session:
        stored = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert stored is not None
    assert stored.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
    assert stored.action_attempt_id is None

    failed = await service.mark_notification_failure(
        chat_id,
        user_id,
        membership.generation,
        "send failed",
        attempt_payload=first_payload,
        next_retry_at=now + timedelta(seconds=30),
        now=now + timedelta(seconds=3),
    )
    assert failed is not None
    assert failed.retry_count == 1
    assert (
        await service.mark_notification_failure(
            chat_id,
            user_id,
            membership.generation,
            "duplicate failure",
            attempt_payload=first_payload,
            now=now + timedelta(seconds=4),
        )
        is None
    )

    retry_service = OnboardingService(factory)
    assert await retry_service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=29)) is None
    assert await retry_service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=31)) is None
    second_payload = await retry_service.retry_verification_token(
        chat_id,
        user_id,
        membership.generation,
        now=now + timedelta(seconds=31),
    )
    assert second_payload is not None
    assert second_payload != first_payload

    async with factory() as session:
        rotated = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert rotated is not None
    assert rotated.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
    assert rotated.retry_count == 1
    assert rotated.last_attempt_at is not None
    assert rotated.next_retry_at is not None
    assert rotated.last_error == "send failed"
    assert await retry_service.verify_token(user_id, first_payload, now=now + timedelta(seconds=32)) is None
    assert await retry_service.verify_token(user_id, second_payload, now=now + timedelta(seconds=32)) is not None

    async with factory() as session:
        cleared = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert cleared is not None
    assert cleared.pending_action is None
    assert cleared.retry_count == 0
    assert cleared.last_attempt_at is None
    assert cleared.next_retry_at is None
    assert cleared.last_error is None
    assert cleared.action_attempt_id is None

    async with factory() as session:
        failure_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.NOTIFICATION_FAILURE.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert len(failure_audits) == 1


@pytest.mark.asyncio
async def test_deadline_removal_supersedes_failed_notification_backoff(app_context):
    factory, _, _ = app_context
    joined_at = datetime(2026, 8, 22, 4, 55, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, joined_at, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, joined_at)

    failed = await service.mark_notification_failure(
        chat_id,
        user_id,
        membership.generation,
        "notification failed",
        attempt_payload=payload,
        next_retry_at=joined_at + timedelta(hours=1),
        now=joined_at + timedelta(minutes=1),
    )
    assert failed is not None
    assert failed.verification_token_hash is None
    assert failed.next_retry_at == joined_at + timedelta(hours=1)

    removal_attempt = await service.claim_removal(
        chat_id,
        user_id,
        membership.generation,
        now=joined_at + timedelta(minutes=6),
    )
    assert removal_attempt is not None
    assert await service.mark_notification_success(
        chat_id,
        user_id,
        membership.generation,
        attempt_payload=payload,
        now=joined_at + timedelta(minutes=6, seconds=1),
    ) is None
    assert await service.mark_notification_failure(
        chat_id,
        user_id,
        membership.generation,
        "late notification failure",
        attempt_payload=payload,
        now=joined_at + timedelta(minutes=6, seconds=1),
    ) is None


@pytest.mark.asyncio
async def test_unmute_claim_retry_stale_callbacks_and_restart_recovery(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, now)
    assert await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=3)) is not None
    await _add_bound_user(factory, now, user_id)

    queued = await service.queue_unmute_for_user(user_id, now=now + timedelta(seconds=4))
    assert len(queued) == 1
    assert queued[0].state == GroupMembershipState.UNMUTE_PENDING.value
    assert await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=4)) is not None
    first_attempt = await service.get_membership(chat_id, user_id)
    assert first_attempt is not None and first_attempt.action_attempt_id is not None
    first_id = first_attempt.action_attempt_id
    assert await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=5)) is None

    failed = await service.fail_unmute(
        chat_id,
        user_id,
        membership.generation,
        "unmute unavailable",
        attempt_id=first_id,
        next_retry_at=now + timedelta(seconds=20),
        now=now + timedelta(seconds=6),
    )
    assert failed is not None
    assert failed.retry_count == 1
    assert failed.last_error == "unmute unavailable"
    assert await service.confirm_unmute(chat_id, user_id, membership.generation, attempt_id=first_id, now=now + timedelta(seconds=7)) is None
    assert await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=19)) is None

    recovered = OnboardingService(factory)
    second_id = await recovered.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=20))
    assert second_id is not None and second_id != first_id
    assert await recovered.confirm_unmute(chat_id, user_id, membership.generation, attempt_id=first_id, now=now + timedelta(seconds=21)) is None
    active = await recovered.confirm_unmute(chat_id, user_id, membership.generation, attempt_id=second_id, now=now + timedelta(seconds=22))
    assert active is not None
    assert active.state == GroupMembershipState.ACTIVE.value
    assert active.unmuted_at == now + timedelta(seconds=22)
    assert active.deadline is None
    assert active.pending_action is None
    assert active.action_attempt_id is None
    assert active.retry_count == 0
    assert active.next_retry_at is None
    assert active.last_error is None
    assert await recovered.confirm_unmute(chat_id, user_id, membership.generation, attempt_id="stale", now=now + timedelta(seconds=23)) is not None


@pytest.mark.asyncio
async def test_unmute_queue_clears_restrict_retry_metadata_but_honors_inflight_lease(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership = await service.begin_join(chat_id, user_id, real_join=True, joined_at=now)

    first_attempt = await service.claim_restriction(chat_id, user_id, membership.generation, now=now)
    assert first_attempt is not None
    failed_restriction = await service.fail_restriction(
        chat_id,
        user_id,
        membership.generation,
        "restrict failed",
        attempt_id=first_attempt,
        next_retry_at=now + timedelta(seconds=20),
        now=now + timedelta(seconds=1),
    )
    assert failed_restriction is not None
    assert failed_restriction.retry_count == 1
    assert failed_restriction.last_error == "restrict failed"

    in_flight_attempt = await service.claim_restriction(
        chat_id,
        user_id,
        membership.generation,
        now=now + timedelta(seconds=20),
    )
    assert in_flight_attempt is not None
    in_flight = await service.get_membership(chat_id, user_id)
    assert in_flight is not None
    assert in_flight.pending_action == OnboardingAction.RESTRICT.value
    assert in_flight.action_attempt_id == in_flight_attempt
    assert in_flight.retry_count == 1
    assert in_flight.last_error == "restrict failed"
    restrict_lease = OnboardingService._persisted_utc(in_flight.next_retry_at)
    assert restrict_lease == now + timedelta(seconds=50)

    await _add_bound_user(factory, now, user_id)
    queued = await service.queue_unmute_for_user(user_id, now=now + timedelta(seconds=21))
    assert len(queued) == 1
    transitioned = await service.get_membership(chat_id, user_id)
    assert transitioned is not None
    assert transitioned.state == GroupMembershipState.UNMUTE_PENDING.value
    assert transitioned.pending_action is None
    assert transitioned.action_attempt_id is None
    assert transitioned.retry_count == 0
    assert transitioned.last_error is None
    assert transitioned.last_attempt_at is None
    assert OnboardingService._persisted_utc(transitioned.next_retry_at) == restrict_lease

    assert await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=49)) is None
    unmute_attempt = await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(seconds=50))
    assert unmute_attempt is not None
    failed_unmute = await service.fail_unmute(
        chat_id,
        user_id,
        membership.generation,
        "unmute failed",
        attempt_id=unmute_attempt,
        next_retry_at=now + timedelta(seconds=70),
        now=now + timedelta(seconds=51),
    )
    assert failed_unmute is not None
    assert failed_unmute.retry_count == 1
    assert failed_unmute.last_error == "unmute failed"


@pytest.mark.asyncio
async def test_timeout_removal_claim_retry_stale_callbacks_and_bind_ordering(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, now)
    assert await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=3)) is not None

    removal_id = await service.claim_removal(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=1))
    assert removal_id is not None
    pending = await service.get_membership(chat_id, user_id)
    assert pending is not None
    assert pending.state == GroupMembershipState.REMOVE_PENDING.value
    assert pending.pending_action == OnboardingAction.REMOVE.value
    assert OnboardingService._persisted_utc(pending.removal_requested_at) == now + timedelta(minutes=5, seconds=1)
    assert await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=2)) is None
    assert await service.claim_removal(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=2)) is None

    failed = await service.fail_removal(
        chat_id,
        user_id,
        membership.generation,
        "removal unavailable",
        attempt_id=removal_id,
        next_retry_at=now + timedelta(minutes=5, seconds=20),
        now=now + timedelta(minutes=5, seconds=3),
    )
    assert failed is not None
    assert failed.retry_count == 1
    assert await service.confirm_removal(chat_id, user_id, membership.generation, attempt_id=removal_id, now=now + timedelta(minutes=5, seconds=4)) is None
    recovered = OnboardingService(factory)
    second_id = await recovered.claim_removal(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=20))
    assert second_id is not None and second_id != removal_id
    assert await recovered.confirm_removal(chat_id, user_id, membership.generation, attempt_id=removal_id, now=now + timedelta(minutes=5, seconds=21)) is None
    removed = await recovered.confirm_removal(chat_id, user_id, membership.generation, attempt_id=second_id, now=now + timedelta(minutes=5, seconds=22))
    assert removed is not None
    assert removed.state == GroupMembershipState.REMOVED.value
    assert removed.removed_at == now + timedelta(minutes=5, seconds=22)
    assert removed.deadline is None
    assert removed.pending_action is None
    assert removed.action_attempt_id is None


@pytest.mark.asyncio
async def test_bind_and_timeout_claims_have_single_winner_per_generation(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, now)
    assert await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=3)) is not None
    await _add_bound_user(factory, now, user_id)

    # Bind reconciliation wins before the deadline: removal cannot claim it.
    assert await service.queue_unmute_for_user(user_id, now=now + timedelta(minutes=5, seconds=1))
    unmute_id = await service.claim_unmute(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=1))
    assert unmute_id is not None
    assert await service.claim_removal(chat_id, user_id, membership.generation, now=now + timedelta(minutes=5, seconds=2)) is None

    # A separate generation with no binding takes the timeout path; unmute is
    # then ineligible even if a binding record is created later.
    second_chat_id = -1002
    await _add_groups(factory, now, second_chat_id)
    second = await service.begin_join(second_chat_id, user_id + 1, real_join=True, joined_at=now)
    second_payload = await service.issue_verification_token(second_chat_id, user_id + 1, second.generation, now=now + timedelta(seconds=1))
    assert second_payload is None
    second_attempt = await service.claim_restriction(second_chat_id, user_id + 1, second.generation, now=now)
    assert second_attempt is not None
    assert await service.confirm_restriction(
        second_chat_id,
        user_id + 1,
        second.generation,
        attempt_id=second_attempt,
        now=now + timedelta(seconds=1),
    ) is not None
    removal = await service.claim_removal(second_chat_id, user_id + 1, second.generation, now=now + timedelta(minutes=5, seconds=1))
    assert removal is not None
    await _add_bound_user(factory, now, user_id + 1)
    assert await service.claim_unmute(second_chat_id, user_id + 1, second.generation, now=now + timedelta(minutes=5, seconds=2)) is None


@pytest.mark.asyncio
async def test_postgresql_concurrent_restriction_claims_are_single_winner(postgres_factories):
    first_factory, second_factory = postgres_factories
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1009000000001
    user_id = 900000001
    async with first_factory() as session:
        async with session.begin():
            await session.execute(delete(GroupMembership).where(GroupMembership.chat_id == chat_id))
            await session.execute(delete(ManagedGroup).where(ManagedGroup.chat_id == chat_id))
    await _add_groups(first_factory, now, chat_id)
    first_service = OnboardingService(first_factory)
    second_service = OnboardingService(second_factory)
    membership = await first_service.begin_join(chat_id, user_id, real_join=True, joined_at=now)

    attempts = await asyncio.gather(
        first_service.claim_restriction(chat_id, user_id, membership.generation, now=now),
        second_service.claim_restriction(chat_id, user_id, membership.generation, now=now),
    )
    claimed = [attempt for attempt in attempts if attempt is not None]
    assert len(claimed) == 1

    async with first_factory() as session:
        row = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert row is not None
    assert row.action_attempt_id == claimed[0]


@pytest.mark.asyncio
async def test_postgresql_concurrent_multigroup_unmute_and_removal_have_single_winner(postgres_factories):
    first_factory, second_factory = postgres_factories
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    first_chat_id = -1009000000011
    second_chat_id = -1009000000012
    user_id = 900000011
    async with first_factory() as session:
        async with session.begin():
            await session.execute(delete(GroupMembership).where(GroupMembership.chat_id.in_([first_chat_id, second_chat_id])))
            await session.execute(delete(ManagedGroup).where(ManagedGroup.chat_id.in_([first_chat_id, second_chat_id])))
            await session.execute(delete(User).where(User.telegram_user_id == user_id))
    await _add_groups(first_factory, now, first_chat_id, second_chat_id)

    setup_service = OnboardingService(first_factory)
    await setup_service.begin_join(
        first_chat_id,
        user_id,
        real_join=True,
        joined_at=now - timedelta(minutes=10),
    )
    second_membership = await setup_service.begin_join(
        second_chat_id,
        user_id,
        real_join=True,
        joined_at=now - timedelta(minutes=10),
    )
    await _add_bound_user(first_factory, now, user_id)

    queue_rows_locked = asyncio.Event()
    release_queue = asyncio.Event()
    removal_started = asyncio.Event()

    class CoordinatedQueueService(OnboardingService):
        async def _user(self, session, telegram_user_id):
            if telegram_user_id == user_id and not queue_rows_locked.is_set():
                queue_rows_locked.set()
                await asyncio.wait_for(release_queue.wait(), timeout=5)
            return await OnboardingService._user(session, telegram_user_id)

    class CoordinatedRemovalService(OnboardingService):
        async def _membership(self, session, chat_id, telegram_user_id, *, lock):
            if lock and chat_id == second_chat_id and telegram_user_id == user_id:
                removal_started.set()
            return await OnboardingService._membership(session, chat_id, telegram_user_id, lock=lock)

    queue_service = CoordinatedQueueService(first_factory)
    removal_service = CoordinatedRemovalService(second_factory)
    queue_task = asyncio.create_task(queue_service.queue_unmute_for_user(user_id, now=now))
    await asyncio.wait_for(queue_rows_locked.wait(), timeout=5)
    removal_task = asyncio.create_task(
        removal_service.claim_removal(second_chat_id, user_id, second_membership.generation, now=now)
    )
    await asyncio.wait_for(removal_started.wait(), timeout=5)
    release_queue.set()
    queued, removal_attempt = await asyncio.wait_for(asyncio.gather(queue_task, removal_task), timeout=5)

    assert {row.chat_id for row in queued} == {first_chat_id, second_chat_id}
    assert removal_attempt is None
    async with first_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(GroupMembership)
                    .where(GroupMembership.telegram_user_id == user_id)
                    .order_by(GroupMembership.chat_id)
                )
            ).all()
        )
    assert [row.chat_id for row in rows] == [first_chat_id, second_chat_id]
    assert all(row.state == GroupMembershipState.UNMUTE_PENDING.value for row in rows)
    assert all(row.pending_action is None and row.action_attempt_id is None for row in rows)
    assert all(row.retry_count == 0 and row.last_error is None for row in rows)


@pytest.mark.asyncio
async def test_verification_notification_crash_recovery_and_success_are_idempotent(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, first_payload = await _prepare_token(service, chat_id, user_id, now)

    recovery_service = OnboardingService(factory)
    assert await recovery_service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=31)) is None
    assert await recovery_service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=33)) is None
    recovered_payload = await recovery_service.retry_verification_token(
        chat_id,
        user_id,
        membership.generation,
        now=now + timedelta(seconds=33),
    )
    assert recovered_payload is not None
    assert recovered_payload != first_payload
    assert await recovery_service.verify_token(user_id, first_payload, now=now + timedelta(seconds=4)) is None

    assert (
        await recovery_service.mark_notification_success(
            chat_id,
            user_id,
            membership.generation,
            attempt_payload=recovered_payload,
            now=now + timedelta(seconds=34),
        )
        is not None
    )
    assert (
        await recovery_service.issue_verification_token(
            chat_id,
            user_id,
            membership.generation,
            now=now + timedelta(seconds=35),
        )
        is None
    )
    assert (
        await recovery_service.retry_verification_token(
            chat_id,
            user_id,
            membership.generation,
            now=now + timedelta(seconds=35),
        )
        is None
    )
    assert await recovery_service.verify_token(user_id, recovered_payload, now=now + timedelta(seconds=35)) is not None


@pytest.mark.asyncio
async def test_verification_claim_blocks_concurrent_issue_and_preserves_first_payload(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, first_payload = await _prepare_token(service, chat_id, user_id, now)

    results = await asyncio.gather(
        service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=3)),
        service.issue_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=3)),
    )
    assert results == [None, None]
    assert await service.verify_token(user_id, first_payload, now=now + timedelta(seconds=4)) is not None

    async with factory() as session:
        issue_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.TOKEN_ISSUED.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert len(issue_audits) == 1


@pytest.mark.asyncio
async def test_retry_rejects_after_verify_or_notification_success(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    user_id = 42
    first_chat_id = -1001
    second_chat_id = -1002
    await _add_groups(factory, now, first_chat_id, second_chat_id)
    service = OnboardingService(factory)

    first_membership, first_payload = await _prepare_token(service, first_chat_id, user_id, now)
    assert await service.verify_token(user_id, first_payload, generation=first_membership.generation, now=now + timedelta(seconds=3)) is not None
    assert await service.retry_verification_token(first_chat_id, user_id, first_membership.generation, now=now + timedelta(seconds=40)) is None

    second_membership, second_payload = await _prepare_token(service, second_chat_id, user_id, now)
    assert (
        await service.mark_notification_success(
            second_chat_id,
            user_id,
            second_membership.generation,
            attempt_payload=second_payload,
            now=now + timedelta(seconds=3),
        )
        is not None
    )
    assert await service.retry_verification_token(second_chat_id, user_id, second_membership.generation, now=now + timedelta(seconds=40)) is None
    assert await service.verify_token(user_id, second_payload, generation=second_membership.generation, now=now + timedelta(seconds=40)) is not None


@pytest.mark.asyncio
async def test_late_notification_failure_after_verification_is_ignored(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, now)

    assert await service.verify_token(user_id, payload, generation=membership.generation, now=now + timedelta(seconds=3)) is not None
    assert (
        await service.mark_notification_failure(
            chat_id,
            user_id,
            membership.generation,
            "late failure",
            attempt_payload=payload,
            now=now + timedelta(seconds=4),
        )
        is None
    )

    async with factory() as session:
        row = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
        failures = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.NOTIFICATION_FAILURE.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert row is not None
    assert row.pending_action is None
    assert row.verification_token_hash is None
    assert row.retry_count == 0
    assert row.last_error is None
    assert failures == []


@pytest.mark.asyncio
async def test_rotated_attempt_rejects_late_old_results_but_current_success_works(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, first_payload = await _prepare_token(service, chat_id, user_id, now)
    second_payload = await service.retry_verification_token(chat_id, user_id, membership.generation, now=now + timedelta(seconds=33))
    assert second_payload is not None
    assert second_payload != first_payload

    assert (
        await service.mark_notification_success(
            chat_id,
            user_id,
            membership.generation,
            attempt_payload=first_payload,
            now=now + timedelta(seconds=34),
        )
        is None
    )
    assert (
        await service.mark_notification_failure(
            chat_id,
            user_id,
            membership.generation,
            "late failure",
            attempt_payload=first_payload,
            now=now + timedelta(seconds=34),
        )
        is None
    )

    async with factory() as session:
        row = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
    assert row is not None
    assert row.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
    assert row.verification_token_hash is not None
    assert row.retry_count == 0
    assert row.last_error is None

    assert (
        await service.mark_notification_success(
            chat_id,
            user_id,
            membership.generation,
            attempt_payload=second_payload,
            now=now + timedelta(seconds=35),
        )
        is not None
    )
    assert await service.verify_token(user_id, second_payload, now=now + timedelta(seconds=36)) is not None

    async with factory() as session:
        success_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.NOTIFICATION_SUCCESS.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
        failure_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == OnboardingAuditAction.NOTIFICATION_FAILURE.value,
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                    )
                )
            ).all()
        )
    assert len(success_audits) == 1
    assert failure_audits == []


@pytest.mark.asyncio
async def test_notification_result_requires_current_payload(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001
    user_id = 42
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership, payload = await _prepare_token(service, chat_id, user_id, now)
    forged_payloads = (None, "missing-prefix", "verify_-1001_wrong", payload.replace(str(chat_id), "-1002", 1))

    for forged_payload in forged_payloads:
        assert (
            await service.mark_notification_success(
                chat_id,
                user_id,
                membership.generation,
                attempt_payload=forged_payload,
                now=now + timedelta(seconds=3),
            )
            is None
        )
        assert (
            await service.mark_notification_failure(
                chat_id,
                user_id,
                membership.generation,
                "forged result",
                attempt_payload=forged_payload,
                now=now + timedelta(seconds=3),
            )
            is None
        )

    async with factory() as session:
        row = await session.scalar(
            select(GroupMembership).where(
                GroupMembership.chat_id == chat_id,
                GroupMembership.telegram_user_id == user_id,
            )
        )
        result_audits = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.target_id == f"{chat_id}:{user_id}",
                        AuditLog.action.in_(
                            [
                                OnboardingAuditAction.NOTIFICATION_SUCCESS.value,
                                OnboardingAuditAction.NOTIFICATION_FAILURE.value,
                            ]
                        ),
                    )
                )
            ).all()
        )
    assert row is not None
    assert row.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value
    assert row.verification_token_hash is not None
    assert row.retry_count == 0
    assert result_audits == []


@pytest.mark.asyncio
async def test_verification_payload_rejects_overlong_payload(app_context, monkeypatch):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    chat_id = -1001234567890
    await _add_groups(factory, now, chat_id)
    service = OnboardingService(factory)
    membership = await service.begin_join(chat_id, 42, real_join=True, joined_at=now)
    attempt_id = await service.claim_restriction(chat_id, 42, membership.generation, now=now)
    assert attempt_id is not None
    await service.confirm_restriction(
        chat_id,
        42,
        membership.generation,
        attempt_id=attempt_id,
        now=now + timedelta(seconds=1),
    )
    monkeypatch.setattr(OnboardingService, "_new_token", staticmethod(lambda: "a" * 55))

    with pytest.raises(OnboardingError, match="无法生成有效的验证链接"):
        await service.issue_verification_token(chat_id, 42, membership.generation, now=now + timedelta(seconds=2))


@pytest.mark.asyncio
async def test_verification_payload_rejects_invalid_chat_id_boundaries(app_context):
    factory, _, _ = app_context
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    service = OnboardingService(factory)
    invalid_payloads = (
        "verify_-9223372036854775809_opaque",
        "verify_-１２３_opaque",
        "verify_123_opaque",
        "verify_0_opaque",
        "verify_+123_opaque",
    )

    for payload in invalid_payloads:
        assert await service.verify_token(42, payload, now=now) is None

    minimum_payload = "verify_-9223372036854775808_opaque"
    assert OnboardingService._parse_token_payload(minimum_payload) == (-(2**63), "opaque")
    assert await service.verify_token(42, minimum_payload, now=now) is None
