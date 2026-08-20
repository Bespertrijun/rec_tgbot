from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator, model_validator


def parse_epoch_or_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Reclaude's timeseries timestamps are epoch milliseconds.
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"unsupported timestamp: {value!r}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AccountRecord(StrictModel):
    """An organization account returned by the recovery discovery endpoint."""

    account_email: str | None = None
    account_id: StrictInt | StrictStr | None = None
    assigned_at: datetime | None = None
    assigned_by: int | str | None = None
    health: str | None = None
    id: int | str | None = None
    lifecycle: str | None = None
    org_id: int | str | None = None
    subscription_type: str | None = None
    user_count: int | None = None

    @field_validator("assigned_at", mode="before")
    @classmethod
    def parse_assigned_at(cls, value: Any) -> datetime | None:
        return None if value is None else parse_epoch_or_datetime(value)

    def has_usable_health(self) -> bool:
        """Return whether health is present and is not the explicitly banned state."""

        if self.health is None:
            return False
        normalized = self.health.strip()
        return bool(normalized) and normalized.casefold() != "banned"


class AccountsResponse(StrictModel):
    items: list[AccountRecord]

    @model_validator(mode="before")
    @classmethod
    def unwrap_items(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"items": value}
        if isinstance(value, dict) and "data" in value and "items" not in value:
            data = value["data"]
            if isinstance(data, list):
                return {"items": data}
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return {"items": data["items"]}
        return value


# Keep the shorter name available to callers that model the endpoint as accounts.
Account = AccountRecord


class Member(StrictModel):
    id: int | str | None = None
    user_id: int | str
    email: str
    account_id: int | str | None = None
    total_usage_usd: Decimal

    @field_validator("total_usage_usd", mode="before")
    @classmethod
    def parse_money(cls, value: Any) -> Decimal:
        return Decimal(str(value))


class MembersResponse(StrictModel):
    items: list[Member]
    total: int | None = None

    @model_validator(mode="before")
    @classmethod
    def unwrap_items(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"items": value}
        if isinstance(value, dict) and "data" in value and "items" not in value:
            data = value["data"]
            if isinstance(data, list):
                return {"items": data}
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return {"items": data["items"]}
        return value


class CurrentAccount(StrictModel):
    status: str
    email_masked: str
    usage_snapshot: UsageSnapshot
    usage_updated_at: datetime

    @field_validator("usage_updated_at", mode="before")
    @classmethod
    def parse_updated(cls, value: Any) -> datetime:
        return parse_epoch_or_datetime(value)


class WeeklyLimit(StrictModel):
    group: str
    kind: str
    scope: Any
    percent: Decimal
    resets_at: datetime | None
    is_active: bool

    @field_validator("percent", mode="before")
    @classmethod
    def parse_percent(cls, value: Any) -> Decimal:
        return Decimal(str(value))

    @field_validator("resets_at", mode="before")
    @classmethod
    def parse_reset(cls, value: Any) -> datetime | None:
        return None if value is None else parse_epoch_or_datetime(value)


class SevenDay(StrictModel):
    utilization: Decimal
    resets_at: datetime

    @field_validator("utilization", mode="before")
    @classmethod
    def parse_percent(cls, value: Any) -> Decimal:
        return Decimal(str(value))

    @field_validator("resets_at", mode="before")
    @classmethod
    def parse_reset(cls, value: Any) -> datetime:
        return parse_epoch_or_datetime(value)


class UsageSnapshot(StrictModel):
    limits: list[WeeklyLimit]
    seven_day: SevenDay


class MeResponse(StrictModel):
    current_account: CurrentAccount

    def weekly_all(self) -> WeeklyLimit:
        candidates = [item for item in self.current_account.usage_snapshot.limits if item.group == "weekly" and item.kind == "weekly_all" and item.scope is None]
        if len(candidates) != 1:
            raise ValueError(f"expected one weekly_all limit, found {len(candidates)}")
        selected = candidates[0]
        if selected.resets_at is None:
            raise ValueError("weekly_all limit is missing resets_at")
        return selected
