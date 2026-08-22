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
from reclaude_bot.infrastructure.db.models import CycleBaseline, QuotaCycle, UpstreamMember, User
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

    async def bind(self, telegram_user_id: int, email: str, *, private_chat: bool = True) -> User:
        if not private_chat:
            raise BindingError("绑定只能在 Bot 私聊中执行")
        now = utcnow()
        self._check_rate(telegram_user_id, now)
        normalized = normalize_email(email)
        if "@" not in normalized or len(normalized) > 320:
            raise BindingError("邮箱格式无效")
        result: User | None = None
        async with self.session_factory() as session:
            async with session.begin():
                member = await session.scalar(select(UpstreamMember).where(UpstreamMember.email_normalized == normalized))
                if member is None:
                    raise BindingError("等待首次成员同步，请稍后重试")
                existing_tg = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())
                existing_email = await session.scalar(select(User).where(User.email_normalized == normalized).with_for_update())
                existing_reclaude = await session.scalar(select(User).where(User.reclaude_user_id == member.reclaude_user_id).with_for_update())
                existing = existing_tg or existing_email or existing_reclaude
                if existing:
                    if existing_tg is not None and existing_tg.binding_status == BindingStatus.UNBOUND.value:
                        existing_tg.email = email.strip()
                        existing_tg.email_normalized = normalized
                        existing_tg.reclaude_user_id = member.reclaude_user_id
                        existing_tg.binding_status = BindingStatus.BOUND.value
                        existing_tg.status = UserStatus.ACTIVE.value
                        existing_tg.updated_at = now
                        cycle = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at > now).order_by(QuotaCycle.reset_at.asc()))
                        if cycle is not None:
                            baseline = await session.scalar(
                                select(CycleBaseline).where(CycleBaseline.reclaude_user_id == member.reclaude_user_id, CycleBaseline.cycle_id == cycle.id)
                            )
                            if baseline is not None:
                                baseline.user_id = existing_tg.id
                                existing_tg.baseline_status = baseline.status
                        await audit(
                            session,
                            actor_telegram_id=telegram_user_id,
                            actor_type="USER",
                            action="REBIND",
                            target_type="USER",
                            target_id=str(existing_tg.id),
                            parameters_summary={"email": masked_email(email), "reclaude_user_id": member.reclaude_user_id},
                        )
                        result = existing_tg
                    elif existing.telegram_user_id == telegram_user_id and existing.email_normalized == normalized and existing.reclaude_user_id == member.reclaude_user_id:
                        result = existing
                    else:
                        raise BindingError("该绑定已被占用，请联系管理员")
                else:
                    row = User(
                        telegram_user_id=telegram_user_id,
                        email=email.strip(),
                        email_normalized=normalized,
                        reclaude_user_id=member.reclaude_user_id,
                        binding_status=BindingStatus.BOUND.value,
                        status=UserStatus.ACTIVE.value,
                        baseline_status=BaselineStatus.UNKNOWN.value,
                        bound_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        raise BindingError("该绑定已被占用，请联系管理员") from exc
                    cycle = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at > now).order_by(QuotaCycle.reset_at.asc()))
                    if cycle is not None:
                        baseline = await session.scalar(
                            select(CycleBaseline).where(CycleBaseline.reclaude_user_id == member.reclaude_user_id, CycleBaseline.cycle_id == cycle.id)
                        )
                        if baseline is not None:
                            baseline.user_id = row.id
                            row.baseline_status = baseline.status
                    await audit(
                        session,
                        actor_telegram_id=telegram_user_id,
                        actor_type="USER",
                        action="BIND",
                        target_type="USER",
                        target_id=str(row.id),
                        parameters_summary={"email": masked_email(email), "reclaude_user_id": member.reclaude_user_id},
                    )
                    result = row
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
            user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
            if user is None:
                raise BindingError("用户未绑定")
            member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == user.reclaude_user_id))
            assigned = member is not None and member.account_id is not None
            user_id = user.reclaude_user_id
        if assigned:
            if not force_revoke:
                raise BindingError("用户当前有上游分配，需选择撤销并解绑")
            if self.gate is not None and not await self.gate.is_enabled():
                raise BindingError("上游写操作已禁用，请先完成恢复核验")
            if self.gateway is None:
                raise BindingError("上游客户端不可用")
            await self.gateway.revoke(user_id)
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())
                if user is None:
                    raise BindingError("用户未绑定")
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
