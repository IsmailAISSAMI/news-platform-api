"""The database quota ledger: the authoritative count of every Gemini request sent.

Design (chosen 2026-11-25): RESERVE BEFORE SEND, FINISH AFTER.
- `reserve` runs in one transaction under `pg_advisory_xact_lock(LEDGER_LOCK_KEY)`: it
  counts today's sent requests and, if the daily limit is not reached, inserts an
  `api_calls` row. The lock is held until that transaction commits, so two processes
  can never both take the last slot. The row is committed BEFORE the request is sent.
- The row is inserted with outcome 'failed' as a placeholder, because the schema allows
  only answered / refused_busy / failed / no_send, and 'failed' is one of the outcomes
  that count toward the daily limit. `record` then UPDATEs it to the real outcome.
- Consequences, both documented: a crash or a lost connection between reserve and record
  leaves a 'failed' row, which OVER-counts (never under-counts, so the limit holds); and
  while a request is in flight, its row reads 'failed' in the database.
- `pending` (rows still in flight) is therefore known exactly only to the process that
  reserved them. Other processes see those rows as 'failed' until they are recorded.
  A real 'pending' outcome would need a new CHECK value in a migration; not done here.

`count_today` takes the same lock, so it never reads a half-finished reservation.

The daily limit is counted per model. Only one model is allowed (`FREE_MODELS`), so this is
the project-wide limit; widening the allowlist would need a global count first.
Only the reservation touches the lock; `record` and `totals` do not need it.
"""

from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.budget import DailyLimitError, DayUsage, Outcome, quota_day_now
from app.models import ApiCall

# Fixed key for the ledger's transaction-level lock ("nelg"). Distinct from the
# analysis-run lock (ANALYZE_LOCK_KEY, session-level).
LEDGER_LOCK_KEY = 0x6E65_6C67

SENT_OUTCOMES = ("answered", "refused_busy", "failed")


async def _lock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LEDGER_LOCK_KEY})


async def _count_sent(session: AsyncSession, model: str, quota_day: date) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(ApiCall)
        .where(
            ApiCall.model == model,
            ApiCall.quota_day == quota_day,
            ApiCall.outcome.in_(SENT_OUTCOMES),
        )
    )
    return int(count or 0)


class DatabaseLedger:
    """`Ledger` backed by the `api_calls` table. Needs a read-write session factory."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        today: Callable[[], date] = quota_day_now,
    ) -> None:
        self._sessions = sessions
        self._today = today
        self._open: dict[int, tuple[str, date]] = {}  # this process's reservations in flight

    async def count_today(self, model: str, quota_day: date) -> int:
        async with self._sessions.begin() as session:
            await _lock(session)
            return await _count_sent(session, model, quota_day)

    async def reserve(  # the reservation's fields
        self,
        model: str,
        quota_day: date,
        per_day: int,
        run_id: int | None,
        est_input_tokens: int | None = None,
    ) -> int:
        async with self._sessions.begin() as session:
            await _lock(session)
            sent = await _count_sent(session, model, quota_day)
            if sent >= per_day:
                msg = f"daily limit reached ({per_day} requests on {quota_day}, Pacific time)"
                raise DailyLimitError(msg)
            row = ApiCall(
                quota_day=quota_day,
                model=model,
                outcome="failed",
                run_id=run_id,
                est_input_tokens=est_input_tokens,
            )
            session.add(row)
            await session.flush()
            reservation_id = row.id
        self._open[reservation_id] = (model, quota_day)
        return reservation_id

    async def record(  # the outcome's fields
        self,
        reservation_id: int,
        outcome: Outcome,
        input_tokens: int | None,
        output_tokens: int | None,
        error: str | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(ApiCall)
                .where(ApiCall.id == reservation_id)
                .values(
                    outcome=outcome,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    error=error,
                )
            )
        if cast(Any, result).rowcount != 1:  # CursorResult; the update touches one row
            msg = f"no api_calls row {reservation_id} to record"
            raise LookupError(msg)
        self._open.pop(reservation_id, None)

    async def recent(self, model: str, window_s: float) -> list[tuple[float, int]]:
        """Requests of the last `window_s` seconds (any process), by database time.

        Ages come from the database clock, so processes never compare their own clocks.
        """
        age = func.extract("epoch", func.clock_timestamp() - ApiCall.requested_at)
        tokens = func.coalesce(func.greatest(ApiCall.input_tokens, ApiCall.est_input_tokens), 0)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(age, tokens)
                    .where(
                        ApiCall.model == model,
                        ApiCall.outcome.in_(SENT_OUTCOMES),
                        ApiCall.requested_at > func.clock_timestamp() - timedelta(seconds=window_s),
                    )
                    .order_by(ApiCall.requested_at)
                )
            ).all()
        return [(float(a), int(t)) for a, t in rows]

    async def totals(self, model: str, days: int) -> list[DayUsage]:
        today = self._today()
        first = today - timedelta(days=days - 1)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ApiCall.quota_day,
                        ApiCall.outcome,
                        func.count(),
                        func.coalesce(func.sum(ApiCall.input_tokens), 0),
                        func.coalesce(func.sum(ApiCall.output_tokens), 0),
                    )
                    .where(
                        ApiCall.model == model,
                        ApiCall.quota_day >= first,
                        ApiCall.quota_day <= today,
                    )
                    .group_by(ApiCall.quota_day, ApiCall.outcome)
                )
            ).all()
        counts: Counter[tuple[date, str]] = Counter()
        tokens_in: Counter[date] = Counter()
        tokens_out: Counter[date] = Counter()
        for quota_day, outcome, count, tin, tout in rows:
            counts[(quota_day, outcome)] += int(count)
            tokens_in[quota_day] += int(tin or 0)
            tokens_out[quota_day] += int(tout or 0)
        in_flight = Counter(day for m, day in self._open.values() if m == model)
        result: list[DayUsage] = []
        for offset in range(days):
            day = first + timedelta(days=offset)
            answered = counts[(day, "answered")]
            busy = counts[(day, "refused_busy")]
            # In-flight reservations are stored as 'failed' placeholders: report them as
            # pending, not failed, so the live counts never double-count a request.
            failed = counts[(day, "failed")] - in_flight[day]
            result.append(
                DayUsage(
                    day=day.isoformat(),
                    requests=answered + busy + failed,
                    passed=answered,
                    failed=failed,
                    busy=busy,
                    pending=in_flight[day],
                    input_tokens=tokens_in[day],
                    output_tokens=tokens_out[day],
                )
            )
        return result
