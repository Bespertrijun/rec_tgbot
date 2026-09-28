from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.application.recovery import RecoveryGate
from reclaude_bot.domain.enums import BaselineStatus, BindingStatus, UserStatus
from reclaude_bot.domain.errors import BindingError
from reclaude_bot.infrastructure.db.models import DeviceAssociation, User
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{(local[:1] or '*')}***@{domain}"


class BindingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: ReclaudeGateway | None = None,
        attempts_per_hour: int = 10,
        gate: RecoveryGate | None = None,
        onboarding: OnboardingService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.attempts_per_hour = attempts_per_hour
        self.gate = gate
        self.onboarding = onboarding
        self._attempts: dict[int, deque[datetime]] = defaultdict(deque)

    def _check_rate(self, telegram_user_id: int, now: datetime) -> None:
        history = self._attempts[telegram_user_id]
        cutoff = now - timedelta(hours=1)
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= self.attempts_per_hour:
            raise BindingError("绑定尝试次数过多，请稍后重试")
        history.append(now)

    async def bind(self, telegram_user_id: int, email: str, *, private_chat: bool = True, telegram_username: str | None = None) -> User:
        if not private_chat:
            raise BindingError("绑定只能在 Bot 私聊中执行")
        now = utcnow()
        username = telegram_username.casefold() if telegram_username else None
        self._check_rate(telegram_user_id, now)
        normalized = normalize_email(email)
        local, separator, domain = normalized.partition("@")
        if normalized.count("@") != 1 or not separator or not local or not domain or any(character.isspace() for character in normalized) or len(normalized) > 320:
            raise BindingError("邮箱格式无效")
        result: User | None = None
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    existing_tg = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())
                    existing_email = await session.scalar(select(User).where(User.email_normalized == normalized))
                    if existing_email is not None and existing_email.telegram_user_id != telegram_user_id:
                        raise BindingError("该邮箱已被其他账号占用，请联系管理员")

                    if existing_tg is not None:
                        if existing_tg.status == UserStatus.BANNED.value or existing_tg.binding_status == BindingStatus.DISPUTED.value:
                            raise BindingError("账号受限，无法绑定或重新绑定，请联系管理员")
                        if existing_tg.binding_status == BindingStatus.BOUND.value:
                            if existing_tg.email_normalized != normalized:
                                raise BindingError("已绑定其他邮箱，请联系管理员解绑后再操作")
                            existing_tg.telegram_username = username
                            existing_tg.updated_at = now
                            result = existing_tg
                        elif existing_tg.binding_status == BindingStatus.UNBOUND.value:
                            existing_tg.telegram_username = username
                            existing_tg.email = email.strip()
                            existing_tg.email_normalized = normalized
                            existing_tg.binding_status = BindingStatus.BOUND.value
                            existing_tg.updated_at = now
                            await audit(
                                session,
                                actor_telegram_id=telegram_user_id,
                                actor_type="USER",
                                action="REBIND",
                                target_type="USER",
                                target_id=str(existing_tg.id),
                                parameters_summary={"email": masked_email(email)},
                            )
                            result = existing_tg
                        else:
                            raise BindingError("账号绑定状态异常，请联系管理员")
                    else:
                        row = User(
                            telegram_user_id=telegram_user_id,
                            telegram_username=username,
                            email=email.strip(),
                            email_normalized=normalized,
                            reclaude_user_id=None,
                            binding_status=BindingStatus.BOUND.value,
                            status=UserStatus.ACTIVE.value,
                            baseline_status=BaselineStatus.UNKNOWN.value,
                            bound_at=now,
                            updated_at=now,
                        )
                        session.add(row)
                        await session.flush()
                        await audit(
                            session,
                            actor_telegram_id=telegram_user_id,
                            actor_type="USER",
                            action="BIND",
                            target_type="USER",
                            target_id=str(row.id),
                            parameters_summary={"email": masked_email(email)},
                        )
                        result = row
        except IntegrityError:
            raise BindingError("该邮箱或账号已被占用，请联系管理员") from None
        assert result is not None
        if self.onboarding is not None:
            try:
                await self.onboarding.queue_unmute_for_user(telegram_user_id)
            except Exception:
                # The binding commit remains authoritative; reconciliation will
                # repair the pending Telegram action after a restart or retry.
                pass
        return result

    async def unbind(self, telegram_user_id: int, *, operator_telegram_id: int, force_revoke: bool = False) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())
                if user is None:
                    raise BindingError("用户未绑定")
                association_id = await session.scalar(
                    select(DeviceAssociation.id).where(DeviceAssociation.user_id == user.id, DeviceAssociation.ended_at.is_(None)).limit(1)
                )
                if association_id is not None:
                    raise BindingError("用户仍有未结束的设备关联，请先完成 deauth 后再解绑")
                user.binding_status = BindingStatus.UNBOUND.value
                user.updated_at = utcnow()
                await audit(
                    session,
                    actor_telegram_id=operator_telegram_id,
                    actor_type="ADMIN",
                    action="UNBIND",
                    target_type="USER",
                    target_id=str(user.id),
                    parameters_summary={"force_revoke": force_revoke},
                )
