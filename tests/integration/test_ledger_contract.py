"""FakeLedger and DatabaseLedger follow the same rules (TI-002).

Each scenario runs unchanged against both ledgers. Rows are committed by DatabaseLedger,
so the fixture deletes them afterwards (test database only).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analysis.budget import DailyLimitError, Outcome
from app.analysis.ledger import DatabaseLedger
from app.db.session import create_session_factory
from app.models import ApiCall
from tests.analysis.fake_ledger import FakeLedger

pytestmark = pytest.mark.integration

MODEL = "contract-test-model"
DAY = date(1999, 1, 2)  # far from any real quota day
Ledger = FakeLedger | DatabaseLedger
Scenario = Callable[[Ledger], Awaitable[object]]
# Allowed by the api_calls CHECK but not by `Outcome`: a row both ledgers must ignore.
NO_SEND = cast(Outcome, "no_send")


@pytest.fixture
async def database_ledger(engine: AsyncEngine) -> AsyncIterator[DatabaseLedger]:
    yield DatabaseLedger(create_session_factory(engine), today=lambda: DAY)
    async with engine.begin() as connection:
        await connection.execute(delete(ApiCall).where(ApiCall.model == MODEL))


async def _both(database_ledger: DatabaseLedger, scenario: Scenario) -> None:
    fake = await scenario(FakeLedger())
    real = await scenario(database_ledger)
    assert fake == real


async def _sent_outcomes_count_and_no_send_does_not(ledger: Ledger) -> object:
    ids = [await ledger.reserve(MODEL, DAY, per_day=10, run_id=None) for _ in range(4)]
    await ledger.record(ids[0], "answered", 100, 10)
    await ledger.record(ids[1], "refused_busy", None, None)
    await ledger.record(ids[2], "failed", None, None, error="cut off")
    await ledger.record(ids[3], NO_SEND, None, None)
    return await ledger.count_today(MODEL, DAY), await ledger.count_today(MODEL, date(1999, 1, 3))


async def _an_in_flight_reservation_counts(ledger: Ledger) -> object:
    await ledger.reserve(MODEL, DAY, per_day=10, run_id=None)
    return await ledger.count_today(MODEL, DAY)


async def _the_daily_limit_refuses_the_next_reservation(ledger: Ledger) -> object:
    first = await ledger.reserve(MODEL, DAY, per_day=2, run_id=None)
    await ledger.record(first, NO_SEND, None, None)  # a no_send frees its slot
    await ledger.reserve(MODEL, DAY, per_day=2, run_id=None)
    await ledger.reserve(MODEL, DAY, per_day=2, run_id=None)
    with pytest.raises(DailyLimitError):
        await ledger.reserve(MODEL, DAY, per_day=2, run_id=None)
    return await ledger.count_today(MODEL, DAY)


async def _recent_lists_sent_requests_with_their_larger_token_count(ledger: Ledger) -> object:
    a = await ledger.reserve(MODEL, DAY, per_day=10, run_id=None, est_input_tokens=50)
    b = await ledger.reserve(MODEL, DAY, per_day=10, run_id=None, est_input_tokens=500)
    c = await ledger.reserve(MODEL, DAY, per_day=10, run_id=None, est_input_tokens=70)
    await ledger.reserve(MODEL, DAY, per_day=10, run_id=None)  # in flight, no estimate
    await ledger.record(a, "answered", 80, 5)  # real count above the estimate
    await ledger.record(b, "answered", 300, 5)  # estimate above the real count
    await ledger.record(c, NO_SEND, None, None)  # never sent: not in the window
    return [tokens for _, tokens in await ledger.recent(MODEL, 60)]


async def _record_of_an_unknown_reservation_fails(ledger: Ledger) -> object:
    with pytest.raises((LookupError, KeyError)):
        await ledger.record(987_654_321, "answered", 1, 1)
    return None


@pytest.mark.parametrize(
    "scenario",
    [
        _sent_outcomes_count_and_no_send_does_not,
        _an_in_flight_reservation_counts,
        _the_daily_limit_refuses_the_next_reservation,
        _recent_lists_sent_requests_with_their_larger_token_count,
        _record_of_an_unknown_reservation_fails,
    ],
)
async def test_fake_and_database_ledgers_agree(
    database_ledger: DatabaseLedger, scenario: Scenario
) -> None:
    await _both(database_ledger, scenario)
