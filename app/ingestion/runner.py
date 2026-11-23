"""One `news ingest` run: lock, walk every active feed, keep feed health, record the run.

Feeds fail independently: one broken feed is recorded and the others still run. The run
row in `ingestion_runs` is finalized even on Ctrl-C (status `interrupted`), and the
advisory lock is always released.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.fetch import FetchError, PoliteFetcher
from app.ingestion.parsers import FeedParseError
from app.ingestion.policy import PolicyError
from app.ingestion.sources import ADAPTERS, WordPressAdapter
from app.ingestion.walk import IngestContext, WalkError, WalkResult, ingest_news_sitemap, walk_rss
from app.models import Feed, IngestionRun, Source
from app.models.enums import FeedKind, FeedState, RunStatus

logger = logging.getLogger(__name__)

# Arbitrary but fixed: identifies "an ingest run" among Postgres advisory locks.
INGEST_LOCK_KEY = 0x6E65_7773  # "news"
DEGRADED_AFTER = 5
DISABLED_AFTER = 20
FEED_ERRORS = (FetchError, WalkError, FeedParseError, PolicyError)
_KIND_ORDER = case((Feed.kind == FeedKind.RSS, 0), (Feed.kind == FeedKind.NEWS_SITEMAP, 1), else_=2)


class AlreadyRunningError(Exception):
    """Another `news ingest` holds the lock."""


@dataclass(frozen=True)
class IngestOptions:
    bootstrap_days: int = 30
    max_pages: int = 500
    include_news_sitemap: bool = True


@dataclass
class FeedReport:
    source: str
    kind: str
    url: str
    result: WalkResult | None = None
    error: str | None = None
    skipped: str | None = None
    state: str | None = None
    uncovered: tuple[datetime, datetime] | None = None


@dataclass
class RunReport:
    run_id: int
    status: RunStatus = RunStatus.RUNNING
    feeds: list[FeedReport] = field(default_factory=list)

    @property
    def failed(self) -> list[FeedReport]:
        return [f for f in self.feeds if f.error]

    def total(self, attribute: str) -> int:
        return sum(getattr(f.result, attribute) for f in self.feeds if f.result is not None)


async def run_ingest(
    session: AsyncSession,
    fetcher: PoliteFetcher,
    *,
    now: datetime,
    options: IngestOptions,
    adapters: dict[str, WordPressAdapter] = ADAPTERS,
) -> RunReport:
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": INGEST_LOCK_KEY}
    )
    if not acquired:
        msg = "another `news ingest` is already running"
        raise AlreadyRunningError(msg)
    try:
        return await _run_locked(session, fetcher, now=now, options=options, adapters=adapters)
    finally:
        await session.rollback()  # leave no half-done transaction before unlocking
        await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": INGEST_LOCK_KEY})
        await session.commit()


async def _run_locked(
    session: AsyncSession,
    fetcher: PoliteFetcher,
    *,
    now: datetime,
    options: IngestOptions,
    adapters: dict[str, WordPressAdapter],
) -> RunReport:
    run = IngestionRun()
    session.add(run)
    await session.commit()
    report = RunReport(run_id=run.id)
    failure: BaseException | None = None
    try:
        sources = (
            await session.execute(select(Source).where(Source.active).order_by(Source.id))
        ).scalars()
        for source in list(sources):
            # A failed feed rolls the session back, which expires every loaded object; async
            # SQLAlchemy cannot lazy-load, so reload explicitly before use.
            await session.refresh(source)
            adapter = adapters.get(source.slug)
            if adapter is None:
                report.feeds.append(FeedReport(source.slug, "-", "-", skipped="no adapter"))
                continue
            ctx = IngestContext(session, fetcher, adapter, source, now)
            feeds = (
                await session.execute(
                    select(Feed).where(Feed.source_id == source.id).order_by(_KIND_ORDER, Feed.id)
                )
            ).scalars()
            for feed in list(feeds):
                report.feeds.append(await _ingest_feed(ctx, feed, options))
        report.status = RunStatus.FAILED if report.failed else RunStatus.SUCCEEDED
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        report.status = RunStatus.INTERRUPTED
        failure = exc
        raise
    except Exception as exc:
        report.status = RunStatus.FAILED
        failure = exc
        raise
    finally:
        await _finish_run(session, run, report, failure)
    return report


async def _ingest_feed(ctx: IngestContext, feed: Feed, options: IngestOptions) -> FeedReport:
    await ctx.session.refresh(feed)  # may have been expired by an earlier feed's rollback
    report = FeedReport(ctx.source.slug, feed.kind, feed.url)
    if feed.state == FeedState.DISABLED:
        report.skipped = f"disabled after {feed.consecutive_failures} consecutive failures"
        return report
    if feed.kind == FeedKind.NEWS_SITEMAP and not options.include_news_sitemap:
        report.skipped = "news sitemap excluded (--no-sitemap)"
        return report
    if feed.kind == FeedKind.ARCHIVE_INDEX:
        report.skipped = "archive backlog not implemented yet (milestone 6, deferred)"
        return report

    try:
        if feed.kind == FeedKind.RSS:
            report.result = await walk_rss(
                ctx,
                feed,
                bootstrap_since=ctx.now - timedelta(days=options.bootstrap_days),
                max_pages=options.max_pages,
            )
        else:
            report.result = await ingest_news_sitemap(ctx, feed)
    except FEED_ERRORS as exc:
        await ctx.session.rollback()
        await ctx.session.refresh(feed)
        await ctx.session.refresh(ctx.source)  # the next feed of this source needs it loaded
        feed.consecutive_failures += 1
        if feed.consecutive_failures >= DISABLED_AFTER:
            feed.state = FeedState.DISABLED
        elif feed.consecutive_failures >= DEGRADED_AFTER:
            feed.state = FeedState.DEGRADED
        report.error = f"{type(exc).__name__}: {exc}"
        logger.warning("Feed %s failed: %s", feed.url, report.error)
    else:
        feed.consecutive_failures = 0
        feed.state = FeedState.ACTIVE
    await ctx.session.commit()

    report.state = feed.state
    if feed.uncovered_since and feed.uncovered_until:
        report.uncovered = (feed.uncovered_since, feed.uncovered_until)
    return report


async def _finish_run(
    session: AsyncSession, run: IngestionRun, report: RunReport, failure: BaseException | None
) -> None:
    await session.rollback()
    await session.refresh(run)
    run.status = report.status
    run.finished_at = datetime.now(UTC)
    run.feeds_processed = sum(1 for f in report.feeds if f.result is not None)
    run.pages_fetched = report.total("pages_fetched")
    run.items_new = report.total("items_new")
    run.items_updated = report.total("items_updated")
    if failure is not None:
        run.error_class = type(failure).__name__
        run.error_detail = str(failure)[:2000] or None
    elif report.failed:
        run.error_class = "FeedErrors"
        run.error_detail = "; ".join(f"{f.url}: {f.error}" for f in report.failed)[:2000]
    await session.commit()
