"""`export_gold` and `consistency` against the test database. Synthetic text only."""

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.eval import consistency, export_gold
from app.models import Article, ArticleAnalysis, ArticleBody, Source

pytestmark = pytest.mark.integration

BODY = "Sources said the council approved the plan.\n\nThe council met in Rabat on Monday."
Q1 = {"paragraph": 1, "quote": "the council approved the plan"}
ANALYSIS: dict[str, Any] = {
    "overview": {"headline": "h", "report_type": "new_event", "event_status": "completed"},
    "what": {"main_event": "The council approved the plan"},
    "who": [{"name_as_written": "council", "full_name": "council", "type": "government_body",
             "role": "approved", "involvement": "central", "evidence": Q1}],
    "when": {"event_date": {"value": None, "evidence": None},
             "timeline": [{"date": "2026-11-23", "event": "Approval", "evidence": Q1}]},
    "where": [{"name_as_written": "Rabat", "role": "event_location",
               "evidence": {"paragraph": 2, "quote": "met in Rabat"}}],
    "claims": [{"text": "The plan was approved", "kind": "fact", "attributed_to": None,
                "evidence": Q1}],
    "how": None, "topics": [], "tone": {"label": "neutral", "explanation": "x"},
}  # fmt: skip


async def seed(db_session: AsyncSession, source: Source, categories: list[list[str]]) -> None:
    for n, category in enumerate(categories):
        article = Article(
            source_id=source.id, external_id=f"eval-{n}",
            canonical_url=f"https://en.hespress.com/eval-{n}.html",
            title=f"Synthetic headline {n}", language="en", categories=category,
            published_at=datetime(2026, 11, 23, 9, tzinfo=UTC), first_seen_via="rss",
            content_hash=f"eval{n}",
        )  # fmt: skip
        db_session.add(article)
        await db_session.flush()
        db_session.add(ArticleBody(article_id=article.id, body_text=BODY, body_hash="h"))
        stored = ArticleAnalysis(
            article_id=article.id, analyzer_version="m|p1|s1", model="m",
            status="succeeded", attempts=1, analysis=ANALYSIS, cleaned=ANALYSIS,
        )  # fmt: skip
        db_session.add(stored)
    await db_session.flush()


async def test_export_writes_private_checklists_and_labels(
    db_session: AsyncSession, source: Source, tmp_path: Path
) -> None:
    await seed(db_session, source, [["Politics"], ["Politics"], ["Sports"], []])

    out = await export_gold(db_session, n=3, out_dir=tmp_path / "gold")

    articles = sorted(out.glob("article-*.md"))
    assert len(articles) == 3
    assert out.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in [*articles, out / "labels.csv"])
    text = articles[0].read_text()
    assert "[P1] Sources said the council approved the plan." in text
    assert "[ ] correct  [ ] wrong  [ ] missing detail" in text
    with (out / "labels.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3 * 4  # who, where, timeline entry, claim
    assert {r["verdict"] for r in rows} == {""}


async def test_consistency_reruns_the_current_checks(
    db_session: AsyncSession, source: Source
) -> None:
    await seed(db_session, source, [["Politics"]])

    result = await consistency(db_session)

    assert result.analyses >= 1
    assert result.articles_by_issue["anonymous_source_unattributed"] >= 1
    assert result.articles_by_issue["timeline_publication_date"] >= 1
    assert "verified quotes" in result.render()
