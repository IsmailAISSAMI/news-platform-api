"""A simulated WordPress publisher for ingestion tests (synthetic content only)."""

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from tests.ingestion.feeds import Post, news_sitemap, rss

PAGE_SIZE = 10


@dataclass
class Publisher:
    """A WordPress site: newest-first posts, 10 per page, 404 past the last page."""

    posts: list[Post] = field(default_factory=list)
    max_pages: int | None = None  # feed depth limit (real feeds don't page forever)
    failing_pages: set[int] = field(default_factory=set)
    raise_on_page: dict[int, BaseException] = field(default_factory=dict)
    page_status: dict[int, int] = field(default_factory=dict)
    sitemap_status: int | None = None
    malformed_pages: set[int] = field(default_factory=set)
    sitemap: list[Post] = field(default_factory=list)
    sitemap_etag: str = 'W/"sitemap-v1"'
    requests: list[str] = field(default_factory=list)

    def publish(self, *posts: Post) -> None:
        self.posts = sorted([*self.posts, *posts], key=lambda p: p.published_at, reverse=True)

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911  # fake routes
        self.requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap-news.xml":
            if self.sitemap_status is not None:
                return httpx.Response(self.sitemap_status)
            if request.headers.get("If-None-Match") == self.sitemap_etag:
                return httpx.Response(304, headers={"ETag": self.sitemap_etag})
            return httpx.Response(
                200, content=news_sitemap(self.sitemap), headers={"ETag": self.sitemap_etag}
            )
        page = int(request.url.params.get("paged", "1"))
        if page in self.raise_on_page:
            raise self.raise_on_page[page]
        if page in self.page_status:
            return httpx.Response(self.page_status[page])
        if page in self.failing_pages:
            return httpx.Response(500)
        if page in self.malformed_pages:
            return httpx.Response(200, content=b"<rss><channel><item>")
        if self.max_pages is not None and page > self.max_pages:
            return httpx.Response(404)
        chunk = self.posts[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        if not chunk and page > 1:
            return httpx.Response(404)
        body = rss(chunk)
        etag = f'W/"{hashlib.sha256(body).hexdigest()[:16]}"'
        if page == 1 and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, content=body, headers={"ETag": etag})

    def page_requests(self) -> list[str]:
        return [u for u in self.requests if "robots" not in u]


def posts(first_id: int, count: int, newest: datetime, *, step: timedelta) -> list[Post]:
    """`count` posts, newest first, one every `step`. IDs deliberately NOT in date order
    (WordPress assigns IDs at draft creation), via a seeded shuffle."""
    ids = random.Random(first_id).sample(range(first_id, first_id + count), count)
    return [Post(post_id, newest - i * step) for i, post_id in enumerate(ids)]
