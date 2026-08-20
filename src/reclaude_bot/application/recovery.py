from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import ServiceState
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway


class RecoveryGate:
    """Durable write gate. A process restart never implicitly enables upstream writes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def ensure_disabled(self, reason: str = "startup_recovery_required") -> ServiceState:
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(ServiceState, 1, with_for_update=True)
                if state is None:
                    state = ServiceState(id=1, write_enabled=False, reason=reason, updated_at=utcnow())
                    session.add(state)
                else:
                    state.write_enabled = False
                    state.reason = reason
                    state.updated_at = utcnow()
                await session.flush()
                return state

    async def disable(self, reason: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(ServiceState, 1, with_for_update=True)
                if state is None:
                    state = ServiceState(id=1, write_enabled=False, reason=reason, updated_at=utcnow())
                    session.add(state)
                else:
                    state.write_enabled = False
                    state.reason = reason
                    state.updated_at = utcnow()

    async def is_enabled(self, session: AsyncSession | None = None) -> bool:
        if session is not None:
            state = await session.get(ServiceState, 1)
            return bool(state and state.write_enabled)
        async with self.session_factory() as owned:
            state = await owned.get(ServiceState, 1)
            return bool(state and state.write_enabled)

    async def enable_after_reconcile(self, operator_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await session.get(ServiceState, 1, with_for_update=True)
                if state is None:
                    raise EligibilityError("恢复门禁尚未初始化")
                state.write_enabled = True
                state.reason = "operator_enabled_after_reconcile"
                state.updated_at = utcnow()
                await audit(session, actor_telegram_id=operator_id, actor_type="ADMIN", action="ENABLE_WRITES", target_type="SERVICE", target_id="1")

    async def disable_from_401(self) -> None:
        await self.disable("reclaude_401_recovery_required")


class RecoveryService:
    def __init__(self, gate: RecoveryGate, quota: QuotaService, gateway: ReclaudeGateway, settings: Settings) -> None:
        self.gate = gate
        self.quota = quota
        self.gateway = gateway
        self.settings = settings

    async def health_sync_reconcile_enable(self, operator_id: int) -> None:
        me = await self.gateway.me()
        accounts = await self.gateway.accounts()
        members = await self.gateway.members()
        if len(accounts) != 1:
            raise EligibilityError("Reclaude 账号数量必须恰好为一个")
        account = accounts[0]
        account_id = account.get("id")
        if account_id is None or (isinstance(account_id, str) and not account_id.strip()):
            raise EligibilityError("Reclaude 唯一账号缺少有效 ID")
        if account.get("email_masked") != self.settings.reclaude_account_email_masked:
            raise EligibilityError("账号邮箱掩码不匹配")
        if me.current_account.status != "bound":
            raise EligibilityError("Reclaude 当前账号未绑定")
        if me.current_account.email_masked != self.settings.reclaude_account_email_masked:
            raise EligibilityError("当前账号邮箱掩码不匹配")
        self.gateway.configure_account_id(account_id)
        await self.quota.sync_cycle_from_me(me=me)
        await self.quota.sync_members(members=members)
        await self.gate.enable_after_reconcile(operator_id)
