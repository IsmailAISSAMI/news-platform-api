"""The lab never breaks on Google's errors: it waits, stops cleanly and always reports.

Synthetic articles and a simulated Gemini API; nothing is sent anywhere.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import lab
from app.analysis.budget import RequestBudget
from app.analysis.llm import GeminiClient
from app.models import Article, ArticleBody, Source

pytestmark = pytest.mark.integration

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
BUSY = httpx.Response(503, json={"error": {"message": "high demand"}})


async def articles(db_session: AsyncSession, source: Source, count: int) -> list[int]:
    ids = []
    for n in range(count):
        article = Article(
            source_id=source.id,
            external_id=f"lab-{n}",
            canonical_url=f"https://en.hespress.com/lab-{n}.html",
            title=f"Synthetic headline {n}",
            language="en",
            categories=[],
            published_at=datetime(2026, 11, 23, tzinfo=UTC),
            first_seen_via="rss",
            content_hash=f"lab{n}",
        )
        db_session.add(article)
        await db_session.flush()
        db_session.add(ArticleBody(article_id=article.id, body_text=BODY, body_hash="h"))
        ids.append(article.id)
    await db_session.flush()
    return ids


def client(tmp_path: Path, chat: Callable[[httpx.Request], httpx.Response]) -> GeminiClient:
    async def no_sleep(_: float) -> None:
        return None

    return GeminiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(chat)),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemma-4-31b-it",
        api_key=SecretStr("test-key"),
        budget=RequestBudget(
            usage_file=tmp_path / "usage.json",
            per_run=10,
            per_day=1000,
            per_minute=10,
            input_tokens_per_minute=8000,
        ),
        sleep=no_sleep,
    )


async def test_busy_errors_are_waited_out_and_every_article_is_analysed(
    db_session: AsyncSession, source: Source, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")
    first, second = await articles(db_session, source, 2)
    calls = {"n": 0}

    def chat(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 4:  # Google busy for the first 4 attempts
            return BUSY
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID}}]})

    out, results = await lab.run_lab(db_session, client(tmp_path, chat), [first, second])

    assert [r.ok for r in results] == [True, True]
    assert calls["n"] == 6
    assert (out / "summary.md").exists()


async def test_a_hard_stop_still_writes_the_report_and_sends_nothing_more(
    db_session: AsyncSession, source: Source, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")
    ids = await articles(db_session, source, 3)
    sent: list[httpx.Request] = []

    def chat(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(429, json={"error": "quota"})

    out, results = await lab.run_lab(db_session, client(tmp_path, chat), ids)

    assert len(sent) == 1
    assert "429" in (results[0].error or "")
    assert all("not sent" in (r.error or "") for r in results[1:])
    assert (out / "summary.md").exists()
