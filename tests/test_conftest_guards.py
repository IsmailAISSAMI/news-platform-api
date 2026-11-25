"""The test-database guards in tests/conftest.py (TI-001, BC-002). Pure functions, no DB."""

import os

import pytest

from tests import conftest
from tests.conftest import CI_ONLY_ENV, TEST_SUFFIX, _local_only, _test_database_url

LOCAL = "postgresql+psycopg://news:test-only-password@localhost:5433/news"
NEON = "postgresql+psycopg://owner:test-only-password@ep-cool-name-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require"
NEON_POOLER = "postgresql+psycopg://owner:test-only-password@ep-cool-name-123456-pooler.eu-central-1.aws.neon.tech/neondb"


@pytest.mark.parametrize("url", [NEON, NEON_POOLER])
def test_a_neon_url_is_refused(url: str) -> None:
    with pytest.raises(RuntimeError, match="Neon"):
        _local_only(url)


@pytest.mark.parametrize("name", ["TEST_DATABASE_URL", "DATABASE_URL"])
def test_a_neon_url_is_refused_from_either_variable(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(name, NEON)
    with pytest.raises(RuntimeError, match="Neon"):
        conftest._real_database_url()


def test_a_local_url_passes_unchanged() -> None:
    assert _local_only(LOCAL) == LOCAL


def test_the_database_name_gets_the_test_suffix() -> None:
    assert _test_database_url(LOCAL).endswith("/news_test")


def test_a_test_database_name_is_left_alone() -> None:
    url = LOCAL + TEST_SUFFIX
    assert _test_database_url(url) == url


def test_the_rewrite_keeps_credentials_host_and_driver() -> None:
    assert (
        _test_database_url(LOCAL)
        == "postgresql+psycopg://news:test-only-password@localhost:5433/news_test"
    )


def test_a_url_without_a_database_becomes_news_test() -> None:
    assert _test_database_url(
        "postgresql+psycopg://news:test-only-password@localhost:5433"
    ).endswith("/news_test")


def test_this_session_points_only_at_test_databases() -> None:
    assert os.environ["DATABASE_URL"].split("?")[0].endswith(TEST_SUFFIX)
    api = os.environ.get("API_DATABASE_URL")
    assert api is None or api.split("?")[0].endswith(TEST_SUFFIX)


def test_unit_tests_never_see_the_ci_only_variables() -> None:
    for name in CI_ONLY_ENV:
        assert name not in os.environ
