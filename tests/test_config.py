from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.db.base import Base


def test_metadata_uses_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert "fk" in Base.metadata.naming_convention


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]  # untyped test overrides


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        make()


def test_database_url_must_use_psycopg_driver() -> None:
    with pytest.raises(ValidationError):
        make(database_url="postgresql+psycopg2://user:test-only-password@localhost/db")


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_provider_urls_are_read_with_psycopg(scheme: str) -> None:
    """Neon's connection string can be pasted as shown."""
    url = f"{scheme}://u:test-only-password@ep-x.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    settings = make(database_url=url, api_database_url=url, test_database_url=url)

    for value in (settings.database_url, settings.api_database_url, settings.test_database_url):
        assert value is not None
        assert value.get_secret_value().startswith("postgresql+psycopg://u:")
        assert value.get_secret_value().endswith("?sslmode=require")


def test_an_empty_api_database_url_means_unset() -> None:
    """docker compose passes an unset API_DATABASE_URL as an empty string."""
    assert make(api_database_url="").api_database_url is None


def test_database_url_is_not_leaked_in_repr() -> None:
    settings = make(database_url="postgresql+psycopg://user:test-only-password@localhost/db")

    assert "s3cret" not in repr(settings)
    assert settings.database_url.get_secret_value().endswith("@localhost/db")


def test_defaults() -> None:
    settings = make(database_url="postgresql+psycopg://user:test-only-password@localhost/db")

    assert settings.environment == "development"
    assert not settings.is_production
    assert settings.cors_origins == []


def test_backup_dir_expands_the_home_directory() -> None:
    settings = make(
        database_url="postgresql+psycopg://u:test-only-password@h/d", backup_dir="~/Backups/news"
    )

    assert settings.backup_dir == Path.home() / "Backups" / "news"


def test_test_database_url_needs_the_psycopg_driver() -> None:
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        Settings(  # type: ignore[call-arg]  # explicit test values
            _env_file=None,
            database_url=SecretStr("postgresql+psycopg://u:change-me@localhost:5433/news"),
            test_database_url=SecretStr("mysql://u:p@localhost:5433/news"),
        )
