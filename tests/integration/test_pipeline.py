"""`news analyze` against the test database and a simulated Gemini API.

The pipeline commits per article from several sessions, so these tests commit real rows
in `news_test` (never the real database) and delete them afterwards. Synthetic text only.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis.budget import RequestBudget
from app.analysis.llm import GeminiClient
from app.analysis.pipeline import (
    MAX_ATTEMPTS,
    AlreadyAnalyzingError,
    AnalyzeOptions,
    analyzer_version,
    pending_article_ids,
    run_analyze,
)
from app.db.session import create_session_factory
from app.models import Article, ArticleAnalysis, ArticleBody, Source

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
PREFIX = "pipeline-test-"
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
    yield sessions
    async with sessions() as session:  # remove this test's rows (analyses cascade)
        await session.execute(delete(Article).where(Article.external_id.like(f"{PREFIX}%")))
        await session.commit()


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


def gemini(tmp_path: Path, chat: Chat, per_day: int = 1000) -> GeminiClient:
    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    return GeminiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(chat)),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model=MODEL,
        api_key=SecretStr("test-key"),
        budget=RequestBudget(
            usage_file=tmp_path / "usage.json",
            per_run=per_day,
            per_day=per_day,
            per_minute=20,
            input_tokens_per_minute=10_000,
        ),
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

    async def chat(_: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)  # Google "thinking"
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
    assert llm.budget.used_today() == 2  # five workers, never a third request
    assert "daily limit" in (report.stop_reason or "")


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
