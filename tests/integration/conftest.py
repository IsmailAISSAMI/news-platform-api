from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.session import create_engine
from app.ingestion.fetch import FetcherConfig, PoliteFetcher
from app.models import Source
from tests.ingestion.publisher import Publisher


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
