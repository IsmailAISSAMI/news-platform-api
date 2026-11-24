"""Hard limits and a usage log for the Gemini API free tier, enforced before every request.

Goal: stay FAR below Google's free-tier limits, so the project never depends on being
close to them. Google's limits for Gemma 4 31B on the owner's project (2026-11-24):
30 requests/minute, 16K input tokens/minute, 14.4K requests/day.

Our ceilings keep at least ONE THIRD of every Google limit unused, as a safety margin
(owner rule, 2026-11-24). Settings may LOWER them, never raise them; see `CEILINGS`:
- 3 ANSWERED requests per run (a test analyses at most 3 articles). Attempts that Google
  refused as busy (503) do not use this limit: Google does not count them either. They
  still count toward the daily limit, which stays the absolute stop;
- 1,000 requests per day (7% of Google's daily limit; owner rule, stricter than 2/3);
- 20 requests per minute (2/3 of Google's 30);
- 10,000 input tokens per minute (under 2/3 of Google's 16,000).

Counting rules:
- a request is counted BEFORE it is sent, so a failed or timed-out request still uses a
  slot: the log can only over-count, never under-count;
- the day is Google's quota day, which resets at midnight Pacific time;
- the log is a file outside the repository, shared by every run and locked while
  written, so two runs never both take the last slot;
- the real token counts from each answer are added afterwards, for the usage report.

Going over a per-minute limit only makes the caller wait. Going over any other limit
refuses the request, which is then never sent.
"""

import fcntl
import json
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

QUOTA_ZONE = ZoneInfo("America/Los_Angeles")
WINDOW_S = 60.0
KEEP_DAYS = 90

# Google's free-tier limits for Gemma 4 31B, read on the owner's AI Studio page.
GOOGLE_LIMITS = {"per_day": 14_400, "per_minute": 30, "input_tokens_per_minute": 16_000}
# Our hard ceilings, far below Google's. Code refuses any setting above them.
CEILINGS = {"per_run": 1000, "per_day": 1000, "per_minute": 20, "input_tokens_per_minute": 10_000}


class BudgetExceededError(Exception):
    """A hard limit would be passed: the request must not be sent."""


class RequestTooLargeError(BudgetExceededError):
    """This one request is over the per-minute token limit: skip it, others may fit."""


def estimate_tokens(text: str) -> int:
    """Estimate made before sending, a little above the real count.

    Measured on Gemma 4 31B (2026-11-24): about 4.4 characters per input token, so
    dividing by 4 over-estimates by about 10%. After each answer the per-minute window
    is corrected to Google's real count (`record_result`), so pacing stays exact.
    """
    return len(text) // 4 + 1


@dataclass(frozen=True)
class DayUsage:
    day: str
    requests: int = 0
    failed: int = 0
    busy: int = 0  # refused by Google as overloaded (part of failed)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RequestBudget:
    usage_file: Path
    per_run: int
    per_day: int
    per_minute: int
    input_tokens_per_minute: int
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    used_this_run: int = 0  # requests Google answered (busy refusals excluded)
    _window: deque[tuple[float, int]] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        for name, ceiling in CEILINGS.items():
            value = getattr(self, name)
            if not 1 <= value <= ceiling:
                msg = f"{name}={value} is outside the allowed range 1..{ceiling}"
                raise ValueError(msg)

    def quota_day(self) -> date:
        return self.now().astimezone(QUOTA_ZONE).date()

    # --- reading the log -------------------------------------------------------------

    def history(self, days: int = 7) -> list[DayUsage]:
        """Usage for the last `days` quota days, oldest first (days without use are 0)."""
        data = self._load_file()
        today = self.quota_day()
        result = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            result.append(DayUsage(day, **data["days"].get(day, {})))
        return result

    def used_today(self) -> int:
        return self.history(1)[0].requests

    # --- per-minute pacing -----------------------------------------------------------

    def wait_seconds(self, tokens: int) -> float:
        """How long to wait before the per-minute limits allow this request (0 = now)."""
        current = self.monotonic()
        while self._window and current - self._window[0][0] >= WINDOW_S:
            self._window.popleft()
        if (
            not self._window
            or tokens > self.input_tokens_per_minute  # never sendable: reserve() refuses
            or (
                len(self._window) < self.per_minute
                and sum(t for _, t in self._window) + tokens <= self.input_tokens_per_minute
            )
        ):
            return 0.0
        return WINDOW_S - (current - self._window[0][0]) + 0.5

    # --- writing the log -------------------------------------------------------------

    def reserve(self, tokens: int) -> None:
        """Count one request, or raise without counting if any hard limit would pass.

        Blocking file I/O: async callers run it in a thread.
        """
        if tokens > self.input_tokens_per_minute:
            msg = (
                f"request too large: about {tokens} input tokens, the per-minute limit is "
                f"{self.input_tokens_per_minute}"
            )
            raise RequestTooLargeError(msg)
        if self.used_this_run >= self.per_run:
            msg = f"per-run limit reached ({self.per_run} answered requests)"
            raise BudgetExceededError(msg)
        with self._locked() as (_, day):
            if day["requests"] >= self.per_day:
                msg = (
                    f"daily limit reached ({self.per_day} requests on "
                    f"{self.quota_day()}, Pacific time)"
                )
                raise BudgetExceededError(msg)
            day["requests"] += 1
        self._window.append((self.monotonic(), tokens))

    def record_result(
        self,
        *,
        ok: bool,
        input_tokens: int | None,
        output_tokens: int | None,
        busy: bool = False,
    ) -> None:
        """Add how the request ended (blocking: async callers use a thread).

        `busy`: Google refused it as overloaded; it does not use the per-run limit.
        """
        if not busy:
            self.used_this_run += 1
        with self._locked() as (_, day):
            day["failed"] += 0 if ok else 1
            day["busy"] += 1 if busy else 0
            day["input_tokens"] += input_tokens or 0
            day["output_tokens"] += output_tokens or 0
        if input_tokens and self._window:
            # Pace on the real count when the estimate was too low.
            stamp, estimate = self._window[-1]
            self._window[-1] = (stamp, max(estimate, input_tokens))

    # --- file handling ---------------------------------------------------------------

    def _load_file(self) -> dict[str, Any]:
        if not self.usage_file.exists():
            return {"days": {}}
        return _parse(self.usage_file.read_text())

    @contextmanager
    def _locked(self) -> Iterator[tuple[dict[str, Any], dict[str, int]]]:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.usage_file.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            data = _parse(handle.read())
            key = self.quota_day().isoformat()
            day = data["days"].setdefault(key, {})
            for counter in ("requests", "failed", "busy", "input_tokens", "output_tokens"):
                day.setdefault(counter, 0)
            yield data, day
            _write(handle, data, keep_from=(self.quota_day() - timedelta(days=KEEP_DAYS)))
        self.usage_file.chmod(0o600)


def _parse(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"days": {}}
    data = json.loads(text)
    if "days" not in data:  # first format (2026-11-24): {"day": ..., "count": ...}
        return {"days": {data["day"]: {"requests": int(data["count"])}}}
    return data  # type: ignore[no-any-return]  # JSON written by _write


def _write(handle: TextIO, data: dict[str, Any], *, keep_from: date) -> None:
    data["days"] = {k: v for k, v in sorted(data["days"].items()) if k >= keep_from.isoformat()}
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(data, indent=1))
