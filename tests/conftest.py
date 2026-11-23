"""Test configuration.

Tests NEVER touch your real database. Before the app is imported, DATABASE_URL is pointed
at a separate `<name>_test` database on the same server: the dev DB `news` becomes
`news_test`, and CI's `news` becomes `news_test` too. `tests/integration/conftest.py`
creates and migrates it on first use. Advisory locks are per database, so tests also
can't collide with a live `news ingest`.
"""

import os
from pathlib import Path

from sqlalchemy import make_url

from app.core.config import Settings

TEST_SUFFIX = "_test"


def _real_database_url() -> str:
    if "DATABASE_URL" in os.environ:
        return os.environ["DATABASE_URL"]
    if Path(".env").exists():
        return Settings().database_url.get_secret_value()  # type: ignore[call-arg]  # from .env
    return "postgresql+psycopg://news:change-me@localhost:5433/news"  # fresh clone


def _test_database_url(real: str) -> str:
    url = make_url(real)
    name = url.database or "news"
    if not name.endswith(TEST_SUFFIX):
        name += TEST_SUFFIX
    return url.set(database=name).render_as_string(hide_password=False)


# Must run before `app.main` is imported: it builds the app (and caches settings).
os.environ["DATABASE_URL"] = _test_database_url(_real_database_url())

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]  # DATABASE_URL comes from the environment
    database = make_url(settings.database_url.get_secret_value()).database or ""
    assert database.endswith(TEST_SUFFIX), f"refusing to test against {database!r}"
    return settings


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # The context manager runs the app lifespan (engine start/stop).
    with TestClient(app) as test_client:
        yield test_client
