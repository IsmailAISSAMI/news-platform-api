"""Test configuration.

Tests NEVER touch your real database. Before the app is imported, DATABASE_URL is pointed
at a separate `<name>_test` database on the same server: the dev DB `news` becomes
`news_test`, and CI's `news` becomes `news_test` too. `tests/integration/conftest.py`
creates and migrates it on first use. Advisory locks are per database, so tests also
can't collide with a live `news ingest`.

Only TEST_DATABASE_URL (if set) or DATABASE_URL is taken from `.env` (for the server's
credentials) and rewritten to `_test`. A Neon URL is refused: tests stay local.
Then `Settings.model_config["env_file"]` is cleared, so nothing else (a real
API_DATABASE_URL or Gemini key) can come from `.env`: `API_DATABASE_URL` is rewritten to
`_test` or dropped, and `GEMINI_API_KEY` is a dummy.
"""

import os
from pathlib import Path

from sqlalchemy import make_url

from app.core.config import Settings

TEST_SUFFIX = "_test"
DEFAULT_URL = "postgresql+psycopg://news:change-me@localhost:5433/news"  # compose default
DUMMY_GEMINI_KEY = "test-dummy-key-not-real"


def _real_database_url() -> str:
    for name in ("TEST_DATABASE_URL", "DATABASE_URL"):
        if name in os.environ:
            return _local_only(os.environ[name])
    if Path(".env").exists():
        env = Settings()  # type: ignore[call-arg]  # from .env
        url = env.test_database_url or env.database_url
        return _local_only(url.get_secret_value())
    return DEFAULT_URL  # fresh clone


def _local_only(url: str) -> str:
    """Tests never create databases on Neon: set TEST_DATABASE_URL to the local server."""
    host = make_url(url).host or ""
    if host.endswith(".neon.tech"):
        msg = (
            "DATABASE_URL points at Neon: set TEST_DATABASE_URL in .env to the local "
            "Docker database (postgresql+psycopg://news:change-me@localhost:5433/news)."
        )
        raise RuntimeError(msg)
    return url


def _test_database_url(real: str) -> str:
    url = make_url(real)
    name = url.database or "news"
    if not name.endswith(TEST_SUFFIX):
        name += TEST_SUFFIX
    return url.set(database=name).render_as_string(hide_password=False)


# Must run before `app.main` is imported: it builds the app (and caches settings).
os.environ["DATABASE_URL"] = _test_database_url(_real_database_url())
if os.environ.get("API_DATABASE_URL"):
    os.environ["API_DATABASE_URL"] = _test_database_url(os.environ["API_DATABASE_URL"])
else:
    os.environ.pop("API_DATABASE_URL", None)
os.environ["GEMINI_API_KEY"] = DUMMY_GEMINI_KEY
# From here on nothing else comes from .env (no real API_DATABASE_URL or Gemini key).
Settings.model_config["env_file"] = None

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]  # DATABASE_URL comes from the environment
    for url in (settings.database_url, settings.api_database_url):
        if url is not None:
            database = make_url(url.get_secret_value()).database or ""
            assert database.endswith(TEST_SUFFIX), f"refusing to test against {database!r}"
    key = settings.gemini_api_key
    assert key is None or key.get_secret_value() == DUMMY_GEMINI_KEY, "real Gemini key in tests"
    return settings


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # The context manager runs the app lifespan (engine start/stop).
    with TestClient(app) as test_client:
        yield test_client


# CI sets these for the whole job (BC-002); only integration tests may see them, so a unit
# test behaves the same locally and in CI.
CI_ONLY_ENV = ("PG_TOOLS_COMMAND", "REQUIRE_BACKUP_TESTS")


@pytest.fixture(autouse=True)
def _unit_tests_ignore_ci_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("integration") is None:
        for name in CI_ONLY_ENV:
            monkeypatch.delenv(name, raising=False)
