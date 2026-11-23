"""Strict, hardened XML parsing for RSS 2.0 and Google News sitemaps.

`defusedxml` rejects entity expansion ("billion laughs"), external entities and DTDs.
Malformed documents raise `FeedParseError`: we would rather record a failed page than
silently ingest garbage. Parsers only extract fields; they make no rights decisions
(that is `policy.apply_policy`'s job).
"""

import email.utils
from dataclasses import dataclass
from datetime import datetime
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


class FeedParseError(Exception):
    """The document is not well-formed, is unsafe, or is not the expected format."""


@dataclass(frozen=True, slots=True)
class RssEntry:
    title: str
    link: str
    guid: str | None
    published_at: datetime | None
    creator: str | None
    categories: tuple[str, ...]
    description: str | None
    content_encoded: str | None


@dataclass(frozen=True, slots=True)
class NewsSitemapEntry:
    loc: str
    title: str
    published_at: datetime | None
    language: str | None


def _parse_xml(content: bytes) -> Element:
    try:
        return SafeET.fromstring(content)
    except (ParseError, DefusedXmlException) as exc:
        msg = f"unparseable or unsafe XML: {type(exc).__name__}: {exc}"
        raise FeedParseError(msg) from exc


def _text(element: Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def parse_rss(content: bytes) -> list[RssEntry]:
    """Items of an RSS 2.0 document, in document order. An empty channel returns []."""
    root = _parse_xml(content)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        msg = f"not an RSS 2.0 document (root <{root.tag}>)"
        raise FeedParseError(msg)

    return [
        RssEntry(
            title=_text(item.find("title")) or "",
            link=_text(item.find("link")) or "",
            guid=_text(item.find("guid")),
            published_at=_parse_rfc822(_text(item.find("pubDate"))),
            creator=_text(item.find("dc:creator", NS)),
            categories=tuple(
                text for c in item.findall("category") if (text := _text(c)) is not None
            ),
            description=_text(item.find("description")),
            content_encoded=_text(item.find("content:encoded", NS)),
        )
        for item in channel.findall("item")
    ]


def parse_news_sitemap(content: bytes) -> list[NewsSitemapEntry]:
    root = _parse_xml(content)
    if root.tag != f"{{{NS['sm']}}}urlset":
        msg = f"not a sitemap urlset (root <{root.tag}>)"
        raise FeedParseError(msg)

    entries = []
    for url in root.findall("sm:url", NS):
        loc = _text(url.find("sm:loc", NS))
        news = url.find("news:news", NS)
        if loc is None or news is None:
            continue
        entries.append(
            NewsSitemapEntry(
                loc=loc,
                title=_text(news.find("news:title", NS)) or "",
                published_at=_parse_iso8601(_text(news.find("news:publication_date", NS))),
                language=_text(news.find("news:publication/news:language", NS)),
            )
        )
    return entries


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None  # naive dates are ambiguous


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
