"""Real backup → restore → verify against the local Docker database.

Skipped where the compose `db` service is not running (e.g. CI, which uses a service
container without `docker compose`). Dumps go to a pytest temp dir; the scratch database
`<db>_restore_check` is created and dropped by the code under test. Live data is only read.
"""

import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

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


def _compose_db_running() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603  # fixed argv
        [docker, "compose", "ps", "--status", "running", "-q", "db"],
        capture_output=True,
        check=False,
        timeout=20,
    )
    return probe.returncode == 0 and bool(probe.stdout.strip())


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _compose_db_running(), reason="needs the compose db container"),
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


async def test_backup_waits_for_a_running_ingest(
    engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    async with engine.connect() as ingest:
        await ingest.execute(text("SELECT pg_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY})
        try:
            with pytest.raises(AlreadyRunningError):
                await create_backup(
                    engine, PgTools.from_settings(settings), tmp_path, keep=3, now=NOW
                )
        finally:
            await ingest.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
    assert list_backups(tmp_path) == []


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
