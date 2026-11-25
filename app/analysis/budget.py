"""Hard limits for the Gemini API free tier, enforced before every request.

Goal: stay FAR below Google's free-tier limits, so the project never depends on being
close to them. Google's limits for Gemma 4 31B on the owner's project (2026-11-24):
30 requests/minute, 16K input tokens/minute, 14.4K requests/day.

Our ceilings keep at least ONE THIRD of every Google limit unused, as a safety margin
(owner rule, 2026-11-24). Settings may LOWER them, never raise them; see `CEILINGS`:
- 3 ANSWERED requests per run (a test analyses at most 3 articles). Attempts that Google
  refused as busy (503) do not use this limit. They still count toward the daily limit;
- 1,000 requests per day (7% of Google's daily limit; owner rule, stricter than 2/3),
  counted in the DATABASE (`api_calls`, see `Ledger`), never in a local file;
- 20 requests per minute (2/3 of Google's 30);
- 10,000 input tokens per minute (under 2/3 of Google's 16,000).

Where each limit lives:
- the DAILY limit is the absolute stop. It is enforced by the `Ledger` (database), under
  a Postgres advisory lock, so two processes can never both take the last slot. Deleting
  or copying any local file cannot change it;
- the PER-RUN limit is a counter in this process: it counts every request Google
  answered with HTTP 200, usable or not. A `news lab` run can never use more than
  `LAB_PER_RUN_CEILING` (3); `news analyze` uses its own per-run cap, at most the daily cap;
- the PER-MINUTE limits are a window in this process, SEEDED from the database at the
  first request (`Ledger.recent`: the requests and tokens of the last 60 s), so a new
  process started right after another one still waits. Only one analysis process runs at
  a time (`ANALYZE_LOCK_KEY`).

Going over a per-minute limit only makes the caller wait. Going over any other limit
refuses the request, which is then never sent.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

QUOTA_ZONE = ZoneInfo("America/Los_Angeles")
WINDOW_S = 60.0

# Google's free-tier limits for Gemma 4 31B, read on the owner's AI Studio page.
GOOGLE_LIMITS = {"per_day": 14_400, "per_minute": 30, "input_tokens_per_minute": 16_000}
# Our hard ceilings, far below Google's. Code refuses any setting above them.
CEILINGS = {
    "per_run": 1000,  # `news analyze`: never above the daily cap
    "lab_per_run": 3,  # `news lab`: 3 answered requests per test
    "per_day": 1000,
    "per_minute": 20,
    "input_tokens_per_minute": 10_000,
    "concurrency": 8,
}
LAB_PER_RUN_CEILING = CEILINGS["lab_per_run"]
# Fields of RequestBudget checked against CEILINGS.
_BUDGET_FIELDS = ("per_run", "per_day", "per_minute", "input_tokens_per_minute")
# Pause added to a per-minute wait so the oldest request has surely left the window.
PACE_PAD_S = 0.5

Outcome = Literal["answered", "refused_busy", "failed"]


class BudgetExceededError(Exception):
    """A hard limit would be passed: the request must not be sent."""


class DailyLimitError(BudgetExceededError):
    """The daily limit is reached (counted in the database): nothing more is sent today."""


class RequestTooLargeError(BudgetExceededError):
    """This one request is over the per-minute token limit: skip it, others may fit."""


def estimate_tokens(text: str) -> int:
    """Estimate made before sending, a little above the real count.

    Measured on Gemma 4 31B (2026-11-24): about 4.4 characters per input token on
    English text. Counting UTF-8 BYTES divided by 3 keeps a wide margin, also for
    accented or non-Latin text (more bytes per character, often more tokens). After each
    answer the window is corrected to Google's real count (`RequestBudget.correct`).
    """
    return len(text.encode("utf-8")) // 3 + 1


def quota_day_now() -> date:
    """Google's quota day (Pacific time) right now."""
    return datetime.now(UTC).astimezone(QUOTA_ZONE).date()


@dataclass(frozen=True)
class DayUsage:
    """One quota day of one model, as the database ledger counts it.

    `requests` is every request actually sent: answered + busy + failed. `failed` is the
    'failed' rows, which include requests still in flight (they are stored as 'failed'
    until their answer is known; see app/analysis/ledger.py). `pending` is how many of
    this process's requests are still in flight.
    """

    day: str
    requests: int = 0
    passed: int = 0
    failed: int = 0
    busy: int = 0
    pending: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class Ledger(Protocol):
    """The authoritative record of every request sent. Implemented by DatabaseLedger."""

    async def count_today(self, model: str, quota_day: date) -> int:
        """Requests sent on `quota_day`: answered + refused_busy + failed."""
        ...

    async def reserve(  # the reservation's fields
        self,
        model: str,
        quota_day: date,
        per_day: int,
        run_id: int | None,
        est_input_tokens: int | None = None,
    ) -> int:
        """Count one request BEFORE it is sent. Returns the reservation id.

        Raises DailyLimitError (and stores nothing) if `per_day` requests were already sent.
        """
        ...

    async def record(  # the outcome's fields
        self,
        reservation_id: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
        error: str | None = None,
    ) -> None:
        """Store how a reserved request ended."""
        ...

    async def recent(self, model: str, window_s: float) -> list[tuple[float, int]]:
        """Requests sent in the last `window_s` seconds, by any process.

        One (age in seconds, input tokens) pair per request, oldest first. Tokens are
        Google's count when known, else the estimate stored at reservation.
        """
        ...

    async def totals(self, model: str, days: int) -> list[DayUsage]:
        """Usage for the last `days` quota days, oldest first (days without use are 0)."""
        ...


@dataclass
class PaceHandle:
    """One request in the per-minute window; `RequestBudget.correct` fixes its tokens."""

    stamp: float
    tokens: int


@dataclass
class RequestBudget:
    """Pacing and per-run limits for one process. It holds no quota state of its own."""

    per_run: int
    per_day: int
    per_minute: int
    input_tokens_per_minute: int
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    used_this_run: int = 0  # HTTP 200 answers in this process (busy and errors excluded)
    in_flight_this_run: int = 0  # admitted, not yet answered or refused
    _window: deque[PaceHandle] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        for name in _BUDGET_FIELDS:
            value, ceiling = getattr(self, name), CEILINGS[name]
            if not 1 <= value <= ceiling:
                msg = f"{name}={value} is outside the allowed range 1..{ceiling}"
                raise ValueError(msg)

    def quota_day(self) -> date:
        return self.now().astimezone(QUOTA_ZONE).date()

    # --- per-run limit (process-local, HTTP-200 answers only) -----------------------

    def check_size(self, tokens: int) -> None:
        if tokens > self.input_tokens_per_minute:
            msg = (
                f"request too large: about {tokens} input tokens, the per-minute limit is "
                f"{self.input_tokens_per_minute}"
            )
            raise RequestTooLargeError(msg)

    def open_run_slot(self) -> None:
        """Take one per-run slot for a request about to be sent."""
        if self.used_this_run + self.in_flight_this_run >= self.per_run:
            msg = f"per-run limit reached ({self.per_run} answered requests)"
            raise BudgetExceededError(msg)
        self.in_flight_this_run += 1

    def settle_run_slot(self, *, answered: bool) -> None:
        """Release the slot; a request Google answered (HTTP 200) keeps it used."""
        self.in_flight_this_run -= 1
        if answered:
            self.used_this_run += 1

    # --- per-minute pacing (seeded from the database, then process-local) ------------

    def _expire(self) -> float:
        current = self.monotonic()
        while self._window and current - self._window[0].stamp >= WINDOW_S:
            self._window.popleft()
        return current

    def window_usage(self) -> tuple[int, int]:
        """(requests, input tokens) in the last 60 s, as this budget counts them."""
        self._expire()
        return len(self._window), sum(h.tokens for h in self._window)

    def wait_seconds(self, tokens: int) -> float:
        """How long to wait before the per-minute limits allow this request (0 = now)."""
        current = self._expire()
        if (
            not self._window
            or tokens > self.input_tokens_per_minute  # never sendable: check_size refuses
            or (
                len(self._window) < self.per_minute
                and sum(h.tokens for h in self._window) + tokens <= self.input_tokens_per_minute
            )
        ):
            return 0.0
        return WINDOW_S - (current - self._window[0].stamp) + PACE_PAD_S

    def seed(self, recent: list[tuple[float, int]]) -> None:
        """Add requests other processes sent in the last minute: (age s, tokens)."""
        current = self.monotonic()
        merged = [PaceHandle(current - age, tokens) for age, tokens in recent]
        merged += self._window
        self._window = deque(sorted(merged, key=lambda h: h.stamp))

    def pace(self, tokens: int) -> PaceHandle:
        """Count a request in the per-minute window, as it is sent."""
        handle = PaceHandle(self.monotonic(), tokens)
        self._window.append(handle)
        return handle

    def correct(self, handle: PaceHandle, input_tokens: int | None) -> None:
        """Pace THIS request on Google's real input count when the estimate was too low."""
        if input_tokens:
            handle.tokens = max(handle.tokens, input_tokens)
