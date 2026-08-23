from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.infrastructure.db.models import ServiceState
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import AccountRecord, AccountsResponse, MembersResponse, MeResponse


class RecoveryGate:
    """Internal write latch; operator lifecycle is stored by :class:`QuotaTaskService`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def ensure_disabled(self, reason: str = "startup_recovery_required") -> ServiceState:
        """Close writes on process startup while preserving a persisted task decision."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                state.write_enabled = False
                state.reason = reason
                state.updated_at = utcnow()
                await session.flush()
                return state

    async def disable(self, reason: str) -> None:
        """Close only the internal latch; callers use ``force_stop`` for safety shutdown."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                state.write_enabled = False
                state.reason = reason
                state.updated_at = utcnow()

    async def force_stop(self, reason: str) -> None:
        """Force both the durable task and internal latch stopped after a safety failure."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                state.quota_task_enabled = False
                state.write_enabled = False
                state.reason = reason
                state.updated_at = now
                state.quota_task_updated_at = now

    async def enable_latch(self, reason: str = "quota_task_resumed") -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                if not state.quota_task_enabled:
                    return False
                state.write_enabled = True
                state.reason = reason
                state.updated_at = utcnow()
                return True

    async def is_enabled(self, session: AsyncSession | None = None) -> bool:
        if session is not None:
            state = await session.get(ServiceState, 1)
            return bool(state and state.write_enabled)
        async with self.session_factory() as owned:
            state = await owned.get(ServiceState, 1)
            return bool(state and state.write_enabled)

    async def is_task_enabled(self, session: AsyncSession | None = None) -> bool:
        if session is not None:
            state = await session.get(ServiceState, 1)
            return bool(state and state.quota_task_enabled)
        async with self.session_factory() as owned:
            state = await owned.get(ServiceState, 1)
            return bool(state and state.quota_task_enabled)

    async def get_state(self) -> ServiceState | None:
        async with self.session_factory() as session:
            return await session.get(ServiceState, 1)

    async def persist_selected_account(self, account_id: int | str, operator_id: int | None = None) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                state.selected_account_id = str(account_id)
                state.write_enabled = False
                state.reason = "account_selected_task_stopped"
                state.updated_at = now
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="SELECT_ACCOUNT",
                    target_type="SERVICE",
                    target_id="1",
                    parameters_summary={"account_id": str(account_id)},
                )

    async def activate_task(self, operator_id: int | None = None) -> None:
        """Compatibility helper for callers that already completed validation."""

        async with self.session_factory() as session:
            async with session.begin():
                state = await self._ensure_state(session, with_for_update=True)
                now = utcnow()
                state.quota_task_enabled = True
                state.write_enabled = True
                state.reason = "quota_task_started"
                state.updated_at = now
                state.quota_task_updated_at = now
                state.quota_task_updated_by = operator_id
                await audit(
                    session,
                    actor_telegram_id=operator_id,
                    actor_type="ADMIN" if operator_id is not None else "SYSTEM",
                    action="QUOTA_TASK_STARTED",
                    target_type="SERVICE",
                    target_id="1",
                )

    async def activate_selected_account(self, account_id: int | str, operator_id: int) -> None:
        """Deprecated compatibility API; new flows select then explicitly start the task."""

        await self.persist_selected_account(account_id, operator_id)

    async def enable_after_reconcile(self, operator_id: int) -> None:
        """Deprecated compatibility alias for old integrations."""

        await self.activate_task(operator_id)

    async def disable_from_401(self) -> None:
        await self.force_stop("reclaude_401_recovery_required")

    @staticmethod
    async def _ensure_state(session: AsyncSession, *, with_for_update: bool = False) -> ServiceState:
        state = await session.get(ServiceState, 1, with_for_update=with_for_update)
        if state is None:
            now = utcnow()
            state = ServiceState(
                id=1,
                write_enabled=False,
                quota_task_enabled=False,
                quota_task_scope_mode="ALL",
                reason="startup_recovery_required",
                updated_at=now,
                quota_task_updated_at=now,
            )
            session.add(state)
            await session.flush()
        return state


@dataclass(frozen=True)
class AccountListing:
    me: MeResponse
    accounts: AccountsResponse
    selected_account_id: str | None


class RecoveryService:
    def __init__(self, gate: RecoveryGate, quota: QuotaService, gateway: ReclaudeGateway, settings: Settings) -> None:
        self.gate = gate
        self.quota = quota
        self.gateway = gateway
        self.settings = settings

    async def list_accounts(self) -> AccountListing:
        """Authenticate and return the live account inventory without opening writes."""

        me = await self.gateway.authenticate()
        if not isinstance(me, MeResponse):
            raise EligibilityError("Reclaude 登录验证响应无效")
        accounts = await self._accounts()
        state = await self.gate.get_state()
        selected_account_id = state.selected_account_id if state is not None else None
        return AccountListing(me=me, accounts=accounts, selected_account_id=selected_account_id)

    async def select_account(self, account_id: int | str, operator_id: int) -> AccountRecord:
        """Validate, reconcile, and persist an account without enabling quota writes."""

        await self.gate.force_stop("reclaude_account_selection_in_progress")
        self.gateway.account_id = None
        try:
            requested_id = self._validated_account_id(account_id)
            me = await self.gateway.authenticate()
            if not isinstance(me, MeResponse):
                raise EligibilityError("Reclaude 登录验证响应无效")
            if me.current_account.status != "bound":
                raise EligibilityError(f"Reclaude 当前账号状态异常：{me.current_account.status}")
            accounts = await self._accounts()
            account = self._matching_account(accounts, requested_id)
            me.weekly_all()
            selected_id = self._validated_account_id(account.account_id)
            self.gateway.configure_account_id(selected_id)
            members = await self.gateway.members()
            if not isinstance(members, MembersResponse):
                raise EligibilityError("Reclaude 成员响应无效")
            await self.quota.sync_cycle_from_me(me=me)
            await self.quota.sync_members(members=members)
            await self.gate.persist_selected_account(selected_id, operator_id)
            return account
        except Exception as exc:
            self.gateway.account_id = None
            reason = "reclaude_account_auth_failed" if isinstance(exc, (ConnectionError, TimeoutError)) else "reclaude_account_selection_failed"
            try:
                await self.gate.force_stop(reason)
            except Exception:
                pass
            raise

    async def validate_selected_account(self) -> AccountRecord:
        """Validate the persisted account before a task start or persisted-task resume."""
        try:
            # Close the internal latch before clearing the in-memory account ID. A
            # running loop may be between ticks while this validation is in flight.
            await self.gate.disable("quota_task_start_validation")
            state = await self.gate.get_state()
            if state is None or state.selected_account_id is None:
                raise EligibilityError("尚未选择 Reclaude 账号，请先使用 /use account_id")
            self.gateway.account_id = None
            me = await self.gateway.authenticate()
            if not isinstance(me, MeResponse):
                raise EligibilityError("Reclaude 登录验证响应无效")
            if me.current_account.status != "bound":
                raise EligibilityError(f"Reclaude 当前账号状态异常：{me.current_account.status}")
            me.weekly_all()
            account = self._matching_account(await self._accounts(), state.selected_account_id)
            self.gateway.configure_account_id(self._validated_account_id(account.account_id))
            return account
        except Exception:
            self.gateway.account_id = None
            try:
                # A failed start validation must close a previously running task as
                # well as its write latch; the selected account remains persisted.
                await self.gate.force_stop("quota_task_start_validation_failed")
            except Exception:
                pass
            raise

    async def restore_persisted_account(self, state: ServiceState | None = None) -> int | str | None:
        """Restore only the configured ID; live validation remains an explicit startup step."""

        self.gateway.account_id = None
        state = state or await self.gate.get_state()
        if state is None or state.selected_account_id is None:
            return None
        try:
            account_id = self._validated_account_id(state.selected_account_id)
        except EligibilityError:
            return None
        self.gateway.configure_account_id(account_id)
        return account_id

    async def health_sync_reconcile_enable(self, operator_id: int) -> None:
        """Legacy recovery flow: reconcile and select, but never independently enable writes."""

        await self.gate.force_stop("reclaude_recovery_in_progress")
        self.gateway.account_id = None
        try:
            me = await self.gateway.authenticate()
            if not isinstance(me, MeResponse):
                raise EligibilityError("Reclaude 登录验证响应无效")
            if me.current_account.status != "bound":
                raise EligibilityError("Reclaude 当前账号未绑定")
            me.weekly_all()
            accounts = await self._accounts()
            bound_accounts = [record for record in accounts.items if (record.lifecycle or "").casefold() == "bound"]
            if len(bound_accounts) == 0:
                raise EligibilityError("Reclaude 没有可用的已绑定账号")
            if len(bound_accounts) > 1:
                raise EligibilityError("Reclaude 已绑定账号不唯一")
            account = bound_accounts[0]
            if not account.has_usable_health():
                raise EligibilityError("Reclaude 已绑定账号健康状态不可用")
            account_id = self._validated_account_id(account.account_id)
            self.gateway.configure_account_id(account_id)
            members = await self.gateway.members()
            if not isinstance(members, MembersResponse):
                raise EligibilityError("Reclaude 成员响应无效")
            await self.quota.sync_cycle_from_me(me=me)
            await self.quota.sync_members(members=members)
            await self.gate.persist_selected_account(account_id, operator_id)
        except Exception as exc:
            self.gateway.account_id = None
            reason = "reclaude_recovery_auth_failed" if isinstance(exc, (ConnectionError, TimeoutError)) else "reclaude_recovery_failed"
            try:
                await self.gate.force_stop(reason)
            except Exception:
                pass
            raise

    async def _accounts(self) -> AccountsResponse:
        try:
            accounts = await self.gateway.accounts()
        except (TypeError, ValueError) as exc:
            raise EligibilityError("Reclaude 账号响应无效") from exc
        if not isinstance(accounts, AccountsResponse):
            raise EligibilityError("Reclaude 账号响应无效")
        return accounts

    @staticmethod
    def _matching_account(accounts: AccountsResponse, account_id: int | str) -> AccountRecord:
        matching = [record for record in accounts.items if RecoveryService._same_account_id(record.account_id, account_id)]
        if not matching:
            raise EligibilityError(f"Reclaude 账号 {account_id} 不存在，请先使用 /account 查看实时账号")
        if len(matching) > 1:
            raise EligibilityError(f"Reclaude 账号 {account_id} 记录不唯一")
        account = matching[0]
        if (account.lifecycle or "").strip().casefold() != "bound":
            raise EligibilityError(f"Reclaude 账号 {account_id} 未绑定（lifecycle={account.lifecycle or 'unknown'}）")
        if not account.has_usable_health():
            raise EligibilityError(f"Reclaude 账号 {account_id} 健康状态不可用（health={account.health or 'unknown'}）")
        return account

    @staticmethod
    def _same_account_id(left: int | str | None, right: int | str) -> bool:
        return left is not None and str(left).strip() == str(right).strip()

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
