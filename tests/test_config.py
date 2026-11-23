from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.db.base import Base


def test_metadata_uses_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert "fk" in Base.metadata.naming_convention


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        make()


def test_database_url_must_use_psycopg_driver() -> None:
    with pytest.raises(ValidationError):
        make(database_url="postgresql://user:pw@localhost/db")


def test_database_url_is_not_leaked_in_repr() -> None:
    settings = make(database_url="postgresql+psycopg://user:s3cret@localhost/db")

    assert "s3cret" not in repr(settings)
    assert settings.database_url.get_secret_value().endswith("@localhost/db")


def test_defaults() -> None:
    settings = make(database_url="postgresql+psycopg://user:pw@localhost/db")

    assert settings.environment == "development"
    assert not settings.is_production
    assert settings.cors_origins == []


def test_backup_dir_expands_the_home_directory() -> None:
    settings = make(database_url="postgresql+psycopg://u:p@h/d", backup_dir="~/Backups/news")

    assert settings.backup_dir == Path.home() / "Backups" / "news"
