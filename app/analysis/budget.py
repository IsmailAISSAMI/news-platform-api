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
- the PER-RUN limit is a counter in this process: it counts only ANSWERED requests;
- the PER-MINUTE limits are a window in this process. They are exact only when one
  analysis process runs at a time, which `news analyze` and `news lab` enforce with the
  same Postgres advisory lock (`ANALYZE_LOCK_KEY`).

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
CEILINGS = {"per_run": 1000, "per_day": 1000, "per_minute": 20, "input_tokens_per_minute": 10_000}

Outcome = Literal["answered", "refused_busy", "failed"]


class BudgetExceededError(Exception):
    """A hard limit would be passed: the request must not be sent."""


class DailyLimitError(BudgetExceededError):
    """The daily limit is reached (counted in the database): nothing more is sent today."""


class RequestTooLargeError(BudgetExceededError):
    """This one request is over the per-minute token limit: skip it, others may fit."""


def estimate_tokens(text: str) -> int:
    """Estimate made before sending, a little above the real count.

    Measured on Gemma 4 31B (2026-11-24): about 4.4 characters per input token, so
    dividing by 4 over-estimates by about 10%. After each answer the per-minute window
    is corrected to Google's real count (`RequestBudget.correct_last_estimate`), so
    pacing stays exact.
    """
    return len(text) // 4 + 1


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

    async def reserve(self, model: str, quota_day: date, per_day: int, run_id: int | None) -> int:
        """Count one request BEFORE it is sent. Returns the reservation id.

        Raises DailyLimitError (and stores nothing) if `per_day` requests were already sent.
        """
        ...

    async def record(
        self,
        reservation_id: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Store how a reserved request ended."""
        ...

    async def totals(self, model: str, days: int) -> list[DayUsage]:
        """Usage for the last `days` quota days, oldest first (days without use are 0)."""
        ...


@dataclass
class RequestBudget:
    """Pacing and per-run limits for one process. It holds no quota state of its own."""

    per_run: int
    per_day: int
    per_minute: int
    input_tokens_per_minute: int
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    used_this_run: int = 0  # answered by Google in this process (busy and failed excluded)
    in_flight_this_run: int = 0  # admitted, not yet answered or refused
    _window: deque[tuple[float, int]] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        for name, ceiling in CEILINGS.items():
            value = getattr(self, name)
            if not 1 <= value <= ceiling:
                msg = f"{name}={value} is outside the allowed range 1..{ceiling}"
                raise ValueError(msg)

    def quota_day(self) -> date:
        return self.now().astimezone(QUOTA_ZONE).date()

    # --- per-run limit (process-local, answered requests only) ----------------------

    def check_size(self, tokens: int) -> None:
        if tokens > self.input_tokens_per_minute:
            msg = (
                f"request too large: about {tokens} input tokens, the per-minute limit is "
                f"{self.input_tokens_per_minute}"
            )
            raise RequestTooLargeError(msg)

    def open_run_slot(self) -> None:
        """Take one answered-request slot for a request about to be sent."""
        if self.used_this_run + self.in_flight_this_run >= self.per_run:
            msg = f"per-run limit reached ({self.per_run} answered requests)"
            raise BudgetExceededError(msg)
        self.in_flight_this_run += 1

    def settle_run_slot(self, *, answered: bool) -> None:
        """Release the slot; only an answered request keeps it used."""
        self.in_flight_this_run -= 1
        if answered:
            self.used_this_run += 1

    # --- per-minute pacing (process-local) -------------------------------------------

    def wait_seconds(self, tokens: int) -> float:
        """How long to wait before the per-minute limits allow this request (0 = now)."""
        current = self.monotonic()
        while self._window and current - self._window[0][0] >= WINDOW_S:
            self._window.popleft()
        if (
            not self._window
            or tokens > self.input_tokens_per_minute  # never sendable: check_size refuses
            or (
                len(self._window) < self.per_minute
                and sum(t for _, t in self._window) + tokens <= self.input_tokens_per_minute
            )
        ):
            return 0.0
        return WINDOW_S - (current - self._window[0][0]) + 0.5

    def pace(self, tokens: int) -> None:
        """Count a request in the per-minute window, as it is sent."""
        self._window.append((self.monotonic(), tokens))

    def correct_last_estimate(self, input_tokens: int | None) -> None:
        """Pace on Google's real input count when the estimate was too low."""
        if input_tokens and self._window:
            stamp, estimate = self._window[-1]
            self._window[-1] = (stamp, max(estimate, input_tokens))
