from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.audit import audit, utcnow
from reclaude_bot.application.task import QuotaTaskService
from reclaude_bot.domain.enums import TaskStatus
from reclaude_bot.domain.errors import EligibilityError
from reclaude_bot.domain.quota import as_decimal
from reclaude_bot.infrastructure.db.models import DeviceTaskScope, QuotaTask
from reclaude_bot.infrastructure.reclaude.client import ReclaudeGateway
from reclaude_bot.infrastructure.reclaude.models import AccountRecord, AccountsResponse, MeResponse


@dataclass(frozen=True)
class OrgAccountUsage:
    org_id: int
    account_id: str | None
    me: MeResponse


class OrgAccountSource(Protocol):
    async def get_usage(self, org_id: int) -> OrgAccountUsage: ...


def _validated_org_id(org_id: object) -> int:
    if isinstance(org_id, bool) or not isinstance(org_id, int) or org_id <= 0:
        raise EligibilityError("组织 ID 必须是正整数")
    return org_id


class SingleOrgAccountSource:
    def __init__(self, gateway: ReclaudeGateway, org_id: int) -> None:
        self.org_id = _validated_org_id(org_id)
        self.gateway = gateway

    async def get_usage(self, org_id: int) -> OrgAccountUsage:
        requested_org_id = _validated_org_id(org_id)
        if requested_org_id != self.org_id:
            raise EligibilityError("当前仅支持已配置的 Reclaude 组织")

        # The cached gateway ID is only a write-routing hint.  Discover the
        # current account from the live organization inventory instead, and
        # bracket /me so a concurrent account replacement cannot be mixed into
        # one usage snapshot.
        before = await self.gateway.accounts()
        before_id = self._bound_account_id(before)
        me = await self.gateway.me()
        after = await self.gateway.accounts()
        after_id = self._bound_account_id(after)
        if after_id != before_id:
            raise EligibilityError("查询期间 Reclaude 账号已切换，不能确认用量所属账号")
        if not isinstance(me, MeResponse):
            raise EligibilityError("Reclaude 用量响应无效")
        if me.current_account.status.strip().casefold() != "bound":
            raise EligibilityError("Reclaude 当前账号未绑定")

        return OrgAccountUsage(
            org_id=requested_org_id,
            account_id=before_id,
            me=me,
        )

    def _bound_account_id(self, accounts: AccountsResponse) -> str:
        if not isinstance(accounts, AccountsResponse):
            raise EligibilityError("Reclaude 账号响应无效")
        bound = [record for record in accounts.items if self._is_bound(record)]
        if len(bound) != 1:
            raise EligibilityError("Reclaude 组织必须恰好有一个已绑定账号")
        record = bound[0]
        account_id = self._validated_account_id(record.account_id)
        if record.org_id not in (None, ""):
            raw_org_id = record.org_id.strip() if isinstance(record.org_id, str) else record.org_id
            if isinstance(raw_org_id, bool) or not isinstance(raw_org_id, (int, str)):
                raise EligibilityError("Reclaude 账号组织不匹配")
            if isinstance(raw_org_id, str) and (not raw_org_id.isdigit() or int(raw_org_id) <= 0):
                raise EligibilityError("Reclaude 账号组织不匹配")
            if int(raw_org_id) != self.org_id:
                raise EligibilityError("Reclaude 账号组织不匹配")
        if not record.has_usable_health():
            raise EligibilityError("Reclaude 当前账号健康状态不可用")
        return str(account_id)

    def _is_bound(self, record: AccountRecord) -> bool:
        if not isinstance(record, AccountRecord):
            raise EligibilityError("Reclaude 账号响应无效")
        return (record.lifecycle or "").strip().casefold() == "bound"

    @staticmethod
    def _validated_account_id(account_id: int | str | None) -> int | str:
        if isinstance(account_id, bool) or account_id is None:
            raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")
        if isinstance(account_id, int):
            if account_id <= 0:
                raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")
            return account_id
        normalized = account_id.strip()
        if len(normalized) <= 128 and normalized.isdigit() and int(normalized) > 0:
            return normalized
        raise EligibilityError("Reclaude 已绑定账号缺少有效 account_id")


@dataclass(frozen=True)
class DeviceTaskContext:
    task_id: int
    name: str
    org_id: int
    limit_usd: Decimal
    enabled: bool


class SingleOrgTaskService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], org_id: int) -> None:
        self.session_factory = session_factory
        self.org_id = _validated_org_id(org_id)
        self.task_service = QuotaTaskService(session_factory)

    async def configure_task(self, name: str, operator_id: int) -> DeviceTaskContext:
        resolved_name = await self.task_service.resolve(name)
        normalized_name = resolved_name.strip().casefold()
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    task = await session.scalar(
                        select(QuotaTask)
                        .where(QuotaTask.name_normalized == normalized_name)
                        .with_for_update()
                    )
                    if task is None:
                        raise EligibilityError("限额任务已不存在，请重新查询任务列表")

                    scope_for_task = await session.get(DeviceTaskScope, task.id)
                    scope_for_org = await session.scalar(
                        select(DeviceTaskScope).where(DeviceTaskScope.org_id == self.org_id)
                    )
                    if scope_for_task is not None or scope_for_org is not None:
                        if (
                            scope_for_task is not None
                            and scope_for_org is not None
                            and scope_for_task.task_id == task.id
                            and scope_for_task.org_id == self.org_id
                        ):
                            return self._context(task)
                        raise EligibilityError("该任务或组织已关联到其他设备任务，不能自动更改")

                    if task.status != TaskStatus.STOPPED.value:
                        raise EligibilityError("首次配置设备组织前，限额任务必须处于 STOPPED 状态")

                    now = utcnow()
                    session.add(
                        DeviceTaskScope(
                            task_id=task.id,
                            org_id=self.org_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                    await audit(
                        session,
                        actor_telegram_id=operator_id,
                        actor_type="ADMIN",
                        action="DEVICE_TASK_CONFIGURED",
                        target_type="DEVICE_TASK_SCOPE",
                        target_id=str(task.id),
                        parameters_summary={"name": task.name, "org_id": self.org_id},
                    )
                    return self._context(task)
        except IntegrityError:
            raise EligibilityError("该组织或任务已由并发操作关联，请重新查询设备任务配置") from None

    async def resolve_task(self, name: str | None = None) -> DeviceTaskContext:
        if name is not None and name.strip():
            resolved_name = await self.task_service.resolve(name)
            async with self.session_factory() as session:
                task = await session.scalar(
                    select(QuotaTask)
                    .join(DeviceTaskScope, DeviceTaskScope.task_id == QuotaTask.id)
                    .where(
                        QuotaTask.name_normalized == resolved_name.strip().casefold(),
                        DeviceTaskScope.org_id == self.org_id,
                    )
                )
            if task is None:
                raise EligibilityError("指定任务未配置到当前 Reclaude 组织")
            return self._context(task)

        async with self.session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(QuotaTask)
                        .join(DeviceTaskScope, DeviceTaskScope.task_id == QuotaTask.id)
                        .where(DeviceTaskScope.org_id == self.org_id)
                    )
                ).all()
            )
        if not tasks:
            raise EligibilityError("当前 Reclaude 组织尚未配置设备限额任务")
        if len(tasks) != 1:
            raise EligibilityError("当前 Reclaude 组织存在多个设备限额任务配置")
        return self._context(tasks[0])

    def _context(self, task: QuotaTask) -> DeviceTaskContext:
        return DeviceTaskContext(
            task_id=task.id,
            name=task.name,
            org_id=self.org_id,
            limit_usd=as_decimal(task.limit_usd),
            enabled=task.status == TaskStatus.RUNNING.value,
        )
