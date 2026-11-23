"""Pre-flight checks for `news ingest`: fail fast with an actionable message."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class NotReadyError(Exception):
    """The environment cannot run ingestion. The message says how to fix it."""


async def wait_for_database(
    engine: AsyncEngine,
    *,
    timeout_s: float = 10.0,
    interval_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry briefly (Docker may still be starting), then give up with a clear message."""
    deadline = clock() + timeout_s
    while True:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OperationalError, OSError) as exc:
            if clock() >= deadline:
                msg = (
                    "PostgreSQL is not reachable. Is Docker running? "
                    "Start the database with `docker compose up -d db`."
                )
                raise NotReadyError(msg) from exc
            await sleep(interval_s)
        else:
            return


def alembic_head(alembic_ini: Path) -> str:
    if not alembic_ini.is_file():
        msg = f"{alembic_ini} not found: run `news` from the project root."
        raise NotReadyError(msg)
    head = ScriptDirectory.from_config(Config(str(alembic_ini))).get_current_head()
    if head is None:  # pragma: no cover - the repo always has migrations
        msg = "no Alembic migrations found"
        raise NotReadyError(msg)
    return head


async def ensure_migrated(connection: AsyncConnection, expected_head: str) -> None:
    """Refuse to ingest into a schema older (or newer) than the code expects."""
    try:
        async with connection.begin_nested():
            current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except (ProgrammingError, DBAPIError) as exc:
        msg = "The database has no schema yet. Run `uv run alembic upgrade head`."
        raise NotReadyError(msg) from exc
    if current != expected_head:
        msg = (
            f"The database is at migration {current}, the code expects {expected_head}. "
            "Run `uv run alembic upgrade head` (then re-run scripts/db_roles.sql)."
        )
        raise NotReadyError(msg)
