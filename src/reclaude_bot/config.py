from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from reclaude_bot.infrastructure.reclaude.constants import DEFAULT_RECLAUDE_USER_AGENT

POSTGRESQL_ASYNCPG_DRIVER = "postgresql+asyncpg"


def validate_postgresql_database_url(value: str) -> str:
    """Require the production async PostgreSQL dialect without echoing credentials."""
    try:
        parsed: URL = make_url(value)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise ValueError("DATABASE_URL must be a valid postgresql+asyncpg URL") from exc
    if parsed.drivername != POSTGRESQL_ASYNCPG_DRIVER or not parsed.host or not parsed.database:
        raise ValueError("DATABASE_URL must use postgresql+asyncpg:// with a host and database")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ADMIN_IDS")
    database_url: str = Field(alias="DATABASE_URL")
    reclaude_base_url: str = Field(default="https://reclaude.example", alias="RECLAUDE_BASE_URL")
    reclaude_org_id: int = Field(default=178, alias="RECLAUDE_ORG_ID")
    reclaude_login_email: str = Field(default="", alias="RECLAUDE_LOGIN_EMAIL")
    reclaude_login_password: SecretStr = Field(default=SecretStr(""), alias="RECLAUDE_LOGIN_PASSWORD")
    reclaude_session_cookie: str = Field(default="", alias="RECLAUDE_SESSION_COOKIE")
    reclaude_cookie_jar_path: Path = Field(default=Path("data/cookies/cookies.json"), alias="RECLAUDE_COOKIE_JAR_PATH")
    reclaude_user_agent: str = Field(default=DEFAULT_RECLAUDE_USER_AGENT, alias="RECLAUDE_USER_AGENT")
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
    def parse_admins(cls, value: str | int | list[int] | tuple[int, ...]) -> list[int]:
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        if isinstance(value, int):
            return [value]
        return [int(item) for item in value]

    @field_validator("quota_limit_usd", mode="before")
    @classmethod
    def parse_quota(cls, value: object) -> Decimal:
        return Decimal(str(value))

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        return validate_postgresql_database_url(value)

    @model_validator(mode="after")
    def validate_login_credentials(self) -> Settings:
        email_set = bool(self.reclaude_login_email.strip())
        password_set = bool(self.reclaude_login_password.get_secret_value())
        if email_set != password_set:
            raise ValueError("RECLAUDE_LOGIN_EMAIL and RECLAUDE_LOGIN_PASSWORD must be configured together")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # DATABASE_URL is supplied by the environment/.env
