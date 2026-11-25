"""Catch-up: collect everything published since the last *completed* run.

The RSS walk pages backwards (`/feed`, `?paged=2`, ...) until it reaches the feed's
checkpoint. Two rules make it safe to interrupt at any point (docs/architecture.md §4.2):

1. Never stop merely because a known article appears. After an interrupted run, page 1
   is full of known articles while the gap lies deeper. Only the checkpoint ends a walk.
2. The checkpoint AND the page-1 validators (ETag / Last-Modified) advance only when the
   walk completes. Otherwise the next run would get `304 Not Modified` and skip the gap.

Pages are committed one by one, so an interruption keeps the work already done; the next
run re-walks the gap, and re-seen articles are no-ops.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.fetch import FetchResult, PoliteFetcher
from app.ingestion.parsers import parse_news_sitemap, parse_rss
from app.ingestion.policy import (
    FetchedItem,
    InvalidItemError,
    PolicyError,
    SourceRights,
    apply_policy,
    may_fetch,
)
from app.ingestion.sources.wordpress import WordPressAdapter
from app.ingestion.store import SaveOutcome, save_article
from app.models import Feed, Source
from app.models.enums import DiscoveryChannel

logger = logging.getLogger(__name__)

# Items this much older than the checkpoint end the walk even if the checkpoint article
# itself was deleted by the publisher (so it never shows up).
CHECKPOINT_MARGIN = timedelta(days=1)
# The checkpoint article only ends the walk if it still carries (about) the date we stored.
# A republished article jumps to the top of page 1 with a new date: stopping there would
# skip the whole gap behind it, so the walk carries on to the date floor instead.
CHECKPOINT_DATE_TOLERANCE = timedelta(hours=1)
HTTP_NOT_FOUND = 404


class StopReason(StrEnum):
    NOT_MODIFIED = "not_modified"  # page 1 returned 304: nothing new
    CHECKPOINT = "checkpoint"  # saw the checkpoint article
    DATE_FLOOR = "date_floor"  # passed checkpoint date minus margin (checkpoint deleted?)
    BOOTSTRAP_FLOOR = "bootstrap_floor"  # first run: reached the bootstrap window start
    REWALK_FLOOR = "rewalk_floor"  # --rewalk-days: re-read the requested window
    FEED_END = "feed_end"  # 404 or empty last page before the checkpoint: gap beyond the feed
    PAGE_LIMIT = "page_limit"  # safety cap hit: walk incomplete


class WalkError(Exception):
    """Unexpected HTTP status (not 200, 304 or 404), or an unexplained empty page."""


@dataclass
class WalkResult:
    stop_reason: StopReason | None = None
    pages_fetched: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    items_skipped: int = 0
    skipped_reasons: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stop_reason is not None and self.stop_reason is not StopReason.PAGE_LIMIT

    def count(self, outcome: SaveOutcome) -> None:
        if outcome is SaveOutcome.NEW:
            self.items_new += 1
        elif outcome is SaveOutcome.UPDATED:
            self.items_updated += 1
        else:
            self.items_unchanged += 1

    def skip(self, reason: str) -> None:
        self.items_skipped += 1
        self.skipped_reasons.append(reason)


@dataclass(frozen=True)
class IngestContext:
    session: AsyncSession
    fetcher: PoliteFetcher
    adapter: WordPressAdapter
    source: Source
    now: datetime

    @property
    def rights(self) -> SourceRights:
        return SourceRights.from_source(self.source)

    def ensure_allowed(self) -> None:
        if not may_fetch(self.rights, self.now):
            msg = f"source {self.source.slug} may not be fetched"
            raise PolicyError(msg)


@dataclass
class _WalkState:
    """What one walk has seen so far."""

    checkpoint_id: str | None
    floor: datetime
    floor_reason: StopReason
    checkpoint_at: datetime | None = None
    rewalk: bool = False
    page_size: int = 0  # items on page 1: a later page with fewer is the last one
    previous_count: int = 0
    result: WalkResult = field(default_factory=WalkResult)
    newest: FetchedItem | None = None
    oldest_seen: datetime | None = None
    validators: tuple[str | None, str | None] = (None, None)

    def is_checkpoint(self, item: FetchedItem) -> bool:
        return (
            self.checkpoint_id is not None
            and item.external_id == self.checkpoint_id
            and self.checkpoint_at is not None
            and item.published_at <= self.checkpoint_at + CHECKPOINT_DATE_TOLERANCE
        )

    def saw(self, item: FetchedItem) -> None:
        if self.newest is None or item.published_at > self.newest.published_at:
            self.newest = item
        if self.oldest_seen is None or item.published_at < self.oldest_seen:
            self.oldest_seen = item.published_at


async def walk_rss(
    ctx: IngestContext,
    feed: Feed,
    *,
    bootstrap_since: datetime,
    max_pages: int,
    rewalk_since: datetime | None = None,
) -> WalkResult:
    """Walk back to the checkpoint, or with `rewalk_since`, re-read a whole window.

    A re-walk ignores the checkpoint and page-1 validators (it must read pages that have
    not changed), so already-stored articles are revisited: used to fill in article text
    after a source's rights policy started allowing it. A re-walk never touches the
    checkpoint, the page-1 validators or the uncovered gap: it may be shallower than the
    gap since the checkpoint, and moving either past that unwalked gap would lose it.
    """
    ctx.ensure_allowed()
    checkpoint_at = feed.checkpoint_published_at
    if rewalk_since is not None:
        state = _WalkState(None, rewalk_since, StopReason.REWALK_FLOOR, rewalk=True)
        purpose = "re-walk"
    elif checkpoint_at is not None:
        state = _WalkState(
            feed.checkpoint_external_id,
            checkpoint_at - CHECKPOINT_MARGIN,
            StopReason.DATE_FLOOR,
            checkpoint_at=checkpoint_at,
        )
        purpose = "since the last completed run"
    else:
        state = _WalkState(None, bootstrap_since, StopReason.BOOTSTRAP_FLOOR)
        purpose = "first run"
    result = state.result
    logger.info(
        "%s: collecting back to %s (%s), one page of ~10 articles per request",
        ctx.source.slug,
        state.floor.date().isoformat(),
        purpose,
    )

    for page in range(1, max_pages + 1):
        conditional = page == 1 and rewalk_since is None
        etag, last_modified = (feed.etag, feed.last_modified) if conditional else (None, None)
        fetched = await ctx.fetcher.get(
            ctx.adapter.page_url(feed.url, page), etag=etag, last_modified=last_modified
        )
        result.pages_fetched += 1
        new_before = result.items_new
        result.stop_reason = await _process_page(ctx, state, page, fetched)
        await ctx.session.commit()  # keep finished pages even if a later one fails
        logger.info(
            "%s page %d: +%d new (%d so far), reached %s",
            ctx.source.slug,
            page,
            result.items_new - new_before,
            result.items_new,
            state.oldest_seen.date().isoformat() if state.oldest_seen else "-",
        )
        if result.stop_reason is not None:
            break
    else:
        result.stop_reason = StopReason.PAGE_LIMIT

    if result.complete:
        _complete_walk(feed, state, now=ctx.now, floor_bound=checkpoint_at or bootstrap_since)
    else:
        logger.warning(
            "Walk of %s stopped at the page limit (%d); checkpoint not advanced",
            feed.url,
            max_pages,
        )
    await ctx.session.commit()
    return result


async def _process_page(
    ctx: IngestContext, state: _WalkState, page: int, fetched: FetchResult
) -> StopReason | None:
    """Store one page's items. Returns why the walk should stop, or None to continue."""
    if fetched.not_modified:
        return StopReason.NOT_MODIFIED
    if fetched.status == HTTP_NOT_FOUND:  # WordPress: past the last page
        return StopReason.FEED_END
    if not fetched.ok or fetched.content is None:
        msg = f"HTTP {fetched.status} for {fetched.url}"
        raise WalkError(msg)

    entries = parse_rss(fetched.content)
    if page == 1:
        state.validators = (fetched.etag, fetched.last_modified)
        state.page_size = len(entries)
    previous, state.previous_count = state.previous_count, len(entries)
    if not entries:
        if page > 1 and previous >= state.page_size:
            # WordPress answers 404 past the last page; an empty page right after a full
            # one is unexplained (a broken page?) and must not end the walk as complete.
            msg = f"empty page {page} after a full page for {fetched.url}"
            raise WalkError(msg)
        return StopReason.FEED_END

    stop: StopReason | None = None
    for entry in entries:
        item = ctx.adapter.rss_item(entry, language=ctx.source.language)
        if item is None:
            state.result.skip(f"page {page}: entry without id, date or link")
            continue
        if state.is_checkpoint(item):
            stop = StopReason.CHECKPOINT
        if item.published_at < state.floor:
            stop = stop or state.floor_reason
            continue  # older than what this walk is responsible for
        if await _save(ctx, state.result, item, channel=DiscoveryChannel.RSS) is not None:
            state.saw(item)
    return stop


def _complete_walk(feed: Feed, state: _WalkState, *, now: datetime, floor_bound: datetime) -> None:
    feed.last_success_at = now
    if state.rewalk or state.result.stop_reason is StopReason.NOT_MODIFIED:
        return

    feed.etag, feed.last_modified = state.validators
    newest = state.newest
    if newest is not None and (
        feed.checkpoint_published_at is None or newest.published_at >= feed.checkpoint_published_at
    ):
        feed.checkpoint_external_id = newest.external_id
        feed.checkpoint_published_at = newest.published_at

    if state.result.stop_reason is StopReason.FEED_END:
        # The feed ran out before reaching what we already have: record the hole.
        gap_until = state.oldest_seen or now
        if gap_until > floor_bound:
            feed.uncovered_since = min(filter(None, (feed.uncovered_since, floor_bound)))
            feed.uncovered_until = max(filter(None, (feed.uncovered_until, gap_until)))
            logger.warning(
                "RSS for %s ends before the checkpoint: %s to %s left for the archive backlog",
                feed.url,
                feed.uncovered_since.isoformat(),
                feed.uncovered_until.isoformat(),
            )


async def ingest_news_sitemap(ctx: IngestContext, feed: Feed) -> WalkResult:
    """The last ~48 h. Adds articles the RSS walk missed; never overwrites RSS data."""
    ctx.ensure_allowed()
    result = WalkResult(pages_fetched=1)
    fetched = await ctx.fetcher.get(feed.url, etag=feed.etag, last_modified=feed.last_modified)
    if fetched.not_modified:
        result.stop_reason = StopReason.NOT_MODIFIED
        feed.last_success_at = ctx.now
        await ctx.session.commit()
        return result
    if not fetched.ok or fetched.content is None:
        msg = f"HTTP {fetched.status} for {fetched.url}"
        raise WalkError(msg)

    for entry in parse_news_sitemap(fetched.content):
        item = ctx.adapter.news_sitemap_item(entry, language=ctx.source.language)
        if item is None:
            result.skip("news sitemap entry without id or date")
            continue
        await _save(ctx, result, item, channel=DiscoveryChannel.NEWS_SITEMAP, insert_only=True)

    feed.etag, feed.last_modified = fetched.etag, fetched.last_modified
    feed.last_success_at = ctx.now
    result.stop_reason = StopReason.FEED_END
    await ctx.session.commit()
    return result


async def _save(
    ctx: IngestContext,
    result: WalkResult,
    item: FetchedItem,
    *,
    channel: DiscoveryChannel,
    insert_only: bool = False,
) -> SaveOutcome | None:
    try:
        decision = apply_policy(item, ctx.rights, ctx.now)
    except InvalidItemError as exc:
        result.skip(str(exc))
        return None
    if decision.downgrade_reason is not None:
        # Reason, source and URL only: never article text.
        logger.warning(
            "rights downgraded to %s for source %s (%s): %s",
            decision.effective_policy,
            ctx.source.slug,
            decision.record.canonical_url,
            decision.downgrade_reason,
        )
    try:
        async with ctx.session.begin_nested():
            outcome = await save_article(
                ctx.session,
                source_id=ctx.source.id,
                decision=decision,
                channel=channel,
                insert_only=insert_only,
            )
    except IntegrityError:
        # e.g. the same URL already stored under another ID: keep going, report it.
        result.skip(f"item {item.external_id}: conflicts with an existing article")
        return None
    result.count(outcome)
    return outcome
