from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ADMIN_IDS")
    database_url: str = Field(default="sqlite+aiosqlite:///./reclaude.db", alias="DATABASE_URL")
    reclaude_base_url: str = Field(default="https://reclaude.example", alias="RECLAUDE_BASE_URL")
    reclaude_org_id: int = Field(default=178, alias="RECLAUDE_ORG_ID")
    reclaude_account_id: int = Field(default=4949, alias="RECLAUDE_ACCOUNT_ID")
    reclaude_account_email_masked: str = Field(default="", alias="RECLAUDE_ACCOUNT_EMAIL_MASKED")
    reclaude_session_cookie: str = Field(default="", alias="RECLAUDE_SESSION_COOKIE")
    reclaude_cookie_jar_path: Path = Field(default=Path("data/cookies/cookies.json"), alias="RECLAUDE_COOKIE_JAR_PATH")
    reclaude_user_agent: str = Field(default="reclaude-quota-bot/1.0", alias="RECLAUDE_USER_AGENT")
    timezone: str = Field(default="Asia/Shanghai", alias="TIMEZONE")
    api_timeout_seconds: float = Field(default=15.0, alias="API_TIMEOUT_SECONDS")
    api_max_retries: int = Field(default=2, alias="API_MAX_RETRIES")
    baseline_capture_window_seconds: int = Field(default=60, alias="BASELINE_CAPTURE_WINDOW_SECONDS")
    member_snapshot_max_age_seconds: int = Field(default=90, alias="MEMBER_SNAPSHOT_MAX_AGE_SECONDS")
    quota_limit_usd: Decimal = Field(default=Decimal("700.00"), alias="QUOTA_LIMIT_USD")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    bind_attempts_per_hour: int = 10

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def parse_admins(cls, value: str | list[int] | tuple[int, ...]) -> list[int]:
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return [int(item) for item in value]

    @field_validator("quota_limit_usd", mode="before")
    @classmethod
    def parse_quota(cls, value: object) -> Decimal:
        return Decimal(str(value))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
