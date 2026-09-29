from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import utcnow
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import ensure_utc
from reclaude_bot.domain.timefmt import format_beijing
from reclaude_bot.infrastructure.db.models import DeviceAccountNotification, User

NotifyCallback = Callable[[int, str], Awaitable[None]]


@dataclass(frozen=True)
class _PendingAccountNotification:
    notification_id: int
    attempt: int
    recipient_id: int
    text: str


class DeviceAccountNotificationService:
    """Durable private notices for automatic account-cycle generations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        user_notify_callback: NotifyCallback | None = None,
        admin_ids: Iterable[int] = (),
        clock: Callable[[], datetime] = utcnow,
        retry_seconds: int = 60,
        send_timeout_seconds: int = 35,
    ) -> None:
        if isinstance(retry_seconds, bool) or not isinstance(retry_seconds, int) or retry_seconds <= 0:
            raise ValueError("retry_seconds must be a positive integer")
        if isinstance(send_timeout_seconds, bool) or not isinstance(send_timeout_seconds, int) or send_timeout_seconds <= 0:
            raise ValueError("send_timeout_seconds must be a positive integer")
        self.session_factory = session_factory
        self.user_notify_callback = user_notify_callback
        self.admin_ids = self._validate_admin_ids(admin_ids)
        self.clock = clock
        self.retry_seconds = retry_seconds
        self.send_timeout_seconds = send_timeout_seconds
        self.lease_seconds = max(retry_seconds, send_timeout_seconds)

    async def queue_reset_success(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        cycle_id: int,
        generation_key: str,
        task_name: str,
        account_id: str,
        previous_account_id: str,
        task_limit_usd: object | None = None,
        reset_at: datetime | None = None,
        affected_user_ids: Iterable[int],
    ) -> int:
        user_ids = tuple(sorted(set(affected_user_ids)))
        users = list(
            (
                await session.scalars(
                    select(User).where(User.id.in_(user_ids)).order_by(User.id)
                )
            ).all()
        ) if user_ids else []
        now = self._now()
        limit_text = "按任务配置"
        if task_limit_usd is not None:
            try:
                limit_text = f"${task_limit_usd:.2f}"  # type: ignore[operator]
            except (TypeError, ValueError):
                pass
        reset_text = f"新周期刷新时间：{format_beijing(reset_at)}。" if reset_at is not None else ""
        base_text = (
            f"任务 {html.escape(task_name)}：本轮已用额度已重置为 $0，历史记录保留；"
            f"任务额度上限按配置为 {limit_text}。上游账号已切换，现有设备不会重新授权，"
            f"后续消费从新周期基线开始统计。{reset_text}"
        )
        inserted = 0
        for user in users:
            user_text = base_text
            inserted += await self._queue_row(
                session,
                task_id=task_id,
                cycle_id=cycle_id,
                generation_key=generation_key,
                kind="ACCOUNT_RESET_SUCCESS",
                recipient_type="USER",
                recipient_id=user.telegram_user_id,
                user_id=user.id,
                text=user_text,
                payload={
                    "text": user_text,
                    "task_name": task_name,
                    "user_id": user.id,
                    "account_id": account_id,
                    "previous_account_id": previous_account_id,
                },
                now=now,
            )
            for admin_id in self.admin_ids:
                admin_text = (
                    f"{base_text} 任务：{html.escape(task_name)}；本地用户 ID：{user.id}；"
                    f"Telegram 用户 ID：{user.telegram_user_id}。"
                )
                inserted += await self._queue_row(
                    session,
                    task_id=task_id,
                    cycle_id=cycle_id,
                    generation_key=generation_key,
                    kind="ACCOUNT_RESET_SUCCESS",
                    recipient_type="ADMIN",
                    recipient_id=admin_id,
                    user_id=user.id,
                    text=admin_text,
                    payload={
                        "text": admin_text,
                        "task_name": task_name,
                        "user_id": user.id,
                        "account_id": account_id,
                        "previous_account_id": previous_account_id,
                    },
                    now=now,
                )
        return inserted

    async def queue_reset_failure(
        self,
        *,
        task_id: int,
        cycle_id: int | None,
        generation_key: str,
        task_name: str,
        account_id: str,
        previous_account_id: str,
        error_text: str,
    ) -> int:
        if not self.admin_ids:
            return 0
        safe_error = html.escape(str(error_text))[:512]
        text = (
            f"托管设备账号自动切换失败。任务：{html.escape(task_name)}；"
            f"账号：{html.escape(previous_account_id)} → {html.escape(account_id)}；"
            f"错误：{safe_error}。写操作已暂停，将自动重试。"
        )
        now = self._now()
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    inserted = 0
                    for admin_id in self.admin_ids:
                        inserted += await self._queue_row(
                            session,
                            task_id=task_id,
                            cycle_id=cycle_id,
                            generation_key=generation_key,
                            kind="ACCOUNT_RESET_FAILURE",
                            recipient_type="ADMIN",
                            recipient_id=admin_id,
                            user_id=None,
                            text=text,
                            payload={
                                "text": text,
                                "task_name": task_name,
                                "account_id": account_id,
                                "previous_account_id": previous_account_id,
                                "error": str(error_text)[:512],
                            },
                            now=now,
                        )
                    return inserted
        except IntegrityError:
            # Another process may have queued the same generation at the same
            # time.  The unique key makes that race harmless and suppresses
            # repeated administrator alerts.
            return 0

    async def deliver_pending(self, now: datetime | None = None, *, limit: int = 50) -> int:
        if self.user_notify_callback is None:
            return 0
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 500:
            raise EligibilityError("notification limit must be between 1 and 500")
        moment = ensure_utc(now or self.clock())
        async with self.session_factory() as session:
            notification_ids = list(
                (
                    await session.scalars(
                        select(DeviceAccountNotification.id)
                        .where(
                            DeviceAccountNotification.status == "PENDING",
                            (DeviceAccountNotification.next_retry_at.is_(None))
                            | (DeviceAccountNotification.next_retry_at <= moment),
                        )
                        .order_by(DeviceAccountNotification.created_at, DeviceAccountNotification.id)
                        .limit(limit)
                    )
                ).all()
            )
        sent = 0
        for notification_id in notification_ids:
            claim_now = max(moment, self._now())
            pending = await self._claim(notification_id, claim_now)
            if pending is None:
                continue
            try:
                async with asyncio.timeout(self.send_timeout_seconds):
                    await self.user_notify_callback(pending.recipient_id, pending.text)
            except Exception:
                await self._finish(pending.notification_id, pending.attempt, max(moment, self._now()), error=True)
                continue
            await self._finish(pending.notification_id, pending.attempt, max(moment, self._now()), error=False)
            sent += 1
        return sent

    async def _queue_row(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        cycle_id: int | None,
        generation_key: str,
        kind: str,
        recipient_type: str,
        recipient_id: int,
        user_id: int | None,
        text: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> int:
        existing = await session.scalar(
            select(DeviceAccountNotification.id).where(
                DeviceAccountNotification.task_id == task_id,
                DeviceAccountNotification.generation_key == generation_key,
                DeviceAccountNotification.kind == kind,
                DeviceAccountNotification.recipient_type == recipient_type,
                DeviceAccountNotification.recipient_id == recipient_id,
                DeviceAccountNotification.user_id == user_id,
            )
        )
        if existing is not None:
            return 0
        session.add(
            DeviceAccountNotification(
                task_id=task_id,
                cycle_id=cycle_id,
                generation_key=generation_key,
                kind=kind,
                recipient_type=recipient_type,
                recipient_id=recipient_id,
                user_id=user_id,
                status="PENDING",
                attempt_count=0,
                next_retry_at=None,
                sent_at=None,
                created_at=now,
                updated_at=now,
                payload={**payload, "text": text},
                last_error_code=None,
            )
        )
        return 1

    async def _claim(self, notification_id: int, now: datetime) -> _PendingAccountNotification | None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(DeviceAccountNotification)
                    .where(DeviceAccountNotification.id == notification_id)
                    .with_for_update()
                )
                if row is None or row.status != "PENDING":
                    return None
                if row.next_retry_at is not None and ensure_utc(row.next_retry_at) > now:
                    return None
                payload = row.payload if isinstance(row.payload, dict) else {}
                text = payload.get("text")
                if not isinstance(text, str) or not text:
                    row.status = "CANCELLED"
                    row.updated_at = now
                    row.last_error_code = "invalid_notification_payload"
                    return None
                row.attempt_count += 1
                attempt = row.attempt_count
                row.next_retry_at = now + timedelta(seconds=self.lease_seconds)
                row.updated_at = now
                return _PendingAccountNotification(row.id, attempt, row.recipient_id, text)

    async def _finish(self, notification_id: int, attempt: int, now: datetime, *, error: bool) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(DeviceAccountNotification)
                    .where(DeviceAccountNotification.id == notification_id)
                    .with_for_update()
                )
                if row is None or row.status != "PENDING" or row.attempt_count != attempt:
                    return
                if error:
                    row.next_retry_at = now + timedelta(seconds=self.lease_seconds)
                    row.last_error_code = "telegram_delivery_failed"
                    row.updated_at = now
                    return
                row.status = "SENT"
                row.sent_at = now
                row.next_retry_at = None
                row.last_error_code = None
                row.updated_at = now

    @staticmethod
    def _validate_admin_ids(admin_ids: Iterable[int]) -> tuple[int, ...]:
        result: list[int] = []
        for admin_id in admin_ids:
            if isinstance(admin_id, bool) or not isinstance(admin_id, int) or admin_id <= 0:
                raise ValueError("admin_ids must contain positive integers")
            if admin_id not in result:
                result.append(admin_id)
        return tuple(result)

    def _now(self) -> datetime:
        return ensure_utc(self.clock())
