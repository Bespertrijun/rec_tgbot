from __future__ import annotations

from typing import Any

from reclaude_bot.domain.errors import EligibilityError

from .models import Member, MembersResponse, MeResponse


class FakeReclaudeGateway:
    """In-memory gateway used by tests and local smoke checks; never contacts Reclaude."""

    def __init__(
        self,
        members: list[Member],
        me: MeResponse,
        accounts: list[dict[str, Any]] | None = None,
        account_id: int | str | None = None,
    ) -> None:
        self.member_rows = {str(member.user_id): member for member in members}
        self.me_response = me
        self.account_rows = list(accounts or [])
        self.account_id: int | str | None = None
        self.assign_calls: list[str] = []
        self.revoke_calls: list[str] = []
        self.members_calls = 0
        self.me_calls = 0
        self.accounts_calls = 0
        if account_id is not None:
            self.configure_account_id(account_id)

    async def members(self) -> MembersResponse:
        self.members_calls += 1
        return MembersResponse(items=list(self.member_rows.values()))

    async def me(self) -> MeResponse:
        self.me_calls += 1
        return self.me_response

    async def accounts(self) -> list[dict[str, Any]]:
        self.accounts_calls += 1
        return list(self.account_rows)

    def configure_account_id(self, account_id: int | str) -> None:
        if isinstance(account_id, bool) or (isinstance(account_id, str) and not account_id.strip()):
            raise EligibilityError("Reclaude 账号 ID 无效")
        if not isinstance(account_id, (int, str)):
            raise EligibilityError("Reclaude 账号 ID 无效")
        self.account_id = account_id

    def set_account_id(self, account_id: int | str) -> None:
        self.configure_account_id(account_id)

    async def assign(self, user_id: str | int) -> None:
        if self.account_id is None:
            raise EligibilityError("Reclaude 账号尚未完成恢复配置")
        key = str(user_id)
        self.assign_calls.append(key)
        member = self.member_rows[key]
        self.member_rows[key] = member.model_copy(update={"account_id": self.account_id})

    async def revoke(self, user_id: str | int) -> None:
        key = str(user_id)
        self.revoke_calls.append(key)
        member = self.member_rows[key]
        self.member_rows[key] = member.model_copy(update={"account_id": None})
