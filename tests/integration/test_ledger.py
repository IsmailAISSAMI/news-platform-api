"""The database quota ledger, against PostgreSQL (`news_test`, never the real database).

Rows are committed (the ledger uses its own sessions, so the per-test rollback of
`db_session` does not apply) and deleted afterwards. Tests that go through the Gemini
client use the real model name, because the client refuses any other; the test database
holds no real data.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analysis.budget import DailyLimitError, RequestBudget
from app.analysis.ledger import DatabaseLedger
from app.analysis.llm import GeminiClient, LLMUnavailableError
from app.db.session import create_session_factory
from app.models import ApiCall

pytestmark = pytest.mark.integration

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
MODEL = "gemma-4-31b-it"
OLD_DAY = date(1999, 1, 1)  # far from any real quota day


class Tiny(BaseModel):
    answer: str


@pytest.fixture
async def ledger_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield engine
    async with engine.begin() as connection:
        await connection.execute(delete(ApiCall).where(ApiCall.model.in_([MODEL, "test-model"])))


def ledger(engine: AsyncEngine) -> DatabaseLedger:
    return DatabaseLedger(create_session_factory(engine), today=lambda: OLD_DAY)


async def test_two_concurrent_reservations_at_the_daily_limit_admit_exactly_one(
    ledger_engine: AsyncEngine,
) -> None:
    model = "test-model"
    first, second = ledger(ledger_engine), ledger(ledger_engine)  # two processes
    await first.reserve(model, OLD_DAY, per_day=2, run_id=None)  # one slot used, one left

    results = await asyncio.gather(
        first.reserve(model, OLD_DAY, per_day=2, run_id=None),
        second.reserve(model, OLD_DAY, per_day=2, run_id=None),
        return_exceptions=True,
    )

    assert sum(isinstance(r, int) for r in results) == 1
    assert sum(isinstance(r, DailyLimitError) for r in results) == 1
    assert await first.count_today(model, OLD_DAY) == 2


async def test_a_local_file_cannot_raise_the_allowed_count(
    ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    model = "test-model"
    db = ledger(ledger_engine)
    usage_file = tmp_path / "gemini-usage.json"
    usage_file.write_text(json.dumps({"days": {OLD_DAY.isoformat(): {"requests": 0}}}))
    await db.reserve(model, OLD_DAY, per_day=1, run_id=None)

    usage_file.unlink()  # deleting the file changes nothing
    with pytest.raises(DailyLimitError):
        await db.reserve(model, OLD_DAY, per_day=1, run_id=None)
    usage_file.write_text(json.dumps({"days": {}}))  # nor does a reset file
    with pytest.raises(DailyLimitError):
        await db.reserve(model, OLD_DAY, per_day=1, run_id=None)
    assert await db.count_today(model, OLD_DAY) == 1


async def test_passed_never_decreases_while_requests_are_in_flight(
    ledger_engine: AsyncEngine,
) -> None:
    model = "test-model"
    db = ledger(ledger_engine)
    passed: list[int] = []

    first = await db.reserve(model, OLD_DAY, per_day=100, run_id=None)
    passed.append((await db.totals(model, 1))[0].passed)
    second = await db.reserve(model, OLD_DAY, per_day=100, run_id=None)  # in flight
    passed.append((await db.totals(model, 1))[0].passed)
    await db.record(first, "answered", 10, 5)
    passed.append((await db.totals(model, 1))[0].passed)
    await db.record(second, "failed", None, None)
    passed.append((await db.totals(model, 1))[0].passed)

    assert passed == [0, 0, 1, 1]  # never decreases, never derived by subtraction
    [today] = await db.totals(model, 1)
    assert (today.requests, today.passed, today.failed, today.busy) == (2, 1, 1, 0)
    assert (today.input_tokens, today.output_tokens) == (10, 5)


async def test_a_refused_busy_request_does_not_use_the_per_run_limit(
    ledger_engine: AsyncEngine,
) -> None:
    calls = {"n": 0}

    def chat(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"code": 503}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer": "ok"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    clock = {"t": 0.0}

    async def no_sleep(seconds: float) -> None:  # time moves on, instantly
        clock["t"] += seconds

    db = DatabaseLedger(create_session_factory(ledger_engine))
    budget = RequestBudget(
        per_run=1, per_day=1000, per_minute=10, input_tokens_per_minute=8000,
        monotonic=lambda: clock["t"],
    )  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.MockTransport(chat)) as http:
        llm = GeminiClient(
            http,
            base_url=GEMINI,
            model=MODEL,
            api_key=SecretStr("test-key"),
            budget=budget,
            ledger=db,
            sleep=no_sleep,
        )
        completion = await llm.structured(Tiny, system="s", user="u")

    assert completion.parsed == Tiny(answer="ok")
    assert budget.used_this_run == 1
    [today] = await db.totals(MODEL, 1)
    assert (today.requests, today.busy, today.passed) == (2, 1, 1)


async def test_the_daily_limit_refuses_before_anything_is_sent(
    ledger_engine: AsyncEngine,
) -> None:
    sent: list[httpx.Request] = []

    def chat(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise AssertionError("must not be sent")

    db = DatabaseLedger(create_session_factory(ledger_engine))
    budget = RequestBudget(per_run=3, per_day=2, per_minute=10, input_tokens_per_minute=8000)
    today = budget.quota_day()
    for _ in range(2):  # two requests already sent today, from any process
        reservation = await db.reserve(MODEL, today, per_day=2, run_id=None)
        await db.record(reservation, "answered", 1, 1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(chat)) as http:
        llm = GeminiClient(
            http,
            base_url=GEMINI,
            model=MODEL,
            api_key=SecretStr("test-key"),
            budget=budget,
            ledger=db,
        )
        with pytest.raises(LLMUnavailableError, match="daily limit"):
            await llm.structured(Tiny, system="s", user="u")

    assert sent == []  # zero calls reached the transport
    assert await db.count_today(MODEL, today) == 2


async def test_a_second_process_is_paced_by_the_first_ones_last_minute(
    ledger_engine: AsyncEngine,
) -> None:
    """Two budgets back to back on one database: the window survives the process."""
    first = ledger(ledger_engine)
    for _ in range(2):
        await first.reserve(MODEL, OLD_DAY, per_day=1000, run_id=None, est_input_tokens=400)
    second = ledger(ledger_engine)  # a new process: nothing in memory
    recent = await second.recent(MODEL, 60.0)
    assert [tokens for _, tokens in recent] == [400, 400]
    assert all(0 <= age < 60 for age, _ in recent)

    budget = RequestBudget(per_run=3, per_day=1000, per_minute=2, input_tokens_per_minute=8000)
    budget.seed(recent)
    assert budget.wait_seconds(10) > 50  # both slots of the minute are taken


async def test_recent_uses_googles_count_when_it_is_higher(ledger_engine: AsyncEngine) -> None:
    book = ledger(ledger_engine)
    rid = await book.reserve(MODEL, OLD_DAY, per_day=1000, run_id=None, est_input_tokens=100)
    await book.record(rid, "failed", 900, None, "cancelled")
    assert [t for _, t in await book.recent(MODEL, 60.0)] == [900]
    async with create_session_factory(ledger_engine)() as session:
        row = await session.get(ApiCall, rid)
        assert row is not None
        assert (row.error, row.est_input_tokens) == ("cancelled", 100)
