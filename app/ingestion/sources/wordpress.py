"""Adapter for WordPress sites (Hespress English is one).

WordPress specifics relied on (verified on en.hespress.com, 2026-11-23):
- RSS pages: `/feed` then `/feed?paged=N`, 10 items each, newest first.
- `<guid isPermaLink="false">https://site/?p=149695</guid>`: the post ID.
- Article URLs start with the post ID: `/149695-slug.html` (used when no guid).
- Post IDs are assigned at draft creation, so they are NOT in publish order.
"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from app.ingestion.parsers import NewsSitemapEntry, RssEntry
from app.ingestion.policy import FetchedItem

_ID_IN_PATH = re.compile(r"^/(\d+)-")


class WordPressAdapter:
    def page_url(self, feed_url: str, page: int) -> str:
        """Page 1 is the plain feed URL, so its conditional-GET validators stay stable."""
        if page < 1:
            msg = "pages start at 1"
            raise ValueError(msg)
        if page == 1:
            return feed_url
        parts = urlsplit(feed_url)
        query = parse_qs(parts.query)
        query["paged"] = [str(page)]
        return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))

    def external_id(self, *, guid: str | None, link: str) -> str | None:
        if guid:
            post_ids = parse_qs(urlsplit(guid).query).get("p")
            if post_ids and post_ids[0].isdigit():
                return post_ids[0]
        match = _ID_IN_PATH.match(urlsplit(link).path)
        return match.group(1) if match else None

    def rss_item(self, entry: RssEntry, *, language: str) -> FetchedItem | None:
        """None when the entry lacks what an article needs (ID or publish date)."""
        external_id = self.external_id(guid=entry.guid, link=entry.link)
        if external_id is None or entry.published_at is None or not entry.link:
            return None
        return FetchedItem(
            external_id=external_id,
            url=entry.link,
            title=entry.title,
            published_at=entry.published_at,
            language=language,
            author=entry.creator,
            categories=entry.categories,
            summary_html=entry.description,
            body_html=entry.content_encoded,  # the rights gate decides; usually dropped
        )

    def news_sitemap_item(self, entry: NewsSitemapEntry, *, language: str) -> FetchedItem | None:
        external_id = self.external_id(guid=None, link=entry.loc)
        if external_id is None or entry.published_at is None:
            return None
        return FetchedItem(
            external_id=external_id,
            url=entry.loc,
            title=entry.title,
            published_at=entry.published_at,
            language=entry.language or language,
        )
