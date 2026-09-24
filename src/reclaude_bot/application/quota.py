from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.config import Settings
from reclaude_bot.domain.enums import BaselineStatus, CycleStatus, UserStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import as_decimal, baseline_is_timely, cycle_used, ensure_utc, estimate_window_total, is_last_24h
from reclaude_bot.infrastructure.db.models import CycleBaseline, QuotaAdjustment, QuotaCycle, RuntimeSetting, UpstreamMember, User
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import MembersResponse, MeResponse

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AccountUsageSnapshot:
    """Account-level usage windows from a live /me read.

    The weekly total is estimated from the locally cached cycle spend divided by the
    reported utilization percent; the 5-hour window has no local spend data to estimate from.
    """

    email_masked: str
    usage_updated_at: datetime
    five_hour_utilization: Decimal | None
    five_hour_resets_at: datetime | None
    seven_day_utilization: Decimal
    seven_day_resets_at: datetime | None
    seven_day_estimated_total: Decimal | None


def normalize_email(email: str) -> str:
    return email.strip().casefold()


class QuotaService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], gateway: ReclaudeGateway, settings: Settings) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.settings = settings
        self._sync_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()

    async def _runtime_limit(self, session: AsyncSession, *, create: bool = True) -> Decimal:
        setting = await session.get(RuntimeSetting, 1)
        if setting is None:
            value = as_decimal(self.settings.quota_limit_usd)
            if create:
                setting = RuntimeSetting(id=1, quota_limit_usd=value, updated_at=utcnow())
                session.add(setting)
                await session.flush()
            else:
                return value
        return as_decimal(setting.quota_limit_usd)

    async def get_limit(self) -> Decimal:
        async with self.session_factory() as session:
            async with session.begin():
                return await self._runtime_limit(session)

    async def current_cycle(self, session: AsyncSession, now: datetime | None = None) -> QuotaCycle | None:
        moment = ensure_utc(now or utcnow())
        return await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at > moment).order_by(QuotaCycle.reset_at.asc()))

    async def current_cycle_from_now(self, now: datetime | None = None) -> QuotaCycle | None:
        async with self.session_factory() as session:
            return await self.current_cycle(session, now)

    async def sync_cycle_from_me(self, *, now: datetime | None = None, me: MeResponse | None = None) -> QuotaCycle:
        moment = ensure_utc(now or utcnow())
        me = me or await self.gateway.me()
        weekly = me.weekly_all()
        if weekly.resets_at is None:
            raise EligibilityError("Reclaude weekly_all 周期缺少刷新时间")
        reset_at = ensure_utc(weekly.resets_at)
        account_ok = me.current_account.status == "bound"
        status = CycleStatus.VERIFIED.value if account_ok else CycleStatus.NEEDS_REVIEW.value
        async with self._cycle_lock:
            async with self.session_factory() as session:
                async with session.begin():
                    cycle = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at == reset_at).with_for_update())
                    if cycle is None:
                        previous = await session.scalar(select(QuotaCycle).where(QuotaCycle.reset_at < reset_at).order_by(desc(QuotaCycle.reset_at)))
                        if previous is not None and previous.status != CycleStatus.EXPIRED.value:
                            previous.status = CycleStatus.EXPIRED.value
                        cycle = QuotaCycle(
                            started_at=previous.reset_at if previous else reset_at - timedelta(days=7),
                            reset_at=reset_at,
                            weekly_percent=None,
                            source_account_id=self._source_account_id(),
                            source_account_email_masked=me.current_account.email_masked,
                            status=status,
                            created_at=moment,
                        )
                        session.add(cycle)
                    else:
                        cycle.status = status
                        cycle.source_account_id = self._source_account_id()
                        cycle.source_account_email_masked = me.current_account.email_masked
                    await session.flush()
                    return cycle

    def _source_account_id(self) -> int | None:
        account_id = self.gateway.account_id
        if account_id is None:
            return None
        try:
            return int(account_id)
        except (TypeError, ValueError):
            return None

    async def ensure_cycle(self, *, now: datetime | None = None) -> QuotaCycle:
        moment = ensure_utc(now or utcnow())
        cycle = await self.current_cycle_from_now(moment)
        if cycle is not None:
            return cycle
        return await self.sync_cycle_from_me(now=moment)

    async def maybe_check_last_day(self, *, now: datetime | None = None) -> QuotaCycle | None:
        moment = ensure_utc(now or utcnow())
        async with self._cycle_lock:
            async with self.session_factory() as session:
                cycle = await self.current_cycle(session, moment)
                if cycle is None or not is_last_24h(moment, cycle.reset_at) or cycle.last_day_checked_at is not None:
                    return cycle
                cycle_id = cycle.id
                reset_at = cycle.reset_at
            me = await self.gateway.me()
            try:
                weekly = me.weekly_all()
                valid = weekly.resets_at is not None and ensure_utc(weekly.resets_at) == ensure_utc(reset_at)
                percent = as_decimal(weekly.percent)
            except (ValueError, TypeError):
                valid = False
                percent = None
            account_ok = me.current_account.status == "bound"
            async with self.session_factory() as session:
                async with session.begin():
                    cycle = await session.get(QuotaCycle, cycle_id, with_for_update=True)
                    if cycle is None:
                        return None
                    cycle.last_day_checked_at = moment
                    cycle.weekly_percent = percent
                    cycle.last_day_allow = bool(valid and account_ok and percent is not None and percent < Decimal("100"))
                    if not valid or not account_ok:
                        cycle.status = CycleStatus.NEEDS_REVIEW.value
                    return cycle

    async def sync_members(self, *, now: datetime | None = None, members: MembersResponse | None = None) -> int:
        moment = ensure_utc(now or utcnow())
        async with self._sync_lock:
            members = members or await self.gateway.members()
            async with self.session_factory() as session:
                cycle = await self.current_cycle(session, moment)
            if cycle is None:
                raise EligibilityError("当前周期尚未初始化")
            async with self.session_factory() as session:
                async with session.begin():
                    for member in members.items:
                        reclaude_user_id = str(member.user_id)
                        normalized = normalize_email(member.email)
                        cache = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == reclaude_user_id).with_for_update())
                        previous_total = cache.total_usage_usd if cache is not None else None
                        if cache is None:
                            cache = UpstreamMember(
                                member_record_id=str(member.id) if member.id is not None else None,
                                reclaude_user_id=reclaude_user_id,
                                email=member.email,
                                email_normalized=normalized,
                                account_id=str(member.account_id) if member.account_id is not None else None,
                                total_usage_usd=as_decimal(member.total_usage_usd),
                                sampled_at=moment,
                            )
                            session.add(cache)
                            await session.flush()
                        else:
                            cache.member_record_id = str(member.id) if member.id is not None else None
                            cache.email = member.email
                            cache.email_normalized = normalized
                            cache.account_id = str(member.account_id) if member.account_id is not None else None
                            cache.total_usage_usd = as_decimal(member.total_usage_usd)
                            cache.sampled_at = moment
                        user = await session.scalar(select(User).where(User.reclaude_user_id == reclaude_user_id).with_for_update())
                        baseline = await session.scalar(
                            select(CycleBaseline).where(CycleBaseline.reclaude_user_id == reclaude_user_id, CycleBaseline.cycle_id == cycle.id).with_for_update()
                        )
                        decreased = previous_total is not None and as_decimal(member.total_usage_usd) < previous_total
                        timely = baseline_is_timely(moment, cycle.started_at, timedelta(seconds=self.settings.baseline_capture_window_seconds))
                        if baseline is None:
                            baseline = CycleBaseline(
                                reclaude_user_id=reclaude_user_id,
                                user_id=user.id if user else None,
                                cycle_id=cycle.id,
                                baseline_total_usd=as_decimal(member.total_usage_usd),
                                baseline_captured_at=moment if timely else None,
                                status=BaselineStatus.NEEDS_REVIEW.value if decreased else (BaselineStatus.VERIFIED.value if timely else BaselineStatus.UNKNOWN.value),
                                source="cycle_boundary" if timely else "first_observation",
                            )
                            session.add(baseline)
                        else:
                            if user is not None and baseline.user_id != user.id:
                                baseline.user_id = user.id
                            if decreased:
                                baseline.status = BaselineStatus.NEEDS_REVIEW.value
                                baseline.source = "upstream_total_decreased"
                            elif baseline.status == BaselineStatus.UNKNOWN.value and timely:
                                baseline.baseline_total_usd = as_decimal(member.total_usage_usd)
                                baseline.baseline_captured_at = moment
                                baseline.status = BaselineStatus.VERIFIED.value
                                baseline.source = "cycle_boundary_recovery"
                        if user is not None:
                            user.baseline_status = baseline.status
                            user.updated_at = moment
            return len(members.items)

    async def list_upstream_members(self) -> list[UpstreamMember]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(UpstreamMember).order_by(UpstreamMember.email_normalized))).all())

    async def set_quota(self, amount: Decimal, operator_id: int) -> Decimal:
        amount = as_decimal(amount)
        if amount < 0:
            raise EligibilityError("额度不能为负数")
        async with self.session_factory() as session:
            async with session.begin():
                setting = await session.get(RuntimeSetting, 1, with_for_update=True)
                old = as_decimal(setting.quota_limit_usd) if setting else as_decimal(self.settings.quota_limit_usd)
                if setting is None:
                    setting = RuntimeSetting(id=1, quota_limit_usd=amount, updated_by=operator_id, updated_at=utcnow())
                    session.add(setting)
                else:
                    setting.quota_limit_usd = amount
                    setting.updated_by = operator_id
                    setting.updated_at = utcnow()
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="SET_QUOTA",
                    target_type="RUNTIME_SETTING",
                    target_id="1",
                    parameters_summary={"old_limit_usd": str(old), "new_limit_usd": str(amount)},
                )
        return amount

    async def add_adjustment(self, user_id: int, cycle_id: int, amount: Decimal, reason: str, operator_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                baseline = await session.scalar(select(CycleBaseline).where(CycleBaseline.user_id == user_id, CycleBaseline.cycle_id == cycle_id).with_for_update())
                if baseline is None:
                    raise EligibilityError("用户没有该周期基线")
                session.add(
                    QuotaAdjustment(
                        user_id=user_id,
                        cycle_id=cycle_id,
                        amount_usd=as_decimal(amount),
                        reason=reason,
                        operator_telegram_id=operator_id,
                        created_at=utcnow(),
                    )
                )
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN",
                    action="QUOTA_ADJUSTMENT",
                    target_type="USER",
                    target_id=str(user_id),
                    parameters_summary={"amount_usd": str(amount), "reason": reason},
                )

    async def record_username(self, telegram_user_id: int, username: str | None) -> None:
        """Refresh the cached Telegram username used to resolve group mentions in /send."""
        if not username:
            return
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id).with_for_update())
                if user is not None and user.telegram_username != username.casefold():
                    user.telegram_username = username.casefold()
                    user.updated_at = utcnow()

    async def transfer_quota(
        self,
        sender_telegram_id: int,
        *,
        amount: Decimal,
        recipient_telegram_id: int | None = None,
        recipient_username: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Move part of the sender's own current-cycle quota to another bound user.

        Both sides land as QuotaAdjustment rows in the current cycle, so /status,
        /taskusers, and the enforcement loop all see the transfer unchanged.
        """
        moment = ensure_utc(now or utcnow())
        amount = as_decimal(amount)
        if not amount.is_finite() or amount <= 0:
            raise EligibilityError("转账金额必须大于 0")
        async with self.session_factory() as session:
            async with session.begin():
                sender = await session.scalar(select(User).where(User.telegram_user_id == sender_telegram_id).with_for_update())
                if sender is None or sender.binding_status != "BOUND":
                    raise EligibilityError("您尚未绑定")
                if sender.status != UserStatus.ACTIVE.value:
                    raise EligibilityError("您的账号已被禁用")
                recipient: User | None = None
                if recipient_telegram_id is not None:
                    recipient = await session.scalar(select(User).where(User.telegram_user_id == recipient_telegram_id).with_for_update())
                elif recipient_username:
                    recipient = await session.scalar(
                        select(User)
                        .where(User.telegram_username == recipient_username.casefold())
                        .order_by(desc(User.updated_at))
                        .limit(1)
                        .with_for_update()
                    )
                if recipient is None or recipient.binding_status != "BOUND":
                    raise EligibilityError("对方尚未绑定，无法转账；若对方刚改过用户名，请让其在群内先使用一次 /status")
                if recipient.id == sender.id:
                    raise EligibilityError("不能转账给自己")
                if recipient.status != UserStatus.ACTIVE.value:
                    raise EligibilityError("对方账号已被禁用")
                cycle = await self.current_cycle(session, moment)
                if cycle is None:
                    raise EligibilityError("当前周期不可用")
                baselines = {
                    baseline.reclaude_user_id: baseline
                    for baseline in (
                        await session.scalars(
                            select(CycleBaseline).where(
                                CycleBaseline.cycle_id == cycle.id,
                                CycleBaseline.reclaude_user_id.in_([sender.reclaude_user_id, recipient.reclaude_user_id]),
                            ).with_for_update()
                        )
                    ).all()
                }
                sender_baseline = baselines.get(sender.reclaude_user_id)
                if sender_baseline is None:
                    raise EligibilityError("您当前周期基线尚未建立")
                if recipient.reclaude_user_id not in baselines:
                    raise EligibilityError("对方当前周期基线尚未建立")
                member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == sender.reclaude_user_id))
                if member is None:
                    raise EligibilityError("等待首次成员同步")
                adjustments = (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.user_id == sender.id, QuotaAdjustment.cycle_id == cycle.id))).all()
                used = cycle_used(member.total_usage_usd, sender_baseline.baseline_total_usd, [item.amount_usd for item in adjustments])
                limit = await self._runtime_limit(session, create=False)
                remaining = max(Decimal("0"), limit - used)
                if amount > remaining:
                    raise EligibilityError(f"剩余额度不足：当前剩余 ${remaining:.2f}")
                session.add(
                    QuotaAdjustment(
                        user_id=sender.id,
                        cycle_id=cycle.id,
                        amount_usd=amount,
                        reason=f"TRANSFER_OUT user:{recipient.id}",
                        operator_telegram_id=sender_telegram_id,
                        created_at=moment,
                    )
                )
                session.add(
                    QuotaAdjustment(
                        user_id=recipient.id,
                        cycle_id=cycle.id,
                        amount_usd=-amount,
                        reason=f"TRANSFER_IN user:{sender.id}",
                        operator_telegram_id=sender_telegram_id,
                        created_at=moment,
                    )
                )
                await audit(
                    session,
                    actor_telegram_id=sender_telegram_id,
                    actor_type="USER",
                    action="QUOTA_TRANSFER",
                    target_type="USER",
                    target_id=str(recipient.id),
                    parameters_summary={"sender_user_id": sender.id, "amount_usd": str(amount)},
                )
                return {
                    "amount_usd": amount,
                    "recipient_email": recipient.email,
                    "sender_remaining_usd": remaining - amount,
                }

    async def get_status(self, telegram_user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        moment = ensure_utc(now or utcnow())
        async with self.session_factory() as session:
            user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
            if user is None or user.binding_status != "BOUND":
                raise EligibilityError("用户未绑定")
            cycle = await self.current_cycle(session, moment)
            if cycle is None:
                raise EligibilityError("当前周期不可用")
            member = await session.scalar(select(UpstreamMember).where(UpstreamMember.reclaude_user_id == user.reclaude_user_id))
            if member is None:
                raise EligibilityError("等待首次成员同步")
            baseline = await session.scalar(select(CycleBaseline).where(CycleBaseline.reclaude_user_id == user.reclaude_user_id, CycleBaseline.cycle_id == cycle.id))
            if baseline is None:
                raise EligibilityError("当前周期基线尚未建立")
            adjustments = (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.user_id == user.id, QuotaAdjustment.cycle_id == cycle.id))).all()
            used = cycle_used(member.total_usage_usd, baseline.baseline_total_usd, [item.amount_usd for item in adjustments])
            limit = await self._runtime_limit(session, create=False)
            return {
                "email": user.email,
                "used_usd": used,
                "limit_usd": limit,
                "remaining_usd": max(Decimal("0"), limit - used),
                "reset_at": cycle.reset_at,
                "last_24h": is_last_24h(moment, cycle.reset_at),
                "allocation_status": "ASSIGNED" if member.account_id is not None else "UNASSIGNED",
                "user_status": user.status,
                "baseline_status": baseline.status,
                "cycle_status": cycle.status,
                "sampled_at": member.sampled_at,
            }

    async def get_account_usage(self, *, now: datetime | None = None) -> AccountUsageSnapshot:
        """Live account-level usage windows; calls the upstream /me endpoint on every invocation."""
        moment = ensure_utc(now or utcnow())
        me = await self.gateway.me()
        snapshot = me.current_account.usage_snapshot
        five_hour = snapshot.five_hour
        seven_day_used = await self._account_cycle_spend(moment)
        return AccountUsageSnapshot(
            email_masked=me.current_account.email_masked,
            usage_updated_at=me.current_account.usage_updated_at,
            five_hour_utilization=five_hour.utilization if five_hour is not None else None,
            five_hour_resets_at=five_hour.resets_at if five_hour is not None else None,
            seven_day_utilization=snapshot.seven_day.utilization,
            seven_day_resets_at=snapshot.seven_day.resets_at,
            seven_day_estimated_total=estimate_window_total(seven_day_used, snapshot.seven_day.utilization),
        )

    async def _account_cycle_spend(self, now: datetime) -> Decimal:
        """Account-wide current-cycle spend summed from the local cache; zero without a cycle."""
        async with self.session_factory() as session:
            cycle = await self.current_cycle(session, now)
            if cycle is None:
                return Decimal("0")
            members = list((await session.scalars(select(UpstreamMember))).all())
            baselines = {
                baseline.reclaude_user_id: baseline
                for baseline in (await session.scalars(select(CycleBaseline).where(CycleBaseline.cycle_id == cycle.id))).all()
            }
            users = {user.reclaude_user_id: user for user in (await session.scalars(select(User))).all()}
            adjustments: dict[int, list[Decimal]] = {}
            for adjustment in (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.cycle_id == cycle.id))).all():
                adjustments.setdefault(adjustment.user_id, []).append(adjustment.amount_usd)
        total = Decimal("0")
        for member in members:
            baseline = baselines.get(member.reclaude_user_id)
            if baseline is None:
                continue
            user = users.get(member.reclaude_user_id)
            total += cycle_used(
                member.total_usage_usd,
                baseline.baseline_total_usd,
                [amount for amount in adjustments.get(user.id, [])] if user is not None else [],
            )
        return total

    async def list_task_usage(self, *, scope_mode: str, member_ids: tuple[str, ...], limit_usd: Decimal, now: datetime | None = None) -> dict[str, Any]:
        """Cache-only usage rows for one task's member scope; never calls the upstream API."""
        moment = ensure_utc(now or utcnow())
        limit = as_decimal(limit_usd)
        async with self.session_factory() as session:
            cycle = await self.current_cycle(session, moment)
            if cycle is None:
                raise EligibilityError("当前周期不可用")
            missing: list[str] = []
            if scope_mode == "ALLOWLIST":
                configured = list(dict.fromkeys(member_ids))
                if configured:
                    members = {
                        member.reclaude_user_id: member
                        for member in (await session.scalars(select(UpstreamMember).where(UpstreamMember.reclaude_user_id.in_(configured)))).all()
                    }
                else:
                    members = {}
                missing = [value for value in configured if value not in members]
            else:
                members = {member.reclaude_user_id: member for member in (await session.scalars(select(UpstreamMember))).all()}
                if scope_mode == "EXCLUDE":
                    excluded = set(member_ids)
                    members = {key: value for key, value in members.items() if key not in excluded}
            baselines = (
                {
                    baseline.reclaude_user_id: baseline
                    for baseline in (
                        await session.scalars(select(CycleBaseline).where(CycleBaseline.cycle_id == cycle.id, CycleBaseline.reclaude_user_id.in_(list(members))))
                    ).all()
                }
                if members
                else {}
            )
            users = (
                {user.reclaude_user_id: user for user in (await session.scalars(select(User).where(User.reclaude_user_id.in_(list(members))))).all()}
                if members
                else {}
            )
            adjustments: dict[int, list[Decimal]] = {}
            for adjustment in (await session.scalars(select(QuotaAdjustment).where(QuotaAdjustment.cycle_id == cycle.id))).all():
                adjustments.setdefault(adjustment.user_id, []).append(adjustment.amount_usd)
        entries: list[dict[str, Any]] = []
        for reclaude_user_id, member in members.items():
            user = users.get(reclaude_user_id)
            baseline = baselines.get(reclaude_user_id)
            used: Decimal | None = None
            remaining: Decimal | None = None
            if baseline is not None:
                used = cycle_used(
                    member.total_usage_usd,
                    baseline.baseline_total_usd,
                    [amount for amount in adjustments.get(user.id, [])] if user is not None else [],
                )
                remaining = max(Decimal("0"), limit - used)
            entries.append(
                {
                    "reclaude_user_id": reclaude_user_id,
                    "email": member.email,
                    "telegram_user_id": user.telegram_user_id if user is not None else None,
                    "user_status": user.status if user is not None else None,
                    "used_usd": used,
                    "remaining_usd": remaining,
                    "missing_upstream": False,
                }
            )
        for value in missing:
            entries.append(
                {
                    "reclaude_user_id": value,
                    "email": None,
                    "telegram_user_id": None,
                    "user_status": None,
                    "used_usd": None,
                    "remaining_usd": None,
                    "missing_upstream": True,
                }
            )
        entries.sort(key=lambda entry: (entry["missing_upstream"], entry["used_usd"] is None, -(entry["used_usd"] or Decimal("0"))))
        return {"limit_usd": limit, "reset_at": cycle.reset_at, "members": entries}
