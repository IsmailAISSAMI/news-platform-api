"""PoliteFetcher against httpx.MockTransport. No live network, no real sleeping."""

import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
import pytest

from app.core.config import Settings
from app.ingestion.fetch import (
    MAX_RETRY_AFTER_S,
    DisallowedByRobotsError,
    FetcherConfig,
    FetchFailedError,
    PoliteFetcher,
    ResponseTooLargeError,
    _parse_retry_after,
    create_http_client,
)

HOST = "https://example.test"
ROBOTS = "Disallow: /private\nCrawl-delay: 5\nUser-agent: *\nDisallow:\n"
Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeTime:
    """A clock that only moves when the fetcher sleeps."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Server:
    """Routes requests: robots.txt plus a scripted queue of responses per path."""

    robots: httpx.Response | Exception = field(
        default_factory=lambda: httpx.Response(200, text=ROBOTS)
    )
    scripts: dict[str, list[httpx.Response | Exception]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            if isinstance(self.robots, Exception):
                raise self.robots
            return self.robots
        queue = self.scripts[request.url.raw_path.decode()]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def page_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]


@pytest.fixture
def time_() -> FakeTime:
    return FakeTime()


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
async def fetcher(server: Server, time_: FakeTime) -> AsyncIterator[PoliteFetcher]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        yield PoliteFetcher(
            client,
            FetcherConfig(
                robots_agent="news-platform-api",
                max_response_bytes=1024,
                min_delay_s=1.0,
                max_attempts=3,
            ),
            clock=time_.clock,
            sleep=time_.sleep,
            rng=random.Random(0),
        )


# --- Success and conditional GET ------------------------------------------------------


async def test_200_returns_body_and_validators(server: Server, fetcher: PoliteFetcher) -> None:
    server.scripts["/feed"] = [
        httpx.Response(200, content=b"<rss/>", headers={"ETag": 'W/"a"', "Last-Modified": "x"})
    ]

    result = await fetcher.get(f"{HOST}/feed")

    assert result.ok
    assert result.content == b"<rss/>"
    assert (result.etag, result.last_modified) == ('W/"a"', "x")


async def test_conditional_headers_are_sent_and_304_reported(
    server: Server, fetcher: PoliteFetcher
) -> None:
    server.scripts["/feed"] = [httpx.Response(304, headers={"ETag": 'W/"a"'})]

    result = await fetcher.get(f"{HOST}/feed", etag='W/"a"', last_modified="Mon, 23 Nov 2026")

    assert result.not_modified
    assert result.content is None
    sent = server.page_requests()[0].headers
    assert sent["If-None-Match"] == 'W/"a"'
    assert sent["If-Modified-Since"] == "Mon, 23 Nov 2026"


async def test_non_retryable_status_is_returned_not_raised(
    server: Server, fetcher: PoliteFetcher
) -> None:
    server.scripts["/feed?paged=999"] = [httpx.Response(404)]

    result = await fetcher.get(f"{HOST}/feed?paged=999")

    assert result.status == 404
    assert len(server.page_requests()) == 1  # no retry


# --- robots.txt -----------------------------------------------------------------------


async def test_disallowed_path_is_never_requested(server: Server, fetcher: PoliteFetcher) -> None:
    with pytest.raises(DisallowedByRobotsError):
        await fetcher.get(f"{HOST}/private/page")

    assert server.page_requests() == []


async def test_robots_is_fetched_once_per_host(server: Server, fetcher: PoliteFetcher) -> None:
    server.scripts["/a"] = [httpx.Response(200, content=b"a")]
    server.scripts["/b"] = [httpx.Response(200, content=b"b")]

    await fetcher.get(f"{HOST}/a")
    await fetcher.get(f"{HOST}/b")

    assert sum(r.url.path == "/robots.txt" for r in server.requests) == 1


async def test_missing_robots_allows_everything(server: Server, fetcher: PoliteFetcher) -> None:
    server.robots = httpx.Response(404)
    server.scripts["/private/x"] = [httpx.Response(200, content=b"ok")]

    assert (await fetcher.get(f"{HOST}/private/x")).ok


@pytest.mark.parametrize(
    "robots", [httpx.Response(503), httpx.ConnectError("down")], ids=["5xx", "unreachable"]
)
async def test_unavailable_robots_disallows_everything(
    server: Server, fetcher: PoliteFetcher, robots: httpx.Response | Exception
) -> None:
    server.robots = robots

    with pytest.raises(DisallowedByRobotsError):
        await fetcher.get(f"{HOST}/feed")
    assert server.page_requests() == []


# --- Politeness -----------------------------------------------------------------------


async def test_crawl_delay_spaces_requests_to_one_host(
    server: Server, fetcher: PoliteFetcher, time_: FakeTime
) -> None:
    server.scripts["/a"] = [httpx.Response(200, content=b"a")]

    await fetcher.get(f"{HOST}/a")
    await fetcher.get(f"{HOST}/a")

    # robots.txt, then /a after 5 s (Crawl-delay beats the 1 s minimum), then /a again.
    assert time_.sleeps == [5.0, 5.0]


async def test_user_agent_identifies_us() -> None:
    settings = Settings.model_validate(
        {"database_url": "postgresql+psycopg://u:test-only-password@h/d"}
    )
    client = create_http_client(settings)
    try:
        assert client.headers["User-Agent"].startswith("news-platform-api/")
        assert "github.com/IsmailAISSAMI/news-platform-api" in client.headers["User-Agent"]
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 20.0
    finally:
        await client.aclose()

    config = FetcherConfig.from_settings(settings)
    assert config.robots_agent == "news-platform-api"
    assert config.max_response_bytes == 5 * 1024 * 1024


# --- Retries --------------------------------------------------------------------------


async def test_5xx_is_retried_with_jittered_backoff(
    server: Server, fetcher: PoliteFetcher, time_: FakeTime
) -> None:
    server.scripts["/feed"] = [httpx.Response(503), httpx.Response(502), httpx.Response(200)]

    result = await fetcher.get(f"{HOST}/feed")

    assert result.ok
    assert len(server.page_requests()) == 3
    backoffs = time_.sleeps[2::2]  # sleeps alternate: backoff, then crawl-delay spacing
    assert all(0 <= b <= 4.0 for b in backoffs)


async def test_retries_are_bounded(server: Server, fetcher: PoliteFetcher) -> None:
    server.scripts["/feed"] = [httpx.Response(500)]

    with pytest.raises(FetchFailedError, match="HTTP 500 after 3 attempts"):
        await fetcher.get(f"{HOST}/feed")
    assert len(server.page_requests()) == 3


async def test_429_honours_retry_after(
    server: Server, fetcher: PoliteFetcher, time_: FakeTime
) -> None:
    server.scripts["/feed"] = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, content=b"ok"),
    ]

    assert (await fetcher.get(f"{HOST}/feed")).ok
    assert 30.0 in time_.sleeps


async def test_excessive_retry_after_gives_up_instead_of_waiting(
    server: Server, fetcher: PoliteFetcher, time_: FakeTime
) -> None:
    server.scripts["/feed"] = [httpx.Response(429, headers={"Retry-After": "3600"})]

    with pytest.raises(FetchFailedError, match="Retry-After"):
        await fetcher.get(f"{HOST}/feed")
    assert max(time_.sleeps) < MAX_RETRY_AFTER_S


async def test_transport_errors_and_timeouts_are_retried(
    server: Server, fetcher: PoliteFetcher
) -> None:
    server.scripts["/feed"] = [
        httpx.ConnectTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.Response(200, content=b"ok"),
    ]

    assert (await fetcher.get(f"{HOST}/feed")).ok


async def test_persistent_timeouts_fail_cleanly(server: Server, fetcher: PoliteFetcher) -> None:
    server.scripts["/feed"] = [httpx.ReadTimeout("slow")]

    with pytest.raises(FetchFailedError, match="ReadTimeout"):
        await fetcher.get(f"{HOST}/feed")


# --- Size cap -------------------------------------------------------------------------


async def test_declared_oversize_body_is_rejected(server: Server, fetcher: PoliteFetcher) -> None:
    server.scripts["/big"] = [httpx.Response(200, headers={"Content-Length": "999999"})]

    with pytest.raises(ResponseTooLargeError, match="Content-Length"):
        await fetcher.get(f"{HOST}/big")


async def test_streamed_oversize_body_is_aborted_and_not_retried(
    server: Server, fetcher: PoliteFetcher
) -> None:
    async def stream() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 200

    server.scripts["/big"] = [httpx.Response(200, content=stream())]

    with pytest.raises(ResponseTooLargeError, match="body exceeds 1024"):
        await fetcher.get(f"{HOST}/big")
    assert len(server.page_requests()) == 1


# --- Retry-After parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("12", 12.0),
        ("soon", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # a past date means "now"
        ("Wed, 21 Oct 2015 07:28:00", 0.0),  # no zone: read as UTC
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert _parse_retry_after(value) == expected
