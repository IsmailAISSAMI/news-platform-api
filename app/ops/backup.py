"""Backups that are proven restorable (docs/architecture.md §8).

- `create_backup`: under the ingest lock (no half-written run in the dump), record exact
  row counts, stream `pg_dump -Fc` to `<dir>/news-<UTC stamp>.dump.partial`, then rename
  atomically and write a JSON manifest beside it. Old backups beyond `keep` are pruned.
- `verify_backup`: check the checksum, restore into a scratch database, compare every
  table's row count with the manifest, mark the manifest verified, drop the scratch DB.

The live database is only ever read. Secrets never reach a command line or a log line:
the pg tools run inside the db container over its local socket.
"""

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess  # fixed argv lists only, never a shell
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import Settings
from app.ingestion.runner import INGEST_LOCK_KEY, AlreadyRunningError

logger = logging.getLogger(__name__)

PREFIX = "news-"
DUMP_SUFFIX = ".dump"
MANIFEST_SUFFIX = ".json"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
CHUNK = 1024 * 1024


class BackupError(Exception):
    """A backup or restore step failed. The message explains which and why."""


@dataclass(frozen=True)
class Manifest:
    dump_file: str
    created_at: datetime
    size_bytes: int
    sha256: str
    alembic_revision: str | None
    row_counts: dict[str, int]
    verified_at: datetime | None = None

    def to_json(self) -> str:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["verified_at"] = self.verified_at.isoformat() if self.verified_at else None
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, raw: str) -> "Manifest":
        data = json.loads(raw)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("verified_at"):
            data["verified_at"] = datetime.fromisoformat(data["verified_at"])
        return cls(**data)


@dataclass(frozen=True)
class Backup:
    dump: Path
    manifest: Manifest

    @property
    def manifest_path(self) -> Path:
        return manifest_path_for(self.dump)


@dataclass
class VerifyResult:
    backup: Backup
    mismatches: dict[str, tuple[int, int | None]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.mismatches


@dataclass(frozen=True)
class PgTools:
    """Builds argv for the PostgreSQL client tools, optionally inside a container."""

    prefix: tuple[str, ...]
    user: str
    database: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "PgTools":
        url = make_url(settings.database_url.get_secret_value())
        if not url.username or not url.database:
            msg = "DATABASE_URL must include a user and a database name"
            raise BackupError(msg)
        return cls(tuple(shlex.split(settings.pg_tools_command)), url.username, url.database)

    def dump(self) -> list[str]:
        return [*self.prefix, "pg_dump", "-U", self.user, "-d", self.database, "-Fc"]

    def create_db(self, name: str) -> list[str]:
        return [*self.prefix, "createdb", "-U", self.user, name]

    def drop_db(self, name: str) -> list[str]:
        return [*self.prefix, "dropdb", "-U", self.user, "--if-exists", name]

    def restore(self, name: str) -> list[str]:
        return [
            *self.prefix,
            "pg_restore",
            "-U",
            self.user,
            "-d",
            name,
            "--no-owner",
            "--no-privileges",  # roles may not exist where a restore is checked
            "--exit-on-error",
        ]

    @property
    def scratch_database(self) -> str:
        return f"{self.database}_restore_check"


# --- Creating backups ---------------------------------------------------------------------


async def create_backup(
    engine: AsyncEngine, tools: PgTools, backup_dir: Path, *, keep: int, now: datetime
) -> Backup:
    await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
    dump = backup_dir / f"{PREFIX}{now.astimezone(UTC).strftime(STAMP_FORMAT)}{DUMP_SUFFIX}"
    partial = dump.with_name(dump.name + ".partial")

    async with engine.connect() as connection:
        if not await connection.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY}
        ):
            msg = "an ingest run is in progress; back up after it finishes"
            raise AlreadyRunningError(msg)
        try:
            counts = await table_row_counts(connection)
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            await connection.commit()
            await asyncio.to_thread(_run_to_file, tools.dump(), partial)
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
            await connection.commit()

    return await asyncio.to_thread(
        _finalize, partial, dump, now=now, revision=revision, counts=counts, keep=keep
    )


def _finalize(  # noqa: PLR0913  # sync file work, run in a worker thread
    partial: Path,
    dump: Path,
    *,
    now: datetime,
    revision: str | None,
    counts: dict[str, int],
    keep: int,
) -> Backup:
    partial.replace(dump)  # atomic: a dump file is either complete or absent
    manifest = Manifest(
        dump_file=dump.name,
        created_at=now,
        size_bytes=dump.stat().st_size,
        sha256=sha256_of(dump),
        alembic_revision=revision,
        row_counts=counts,
    )
    _write_private(manifest_path_for(dump), manifest.to_json())
    prune(dump.parent, keep=keep)
    return Backup(dump, manifest)


async def table_row_counts(connection: AsyncConnection) -> dict[str, int]:
    """Exact counts for every table in `public`. Fine at this scale (thousands of rows)."""
    tables = (
        await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        )
    ).scalars()
    counts: dict[str, int] = {}
    for table in list(tables):
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = int(await connection.scalar(text(f"SELECT count(*) FROM {quoted}")))  # noqa: S608  # identifier from pg_tables, quoted
    return counts


# --- Verifying backups --------------------------------------------------------------------


async def verify_backup(
    backup: Backup, tools: PgTools, settings: Settings, *, now: datetime
) -> VerifyResult:
    if await asyncio.to_thread(sha256_of, backup.dump) != backup.manifest.sha256:
        msg = f"{backup.dump.name} does not match its recorded checksum (corrupted?)"
        raise BackupError(msg)

    scratch = tools.scratch_database
    await asyncio.to_thread(_run, tools.drop_db(scratch))
    await asyncio.to_thread(_run, tools.create_db(scratch))
    try:
        await asyncio.to_thread(_run_from_file, tools.restore(scratch), backup.dump)
        restored = await _counts_in(settings, scratch)
    finally:
        await asyncio.to_thread(_run, tools.drop_db(scratch))

    result = VerifyResult(backup)
    for table, expected in backup.manifest.row_counts.items():
        actual = restored.get(table)
        if actual != expected:
            result.mismatches[table] = (expected, actual)
    if result.ok:
        verified = replace(backup.manifest, verified_at=now)
        await asyncio.to_thread(_write_private, backup.manifest_path, verified.to_json())
        result.backup = Backup(backup.dump, verified)
    return result


async def _counts_in(settings: Settings, database: str) -> dict[str, int]:
    url = make_url(settings.database_url.get_secret_value()).set(database=database)
    engine = create_async_engine(url, connect_args={"connect_timeout": 5})
    try:
        async with engine.connect() as connection:
            return await table_row_counts(connection)
    finally:
        await engine.dispose()


# --- Listing, pruning, freshness ---------------------------------------------------------


def list_backups(backup_dir: Path) -> list[Backup]:
    """Complete backups (dump + readable manifest), newest first."""
    if not backup_dir.is_dir():
        return []
    backups = []
    for dump in sorted(backup_dir.glob(f"{PREFIX}*{DUMP_SUFFIX}"), reverse=True):
        manifest_path = manifest_path_for(dump)
        try:
            manifest = Manifest.from_json(manifest_path.read_text())
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Ignoring %s: missing or unreadable manifest", dump.name)
            continue
        backups.append(Backup(dump, manifest))
    return backups


def prune(backup_dir: Path, *, keep: int) -> list[Path]:
    """Delete the oldest backups beyond `keep`. Only files this tool created are touched."""
    removed: list[Path] = []
    for backup in list_backups(backup_dir)[keep:]:
        for path in (backup.dump, backup.manifest_path):
            path.unlink(missing_ok=True)
            removed.append(path)
    for leftover in backup_dir.glob(f"{PREFIX}*{DUMP_SUFFIX}.partial"):
        leftover.unlink(missing_ok=True)  # from an interrupted dump
        removed.append(leftover)
    return removed


def freshness_warning(backup_dir: Path, *, max_age: timedelta, now: datetime) -> str | None:
    """A reminder for `news ingest`, or None when a recent verified backup exists."""
    backups = list_backups(backup_dir)
    verified = [b for b in backups if b.manifest.verified_at is not None]
    if not backups:
        return "No backup yet. Run `uv run news backup` then `uv run news restore-test`."
    if not verified:
        return "No verified backup yet. Run `uv run news restore-test`."
    age = now - verified[0].manifest.created_at
    if age > max_age:
        return (
            f"Newest verified backup is {age.days} days old. "
            "Run `uv run news backup` then `uv run news restore-test`."
        )
    return None


# --- helpers ------------------------------------------------------------------------------


def manifest_path_for(dump: Path) -> Path:
    return dump.with_suffix(MANIFEST_SUFFIX)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)


def _run(argv: list[str]) -> None:
    completed = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603  # fixed argv
    if completed.returncode != 0:
        raise BackupError(_failure(argv, completed.stderr))


def _run_to_file(argv: list[str], destination: Path) -> None:
    with destination.open("wb") as out:
        destination.chmod(0o600)
        completed = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, check=False)  # noqa: S603  # fixed argv
    if completed.returncode != 0:
        destination.unlink(missing_ok=True)
        raise BackupError(_failure(argv, completed.stderr))


def _run_from_file(argv: list[str], source: Path) -> None:
    with source.open("rb") as handle:
        completed = subprocess.run(argv, stdin=handle, capture_output=True, check=False)  # noqa: S603  # fixed argv
    if completed.returncode != 0:
        raise BackupError(_failure(argv, completed.stderr))


def _failure(argv: list[str], stderr: bytes) -> str:
    tool = next((a for a in argv if a.startswith(("pg_", "createdb", "dropdb"))), argv[0])
    detail = stderr.decode(errors="replace").strip().splitlines()
    return f"{tool} failed: {detail[-1] if detail else 'no error output'}"
