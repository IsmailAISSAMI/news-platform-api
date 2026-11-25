"""`news ingest` orchestration: lock, feed isolation, feed health, run records, readiness."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.ingestion.fetch import PoliteFetcher
from app.ingestion.readiness import (
    NotReadyError,
    alembic_head,
    ensure_migrated,
    wait_for_database,
)
from app.ingestion.runner import (
    DEGRADED_AFTER,
    DISABLED_AFTER,
    INGEST_LOCK_KEY,
    AlreadyRunningError,
    IngestOptions,
    RunReport,
    run_ingest,
)
from app.ingestion.walk import StopReason
from app.models import Feed, IngestionRun, Source
from app.models.enums import FeedKind, FeedState, RunStatus
from tests.ingestion.feeds import Post
from tests.ingestion.publisher import Publisher, posts

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
OPTIONS = IngestOptions(bootstrap_days=30, max_pages=50)


async def ingest(session: AsyncSession, fetcher: PoliteFetcher, **overrides: object) -> RunReport:
    options = replace(OPTIONS, **overrides)  # type: ignore[arg-type]  # test-only overrides
    return await run_ingest(session, fetcher, now=NOW, options=options)


async def rss_feed(session: AsyncSession, source: Source) -> Feed:
    await session.refresh(source)  # the runner's rollbacks expire objects on this session
    return (
        await session.execute(
            select(Feed).where(Feed.source_id == source.id, Feed.kind == FeedKind.RSS)
        )
    ).scalar_one()


async def run_row(session: AsyncSession, run_id: int) -> IngestionRun:
    row = await session.get(IngestionRun, run_id, populate_existing=True)
    assert row is not None
    return row


# --- A normal run ----------------------------------------------------------------------


async def test_full_run_ingests_rss_and_sitemap_and_records_the_run(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher, source: Source
) -> None:
    publisher.publish(*posts(1000, 25, NOW, step=timedelta(hours=2)))
    publisher.sitemap = [Post(9999, NOW - timedelta(minutes=30))]  # only in the sitemap

    report = await ingest(db_session, fetcher)

    assert report.status is RunStatus.SUCCEEDED
    by_kind = {f.kind: f for f in report.feeds}
    rss_result = by_kind[FeedKind.RSS].result
    sitemap_result = by_kind[FeedKind.NEWS_SITEMAP].result
    assert rss_result is not None
    assert rss_result.items_new == 25
    assert sitemap_result is not None
    assert sitemap_result.items_new == 1
    assert by_kind[FeedKind.ARCHIVE_INDEX].skipped is not None
    assert [f.kind for f in report.feeds] == ["rss", "news_sitemap", "archive_index"]

    row = await run_row(db_session, report.run_id)
    assert row.status == RunStatus.SUCCEEDED
    assert (row.items_new, row.feeds_processed) == (26, 2)
    assert row.finished_at is not None
    assert row.error_class is None


async def test_no_sitemap_option_skips_the_news_sitemap(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher
) -> None:
    publisher.publish(Post(1, NOW))

    report = await ingest(db_session, fetcher, include_news_sitemap=False)

    sitemap = next(f for f in report.feeds if f.kind == FeedKind.NEWS_SITEMAP)
    assert sitemap.skipped == "news sitemap excluded (--no-sitemap)"
    assert not any("sitemap-news" in url for url in publisher.requests)


async def test_sources_without_an_adapter_are_reported_not_crashed(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher
) -> None:
    db_session.add(
        Source(
            slug="zz-unknown",
            name="Unknown",
            site_url="https://x.test",
            language="en",
            rights_policy="metadata_only",
        )
    )
    await db_session.flush()

    report = await ingest(db_session, fetcher)

    assert any(f.source == "zz-unknown" and f.skipped == "no adapter" for f in report.feeds)


# --- Failures are isolated and tracked -------------------------------------------------


async def test_one_failing_feed_does_not_stop_the_others(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher, source: Source
) -> None:
    publisher.page_status = {1: 403}
    publisher.sitemap = [Post(1, NOW)]

    report = await ingest(db_session, fetcher)

    assert report.status is RunStatus.FAILED
    [failed] = report.failed
    assert failed.kind == FeedKind.RSS
    assert failed.error is not None
    assert "HTTP 403" in failed.error
    sitemap = next(f for f in report.feeds if f.kind == FeedKind.NEWS_SITEMAP)
    assert sitemap.result is not None
    assert sitemap.result.items_new == 1

    assert (await rss_feed(db_session, source)).consecutive_failures == 1
    row = await run_row(db_session, report.run_id)
    assert row.status == RunStatus.FAILED
    assert row.error_class == "FeedErrors"
    assert row.error_detail is not None
    assert "HTTP 403" in row.error_detail


@pytest.mark.parametrize(
    ("before", "state"),
    [
        (DEGRADED_AFTER - 2, FeedState.ACTIVE),
        (DEGRADED_AFTER - 1, FeedState.DEGRADED),
        (DISABLED_AFTER - 1, FeedState.DISABLED),
    ],
)
async def test_repeated_failures_degrade_then_disable_a_feed(
    db_session: AsyncSession,
    fetcher: PoliteFetcher,
    publisher: Publisher,
    source: Source,
    before: int,
    state: FeedState,
) -> None:
    feed = await rss_feed(db_session, source)
    feed.consecutive_failures = before
    await db_session.flush()
    publisher.page_status = {1: 403}

    await ingest(db_session, fetcher)

    feed = await rss_feed(db_session, source)
    assert feed.consecutive_failures == before + 1
    assert feed.state == state


async def test_disabled_feeds_are_skipped_without_requests(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher, source: Source
) -> None:
    feed = await rss_feed(db_session, source)
    feed.state = FeedState.DISABLED
    feed.consecutive_failures = DISABLED_AFTER
    await db_session.flush()

    report = await ingest(db_session, fetcher, include_news_sitemap=False)

    rss = next(f for f in report.feeds if f.kind == FeedKind.RSS)
    assert rss.skipped is not None
    assert "disabled" in rss.skipped
    assert not any("/feed" in url for url in publisher.page_requests())


async def test_a_success_resets_feed_health(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher, source: Source
) -> None:
    feed = await rss_feed(db_session, source)
    feed.consecutive_failures = DEGRADED_AFTER + 1
    feed.state = FeedState.DEGRADED
    await db_session.flush()
    publisher.publish(Post(1, NOW))

    await ingest(db_session, fetcher)

    feed = await rss_feed(db_session, source)
    assert (feed.consecutive_failures, feed.state) == (0, FeedState.ACTIVE)


async def test_uncovered_gap_is_reported(
    db_session: AsyncSession, fetcher: PoliteFetcher, publisher: Publisher
) -> None:
    publisher.publish(*posts(1000, 5, NOW, step=timedelta(hours=1)))  # far less than 30 days

    report = await ingest(db_session, fetcher)

    rss = next(f for f in report.feeds if f.kind == FeedKind.RSS)
    assert rss.result is not None
    assert rss.result.stop_reason is StopReason.FEED_END
    assert rss.uncovered is not None


# --- Locking and interruption ------------------------------------------------------------


async def try_lock_elsewhere(engine: AsyncEngine) -> bool:
    async with engine.connect() as other:
        acquired = bool(
            await other.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY})
        )
        if acquired:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
        return acquired


async def test_a_second_concurrent_run_is_refused(
    db_session: AsyncSession, fetcher: PoliteFetcher, engine: AsyncEngine
) -> None:
    async with engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY})
        runs_before = await db_session.scalar(select(text("count(*)")).select_from(IngestionRun))

        with pytest.raises(AlreadyRunningError):
            await ingest(db_session, fetcher)

        runs_after = await db_session.scalar(select(text("count(*)")).select_from(IngestionRun))
        assert runs_after == runs_before  # nothing recorded for a refused run
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})


async def test_the_lock_is_released_after_a_run(
    db_session: AsyncSession, fetcher: PoliteFetcher, engine: AsyncEngine, publisher: Publisher
) -> None:
    publisher.publish(Post(1, NOW))

    await ingest(db_session, fetcher)

    assert await try_lock_elsewhere(engine)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), asyncio.CancelledError()])
async def test_interrupted_run_is_recorded_and_releases_the_lock(
    db_session: AsyncSession,
    fetcher: PoliteFetcher,
    engine: AsyncEngine,
    publisher: Publisher,
    source: Source,
    interruption: BaseException,
) -> None:
    publisher.publish(*posts(1000, 30, NOW, step=timedelta(hours=1)))
    publisher.raise_on_page = {2: interruption}

    with pytest.raises(type(interruption)):
        await ingest(db_session, fetcher)

    row = (
        await db_session.execute(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
    ).scalar_one()
    assert row.status == RunStatus.INTERRUPTED
    assert row.error_class == type(interruption).__name__
    assert await try_lock_elsewhere(engine)
    feed = await rss_feed(db_session, source)
    assert feed.checkpoint_external_id is None  # not advanced: the next run resumes the gap


# --- Readiness ------------------------------------------------------------------------------


async def test_unreachable_database_fails_fast_with_guidance() -> None:
    engine = create_async_engine("postgresql+psycopg://u:test-only-password@127.0.0.1:1/none")
    ticks = iter(range(100))

    async def no_sleep(_: float) -> None:
        return None

    try:
        with pytest.raises(NotReadyError, match="docker compose up -d db"):
            await wait_for_database(
                engine, timeout_s=3, clock=lambda: float(next(ticks)), sleep=no_sleep
            )
    finally:
        await engine.dispose()


async def test_reachable_database_is_ready(engine: AsyncEngine) -> None:
    await wait_for_database(engine, timeout_s=1)


async def test_migration_check_accepts_head_and_rejects_drift(engine: AsyncEngine) -> None:
    head = alembic_head(Path("alembic.ini"))

    async with engine.connect() as connection:
        await ensure_migrated(connection, head)
        with pytest.raises(NotReadyError, match="alembic upgrade head"):
            await ensure_migrated(connection, "0000deadbeef")


async def test_missing_schema_is_reported(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("CREATE TEMP TABLE probe (x int)"))
        await connection.execute(text("SET LOCAL search_path TO pg_temp"))
        with pytest.raises(NotReadyError, match="no schema yet"):
            await ensure_migrated(connection, "anything")
        await connection.rollback()


def test_missing_alembic_ini_is_reported(tmp_path: Path) -> None:
    with pytest.raises(NotReadyError, match="project root"):
        alembic_head(tmp_path / "alembic.ini")


async def test_unexpected_error_is_recorded_and_releases_the_lock(
    db_session: AsyncSession, fetcher: PoliteFetcher, engine: AsyncEngine, publisher: Publisher
) -> None:
    publisher.publish(Post(1, NOW))
    publisher.raise_on_page = {1: RuntimeError("bug")}

    with pytest.raises(RuntimeError, match="bug"):
        await ingest(db_session, fetcher)

    row = (
        await db_session.execute(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
    ).scalar_one()
    assert (row.status, row.error_class, row.error_detail) == (
        RunStatus.FAILED,
        "RuntimeError",
        "bug",
    )
    assert await try_lock_elsewhere(engine)
