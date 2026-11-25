"""Polite HTTP fetching for ingestion (docs/architecture.md §4.4).

Every outbound request goes through `PoliteFetcher.get`, which:

- checks robots.txt (fetched once per host per run; RFC 9309 failure semantics);
- spaces requests to the same host by max(min delay, Crawl-delay);
- sends conditional-GET validators and reports 304s;
- retries only 429, 5xx, transport errors and total timeouts, with capped exponential
  backoff + full jitter, honouring Retry-After;
- bounds each attempt by a total timeout (a slow-drip body cannot hold a run forever);
- follows redirects itself (at most MAX_REDIRECTS hops), re-checking robots.txt and
  per-host spacing on every hop, so a redirect can never reach a disallowed URL;
- streams the body and aborts past a size cap.

Non-retryable statuses (e.g. 404) are returned, not raised: for a paged feed, 404 means
"no more pages", which the caller must see.
"""

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.ingestion.robots import RobotsPolicy, parse_robots

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 120.0
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0
MAX_ROBOTS_BYTES = 512 * 1024  # RFC 9309 asks parsers to handle at least 500 KiB
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


class DisallowedByRobotsError(FetchError):
    """robots.txt forbids this URL. Never retried."""


class ResponseTooLargeError(FetchError):
    """The body exceeded the size cap. Never retried."""


class TooManyRedirectsError(FetchError):
    """More than MAX_REDIRECTS hops, or a redirect without a usable Location."""


class FetchFailedError(FetchError):
    """Retries exhausted (or Retry-After too long) on a retryable failure."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    content: bytes | None
    etag: str | None = None
    last_modified: str | None = None
    retry_after_s: float | None = None
    location: str | None = None  # absolute target of a 3xx redirect

    @property
    def not_modified(self) -> bool:
        return self.status == httpx.codes.NOT_MODIFIED

    @property
    def ok(self) -> bool:
        return self.status == httpx.codes.OK


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(
            settings.http_total_timeout_s, connect=settings.http_connect_timeout_s
        ),
        # Content redirects are followed by PoliteFetcher, hop by hop, with robots checks.
        follow_redirects=False,
        max_redirects=MAX_REDIRECTS,
    )


@dataclass(frozen=True, slots=True)
class FetcherConfig:
    robots_agent: str  # the name matched against robots.txt User-agent lines
    max_response_bytes: int
    min_delay_s: float
    max_attempts: int
    total_timeout_s: float = 20.0  # whole attempt: connect, headers and the full body

    @classmethod
    def from_settings(cls, settings: Settings) -> "FetcherConfig":
        return cls(
            robots_agent=settings.user_agent.split("/", 1)[0],
            max_response_bytes=settings.http_max_response_bytes,
            min_delay_s=settings.http_min_delay_s,
            max_attempts=settings.http_max_attempts,
            total_timeout_s=settings.http_total_timeout_s,
        )


class PoliteFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: FetcherConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._robots_agent = config.robots_agent
        self._max_bytes = config.max_response_bytes
        self._min_delay_s = config.min_delay_s
        self._max_attempts = config.max_attempts
        self._total_timeout_s = config.total_timeout_s
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311  # jitter, not cryptography
        self._robots: dict[str, RobotsPolicy] = {}
        self._last_request_at: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def get(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            result = await self._get_one(current, headers)
            if result.status not in REDIRECT_STATUSES:
                return result
            if not result.location:
                raise TooManyRedirectsError(current, f"HTTP {result.status} without Location")
            logger.info("Redirect %s -> %s", current, result.location)
            current = result.location
        raise TooManyRedirectsError(url, f"more than {MAX_REDIRECTS} redirects")

    async def _get_one(self, url: str, headers: dict[str, str]) -> FetchResult:
        """One hop: robots check, then the retry loop. Redirects are returned, not followed."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise FetchError(url, "not an absolute http(s) URL")
        host = f"{parts.scheme}://{parts.netloc}"
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        robots = await self._robots_for(host)
        if not robots.can_fetch(path):
            raise DisallowedByRobotsError(url, "disallowed by robots.txt")

        for attempt in range(1, self._max_attempts + 1):
            await self._wait_turn(host, robots)
            try:
                async with asyncio.timeout(self._total_timeout_s):
                    result = await self._request(url, headers)
            except ResponseTooLargeError:
                raise
            except (httpx.TransportError, TimeoutError) as exc:
                if attempt == self._max_attempts:
                    reason = (
                        f"total timeout of {self._total_timeout_s:g}s"
                        if isinstance(exc, TimeoutError)
                        else f"transport error: {type(exc).__name__}"
                    )
                    raise FetchFailedError(url, reason) from exc
                await self._backoff(url, attempt, retry_after=None)
                continue
            except httpx.RequestError as exc:  # not a transport problem: retrying won't help
                raise FetchError(url, f"request error: {type(exc).__name__}") from exc

            if result.status not in RETRYABLE_STATUSES:
                return result
            if attempt == self._max_attempts:
                raise FetchFailedError(url, f"HTTP {result.status} after {attempt} attempts")
            await self._backoff(url, attempt, retry_after=result.retry_after_s)

        raise AssertionError("unreachable")  # pragma: no cover

    # --- internals -----------------------------------------------------------------

    async def _request(self, url: str, headers: dict[str, str]) -> FetchResult:
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code in RETRYABLE_STATUSES:
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content=None,
                    retry_after_s=_parse_retry_after(response.headers.get("Retry-After")),
                )
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content=None,
                    location=str(response.url.join(location)) if location else None,
                )
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content=None,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
            content = await self._read_capped(url, response, self._max_bytes)
            return FetchResult(
                url=str(response.url),
                status=response.status_code,
                content=content,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )

    @staticmethod
    async def _read_capped(url: str, response: httpx.Response, max_bytes: int) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLargeError(url, f"Content-Length {declared} exceeds {max_bytes}")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(url, f"body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _wait_turn(self, host: str, robots: RobotsPolicy) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = max(self._min_delay_s, robots.crawl_delay_s or 0.0)
            last = self._last_request_at.get(host)
            if last is not None:
                remaining = delay - (self._clock() - last)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()

    async def _backoff(self, url: str, attempt: int, *, retry_after: float | None) -> None:
        if retry_after is not None:
            if retry_after > MAX_RETRY_AFTER_S:
                raise FetchFailedError(url, f"Retry-After {retry_after:.0f}s is too long")
            delay = retry_after
        else:
            ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
            delay = self._rng.uniform(0, ceiling)  # full jitter
        logger.info("Retrying %s in %.1fs (attempt %d)", url, delay, attempt + 1)
        await self._sleep(delay)

    async def _robots_for(self, host: str) -> RobotsPolicy:
        cached = self._robots.get(host)
        if cached is not None:
            return cached
        policy = await self._fetch_robots(host)
        self._robots[host] = policy
        return policy

    async def _fetch_robots(self, host: str) -> RobotsPolicy:
        """RFC 9309 §2.3.1: 4xx → no restrictions; 5xx or unreachable → assume full disallow."""
        url = f"{host}/robots.txt"
        await self._wait_turn(host, RobotsPolicy.allow_everything())
        try:
            async with (
                asyncio.timeout(self._total_timeout_s),
                # RFC 9309 §2.3.1.2: follow at least five redirects for robots.txt.
                self._client.stream("GET", url, follow_redirects=True) as response,
            ):
                if response.is_client_error:
                    return RobotsPolicy.allow_everything()
                if response.status_code != httpx.codes.OK:
                    logger.warning("robots.txt %s returned %s", url, response.status_code)
                    return RobotsPolicy.deny_everything()
                body = await self._read_capped(url, response, MAX_ROBOTS_BYTES)
        except (httpx.TransportError, httpx.TooManyRedirects, TimeoutError) as exc:
            logger.warning("robots.txt %s unavailable: %s", url, type(exc).__name__)
            return RobotsPolicy.deny_everything()
        except ResponseTooLargeError as exc:
            logger.warning("robots.txt %s unavailable: %s", url, exc)
            return RobotsPolicy.deny_everything()
        except httpx.RequestError as exc:
            raise FetchError(url, f"robots.txt request error: {type(exc).__name__}") from exc
        return parse_robots(body.decode("utf-8", errors="replace"), self._robots_agent)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())
