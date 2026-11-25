"""`news analyze` against the test database and a simulated Gemini API.

The pipeline commits per article from several sessions, so these tests commit real rows
in `news_test` (never the real database) and delete them afterwards. Synthetic text only.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis import lab, pipeline
from app.analysis.budget import RequestBudget
from app.analysis.llm import GeminiClient
from app.analysis.pipeline import (
    COMPACT_INSTRUCTION,
    MAX_ATTEMPTS,
    AlreadyAnalyzingError,
    AnalyzeOptions,
    analyzer_version,
    pending_article_ids,
    run_analyze,
)
from app.db.session import create_session_factory
from app.models import Article, ArticleAnalysis, ArticleBody, Source
from app.models.analysis_run import AnalysisRun
from tests.analysis.fake_ledger import FakeLedger

pytestmark = pytest.mark.integration

MODEL = "gemma-4-31b-it"
BODY = "The ministry announced the alert system on Monday in Rabat."
QUOTE = {"paragraph": 1, "quote": "announced the alert system on Monday"}
VALID = json.dumps(
    {
        "overview": {"headline": "h", "report_type": "new_event", "event_status": "completed"},
        "what": {"main_event": "x"},
        "who": [],
        "when": {"event_date": {"value": "2026-11-23", "evidence": QUOTE}},
        "where": [],
        "claims": [],
        "how": None,
        "topics": [],
        "tone": {"label": "neutral", "explanation": "x"},
    }
)
PREFIX = f"pipeline-test-{uuid.uuid4().hex[:8]}-"  # unique per process: no clash with other runs
Chat = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def ok(content: str = VALID) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    sessions = create_session_factory(engine)
    started = datetime.now(UTC)
    yield sessions
    async with sessions() as session:  # remove this test's rows (analyses cascade)
        await session.execute(delete(Article).where(Article.external_id.like(f"{PREFIX}%")))
        await session.execute(delete(AnalysisRun).where(AnalysisRun.started_at >= started))
        await session.commit()


def answer(quote: str) -> str:
    """The valid answer, with its one evidence quote replaced."""
    data = json.loads(VALID)
    data["when"]["event_date"]["evidence"] = {"paragraph": 1, "quote": quote}
    return json.dumps(data)


async def unverified(sessions: async_sessionmaker[AsyncSession], article_id: int) -> object:
    async with sessions() as session:
        return await session.scalar(
            select(ArticleAnalysis.unverified_paths).where(ArticleAnalysis.article_id == article_id)
        )


async def seed(sessions: async_sessionmaker[AsyncSession], count: int) -> list[int]:
    async with sessions() as session:
        source = (
            await session.execute(select(Source).where(Source.slug == "hespress-en"))
        ).scalar_one()
        ids = []
        for n in range(count):
            article = Article(
                source_id=source.id,
                external_id=f"{PREFIX}{n}",
                canonical_url=f"https://en.hespress.com/{PREFIX}{n}.html",
                title=f"Synthetic headline {n}",
                language="en",
                categories=[],
                # Far in the future: always first in "newest first", whatever real rows exist.
                published_at=datetime(2099, 1, 1, tzinfo=UTC) + timedelta(hours=n),
                first_seen_via="rss",
                content_hash=f"{PREFIX}{n}",
            )
            session.add(article)
            await session.flush()
            session.add(ArticleBody(article_id=article.id, body_text=BODY, body_hash="h"))
            ids.append(article.id)
        await session.commit()
        return ids


def gemini(
    tmp_path: Path, chat: Chat, per_day: int = 1000, per_run: int | None = None
) -> GeminiClient:
    clock = [0.0]  # simulated time: a pacing wait passes instantly, but it does pass

    async def no_sleep(seconds: float) -> None:
        clock[0] += max(seconds, 0.0)
        await asyncio.sleep(0)

    return GeminiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(chat)),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model=MODEL,
        api_key=SecretStr("test-key"),
        budget=RequestBudget(
            per_run=per_run or per_day,
            per_day=per_day,
            per_minute=20,
            input_tokens_per_minute=10_000,
            monotonic=lambda: clock[0],
        ),
        ledger=FakeLedger(),
        sleep=no_sleep,
    )


async def statuses(
    sessions: async_sessionmaker[AsyncSession], ids: list[int]
) -> dict[int, tuple[str, int]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                ArticleAnalysis.article_id, ArticleAnalysis.status, ArticleAnalysis.attempts
            ).where(ArticleAnalysis.article_id.in_(ids))
        )
        return {article_id: (status, attempts) for article_id, status, attempts in rows}


async def test_articles_are_analysed_in_parallel_and_never_twice(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ids = await seed(factory, 6)
    in_flight = {"now": 0, "max": 0}

    three_in_flight = asyncio.Event()

    async def chat(_: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if in_flight["now"] == 3:
            three_in_flight.set()
        with contextlib.suppress(TimeoutError):  # "thinking" until three are in flight
            await asyncio.wait_for(three_in_flight.wait(), timeout=2)
        in_flight["now"] -= 1
        return ok()

    report = await run_analyze(
        factory, gemini(tmp_path, chat), AnalyzeOptions(concurrency=3, limit=6)
    )

    assert report.succeeded == 6
    assert in_flight["max"] == 3  # parallel, and never more than asked
    assert set((await statuses(factory, ids)).values()) == {("succeeded", 1)}
    async with factory() as session:
        pending = await pending_article_ids(session, analyzer_version(MODEL), limit=6)
    assert not set(pending) & set(ids)  # a second run would not send them again


async def test_unusable_answers_are_retried_then_left_for_retry_failed(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)
    cut_off = httpx.Response(
        200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}
    )

    async def chat(_: httpx.Request) -> httpx.Response:
        return cut_off

    report = await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    assert (report.retried, report.failed) == (MAX_ATTEMPTS - 1, 1)
    assert (await statuses(factory, [article_id]))[article_id] == ("failed", MAX_ATTEMPTS)
    async with factory() as session:
        version = analyzer_version(MODEL)
        assert article_id not in await pending_article_ids(session, version, limit=5)
        assert article_id in await pending_article_ids(session, version, retry_failed=True, limit=5)


async def test_a_stop_keeps_the_remaining_articles_pending(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ids = await seed(factory, 4)
    sent = {"n": 0}

    async def chat(_: httpx.Request) -> httpx.Response:
        sent["n"] += 1
        return ok() if sent["n"] == 1 else httpx.Response(429, json={"error": "quota"})

    report = await run_analyze(
        factory, gemini(tmp_path, chat), AnalyzeOptions(concurrency=1, limit=4)
    )

    assert report.succeeded == 1
    assert "429" in (report.stop_reason or "")
    assert report.not_sent == 3
    async with factory() as session:
        pending = await pending_article_ids(session, analyzer_version(MODEL), limit=4)
    assert len(set(pending) & set(ids)) == 3  # resumed by the next run


async def test_the_daily_limit_holds_with_many_workers(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await seed(factory, 5)

    async def chat(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return ok()

    llm = gemini(tmp_path, chat, per_day=2)
    report = await run_analyze(factory, llm, AnalyzeOptions(concurrency=5, limit=5))

    assert report.succeeded == 2
    rows = cast(FakeLedger, llm.ledger).rows.values()
    assert sum(1 for r in rows if r["outcome"] == "answered") == 2  # five workers, never a third
    assert "limit" in (report.stop_reason or "")  # the per-run cap equals the day cap here


async def test_only_one_run_at_a_time(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await seed(factory, 1)
    release = asyncio.Event()

    async def chat(_: httpx.Request) -> httpx.Response:
        await release.wait()
        return ok()

    first = asyncio.create_task(
        run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))
    )
    await asyncio.sleep(0.2)
    with pytest.raises(AlreadyAnalyzingError):
        await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))
    release.set()
    await first


async def test_a_fabricated_quote_is_needs_review_never_succeeded(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok(answer("a sentence the article never says"))

    report = await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    assert (report.succeeded, report.needs_review) == (0, 1)
    assert (await statuses(factory, [article_id]))[article_id] == ("needs_review", 1)
    assert await unverified(factory, article_id) == ["$.when.event_date.evidence"]
    async with factory() as session:  # a needs_review article is never sent again by itself
        assert article_id not in await pending_article_ids(
            session, analyzer_version(MODEL), limit=5
        )


async def test_a_clean_answer_is_succeeded_with_no_unverified_paths(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    report = await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    assert (report.succeeded, report.needs_review) == (1, 0)
    assert (await statuses(factory, [article_id]))[article_id] == ("succeeded", 1)
    assert await unverified(factory, article_id) == []


async def test_a_cut_off_answer_is_retried_with_the_compact_instruction(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)
    bodies: list[str] = []
    cut_off = httpx.Response(
        200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}
    )

    async def chat(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return cut_off if len(bodies) == 1 else ok()

    report = await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    assert len(bodies) == 2
    assert COMPACT_INSTRUCTION not in bodies[0]
    assert COMPACT_INSTRUCTION in bodies[1]
    assert (report.retried, report.succeeded) == (1, 1)
    assert (await statuses(factory, [article_id]))[article_id] == ("succeeded", 2)


async def test_the_model_is_called_through_one_analysis_function(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [article_id] = await seed(factory, 1)
    calls: list[int] = []
    original = pipeline.analyse_article

    async def counting(*args: Any, **kwargs: Any) -> pipeline.AnalysisOutcome:
        calls.append(args[2])
        return await original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "analyse_article", counting)
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    async with factory() as session:
        await lab.run_lab(session, gemini(tmp_path, chat, per_run=3), [article_id])
    await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    assert calls == [article_id, article_id]  # lab and analyze: the same function


async def test_lab_stores_no_analysis_rows(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await seed(factory, 2)
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    async with factory() as session:
        _, results = await lab.run_lab(session, gemini(tmp_path, chat, per_run=3), ids)

    assert all(r.ok for r in results)
    async with factory() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(ArticleAnalysis)
            .where(ArticleAnalysis.article_id.in_(ids))
        )
    assert rows == 0


async def test_a_run_row_is_created_and_finished(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await seed(factory, 2)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    report = await run_analyze(
        factory, gemini(tmp_path, chat), AnalyzeOptions(concurrency=2, limit=2)
    )

    assert report.run_id is not None
    async with factory() as session:
        run = await session.get(AnalysisRun, report.run_id)
    assert run is not None
    assert (run.command, run.queued, run.concurrency) == ("analyze", 2, 2)
    assert run.finished_at is not None
    assert (run.succeeded, run.needs_review, run.failed) == (2, 0, 0)
    assert run.stop_reason is None


async def test_a_crash_leaves_the_run_unfinished(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(factory, 1)

    async def boom(*_: Any, **__: Any) -> pipeline.AnalysisOutcome:
        msg = "simulated crash"
        raise RuntimeError(msg)

    monkeypatch.setattr(pipeline, "analyse_article", boom)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    with pytest.raises(RuntimeError, match="simulated crash"):
        await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    async with factory() as session:
        run = await session.scalar(
            select(AnalysisRun).where(AnalysisRun.command == "analyze").order_by(
                AnalysisRun.id.desc()
            ).limit(1)
        )  # fmt: skip
    assert run is not None
    assert run.finished_at is None


async def test_every_api_call_of_a_run_points_to_its_run_row(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Audit trail: api_calls.run_id links each request to the analysis_runs row."""
    from app.analysis.ledger import DatabaseLedger  # noqa: PLC0415
    from app.models import ApiCall  # noqa: PLC0415

    await seed(factory, 1)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    llm = gemini(tmp_path, chat)
    llm.ledger = DatabaseLedger(factory)  # the real ledger; its rows are deleted below
    report = await run_analyze(factory, llm, AnalyzeOptions(limit=1))

    try:
        async with factory() as session:
            run_ids = set(
                await session.scalars(select(ApiCall.run_id).where(ApiCall.run_id == report.run_id))
            )
        assert report.run_id is not None
        assert run_ids == {report.run_id}
    finally:
        async with factory() as session:
            await session.execute(delete(ApiCall).where(ApiCall.run_id == report.run_id))
            await session.commit()


async def stored(sessions: async_sessionmaker[AsyncSession], article_id: int) -> ArticleAnalysis:
    async with sessions() as session:
        row = await session.scalar(
            select(ArticleAnalysis).where(ArticleAnalysis.article_id == article_id)
        )
        assert row is not None
        return row


def actor(name: str, quote: str) -> dict[str, object]:
    return {"name_as_written": name, "full_name": name, "type": "government_body",
            "role": "acted", "involvement": "central",
            "evidence": {"paragraph": 1, "quote": quote}}  # fmt: skip


async def test_no_unverified_item_survives_when_cleanup_drops_an_earlier_item(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)
    data = json.loads(VALID)
    data["who"] = [
        actor("Hespress English", "announced the alert system on Monday"),  # publisher: dropped
        actor("ministry", "the ministry closed every school in the city"),  # fabricated
    ]

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok(json.dumps(data))

    await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    row = await stored(factory, article_id)
    assert row.status == "needs_review"
    assert row.unverified_paths == ["$.who[1].evidence"]  # into the model's answer
    assert row.cleaned is not None
    assert [a["name_as_written"] for a in row.cleaned["who"]] == ["ministry"]  # now who[0]
    assert row.evidence is not None
    assert row.evidence["cleaned:$.who[0].evidence"] == "fabricated"
    assert row.briefing is not None
    assert row.briefing["who"] == []  # the fabricated actor is never served
    assert row.briefing["review_required"] is True


async def test_text_copied_from_the_article_is_never_served(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)
    data = json.loads(VALID)
    data["what"] = {
        "main_event": "The ministry announced the alert system on Monday in Rabat",  # copied
        "secondary_events": ["An alert system was presented", BODY],
    }

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok(json.dumps(data))

    await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    briefing = (await stored(factory, article_id)).briefing
    assert briefing is not None
    assert briefing["what"] == {
        "main_event": None,
        "secondary_events": ["An alert system was presented"],
    }
    assert set(briefing["copied_paths"]) == {"$.what.main_event", "$.what.secondary_events[1]"}
    assert (briefing["removed_items"], briefing["copied_items"]) == (2, 2)
    assert "$.what.secondary_events" in briefing["unverified_fields"]
    assert "$.overview.headline" in briefing["unverified_fields"]


async def test_every_attempt_is_kept_in_the_history_without_raw_text(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [article_id] = await seed(factory, 1)
    answers = [
        httpx.Response(
            200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}
        ),
        ok("not json"),
        ok(),
    ]

    async def chat(_: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    row = await stored(factory, article_id)
    assert row.status == "succeeded"
    history = row.attempt_history
    assert [(h["attempt"], h["status"], h["finish_reason"]) for h in history] == [
        (1, "failed", "length"),
        (2, "failed", "invalid"),
        (3, "succeeded", "stop"),
    ]
    assert history[2]["input_tokens"] == 100
    assert all(h["at"] for h in history)
    dumped = json.dumps(history)
    assert "not json" not in dumped
    assert "alert system" not in dumped


async def test_an_interruption_finishes_the_run_row(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(factory, 1)

    async def interrupted(*_: Any, **__: Any) -> pipeline.AnalysisOutcome:
        raise asyncio.CancelledError

    monkeypatch.setattr(pipeline, "analyse_article", interrupted)

    async def chat(_: httpx.Request) -> httpx.Response:
        return ok()

    with pytest.raises(asyncio.CancelledError):
        await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=1))

    async with factory() as session:
        run = await session.scalar(
            select(AnalysisRun).where(AnalysisRun.command == "analyze").order_by(
                AnalysisRun.id.desc()
            ).limit(1)
        )  # fmt: skip
    assert run is not None
    assert run.finished_at is not None
    assert run.stop_reason == "interrupted"
