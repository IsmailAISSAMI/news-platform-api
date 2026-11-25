"""Real backup → restore → verify against the test database.

Runs wherever the pg tools can reach DATABASE_URL: inside the compose `db` container
(the default PG_TOOLS_COMMAND), or host tools when PG_TOOLS_COMMAND="" (CI installs
postgresql-client). Skipped otherwise. Dumps go to a pytest temp dir; the scratch database
`<db>_restore_check` is created and dropped by the code under test. Live data is only read.
"""

import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analysis.pipeline import ANALYZE_LOCK_KEY
from app.core.config import Settings
from app.ingestion.runner import INGEST_LOCK_KEY, AlreadyRunningError
from app.ops.backup import (
    Backup,
    BackupError,
    PgTools,
    create_backup,
    list_backups,
    verify_backup,
)


def _pg_tools_reach_the_database() -> bool:
    """Probe the configured tools against DATABASE_URL (`pg_dump --schema-only` on it)."""
    try:
        tools = PgTools.from_settings(Settings())  # type: ignore[call-arg]  # from the environment
    except BackupError:
        return False
    if not tools.prefix and shutil.which("pg_dump") is None:
        return False
    if tools.prefix and shutil.which(tools.prefix[0]) is None:
        return False
    try:
        probe = subprocess.run(  # noqa: S603  # fixed argv
            [*tools.dump(), "--schema-only", "-t", "alembic_version"],
            capture_output=True,
            check=False,
            timeout=30,
            env=tools.env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


_REACHABLE = _pg_tools_reach_the_database()
if not _REACHABLE and os.environ.get("REQUIRE_BACKUP_TESTS") == "1":
    msg = "REQUIRE_BACKUP_TESTS=1 but pg tools cannot reach DATABASE_URL"
    raise RuntimeError(msg)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _REACHABLE, reason="pg tools cannot reach DATABASE_URL"),
]

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


async def test_backup_restores_completely_and_is_marked_verified(
    engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    tools = PgTools.from_settings(settings)

    backup = await create_backup(engine, tools, tmp_path, keep=3, now=NOW)

    assert backup.dump.exists()
    assert backup.manifest.row_counts["sources"] >= 1
    assert backup.manifest.alembic_revision
    assert not list(tmp_path.glob("*.partial"))

    result = await verify_backup(backup, tools, settings, now=NOW)

    assert result.ok, result.mismatches
    assert list_backups(tmp_path)[0].manifest.verified_at == NOW
    async with engine.connect() as connection:  # the scratch database is gone again
        scratch = await connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": tools.scratch_database}
        )
    assert scratch is None


async def test_corrupted_dump_is_detected_before_restoring(
    engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    tools = PgTools.from_settings(settings)
    backup = await create_backup(engine, tools, tmp_path, keep=3, now=NOW)
    with backup.dump.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(BackupError, match="checksum"):
        await verify_backup(backup, tools, settings, now=NOW)


@pytest.mark.parametrize("key", [INGEST_LOCK_KEY, ANALYZE_LOCK_KEY], ids=["ingest", "analyze"])
async def test_backup_waits_for_a_running_ingest_or_analyze(
    engine: AsyncEngine, settings: Settings, tmp_path: Path, key: int
) -> None:
    async with engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            with pytest.raises(AlreadyRunningError):
                await create_backup(
                    engine, PgTools.from_settings(settings), tmp_path, keep=3, now=NOW
                )
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    assert list_backups(tmp_path) == []
    async with engine.connect() as connection:  # the backup released whatever it took
        for lock in (INGEST_LOCK_KEY, ANALYZE_LOCK_KEY):
            assert await connection.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock})
            await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock})


async def test_row_count_mismatch_fails_and_is_not_marked_verified(
    engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    tools = PgTools.from_settings(settings)
    backup = await create_backup(engine, tools, tmp_path, keep=3, now=NOW)
    wrong_counts = {**backup.manifest.row_counts, "sources": 999}
    tampered = Backup(backup.dump, replace(backup.manifest, row_counts=wrong_counts))

    result = await verify_backup(tampered, tools, settings, now=NOW)

    assert not result.ok
    assert result.mismatches == {"sources": (999, backup.manifest.row_counts["sources"])}
    assert list_backups(tmp_path)[0].manifest.verified_at is None
