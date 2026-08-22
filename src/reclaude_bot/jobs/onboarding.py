from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.onboarding import OnboardingAction, OnboardingCandidate, OnboardingService

log = structlog.get_logger(__name__)

_NOT_MEMBER_MARKERS = (
    "not a member",
    "member not found",
    "user not found",
    "participant_id_invalid",
)
_ALREADY_APPLIED_MARKERS = ("not modified", "already restricted", "already unbanned")
_MAX_RETRY_DELAY = 60


class OnboardingTelegramGateway(Protocol):
    async def restrict_member(self, chat_id: int, user_id: int) -> bool: ...

    async def restore_member(self, chat_id: int, user_id: int) -> bool: ...

    async def remove_member(self, chat_id: int, user_id: int) -> bool: ...

    async def bot_username(self) -> str: ...


AlertCallback = Callable[[str], Awaitable[None]]


def retry_delay(retry_count: int) -> timedelta:
    """Bound transient Telegram retries to one minute."""
    return timedelta(seconds=min(_MAX_RETRY_DELAY, 5 * (2 ** max(0, retry_count - 1))))


def _error_text(error: BaseException) -> str:
    return str(getattr(error, "message", error))


def _has_marker(error: BaseException, markers: Iterable[str]) -> bool:
    text = _error_text(error).casefold()
    return any(marker in text for marker in markers)


class OnboardingWorker:
    """Independent durable Telegram reconciler for group onboarding."""

    def __init__(
        self,
        service: OnboardingService,
        gateway: OnboardingTelegramGateway,
        bot: Any | None = None,
        *,
        owner_ids: Iterable[int] = (),
        alert_callback: AlertCallback | None = None,
        interval: float = 5.0,
        scan_limit: int = 100,
        group_titles: Any | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if scan_limit <= 0:
            raise ValueError("scan_limit must be positive")
        self.service = service
        self.gateway = gateway
        self.bot = bot if bot is not None else getattr(gateway, "bot", None)
        self.owner_ids = tuple(owner_ids)
        self.alert_callback = alert_callback
        self.interval = interval
        self.scan_limit = scan_limit
        self.group_titles = group_titles
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._alerted: set[tuple[int, int, int, str]] = set()
        self._display_names: dict[tuple[int, int, int], str] = {}

    @staticmethod
    def _now() -> datetime:
        return utcnow()

    async def start(self) -> None:
        await self.run_once()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("onboarding_worker_pass_failed", error=type(exc).__name__)

    async def run_once(self, *, now: datetime | None = None) -> int:
        instant = now or self._now()
        await self.service.reconcile_bound_memberships(limit=self.scan_limit, now=instant)
        candidates = await self.service.scan_candidates(now=instant, limit=self.scan_limit)
        processed = 0
        for candidate in candidates:
            try:
                if await self._process(candidate, instant):
                    processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception(
                    "onboarding_candidate_failed",
                    chat_id=candidate.chat_id,
                    telegram_user_id=candidate.telegram_user_id,
                    generation=candidate.generation,
                    action=candidate.pending_action or candidate.state,
                    error=type(exc).__name__,
                )
        return processed

    reconcile_once = run_once

    async def handle_join(
        self,
        chat_id: int,
        telegram_user_id: int,
        *,
        joined_at: datetime | None = None,
        now: datetime | None = None,
        display_name: str | None = None,
        is_telegram_admin: bool = False,
        is_group_owner: bool = False,
    ) -> bool:
        """Persist and immediately reconcile one genuine member transition."""
        instant = now or joined_at or self._now()
        row = await self.service.begin_join(
            chat_id,
            telegram_user_id,
            real_join=True,
            joined_at=joined_at or instant,
            is_telegram_admin=is_telegram_admin,
            is_group_owner=is_group_owner,
        )
        if display_name:
            self._display_names[(chat_id, telegram_user_id, row.generation)] = display_name
        if row.state not in {"RESTRICT_PENDING", "MUTED", "UNMUTE_PENDING", "REMOVE_PENDING"}:
            return False
        candidate = OnboardingCandidate(
            chat_id=row.chat_id,
            telegram_user_id=row.telegram_user_id,
            generation=row.generation,
            state=row.state,
            pending_action=row.pending_action,
            deadline=self.service._persisted_utc(row.deadline),
            next_retry_at=self.service._persisted_utc(row.next_retry_at),
            retry_count=row.retry_count,
            last_alerted_retry_count=row.last_alerted_retry_count,
            last_alerted_action=row.last_alerted_action,
            verification_started_at=self.service._persisted_utc(row.verification_started_at),
            has_verification_token=row.verification_token_hash is not None,
        )
        return await self._process(candidate, instant)

    async def _process(self, candidate: OnboardingCandidate, now: datetime) -> bool:
        deadline_due = candidate.deadline is not None and now >= candidate.deadline
        if deadline_due and candidate.state in {
            "RESTRICT_PENDING",
            "MUTED",
            "REMOVE_PENDING",
        }:
            return await self._remove(candidate, now)
        if candidate.state == "RESTRICT_PENDING":
            return await self._restrict(candidate, now)
        if candidate.state == "MUTED":
            if candidate.verification_started_at is not None:
                return False
            return await self._notify(candidate, now)
        if candidate.state == "UNMUTE_PENDING":
            return await self._unmute(candidate, now)
        if candidate.state == "REMOVE_PENDING":
            return await self._remove(candidate, now)
        return False

    async def _restrict(self, candidate: OnboardingCandidate, now: datetime) -> bool:
        attempt_id = await self.service.claim_restriction(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            now=now,
        )
        if attempt_id is None:
            return False
        try:
            applied = await self.gateway.restrict_member(candidate.chat_id, candidate.telegram_user_id)
            if not applied:
                raise RuntimeError("Telegram did not confirm restriction")
        except Exception as exc:
            if _has_marker(exc, _NOT_MEMBER_MARKERS):
                await self.service.mark_left(candidate.chat_id, candidate.telegram_user_id, candidate.generation, now=now)
                return True
            if not _has_marker(exc, _ALREADY_APPLIED_MARKERS):
                row = await self.service.fail_restriction(
                    candidate.chat_id,
                    candidate.telegram_user_id,
                    candidate.generation,
                    _error_text(exc),
                    attempt_id=attempt_id,
                    next_retry_at=now + retry_delay(candidate.retry_count + 1),
                    now=now,
                )
                await self._alert_after_failure(candidate, OnboardingAction.RESTRICT.value, row)
                return row is not None
        row = await self.service.confirm_restriction(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            attempt_id=attempt_id,
            now=now,
        )
        if row is None or row.state != "MUTED":
            return row is not None
        return await self._notify(
            OnboardingCandidate(
                candidate.chat_id,
                candidate.telegram_user_id,
                candidate.generation,
                row.state,
                row.pending_action,
                row.deadline,
                row.next_retry_at,
                row.retry_count,
                row.last_alerted_retry_count,
                row.last_alerted_action,
                row.verification_started_at,
                row.verification_token_hash is not None,
            ),
            now,
        )

    async def _notify(self, candidate: OnboardingCandidate, now: datetime) -> bool:
        if candidate.pending_action is None and candidate.has_verification_token:
            return False
        if candidate.pending_action == OnboardingAction.VERIFICATION_NOTIFICATION.value:
            payload = await self.service.retry_verification_token(
                candidate.chat_id,
                candidate.telegram_user_id,
                candidate.generation,
                now=now,
            )
        else:
            payload = await self.service.issue_verification_token(
                candidate.chat_id,
                candidate.telegram_user_id,
                candidate.generation,
                now=now,
            )
        if payload is None:
            return False
        try:
            username = await self.gateway.bot_username()
            url = f"https://t.me/{username}?start={payload}"
            title = await self._group_title(candidate.chat_id)
            display_name = self._display_names.get(
                (candidate.chat_id, candidate.telegram_user_id, candidate.generation),
                "该成员",
            )
            mention = f'<a href="tg://user?id={candidate.telegram_user_id}">{html.escape(display_name)}</a>'
            text = f"{html.escape(title)}：{mention} 请点击按钮进入私聊并完成绑定。"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="验证并绑定", url=url)]]
            )
            if self.bot is None:
                raise RuntimeError("Telegram bot unavailable")
            await self.bot.send_message(candidate.chat_id, text, reply_markup=markup)
        except Exception as exc:
            row = await self.service.mark_notification_failure(
                candidate.chat_id,
                candidate.telegram_user_id,
                candidate.generation,
                _error_text(exc),
                attempt_payload=payload,
                next_retry_at=now + retry_delay(candidate.retry_count + 1),
                now=now,
            )
            await self._alert_after_failure(candidate, OnboardingAction.VERIFICATION_NOTIFICATION.value, row)
            return row is not None
        return await self.service.mark_notification_success(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            attempt_payload=payload,
            now=now,
        ) is not None

    async def _unmute(self, candidate: OnboardingCandidate, now: datetime) -> bool:
        attempt_id = await self.service.claim_unmute(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            now=now,
        )
        if attempt_id is None:
            return False
        try:
            applied = await self.gateway.restore_member(candidate.chat_id, candidate.telegram_user_id)
            if not applied:
                raise RuntimeError("Telegram did not confirm permission restoration")
        except Exception as exc:
            if _has_marker(exc, _ALREADY_APPLIED_MARKERS):
                applied = True
            elif _has_marker(exc, _NOT_MEMBER_MARKERS):
                await self.service.mark_left(candidate.chat_id, candidate.telegram_user_id, candidate.generation, now=now)
                return True
            else:
                row = await self.service.fail_unmute(
                    candidate.chat_id,
                    candidate.telegram_user_id,
                    candidate.generation,
                    _error_text(exc),
                    attempt_id=attempt_id,
                    next_retry_at=now + retry_delay(candidate.retry_count + 1),
                    now=now,
                )
                await self._alert_after_failure(candidate, OnboardingAction.UNMUTE.value, row)
                return row is not None
        return await self.service.confirm_unmute(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            attempt_id=attempt_id,
            now=now,
        ) is not None

    async def _remove(self, candidate: OnboardingCandidate, now: datetime) -> bool:
        attempt_id = await self.service.claim_removal(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            now=now,
        )
        if attempt_id is None:
            return False
        try:
            applied = await self.gateway.remove_member(candidate.chat_id, candidate.telegram_user_id)
            if not applied:
                raise RuntimeError("Telegram did not confirm timeout removal")
        except Exception as exc:
            if _has_marker(exc, _NOT_MEMBER_MARKERS + _ALREADY_APPLIED_MARKERS):
                applied = True
            else:
                row = await self.service.fail_removal(
                    candidate.chat_id,
                    candidate.telegram_user_id,
                    candidate.generation,
                    _error_text(exc),
                    attempt_id=attempt_id,
                    next_retry_at=now + retry_delay(candidate.retry_count + 1),
                    now=now,
                )
                await self._alert_after_failure(candidate, OnboardingAction.REMOVE.value, row)
                return row is not None
        return await self.service.confirm_removal(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            attempt_id=attempt_id,
            now=now,
        ) is not None

    async def _group_title(self, chat_id: int) -> str:
        if self.group_titles is not None:
            try:
                row = await self.group_titles.get_status(chat_id)
                title = getattr(row, "title", None)
                if title:
                    return str(title)
            except Exception:
                pass
        return "群组"

    async def _alert_after_failure(self, candidate: OnboardingCandidate, action: str, row: Any) -> None:
        retry_count = getattr(row, "retry_count", None)
        if retry_count is None or retry_count < 5:
            return
        if (
            getattr(row, "last_alerted_retry_count", None) == retry_count
            and getattr(row, "last_alerted_action", None) == action
        ):
            return
        key = (candidate.chat_id, candidate.telegram_user_id, candidate.generation, action)
        if key in self._alerted:
            return
        self._alerted.add(key)
        if not await self.service.mark_failure_alerted(
            candidate.chat_id,
            candidate.telegram_user_id,
            candidate.generation,
            action,
            retry_count,
        ):
            return
        message = f"群组 onboarding 操作连续失败：{action}，chat_id={candidate.chat_id}，user_id={candidate.telegram_user_id}。"
        if self.alert_callback is not None:
            await self.alert_callback(message)
            return
        if self.bot is None:
            return
        for owner_id in self.owner_ids:
            try:
                await self.bot.send_message(owner_id, message)
            except Exception:
                log.warning("onboarding_failure_alert_failed", owner_id=owner_id, action=action)


__all__ = ["OnboardingWorker", "OnboardingTelegramGateway", "retry_delay"]
