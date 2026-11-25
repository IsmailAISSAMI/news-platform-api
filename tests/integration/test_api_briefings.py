"""Briefing API against the real database (synthetic data only, rolled back per test)."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.briefing import make_briefing
from app.analysis.pipeline import AnalyzeOptions, run_analyze
from app.api.deps import get_db
from app.models import Article, ArticleAnalysis, ArticleBody, Source
from tests.integration.test_pipeline import (  # the pipeline test's synthetic publisher
    answer,
    factory,
    gemini,
    ok,
    seed,
)
from tests.test_api_contract import check, response_schema

pytestmark = pytest.mark.integration
__all__ = ["factory"]  # a fixture, re-exported for the pipeline test below

NOW = datetime(2098, 6, 1, 12, 0, tzinfo=UTC)  # far future: before any real row
FORBIDDEN_KEYS = {"evidence", "quote", "raw_output", "body_text", "analysis", "cleaned"}


def ev(n: int) -> dict[str, Any]:
    return {"paragraph": 1, "quote": f"synthetic quote {n}"}


def cleaned(n: int) -> dict[str, Any]:
    return {
        "overview": {
            "headline": f"Synthetic briefing {n}",
            "report_type": "new_event",
            "event_status": "completed",
        },
        "what": {"main_event": "A synthetic event", "secondary_events": []},
        "who": [
            {
                "name_as_written": f"Person {i}",
                "full_name": f"Person {i}",
                "type": "person",
                "role": "spoke",
                "involvement": "mentioned" if i == 0 else "central",
                "evidence": ev(i),
            }
            for i in range(5)
        ],
        "when": {"event_date": {"value": "2098-06-01", "evidence": ev(9)}, "timeline": []},
        "where": [
            {"name_as_written": "Placeville", "role": "mentioned", "evidence": ev(1)},
            {"name_as_written": "Townsburg", "role": "event_location", "evidence": ev(2)},
        ],
        "claims": [
            {"text": "Kept claim", "kind": "fact", "attributed_to": None, "evidence": ev(3)},
            {"text": "SECRET-UNVERIFIED", "kind": "fact", "attributed_to": None, "evidence": ev(4)},
        ],
        "why": [],
        "how": None,
        "impact": [],
        "context": [],
        "open_questions": [],
        "topics": ["synthetic"],
        "tone": {"label": "neutral", "explanation": "x"},
    }


@pytest.fixture
async def client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def test_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = test_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def add(
    session: AsyncSession, source: Source, n: int, status: str, unverified: list[str]
) -> Article:
    article = Article(
        source_id=source.id,
        external_id=f"briefing-test-{n}",
        canonical_url=f"https://en.hespress.com/briefing-test-{n}.html",
        title=f"Synthetic title {n}",
        language="en",
        categories=[],
        published_at=NOW - timedelta(hours=n),
        first_seen_via="rss",
        content_hash=f"bt{n}",
    )
    session.add(article)
    await session.flush()
    data = cleaned(n)
    session.add(
        ArticleAnalysis(
            article_id=article.id,
            analyzer_version="test|p1|s1",
            model="test",
            status=status,
            raw_output="RAW-SECRET",
            analysis=data,
            cleaned=data,
            unverified_paths=unverified,
            briefing=make_briefing(data, unverified) if status != "failed" else None,
        )
    )
    await session.flush()
    return article


def keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys(v)}
    if isinstance(node, list):
        return {k for item in node for k in keys(item)}
    return set()


async def test_briefings_are_paginated_newest_first(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    articles = [await add(db_session, source, n, "succeeded", []) for n in range(5)]
    await add(db_session, source, 99, "failed", [])  # never listed

    seen: list[int] = []
    cursor = None
    for _ in range(3):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/briefings", params=params)
        assert response.status_code == 200
        page = response.json()
        seen += [item["article_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    ours = [a.id for a in articles]
    assert [i for i in seen if i in ours][:5] == ours  # real rows, if any, are older
    item = (await client.get("/api/v1/briefings", params={"limit": 1})).json()["items"][0]
    assert item["headline"] == "Synthetic briefing 0"
    assert item["canonical_url"] == "https://en.hespress.com/briefing-test-0.html"
    assert item["event_date"] == "2098-06-01"
    assert [p["name_as_written"] for p in item["places"]] == ["Townsburg", "Placeville"]
    assert len(item["actors"]) == 3
    assert all(a["involvement"] == "central" for a in item["actors"])
    assert item["review_required"] is False
    assert item["source"] == {
        "slug": source.slug,
        "name": source.name,
        "site_url": source.site_url,
    }


async def test_limit_is_bounded_and_cursor_checked(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/briefings", params={"limit": 101})).status_code == 422
    assert (await client.get("/api/v1/briefings", params={"cursor": "bad"})).status_code == 400


async def test_detail_404_without_a_servable_briefing(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    failed = await add(db_session, source, 1, "failed", [])
    assert (await client.get(f"/api/v1/articles/{failed.id}/briefing")).status_code == 404
    assert (await client.get("/api/v1/articles/999999999/briefing")).status_code == 404


async def test_needs_review_never_exposes_the_removed_item(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    unverified = ["$.claims[1].evidence", "$.when.event_date.evidence"]
    article = await add(db_session, source, 2, "needs_review", unverified)

    response = await client.get(f"/api/v1/articles/{article.id}/briefing")

    assert response.status_code == 200
    body = response.json()
    assert "SECRET-UNVERIFIED" not in response.text
    assert "RAW-SECRET" not in response.text
    assert "synthetic quote" not in response.text
    assert [c["text"] for c in body["briefing"]["claims"]] == ["Kept claim"]
    assert body["briefing"]["when"]["event_date"]["value"] is None
    assert (body["status"], body["review_required"], body["removed_items"]) == (
        "needs_review",
        True,
        2,
    )
    assert body["source_url"] == article.canonical_url
    assert "url" not in body  # the duplicate of source_url is gone
    assert body["copied_items"] == 0
    # Fields with no evidence are listed as unchecked, never the quarantined items.
    assert all(f.startswith("$.") for f in body["unverified_fields"])
    assert not any(f.startswith("$.claims") for f in body["unverified_fields"])
    assert body["source"]["name"] == source.name
    listed = (await client.get("/api/v1/briefings", params={"limit": 100})).json()["items"]
    mine = next(i for i in listed if i["article_id"] == article.id)
    assert mine["review_required"] is True
    assert mine["event_date"] is None


async def test_no_response_contains_article_text_keys(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    article = await add(db_session, source, 3, "needs_review", ["$.who[1].evidence"])
    for path in ("/api/v1/briefings", f"/api/v1/articles/{article.id}/briefing"):
        response = await client.get(path)
        assert response.status_code == 200
        assert not keys(response.json()) & FORBIDDEN_KEYS, path
        assert "synthetic quote" not in response.text


async def test_the_pipeline_saves_the_briefing(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [good, bad] = await seed(factory, 2)
    answers = iter([ok(), ok(answer("a sentence the article never says"))])

    async def chat(_: httpx.Request) -> httpx.Response:
        return next(answers)

    await run_analyze(factory, gemini(tmp_path, chat), AnalyzeOptions(limit=2, concurrency=1))

    async with factory() as session:
        rows: dict[int, dict[str, Any]] = dict(
            (
                await session.execute(
                    select(ArticleAnalysis.article_id, ArticleAnalysis.briefing).where(
                        ArticleAnalysis.article_id.in_([good, bad])
                    )
                )
            ).all()
        )
    # Newest first: `bad` (seeded later) gets the first answer.
    briefings = {rows[bad]["review_required"]: rows[bad], rows[good]["review_required"]: rows[good]}
    clean, review = briefings[False], briefings[True]
    assert clean["when"]["event_date"] == {"value": "2026-11-23"}
    assert review["when"]["event_date"] == {"value": None}
    # Cleanup already nulls the unverified event date, so the quarantine removes nothing more.
    assert review["removed_items"] == 0
    assert "evidence" not in keys(clean) | keys(review)


async def test_every_endpoint_body_matches_its_openapi_schema(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    article = await add(db_session, source, 7, "needs_review", ["$.claims[1].evidence"])
    spec = app.openapi()
    cases = [
        ("/api/v1/sources", "/api/v1/sources", 200),
        ("/api/v1/feeds", "/api/v1/feeds", 200),
        ("/api/v1/articles?limit=2", "/api/v1/articles", 200),
        (f"/api/v1/articles/{article.id}", "/api/v1/articles/{article_id}", 200),
        ("/api/v1/briefings?limit=2", "/api/v1/briefings", 200),
        (f"/api/v1/articles/{article.id}/briefing", "/api/v1/articles/{article_id}/briefing", 200),
        ("/api/v1/articles/999999999", "/api/v1/articles/{article_id}", 404),
        (
            "/api/v1/articles/999999999/briefing",
            "/api/v1/articles/{article_id}/briefing",
            404,
        ),
    ]
    for url, route, status in cases:
        response = await client.get(url)
        assert response.status_code == status, url
        check(spec, response_schema(spec, route, status), response.json(), url)


async def test_a_malformed_stored_briefing_is_skipped_and_logged_by_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    source: Source,
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = await add(db_session, source, 1, "succeeded", [])
    bad = await add(db_session, source, 2, "succeeded", [])
    analysis = await db_session.scalar(
        select(ArticleAnalysis).where(ArticleAnalysis.article_id == bad.id)
    )
    assert analysis is not None
    analysis.briefing = {"overview": "MALFORMED-CONTENT", "review_required": False}
    await db_session.flush()

    with caplog.at_level(logging.WARNING, logger="app.api.v1.analysis"):
        listed = await client.get("/api/v1/briefings", params={"limit": 100})
        detail = await client.get(f"/api/v1/articles/{bad.id}/briefing")

    assert listed.status_code == 200
    ids = [i["article_id"] for i in listed.json()["items"]]
    assert good.id in ids
    assert bad.id not in ids
    assert detail.status_code == 404
    records = [r for r in caplog.records if r.name == "app.api.v1.analysis"]
    assert records
    assert {getattr(r, "article_id", None) for r in records} == {bad.id}
    assert all("MALFORMED-CONTENT" not in r.getMessage() for r in records)


async def test_new_briefing_keys_are_exposed(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    article = await add(db_session, source, 4, "needs_review", ["$.claims[1].evidence"])
    analysis = await db_session.scalar(
        select(ArticleAnalysis).where(ArticleAnalysis.article_id == article.id)
    )
    assert analysis is not None and analysis.briefing is not None
    analysis.briefing = analysis.briefing | {
        "unverified_fields": ["$.claims[1]"],
        "copied_items": 2,
    }
    await db_session.flush()

    body = (await client.get(f"/api/v1/articles/{article.id}/briefing")).json()

    assert (body["unverified_fields"], body["copied_items"]) == (["$.claims[1]"], 2)


async def test_the_latest_analyzer_version_is_served(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    article = await add(db_session, source, 5, "succeeded", [])
    newer = cleaned(5)
    newer["overview"]["headline"] = "Newer version"
    db_session.add(
        ArticleAnalysis(
            article_id=article.id,
            analyzer_version="test|p2|s1",
            model="test",
            status="succeeded",
            raw_output="RAW-SECRET",
            analysis=newer,
            cleaned=newer,
            unverified_paths=[],
            briefing=make_briefing(newer, []),
            updated_at=NOW + timedelta(days=1),
        )
    )
    await db_session.flush()

    listed = (await client.get("/api/v1/briefings", params={"limit": 100})).json()["items"]
    detail = (await client.get(f"/api/v1/articles/{article.id}/briefing")).json()

    assert [i["headline"] for i in listed if i["article_id"] == article.id] == ["Newer version"]
    assert detail["analyzer_version"] == "test|p2|s1"


async def test_if_none_match_star_returns_304(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    await add(db_session, source, 6, "succeeded", [])

    response = await client.get("/api/v1/briefings", headers={"If-None-Match": "*"})

    assert response.status_code == 304


BODY_SENTINEL = "BODY-SENTINEL-MUST-NEVER-BE-SERVED"
COPIED = "the synthetic council approved the new water plan for twelve rural communes"


async def test_copied_scalars_and_stored_bodies_on_the_briefing_routes(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    """IA-001 end to end, and TI-003: a briefing whose headline, main event and tone
    explanation were nulled by the originality filter is still served (list and detail),
    and the stored body never appears in either response."""
    article = await add(db_session, source, 4, "succeeded", [])
    paragraphs = [f"{BODY_SENTINEL} Yesterday {COPIED} after a long debate."]
    db_session.add(
        ArticleBody(article_id=article.id, body_text="\n\n".join(paragraphs), body_hash="h4")
    )
    data = cleaned(4)
    data["overview"]["headline"] = f"Officials say {COPIED}"
    data["what"]["main_event"] = f"Members said {COPIED}"
    data["tone"]["explanation"] = f"Calm: {COPIED}"
    analysis = (
        await db_session.execute(
            select(ArticleAnalysis).where(ArticleAnalysis.article_id == article.id)
        )
    ).scalar_one()
    analysis.briefing = make_briefing(data, [], paragraphs=paragraphs)
    await db_session.flush()

    detail = await client.get(f"/api/v1/articles/{article.id}/briefing")
    listed = await client.get("/api/v1/briefings", params={"limit": 100})

    assert detail.status_code == 200
    briefing = detail.json()["briefing"]
    assert briefing["overview"]["headline"] is None
    assert briefing["what"]["main_event"] is None
    assert briefing["tone"]["explanation"] is None
    assert detail.json()["copied_items"] == 3
    mine = next(i for i in listed.json()["items"] if i["article_id"] == article.id)
    assert mine["headline"] is None
    for response in (detail, listed):
        assert BODY_SENTINEL not in response.text
        assert COPIED not in response.text
