import logging

import pytest

from app.ingestion.robots import MAX_CRAWL_DELAY_S, RobotsPolicy, normalize_path, parse_robots

AGENT = "news-platform-api"

# Same structure as Hespress's robots.txt on 2026-11-23: rules *before* any User-agent
# line (a WordPress plugin block), then a permissive Yoast group.
HESPRESS_LIKE = """\
# XML Sitemap plugin
Sitemap: https://example.test/sitemap.xml
Sitemap: https://example.test/sitemap-news.xml

Disallow: /author/
Disallow: /wp-admin/
Disallow: /*?print
Crawl-delay: 5

# START YOAST BLOCK
User-agent: *
Disallow:
# END YOAST BLOCK
"""


@pytest.fixture
def hespress() -> RobotsPolicy:
    return parse_robots(HESPRESS_LIKE, AGENT)


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/feed", True),
        ("/feed?paged=2", True),
        ("/149695-placeholder.html", True),
        ("/sitemap-posttype-post.202611.xml", True),
        ("/author/someone", False),  # orphan rule applied, conservatively
        ("/wp-admin/", False),
        ("/149695-placeholder.html?print", False),  # wildcard; stdlib parser misses this
        ("/robots.txt", True),
    ],
)
def test_hespress_structure(hespress: RobotsPolicy, path: str, allowed: bool) -> None:
    assert hespress.can_fetch(path) is allowed


def test_orphan_crawl_delay_applies(hespress: RobotsPolicy) -> None:
    assert hespress.crawl_delay_s == 5.0


def test_sitemaps_are_collected(hespress: RobotsPolicy) -> None:
    assert hespress.sitemaps == (
        "https://example.test/sitemap.xml",
        "https://example.test/sitemap-news.xml",
    )


def test_specific_group_overrides_wildcard_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: News-Platform-API\nAllow: /\n"

    policy = parse_robots(text, AGENT)

    assert policy.can_fetch("/anything")


def test_wildcard_groups_are_merged() -> None:
    text = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"

    policy = parse_robots(text, AGENT)

    assert not policy.can_fetch("/a1")
    assert not policy.can_fetch("/b1")
    assert policy.can_fetch("/c")


def test_consecutive_user_agents_share_one_group() -> None:
    text = "User-agent: other\nUser-agent: news-platform-api\nDisallow: /private\n"

    assert not parse_robots(text, AGENT).can_fetch("/private/x")


def test_longest_match_wins_and_allow_wins_ties() -> None:
    text = "User-agent: *\nDisallow: /feed\nAllow: /feed/public\nAllow: /x\nDisallow: /x\n"
    policy = parse_robots(text, AGENT)

    assert not policy.can_fetch("/feed/private")
    assert policy.can_fetch("/feed/public/1")
    assert policy.can_fetch("/x")  # equal length: Allow wins


def test_dollar_anchors_the_end() -> None:
    policy = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", AGENT)

    assert not policy.can_fetch("/files/report.pdf")
    assert policy.can_fetch("/files/report.pdf?download=1")


def test_comments_blank_and_malformed_lines_are_ignored() -> None:
    text = "garbage line\nUser-agent: * # everyone\nDisallow: /drafts # wip\nCrawl-delay: soon\n"
    policy = parse_robots(text, AGENT)

    assert not policy.can_fetch("/drafts/x")
    assert policy.crawl_delay_s is None


def test_largest_crawl_delay_wins() -> None:
    text = "Crawl-delay: 2\nUser-agent: *\nCrawl-delay: 7\nCrawl-delay: 3\n"

    assert parse_robots(text, AGENT).crawl_delay_s == 7.0


def test_empty_file_allows_everything() -> None:
    policy = parse_robots("", AGENT)

    assert policy.can_fetch("/anything")
    assert policy.crawl_delay_s is None


def test_deny_everything_blocks_all_but_robots_txt() -> None:
    policy = RobotsPolicy.deny_everything()

    assert not policy.can_fetch("/")
    assert not policy.can_fetch("/robots.txt")


@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "-1"])
def test_crawl_delay_must_be_finite_and_positive(value: str) -> None:
    assert parse_robots(f"User-agent: *\nCrawl-delay: {value}\n", AGENT).crawl_delay_s is None


def test_huge_crawl_delay_is_capped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        policy = parse_robots("User-agent: *\nCrawl-delay: 86400\n", AGENT)

    assert policy.crawl_delay_s == MAX_CRAWL_DELAY_S == 60.0
    assert "Crawl-delay" in caplog.text


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ("/%7Euser/", "/~user/x"),  # unreserved escape decoded
        ("/~user/", "/%7euser/x"),
        ("/caf%c3%a9", "/caf%C3%A9/menu"),  # escape case ignored
        ("/café", "/caf%C3%A9"),  # raw UTF-8 in the rule
        ("/caf%C3%A9", "/café"),  # raw UTF-8 in the path
    ],
)
def test_matching_normalises_percent_encoding(rule: str, path: str) -> None:
    assert not parse_robots(f"User-agent: *\nDisallow: {rule}\n", AGENT).can_fetch(path)


def test_reserved_escapes_stay_escaped() -> None:
    assert normalize_path("/a%2fb") == "/a%2Fb"  # %2F is not "/"
    assert parse_robots("User-agent: *\nDisallow: /a/b\n", AGENT).can_fetch("/a%2Fb")
