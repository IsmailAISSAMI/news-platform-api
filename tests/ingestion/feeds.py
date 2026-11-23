"""Synthetic feed builders. Never real publisher text: this repository is public."""

from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime


@dataclass(frozen=True)
class Post:
    post_id: int
    published_at: datetime
    title: str = ""
    with_guid: bool = True
    url_override: str | None = None
    with_date: bool = True

    @property
    def url(self) -> str:
        return self.url_override or f"https://en.hespress.com/{self.post_id}-placeholder-story.html"

    @property
    def headline(self) -> str:
        return self.title or f"Placeholder headline {self.post_id}"


def rss(posts: list[Post]) -> bytes:
    items = "".join(_item(post) for post in posts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Synthetic</title><link>https://en.hespress.com/</link>
<language>en-US</language>{items}</channel></rss>""".encode()


def _item(post: Post) -> str:
    guid = (
        f'<guid isPermaLink="false">https://en.hespress.com/?p={post.post_id}</guid>'
        if post.with_guid
        else ""
    )
    return f"""
<item>
  <title>{post.headline}</title>
  <link>{post.url}</link>
  <dc:creator><![CDATA[Synthetic Desk]]></dc:creator>
  {_pub_date(post)}
  <category><![CDATA[Economy]]></category>
  {guid}
  <description><![CDATA[<p>Synthetic summary {post.post_id}.</p>]]></description>
  <content:encoded><![CDATA[<p>SYNTHETIC-FULL-BODY {post.post_id}</p>]]></content:encoded>
</item>"""


def _pub_date(post: Post) -> str:
    return f"<pubDate>{format_datetime(post.published_at)}</pubDate>" if post.with_date else ""


def news_sitemap(posts: list[Post]) -> bytes:
    urls = "".join(
        f"""
<url><loc>{post.url}</loc><news:news>
  <news:publication><news:name>Synthetic</news:name><news:language>en</news:language>
  </news:publication>
  <news:publication_date>{post.published_at.isoformat()}</news:publication_date>
  <news:title>{post.headline}</news:title>
</news:news></url>"""
        for post in posts
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{urls}</urlset>""".encode()
