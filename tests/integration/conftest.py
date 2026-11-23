import os
import subprocess
import sys
from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import make_url, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.session import create_engine
from app.ingestion.fetch import FetcherConfig, PoliteFetcher
from app.models import Source
from tests.conftest import TEST_SUFFIX
from tests.ingestion.publisher import Publisher


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    """Create the `<name>_test` database if needed and migrate it to head (once per run)."""
    test_url = make_url(os.environ["DATABASE_URL"])
    name = test_url.database or ""
    assert name.endswith(TEST_SUFFIX), f"refusing to prepare {name!r} for tests"

    maintenance = test_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        maintenance.render_as_string(hide_password=False), autocommit=True, connect_timeout=5
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    # A subprocess keeps Alembic's logging configuration out of the test process.
    subprocess.run(  # fixed argv, current interpreter
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session wrapped in a transaction that is rolled back after each test."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        # expire_on_commit=False matches app.db.session (code that commits mid-way, like the
        # RSS walk, keeps using its loaded objects).
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
def publisher() -> Publisher:
    return Publisher()


@pytest.fixture
async def fetcher(publisher: Publisher) -> AsyncIterator[PoliteFetcher]:
    async def no_sleep(_: float) -> None:
        return None

    config = FetcherConfig("news-platform-api", 1024 * 1024, min_delay_s=0, max_attempts=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(publisher.handle)) as client:
        yield PoliteFetcher(client, config, sleep=no_sleep)


@pytest.fixture
async def source(db_session: AsyncSession) -> Source:
    return (
        await db_session.execute(select(Source).where(Source.slug == "hespress-en"))
    ).scalar_one()
