"""In-memory `Ledger` for unit tests: the same rules as DatabaseLedger, no database."""

import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

from app.analysis.budget import DailyLimitError, DayUsage, Outcome
from app.analysis.ledger import SENT_OUTCOMES

TODAY = date(2026, 11, 24)


class FakeLedger:
    """In-memory `Ledger` with the same rules as DatabaseLedger (no database)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.clock = clock  # stands in for the database clock (requested_at)

    async def count_today(self, model: str, quota_day: date) -> int:
        return sum(
            1
            for r in self.rows.values()
            if r["model"] == model and r["day"] == quota_day and r["outcome"] in SENT_OUTCOMES
        )

    async def reserve(  # same signature as the Ledger protocol
        self,
        model: str,
        quota_day: date,
        per_day: int,
        run_id: int | None,
        est_input_tokens: int | None = None,
    ) -> int:
        if await self.count_today(model, quota_day) >= per_day:
            msg = f"daily limit reached ({per_day} requests on {quota_day}, Pacific time)"
            raise DailyLimitError(msg)
        rid = len(self.rows) + 1
        self.rows[rid] = {
            "model": model,
            "day": quota_day,
            "outcome": "failed",
            "in": None,
            "out": None,
            "est": est_input_tokens,
            "error": None,
            "at": self.clock(),
        }
        return rid

    async def record(  # same signature as the Ledger protocol
        self,
        reservation_id: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
        error: str | None = None,
    ) -> None:
        self.rows[reservation_id].update(
            outcome=outcome, error=error, **{"in": input_tokens, "out": output_tokens}
        )

    async def recent(self, model: str, window_s: float) -> list[tuple[float, int]]:
        now = self.clock()
        return [
            (now - r["at"], max(r["in"] or 0, r["est"] or 0))
            for r in sorted(self.rows.values(), key=lambda r: r["at"])
            if r["model"] == model and r["outcome"] in SENT_OUTCOMES and now - r["at"] < window_s
        ]

    async def totals(self, model: str, days: int) -> list[DayUsage]:
        counts: Counter[str] = Counter(
            r["outcome"] for r in self.rows.values() if r["model"] == model
        )
        tokens_in = sum(r["in"] or 0 for r in self.rows.values() if r["model"] == model)
        tokens_out = sum(r["out"] or 0 for r in self.rows.values() if r["model"] == model)
        answered, busy, failed = counts["answered"], counts["refused_busy"], counts["failed"]
        return [
            DayUsage(
                day=TODAY.isoformat(),
                requests=answered + busy + failed,
                passed=answered,
                failed=failed,
                busy=busy,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        ]
