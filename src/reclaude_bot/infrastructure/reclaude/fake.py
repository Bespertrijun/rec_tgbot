from __future__ import annotations

from typing import Any

from .models import Member, MembersResponse, MeResponse


class FakeReclaudeGateway:
    """In-memory gateway used by tests and local smoke checks; never contacts Reclaude."""

    def __init__(self, members: list[Member], me: MeResponse) -> None:
        self.member_rows = {str(member.user_id): member for member in members}
        self.me_response = me
        self.assign_calls: list[str] = []
        self.revoke_calls: list[str] = []
        self.members_calls = 0
        self.me_calls = 0
        self.accounts_calls = 0

    async def members(self) -> MembersResponse:
        self.members_calls += 1
        return MembersResponse(items=list(self.member_rows.values()))

    async def me(self) -> MeResponse:
        self.me_calls += 1
        return self.me_response

    async def accounts(self) -> list[dict[str, Any]]:
        self.accounts_calls += 1
        account = self.me_response.current_account
        return [{"id": 4949, "email_masked": account.email_masked}]

    async def assign(self, user_id: str | int) -> None:
        key = str(user_id)
        self.assign_calls.append(key)
        member = self.member_rows[key]
        self.member_rows[key] = member.model_copy(update={"account_id": 4949})

    async def revoke(self, user_id: str | int) -> None:
        key = str(user_id)
        self.revoke_calls.append(key)
        member = self.member_rows[key]
        self.member_rows[key] = member.model_copy(update={"account_id": None})
