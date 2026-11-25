from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analysis.budget import CEILINGS


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
    # Tests only (tests/conftest.py): the server the `<db>_test` database lives on. Set it
    # when DATABASE_URL points at a remote database (Neon): tests then stay local.
    test_database_url: SecretStr | None = None

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

    # AI analysis: Google's Gemini API, FREE TIER ONLY (owner decision 2026-11-24,
    # docs/architecture.md 5.5). The Google project must have no billing account: then
    # going over a Google limit is refused (HTTP 429) and nothing can ever be charged.
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemma-4-31b-it"
    # Our limits keep 1/3 of every Google limit unused. They can only be LOWERED:
    # the code refuses values above the ceilings in app/analysis/budget.py.
    # The per-run setting is the `news lab` limit (3 answered requests per test).
    gemini_max_requests_per_run: int = Field(3, ge=1, le=CEILINGS["lab_per_run"])
    gemini_max_requests_per_day: int = Field(1000, ge=1, le=CEILINGS["per_day"])
    gemini_max_requests_per_minute: int = Field(20, ge=1, le=CEILINGS["per_minute"])
    gemini_max_input_tokens_per_minute: int = Field(
        10_000, ge=1, le=CEILINGS["input_tokens_per_minute"]
    )
    # Articles in flight at once in `news analyze` (each answer takes about 3 minutes).
    gemini_concurrency: int = Field(8, ge=1, le=CEILINGS["concurrency"])

    # Backups (`news backup` / `news restore-test`). Outside the repo and outside iCloud.
    backup_dir: Path = Path.home() / "Backups" / "news"
    backup_keep: int = 14
    backup_max_age_days: int = 7
    # How to run pg_dump/pg_restore/createdb/dropdb: inside the compose db container by
    # default, so tool and server versions always match. Use "" for host-installed tools.
    pg_tools_command: str = "docker compose exec -T db"

    @field_validator("database_url", "api_database_url", "test_database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: object) -> object:
        # docker compose passes an unset variable as "": treat it as not configured.
        if value == "":
            return None
        # Neon (and most providers) show `postgresql://` or `postgres://`: use psycopg 3.
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str):
            for prefix in ("postgresql://", "postgres://"):
                if raw.startswith(prefix):
                    return "postgresql+psycopg://" + raw.removeprefix(prefix)
        return value

    @field_validator("database_url", "api_database_url", "test_database_url")
    @classmethod
    def _require_async_capable_driver(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("postgresql+psycopg://"):
            msg = "database URLs must start with 'postgresql+psycopg://'"
            raise ValueError(msg)
        return value

    @field_validator("backup_dir")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        # "~/Backups/news" from .env must not become a literal "~" folder in the repo.
        return value.expanduser()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
