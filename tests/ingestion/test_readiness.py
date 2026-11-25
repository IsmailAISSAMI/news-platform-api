"""Readiness guards that need no database (IA-012)."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.ingestion.readiness import NotReadyError, refuse_pooled_host, wait_for_database


@pytest.mark.parametrize(
    "host",
    ["ep-cool-name-123456-pooler.eu-central-1.aws.neon.tech", "EP-X-POOLER.neon.tech"],
)
def test_a_pooler_host_is_refused_with_the_fix(host: str) -> None:
    with pytest.raises(NotReadyError, match="Neon's direct endpoint"):
        refuse_pooled_host(host)


@pytest.mark.parametrize(
    "host", ["ep-cool-name-123456.eu-central-1.aws.neon.tech", "localhost", "db", None]
)
def test_a_direct_host_is_accepted(host: str | None) -> None:
    refuse_pooled_host(host)


async def test_wait_for_database_refuses_a_pooler_before_connecting() -> None:
    engine = create_async_engine(
        "postgresql+psycopg://u:test-only-password@ep-x-pooler.invalid.neon.tech:5432/neondb"
    )
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:  # pragma: no cover - must not be reached
        slept.append(seconds)

    try:
        with pytest.raises(NotReadyError, match="pooler"):
            await wait_for_database(engine, timeout_s=0, sleep=no_sleep)
    finally:
        await engine.dispose()
    assert slept == []
