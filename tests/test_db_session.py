"""Engine options and pg-tool argv (no database needed)."""

from typing import Any

import pytest

from app.core.config import Settings
from app.db import session
from app.db.session import API_STATEMENT_TIMEOUT_MS, create_engine
from app.ops.backup import PgTools

URL = "postgresql+psycopg://news:change-me@db.example:5544/news_test"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": URL, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg,arg-type]  # test overrides


def test_read_only_engine_sets_read_only_and_a_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake(url: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake)
    session.create_engine(make_settings(), read_only=True)
    assert captured["hide_parameters"] is True
    options = captured["connect_args"]["options"]
    assert "default_transaction_read_only=on" in options
    assert f"statement_timeout={API_STATEMENT_TIMEOUT_MS}" in options


def test_all_engines_hide_parameters() -> None:
    for read_only in (False, True):
        engine = create_engine(make_settings(), read_only=read_only)
        assert engine.sync_engine.hide_parameters


def test_statement_timeout_is_five_seconds() -> None:
    assert API_STATEMENT_TIMEOUT_MS == 5000


def test_host_pg_tools_pass_the_password_in_the_env_only() -> None:
    tools = PgTools.from_settings(make_settings(pg_tools_command=""))
    argv = tools.dump("00000003-1")
    assert "change-me" not in " ".join(argv)
    assert argv[-1] == "--snapshot=00000003-1"
    assert argv[3:7] == ["-h", "db.example", "-p", "5544"]
    env = tools.env()
    assert env is not None
    assert env["PGPASSWORD"] == "change-me"
    assert "change-me" not in repr(tools)


def test_container_pg_tools_use_the_socket_for_the_local_server() -> None:
    local = "postgresql+psycopg://news:change-me@localhost:5433/news"
    tools = PgTools.from_settings(
        make_settings(database_url=local, pg_tools_command="docker compose exec -T db")
    )
    assert tools.env() is None
    assert tools.dump()[:5] == ["docker", "compose", "exec", "-T", "db"]
    assert "-h" not in tools.dump()


def test_a_remote_server_uses_a_throwaway_container_with_tls_in_the_env() -> None:
    url = (
        "postgresql+psycopg://owner:change-me@ep-x.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    tools = PgTools.from_settings(
        make_settings(database_url=url, pg_tools_command="docker compose exec -T db")
    )
    argv = tools.dump()
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert "-h" in argv and "ep-x.eu-central-1.aws.neon.tech" in argv
    assert "change-me" not in " ".join(argv + tools.restore("x") + tools.create_db("x"))
    assert "change-me" not in repr(tools)
    env = tools.env()
    assert env is not None
    assert env["PGPASSWORD"] == "change-me"
    assert env["PGSSLMODE"] == "require"
    assert env["PGCHANNELBINDING"] == "require"
