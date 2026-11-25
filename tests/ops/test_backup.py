"""Backup bookkeeping, without a database or Docker."""

import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.ops.backup import (
    BackupError,
    Manifest,
    PgTools,
    _run,
    _run_from_file,
    _run_to_file,
    _write_private,
    freshness_warning,
    list_backups,
    manifest_path_for,
    prune,
    sha256_of,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
FAIL = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\nlast line'); sys.exit(3)"]


def make_backup(directory: Path, created: datetime, *, verified: bool = False) -> Path:
    dump = directory / f"news-{created.strftime('%Y%m%dT%H%M%SZ')}.dump"
    dump.write_bytes(b"dump")
    manifest = Manifest(
        dump_file=dump.name,
        created_at=created,
        size_bytes=4,
        sha256=sha256_of(dump),
        alembic_revision="abc",
        row_counts={"articles": 3},
        verified_at=created if verified else None,
    )
    manifest_path_for(dump).write_text(manifest.to_json())
    return dump


def test_manifest_roundtrip() -> None:
    manifest = Manifest("d.dump", NOW, 10, "h", None, {"a": 1, "b": 0}, verified_at=NOW)

    assert Manifest.from_json(manifest.to_json()) == manifest


def test_list_is_newest_first_and_skips_broken_or_foreign_files(tmp_path: Path) -> None:
    old = make_backup(tmp_path, NOW - timedelta(days=2))
    new = make_backup(tmp_path, NOW)
    orphan = tmp_path / "news-20200101T000000Z.dump"  # no manifest
    orphan.write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not ours")

    assert [b.dump for b in list_backups(tmp_path)] == [new, old]


def test_missing_directory_has_no_backups(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "absent") == []


def test_prune_keeps_the_newest_and_removes_partials(tmp_path: Path) -> None:
    dumps = [make_backup(tmp_path, NOW - timedelta(days=d)) for d in range(5)]
    partial = tmp_path / "news-20261123T000000Z.dump.partial"
    partial.write_bytes(b"half")
    foreign = tmp_path / "keep-me.dump"
    foreign.write_bytes(b"x")

    prune(tmp_path, keep=2)

    assert [b.dump for b in list_backups(tmp_path)] == dumps[:2]
    assert not manifest_path_for(dumps[4]).exists()
    assert not partial.exists()
    assert foreign.exists()  # never touches files it did not create


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", "No backup yet"),
        ("unverified", "No verified backup yet"),
        ("stale", "8 days old"),
        ("fresh", None),
    ],
)
def test_freshness_warning(tmp_path: Path, setup: str, expected: str | None) -> None:
    if setup == "unverified":
        make_backup(tmp_path, NOW)
    elif setup == "stale":
        make_backup(tmp_path, NOW - timedelta(days=8), verified=True)
        make_backup(tmp_path, NOW)  # newer but unverified: does not count
    elif setup == "fresh":
        make_backup(tmp_path, NOW - timedelta(days=2), verified=True)

    warning = freshness_warning(tmp_path, max_age=timedelta(days=7), now=NOW)

    if expected is None:
        assert warning is None
    else:
        assert warning is not None
        assert expected in warning


def settings_with(url: str, tools: str = "docker compose exec -T db") -> Settings:
    return Settings.model_validate({"database_url": url, "pg_tools_command": tools})


def test_pg_tools_run_inside_the_container_by_default() -> None:
    tools = PgTools.from_settings(
        settings_with("postgresql+psycopg://news:test-only-password@localhost:5433/news")
    )

    assert tools.dump() == [
        "docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "news", "-d", "news", "-Fc"
    ]  # fmt: skip
    assert tools.scratch_database == "news_restore_check"
    assert "test-only-password" not in " ".join(
        tools.dump() + tools.restore("x") + tools.create_db("x")
    )
    assert "--no-privileges" in tools.restore("x")


def test_pg_tools_can_run_on_the_host() -> None:
    tools = PgTools.from_settings(
        settings_with("postgresql+psycopg://u:test-only-password@h/db", tools="")
    )

    assert tools.drop_db("db_x") == ["dropdb", "-U", "u", "-h", "h", "--if-exists", "db_x"]
    assert tools.env() == {**os.environ, "PGPASSWORD": "test-only-password"}


def test_pg_tools_need_a_user_and_database() -> None:
    with pytest.raises(BackupError, match="user and a database"):
        PgTools.from_settings(settings_with("postgresql+psycopg://localhost:5433"))


def test_private_files_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "m.json"

    _write_private(path, "{}")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_tool_raises_with_its_last_error_line(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="failed: last line"):
        _run(FAIL)

    target = tmp_path / "out.partial"
    with pytest.raises(BackupError):
        _run_to_file(FAIL, target)
    assert not target.exists()  # a failed dump leaves nothing behind

    source = tmp_path / "in.dump"
    source.write_bytes(b"x")
    with pytest.raises(BackupError):
        _run_from_file(FAIL, source)


def test_successful_tool_writes_its_output(tmp_path: Path) -> None:
    target = tmp_path / "out"

    _run_to_file([sys.executable, "-c", "print('data', end='')"], target)

    assert target.read_bytes() == b"data"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
