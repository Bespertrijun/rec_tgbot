from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import ServiceState
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import AccountRecord, AccountsResponse, MeResponse


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
        await self.gate.disable("reclaude_recovery_in_progress")
        # A previous successful run must not leave a stale account usable during this run.
        self.gateway.account_id = None
        try:
            me = await self.gateway.authenticate()
            if not isinstance(me, MeResponse):
                raise EligibilityError("Reclaude 登录验证响应无效")
            if me.current_account.status != "bound":
                raise EligibilityError("Reclaude 当前账号未绑定")
            # Validate the selected cycle before touching account configuration.
            me.weekly_all()

            try:
                accounts = await self.gateway.accounts()
            except (TypeError, ValueError) as exc:
                raise EligibilityError("Reclaude 账号响应无效") from exc
            if not isinstance(accounts, AccountsResponse):
                raise EligibilityError("Reclaude 账号响应无效")
            bound_accounts = [record for record in accounts.items if record.lifecycle == "bound"]
            if len(bound_accounts) == 0:
                raise EligibilityError("Reclaude 没有可用的已绑定账号")
            if len(bound_accounts) > 1:
                raise EligibilityError("Reclaude 已绑定账号不唯一")
            account = bound_accounts[0]
            if not isinstance(account, AccountRecord) or not account.has_usable_health():
                raise EligibilityError("Reclaude 已绑定账号健康状态不可用")
            account_id = self._validated_account_id(account.account_id)

            self.gateway.configure_account_id(account_id)
            members = await self.gateway.members()
            await self.quota.sync_cycle_from_me(me=me)
            await self.quota.sync_members(members=members)
            await self.gate.enable_after_reconcile(operator_id)
        except Exception as exc:
            reason = "reclaude_recovery_auth_failed" if isinstance(exc, (ConnectionError, TimeoutError)) else "reclaude_recovery_failed"
            try:
                await self.gate.disable(reason)
            except Exception:
                # The entry transition already disabled the durable gate; retain the original failure.
                pass
            raise

    @staticmethod
    def _validated_account_id(account_id: int | str | None) -> int | str:
        if isinstance(account_id, bool) or account_id is None:
            raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")
        if isinstance(account_id, int):
            if account_id <= 0:
                raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")
            return account_id
        if account_id.strip().isdigit() and int(account_id.strip()) > 0:
            return account_id
        raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")
