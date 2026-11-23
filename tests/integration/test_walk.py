"""The catch-up walk against a simulated WordPress publisher and the real database.

Scenarios from docs/architecture.md §4.2/§10: first run, nothing new, normal catch-up,
interrupted run, deleted checkpoint, downtime beyond the feed's depth, page limit,
duplicates across feed and sitemap, malformed XML, invalid items, rights enforcement.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.fetch import FetchFailedError, PoliteFetcher
from app.ingestion.parsers import FeedParseError
from app.ingestion.policy import FetchedItem, PolicyError, SourceRights, apply_policy
from app.ingestion.sources.wordpress import WordPressAdapter
from app.ingestion.store import SaveOutcome, save_article
from app.ingestion.walk import (
    IngestContext,
    StopReason,
    WalkError,
    WalkResult,
    ingest_news_sitemap,
    walk_rss,
)
from app.models import Article, ArticleBody, Feed, Source
from app.models.enums import DiscoveryChannel, FeedKind, RightsPolicy
from tests.ingestion.feeds import Post
from tests.ingestion.publisher import Publisher, posts

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


async def feed_of(session: AsyncSession, source: Source, kind: FeedKind) -> Feed:
    return (
        await session.execute(select(Feed).where(Feed.source_id == source.id, Feed.kind == kind))
    ).scalar_one()


@dataclass
class Runner:
    session: AsyncSession
    fetcher: PoliteFetcher
    source: Source

    def ctx(self, now: datetime = NOW) -> IngestContext:
        return IngestContext(self.session, self.fetcher, WordPressAdapter(), self.source, now)

    async def rss(self, *, now: datetime = NOW, days: int = 30, max_pages: int = 100) -> WalkResult:
        feed = await feed_of(self.session, self.source, FeedKind.RSS)
        return await walk_rss(
            self.ctx(now), feed, bootstrap_since=now - timedelta(days=days), max_pages=max_pages
        )

    async def stored_ids(self) -> set[str]:
        rows = await self.session.execute(
            select(Article.external_id).where(Article.source_id == self.source.id)
        )
        return set(rows.scalars())

    async def feed(self) -> Feed:
        return await feed_of(self.session, self.source, FeedKind.RSS)


@pytest.fixture
def run(db_session: AsyncSession, fetcher: PoliteFetcher, source: Source) -> Runner:
    return Runner(db_session, fetcher, source)


# --- First run, nothing new, normal catch-up -------------------------------------------


async def test_first_run_bootstraps_only_the_requested_window(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 60, NOW, step=timedelta(hours=3)))  # 7.5 days

    result = await run.rss(days=3)

    assert result.stop_reason is StopReason.BOOTSTRAP_FLOOR
    assert result.items_new == 25  # 3 days / 3 h, plus the item exactly on the boundary
    feed = await run.feed()
    assert feed.checkpoint_published_at == NOW
    assert feed.etag is not None


async def test_second_run_with_nothing_new_costs_one_304(publisher: Publisher, run: Runner) -> None:
    publisher.publish(*posts(1000, 30, NOW, step=timedelta(hours=1)))
    await run.rss()
    publisher.requests.clear()

    result = await run.rss(now=NOW + timedelta(hours=1))

    assert result.stop_reason is StopReason.NOT_MODIFIED
    assert result.items_new == 0
    assert len(publisher.page_requests()) == 1


async def test_catch_up_fetches_exactly_what_was_missed(publisher: Publisher, run: Runner) -> None:
    publisher.publish(*posts(1000, 30, NOW, step=timedelta(hours=1)))
    await run.rss()
    publisher.publish(*posts(5000, 15, NOW + timedelta(hours=15), step=timedelta(hours=1)))

    result = await run.rss(now=NOW + timedelta(hours=16))

    assert result.stop_reason is StopReason.CHECKPOINT
    assert result.items_new == 15
    assert result.pages_fetched == 2
    assert len(await run.stored_ids()) == 45
    assert (await run.feed()).checkpoint_published_at == NOW + timedelta(hours=15)


async def test_repeated_runs_are_idempotent(publisher: Publisher, run: Runner) -> None:
    publisher.publish(*posts(1000, 25, NOW, step=timedelta(hours=1)))
    await run.rss()
    feed = await run.feed()
    feed.etag = None  # force a full re-read of known pages
    feed.last_modified = None

    result = await run.rss()

    assert result.items_new == 0
    assert result.items_updated == 0
    assert len(await run.stored_ids()) == 25


# --- Interruption and deleted checkpoint -------------------------------------------------


async def test_interrupted_run_keeps_progress_and_next_run_completes_the_gap(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 20, NOW, step=timedelta(hours=1)))
    await run.rss()
    old_feed = await run.feed()
    old_checkpoint, old_etag = old_feed.checkpoint_external_id, old_feed.etag

    publisher.publish(*posts(5000, 35, NOW + timedelta(hours=35), step=timedelta(hours=1)))
    publisher.failing_pages = {3}
    later = NOW + timedelta(hours=36)

    with pytest.raises(FetchFailedError):
        await run.rss(now=later)

    feed = await run.feed()
    assert feed.checkpoint_external_id == old_checkpoint  # not advanced
    assert feed.etag == old_etag  # not advanced: next run must not get a 304
    assert len(await run.stored_ids()) == 20 + 20  # pages 1-2 were kept

    publisher.failing_pages = set()
    result = await run.rss(now=later)

    assert result.stop_reason is StopReason.CHECKPOINT
    assert len(await run.stored_ids()) == 20 + 35  # the whole gap, no duplicates
    assert (await run.feed()).checkpoint_published_at == NOW + timedelta(hours=35)


async def test_deleted_checkpoint_article_stops_at_the_date_floor(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 40, NOW, step=timedelta(hours=1)))
    await run.rss()
    checkpoint_id = (await run.feed()).checkpoint_external_id
    publisher.posts = [p for p in publisher.posts if str(p.post_id) != checkpoint_id]
    publisher.publish(*posts(5000, 5, NOW + timedelta(hours=5), step=timedelta(hours=1)))

    result = await run.rss(now=NOW + timedelta(hours=6))

    assert result.stop_reason is StopReason.DATE_FLOOR
    assert result.items_new == 5
    assert result.pages_fetched <= 5  # bounded: 1 day of margin, not the whole feed


# --- Downtime beyond the feed and page limits --------------------------------------------


async def test_downtime_beyond_feed_depth_records_the_uncovered_gap(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 10, NOW, step=timedelta(days=4)))  # spans 36 days
    first = await run.rss()
    assert first.stop_reason is StopReason.BOOTSTRAP_FLOOR  # so no gap from the first run
    publisher.max_pages = 3  # the feed only reaches 30 items back
    newest = NOW + timedelta(days=5)
    publisher.publish(*posts(5000, 100, newest, step=timedelta(hours=1)))

    result = await run.rss(now=newest + timedelta(hours=1))

    assert result.stop_reason is StopReason.FEED_END
    assert result.items_new == 30
    feed = await run.feed()
    assert feed.checkpoint_published_at == newest  # advanced: the gap is tracked instead
    assert feed.uncovered_since == NOW
    assert feed.uncovered_until == newest - timedelta(hours=29)


async def test_first_run_on_a_short_feed_records_the_bootstrap_gap(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 10, NOW, step=timedelta(hours=1)))  # only 10 h of feed

    result = await run.rss(days=30)

    assert result.stop_reason is StopReason.FEED_END
    feed = await run.feed()
    assert feed.uncovered_since == NOW - timedelta(days=30)
    assert feed.uncovered_until == NOW - timedelta(hours=9)


async def test_page_limit_leaves_the_checkpoint_untouched(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 10, NOW, step=timedelta(hours=1)))
    await run.rss()
    before = (await run.feed()).checkpoint_external_id
    publisher.publish(*posts(5000, 50, NOW + timedelta(hours=50), step=timedelta(hours=1)))

    result = await run.rss(now=NOW + timedelta(hours=51), max_pages=2)

    assert result.stop_reason is StopReason.PAGE_LIMIT
    assert not result.complete
    assert (await run.feed()).checkpoint_external_id == before


# --- Data quality --------------------------------------------------------------------------


async def test_malformed_page_fails_loudly_and_keeps_earlier_pages(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 30, NOW, step=timedelta(hours=1)))
    publisher.malformed_pages = {2}

    with pytest.raises(FeedParseError):
        await run.rss()

    assert len(await run.stored_ids()) == 10
    assert (await run.feed()).checkpoint_external_id is None


async def test_invalid_items_are_skipped_and_reported(publisher: Publisher, run: Runner) -> None:
    publisher.publish(
        Post(1, NOW),
        Post(2, NOW - timedelta(hours=1), title=" "),  # no title
        Post(3, NOW - timedelta(hours=2), with_guid=False),  # ID still found in the URL
    )

    result = await run.rss()

    assert result.items_new == 2
    assert result.items_skipped == 1
    assert "no title" in result.skipped_reasons[0]


async def test_no_body_or_excerpt_is_stored_for_hespress(publisher: Publisher, run: Runner) -> None:
    publisher.publish(*posts(1000, 5, NOW, step=timedelta(hours=1)))

    await run.rss()

    articles = (await run.session.execute(select(Article))).scalars().all()
    assert articles
    assert all(a.excerpt is None for a in articles)
    for article in articles:
        values = " ".join(str(getattr(article, c.key)) for c in Article.__table__.columns)
        assert "SYNTHETIC-FULL-BODY" not in values


async def test_disabled_source_is_never_fetched(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    source.rights_policy = "disabled"

    with pytest.raises(PolicyError):
        await run.rss()
    assert publisher.page_requests() == []


# --- News sitemap ----------------------------------------------------------------------------


async def test_news_sitemap_adds_missing_articles_without_overwriting_rss(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    shared, sitemap_only = Post(1, NOW), Post(2, NOW - timedelta(hours=1))
    publisher.publish(shared)
    await run.rss()
    publisher.sitemap = [shared, sitemap_only]

    sitemap_feed = await feed_of(run.session, source, FeedKind.NEWS_SITEMAP)
    result = await ingest_news_sitemap(run.ctx(), sitemap_feed)

    assert (result.items_new, result.items_unchanged) == (1, 1)
    rss_row = await run.session.scalar(select(Article).where(Article.external_id == "1"))
    assert rss_row is not None
    assert rss_row.author == "Synthetic Desk"  # not wiped by the thinner sitemap entry
    assert rss_row.first_seen_via == DiscoveryChannel.RSS


async def test_rss_enriches_an_article_first_seen_in_the_sitemap(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    post = Post(1, NOW)
    publisher.sitemap = [post]
    await ingest_news_sitemap(run.ctx(), await feed_of(run.session, source, FeedKind.NEWS_SITEMAP))
    publisher.publish(post)

    result = await run.rss()

    assert result.items_updated == 1
    row = await run.session.scalar(select(Article).where(Article.external_id == "1"))
    assert row is not None
    assert row.author == "Synthetic Desk"
    assert row.categories == ["Economy"]
    assert row.first_seen_via == DiscoveryChannel.NEWS_SITEMAP
    assert await run.session.scalar(select(func.count()).select_from(Article)) == 1


async def test_unchanged_news_sitemap_returns_304(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    publisher.sitemap = [Post(1, NOW)]
    sitemap_feed = await feed_of(run.session, source, FeedKind.NEWS_SITEMAP)
    await ingest_news_sitemap(run.ctx(), sitemap_feed)

    assert sitemap_feed.etag == publisher.sitemap_etag
    result = await ingest_news_sitemap(run.ctx(), sitemap_feed)

    assert result.stop_reason is StopReason.NOT_MODIFIED
    assert result.items_new == 0


# --- Error paths -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 410])
async def test_unexpected_status_aborts_the_walk(
    publisher: Publisher, run: Runner, status: int
) -> None:
    publisher.publish(*posts(1000, 20, NOW, step=timedelta(hours=1)))
    publisher.page_status = {2: status}

    with pytest.raises(WalkError, match=f"HTTP {status}"):
        await run.rss()
    assert (await run.feed()).checkpoint_external_id is None


async def test_empty_first_page_ends_the_walk(publisher: Publisher, run: Runner) -> None:
    result = await run.rss()

    assert result.stop_reason is StopReason.FEED_END
    assert result.items_new == 0


async def test_entries_without_a_date_or_id_are_skipped(publisher: Publisher, run: Runner) -> None:
    publisher.publish(
        Post(1, NOW),
        Post(2, NOW - timedelta(hours=1), with_date=False),
        Post(3, NOW - timedelta(hours=2), with_guid=False, url_override="https://x.test/about"),
    )

    result = await run.rss()

    assert result.items_new == 1
    assert result.items_skipped == 2


async def test_url_already_stored_under_another_id_is_skipped_not_fatal(
    publisher: Publisher, run: Runner
) -> None:
    same_url = "https://en.hespress.com/1-placeholder-story.html"
    publisher.publish(
        Post(1, NOW),
        Post(2, NOW - timedelta(hours=1), url_override=same_url),
        Post(3, NOW - timedelta(hours=2)),
    )

    result = await run.rss()

    assert result.items_new == 2
    assert result.items_skipped == 1
    assert "conflicts with an existing article" in result.skipped_reasons[0]


async def test_news_sitemap_error_status_raises(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    publisher.sitemap_status = 403

    with pytest.raises(WalkError, match="HTTP 403"):
        await ingest_news_sitemap(
            run.ctx(), await feed_of(run.session, source, FeedKind.NEWS_SITEMAP)
        )


async def test_news_sitemap_skips_entries_without_an_id(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    publisher.sitemap = [Post(1, NOW), Post(2, NOW, url_override="https://x.test/about")]

    result = await ingest_news_sitemap(
        run.ctx(), await feed_of(run.session, source, FeedKind.NEWS_SITEMAP)
    )

    assert (result.items_new, result.items_skipped) == (1, 1)


async def test_news_sitemap_refuses_disabled_sources(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    source.active = False

    with pytest.raises(PolicyError):
        await ingest_news_sitemap(
            run.ctx(), await feed_of(run.session, source, FeedKind.NEWS_SITEMAP)
        )
    assert publisher.page_requests() == []


async def test_bodies_are_stored_privately_and_edits_update_them(
    db_session: AsyncSession, source: Source
) -> None:
    licensed = SourceRights(RightsPolicy.LICENSED_FULL_TEXT, 0, "LIC-1", None, active=True)

    async def save(body: str) -> SaveOutcome:
        item = FetchedItem("9", "https://x.test/9-a.html", "Title", NOW, "en", body_html=body)
        return await save_article(
            db_session,
            source_id=source.id,
            decision=apply_policy(item, licensed, NOW),
            channel=DiscoveryChannel.RSS,
        )

    assert await save("<p>first</p>") is SaveOutcome.NEW
    assert await save("<p>first</p>") is SaveOutcome.UNCHANGED
    assert await save("<p>corrected</p>") is SaveOutcome.UPDATED

    article = await db_session.scalar(select(Article).where(Article.external_id == "9"))
    assert article is not None
    body = await db_session.get(ArticleBody, article.id, populate_existing=True)
    assert body is not None
    assert body.body_text == "corrected"


async def test_hespress_walk_stores_bodies_only_in_the_private_table(
    publisher: Publisher, run: Runner
) -> None:
    publisher.publish(*posts(1000, 5, NOW, step=timedelta(hours=1)))

    await run.rss()

    bodies = (await run.session.execute(select(ArticleBody.body_text))).scalars().all()
    assert len(bodies) == 5
    assert all(b.startswith("SYNTHETIC-FULL-BODY") for b in bodies)


async def test_rewalk_fills_in_bodies_for_articles_stored_without_them(
    publisher: Publisher, run: Runner, source: Source
) -> None:
    publisher.publish(*posts(1000, 25, NOW, step=timedelta(hours=1)))
    source.rights_policy = "metadata_only"  # as before the owner's decision
    await run.rss()
    assert (await run.session.scalar(select(func.count()).select_from(ArticleBody))) == 0
    checkpoint = (await run.feed()).checkpoint_published_at

    source.rights_policy = "restricted"
    normal = await run.rss()  # an ordinary run sees a 304 and does nothing
    assert normal.stop_reason is StopReason.NOT_MODIFIED

    feed = await run.feed()
    result = await walk_rss(
        run.ctx(), feed, bootstrap_since=NOW, max_pages=50, rewalk_since=NOW - timedelta(days=2)
    )

    assert result.stop_reason is StopReason.FEED_END  # read the whole (short) feed
    assert result.items_updated == 25  # bodies added to existing articles
    assert (await run.session.scalar(select(func.count()).select_from(ArticleBody))) == 25
    assert (await run.feed()).checkpoint_published_at == checkpoint  # not moved backwards
