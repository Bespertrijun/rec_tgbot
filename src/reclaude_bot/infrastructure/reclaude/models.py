from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


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


class UsageWindow(StrictModel):
    """A rolling usage window such as five_hour; resets_at is null while the window is inactive."""

    utilization: Decimal
    resets_at: datetime | None = None

    @field_validator("utilization", mode="before")
    @classmethod
    def parse_percent(cls, value: Any) -> Decimal:
        return Decimal(str(value))

    @field_validator("resets_at", mode="before")
    @classmethod
    def parse_reset(cls, value: Any) -> datetime | None:
        return None if value is None else parse_epoch_or_datetime(value)


class UsageSnapshot(StrictModel):
    limits: list[WeeklyLimit]
    seven_day: SevenDay
    five_hour: UsageWindow | None = None


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


class DeviceAuthDescription(StrictModel):
    state: StrictStr = Field(repr=False)
    device_name: StrictStr
    created_at: datetime
    expires_at: datetime
    hostname: StrictStr | None = None
    os: StrictStr | None = None
    arch: StrictStr | None = None
    client_ip_masked: StrictStr | None = None
    client_user_agent: StrictStr | None = None
    machine_summary: Any = None

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def parse_times(cls, value: Any) -> datetime:
        if isinstance(value, bool):
            raise ValueError("invalid device authorization timestamp")
        return parse_epoch_or_datetime(value)


class DeviceAuthApproval(StrictModel):
    ok: StrictBool
    device_id: StrictInt
    reused: StrictBool
    user_email: StrictStr | None = None

    @field_validator("ok", mode="before")
    @classmethod
    def require_true_ok(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("device authorization was not confirmed")
        return value

    @field_validator("device_id")
    @classmethod
    def require_positive_device_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("device ID must be positive")
        return value


class DeviceRecord(StrictModel):
    id: StrictInt
    org_id: StrictInt
    name: StrictStr
    created_at: datetime
    revoked_at: datetime | None
    client_platform: StrictStr | None = None
    client_version: StrictStr | None = None
    last_used_at: datetime | None = None
    last_used_ip: StrictStr | None = None
    machine_summary: Any = None
    sk_prefix: StrictStr | None = None
    sk_rotated_at: datetime | None = None

    @field_validator("id", "org_id")
    @classmethod
    def require_positive_ids(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("device and organization IDs must be positive")
        return value

    @field_validator("created_at", "last_used_at", "revoked_at", "sk_rotated_at", mode="before")
    @classmethod
    def parse_device_times(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("invalid device timestamp")
        return parse_epoch_or_datetime(value)


class DeviceRevokeResponse(StrictModel):
    ok: StrictBool

    @field_validator("ok", mode="before")
    @classmethod
    def require_true_ok(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("device revocation was not confirmed")
        return value


class DeviceUsageOverview(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total_usd: Decimal
    heatmap: Any = None

    @field_validator("total_usd", mode="before")
    @classmethod
    def parse_total_usd(cls, value: Any) -> Decimal:
        if isinstance(value, bool):
            raise ValueError("usage total must be a non-negative decimal")
        try:
            total = Decimal(str(value))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ValueError("usage total must be a non-negative decimal") from exc
        if not total.is_finite() or total < 0:
            raise ValueError("usage total must be a finite non-negative decimal")
        return total


class DeviceUsage(StrictModel):
    range: Literal["all", "7d"]
    overview: DeviceUsageOverview
    models_granularity: StrictStr | None = None
    models: Any = None
