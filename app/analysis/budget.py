"""Hard request limits for a free-tier API, enforced in code before every request.

A request is counted BEFORE it is sent, so a failed or timed-out request still uses a
slot: the counter can only over-count, never under-count. The daily count is saved to a
file outside the repository, so separate runs share it. Google resets free-tier daily
quotas at midnight Pacific time, so the day is computed in that zone.

Limits (all must hold, or the request is refused and never sent):
- per run: a test never sends more than `per_run` requests;
- per day: never more than `per_day` requests (set below the provider's own limit);
- per minute: requests and estimated input tokens in a sliding 60 s window. Exceeding
  these only makes the caller wait; they never fail.
"""

import fcntl
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

QUOTA_ZONE = ZoneInfo("America/Los_Angeles")
WINDOW_S = 60.0


class BudgetExceededError(Exception):
    """A hard limit would be passed: the request must not be sent."""


def estimate_tokens(text: str) -> int:
    """Conservative estimate (about 3 characters per token, real English is nearer 4)."""
    return len(text) // 3 + 1


@dataclass
class RequestBudget:
    usage_file: Path
    per_run: int
    per_day: int
    per_minute: int
    input_tokens_per_minute: int
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    used_this_run: int = 0
    _window: deque[tuple[float, int]] = field(default_factory=deque, init=False)

    def quota_day(self) -> date:
        return self.now().astimezone(QUOTA_ZONE).date()

    def used_today(self) -> int:
        if not self.usage_file.exists():
            return 0
        data = json.loads(self.usage_file.read_text())
        return int(data["count"]) if data.get("day") == self.quota_day().isoformat() else 0

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

    def reserve(self, tokens: int) -> None:
        """Count one request, or raise without counting if any hard limit would pass.

        Blocking file I/O: async callers run it in a thread.
        """
        if tokens > self.input_tokens_per_minute:
            msg = (
                f"request too large: about {tokens} input tokens, the per-minute limit is "
                f"{self.input_tokens_per_minute}"
            )
            raise BudgetExceededError(msg)
        if self.used_this_run >= self.per_run:
            msg = f"per-run limit reached ({self.per_run} requests)"
            raise BudgetExceededError(msg)
        self.usage_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.usage_file.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)  # separate runs never both take the last slot
            handle.seek(0)
            text = handle.read()
            data = json.loads(text) if text.strip() else {}
            day = self.quota_day().isoformat()
            count = int(data["count"]) if data.get("day") == day else 0
            if count >= self.per_day:
                msg = f"daily limit reached ({self.per_day} requests on {day}, Pacific time)"
                raise BudgetExceededError(msg)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"day": day, "count": count + 1}))
        self.used_this_run += 1
        self._window.append((self.monotonic(), tokens))
