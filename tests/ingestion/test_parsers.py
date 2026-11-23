from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.ingestion.parsers import (
    FeedParseError,
    NewsSitemapEntry,
    parse_news_sitemap,
    parse_rss,
)
from app.ingestion.sources.wordpress import WordPressAdapter
from tests.ingestion.feeds import Post, news_sitemap, rss

T = datetime(2026, 11, 23, 12, 30, 16, tzinfo=UTC)


def test_parse_rss_extracts_every_field() -> None:
    [entry] = parse_rss(rss([Post(149695, T)]))

    assert entry.title == "Placeholder headline 149695"
    assert entry.link == "https://en.hespress.com/149695-placeholder-story.html"
    assert entry.guid == "https://en.hespress.com/?p=149695"
    assert entry.published_at == T
    assert entry.creator == "Synthetic Desk"
    assert entry.categories == ("Economy",)
    assert entry.description == "<p>Synthetic summary 149695.</p>"
    assert entry.content_encoded == "<p>SYNTHETIC-FULL-BODY 149695</p>"


def test_parse_rss_keeps_document_order_and_handles_empty_channels() -> None:
    posts = [Post(3, T), Post(1, T), Post(2, T)]

    assert [e.guid for e in parse_rss(rss(posts))] == [
        f"https://en.hespress.com/?p={n}" for n in (3, 1, 2)
    ]
    assert parse_rss(rss([])) == []


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (b"<rss><channel><item>", "unparseable"),
        (b"not xml at all", "unparseable"),
        (b"<feed xmlns='http://www.w3.org/2005/Atom'/>", "not an RSS 2.0"),
        (b"<rss version='2.0'/>", "not an RSS 2.0"),
    ],
)
def test_parse_rss_rejects_bad_documents(document: bytes, message: str) -> None:
    with pytest.raises(FeedParseError, match=message):
        parse_rss(document)


def test_entity_expansion_attack_is_refused() -> None:
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title></item></channel></rss>"""

    with pytest.raises(FeedParseError, match=r"unsafe|Forbidden"):
        parse_rss(bomb)


def test_external_entity_attack_is_refused() -> None:
    xxe = b"""<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&x;</title></item></channel></rss>"""

    with pytest.raises(FeedParseError):
        parse_rss(xxe)


@pytest.mark.parametrize("pub_date", ["", "yesterday", "Mon, 23 Nov 2026 12:30:16"])
def test_missing_invalid_or_zoneless_dates_become_none(pub_date: str) -> None:
    document = (
        "<rss version='2.0'><channel><item><title>t</title><link>l</link>"
        f"<pubDate>{pub_date}</pubDate></item></channel></rss>"
    ).encode()

    assert parse_rss(document)[0].published_at is None


def test_parse_news_sitemap() -> None:
    [entry] = parse_news_sitemap(news_sitemap([Post(149712, T)]))

    assert entry.loc.endswith("/149712-placeholder-story.html")
    assert entry.title == "Placeholder headline 149712"
    assert entry.published_at == T
    assert entry.language == "en"


def test_news_sitemap_skips_urls_without_news_block_and_rejects_other_roots() -> None:
    plain = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://x.test/1-a.html</loc></url></urlset>"
    )
    assert parse_news_sitemap(plain) == []

    with pytest.raises(FeedParseError, match="not a sitemap urlset"):
        parse_news_sitemap(b"<sitemapindex/>")


@pytest.mark.parametrize("bad", [b"not-a-date", b"", b"2026-11-23T12:00:00"])
def test_news_sitemap_bad_empty_or_zoneless_date_becomes_none(bad: bytes) -> None:
    document = news_sitemap([Post(1, T)]).replace(T.isoformat().encode(), bad)

    assert parse_news_sitemap(document)[0].published_at is None


# --- WordPress adapter ------------------------------------------------------------------

adapter = WordPressAdapter()


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (1, "https://en.hespress.com/feed"),
        (2, "https://en.hespress.com/feed?paged=2"),
        (50, "https://en.hespress.com/feed?paged=50"),
    ],
)
def test_page_urls(page: int, expected: str) -> None:
    assert adapter.page_url("https://en.hespress.com/feed", page) == expected


def test_page_urls_keep_existing_query_and_reject_page_zero() -> None:
    assert (
        adapter.page_url("https://x.test/feed?lang=en", 3) == "https://x.test/feed?lang=en&paged=3"
    )
    with pytest.raises(ValueError, match="pages start at 1"):
        adapter.page_url("https://x.test/feed", 0)


@pytest.mark.parametrize(
    ("guid", "link", "expected"),
    [
        ("https://x.test/?p=149695", "https://x.test/149695-a.html", "149695"),
        (None, "https://x.test/149712-b.html", "149712"),  # fallback: URL prefix
        ("https://x.test/?p=abc", "https://x.test/42-c.html", "42"),  # bad guid: fallback
        (None, "https://x.test/about-us", None),
    ],
)
def test_external_id(guid: str | None, link: str, expected: str | None) -> None:
    assert adapter.external_id(guid=guid, link=link) == expected


def test_rss_item_maps_fields_and_carries_the_body_to_the_gate() -> None:
    [entry] = parse_rss(rss([Post(7, T)]))

    item = adapter.rss_item(entry, language="en")

    assert item is not None
    assert (item.external_id, item.language, item.author) == ("7", "en", "Synthetic Desk")
    assert item.body_html is not None  # dropped later by the rights gate, not here


def test_rss_item_is_none_without_an_id() -> None:
    [entry] = parse_rss(rss([Post(7, T)]))

    no_id = replace(entry, guid=None, link="https://x.test/about")
    no_date = replace(entry, published_at=None)

    assert adapter.rss_item(no_id, language="en") is None
    assert adapter.rss_item(no_date, language="en") is None
    no_id_entry = NewsSitemapEntry(
        loc="https://x.test/about", title="t", published_at=T, language=None
    )
    assert adapter.news_sitemap_item(no_id_entry, language="en") is None
