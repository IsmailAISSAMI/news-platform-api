from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _package_version() -> str:
    try:
        return version("news-backend")
    except PackageNotFoundError:
        return "0.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "News API"
    app_version: str = _package_version()
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = []

    # Required: no default so a misconfigured deployment fails fast instead of
    # silently connecting with development credentials.
    database_url: SecretStr
    # Optional: a login user in the SELECT-only `news_api` role, used by the API.
    api_database_url: SecretStr | None = None

    # Outbound HTTP (ingestion). The User-Agent identifies us and gives publishers a contact.
    user_agent: str = (
        f"news-platform-api/{_package_version()} "
        "(+https://github.com/IsmailAISSAMI/news-platform-api)"
    )
    http_connect_timeout_s: float = 5.0
    http_total_timeout_s: float = 20.0
    http_max_response_bytes: int = 5 * 1024 * 1024
    # Minimum gap between requests to one host; a robots.txt Crawl-delay can only raise it.
    http_min_delay_s: float = 1.0
    http_max_attempts: int = 3

    @field_validator("database_url", "api_database_url")
    @classmethod
    def _require_async_capable_driver(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("postgresql+psycopg://"):
            msg = "database URLs must start with 'postgresql+psycopg://'"
            raise ValueError(msg)
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
