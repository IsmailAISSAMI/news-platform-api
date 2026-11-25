"""The analysis status set, as PostgreSQL stores it (IA-008).

`alembic check` compares neither CHECK text nor views nor index predicates, so this test
reads them from the migrated test database and compares each with one expected set.
"""

import re

import pytest
from sqlalchemy import CheckConstraint, Index, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

pytestmark = pytest.mark.integration

STATUSES = {"succeeded", "needs_review", "failed", "skipped"}
BRIEFING_STATUSES = {"succeeded", "needs_review"}
TABLE = Base.metadata.tables["article_analyses"]
BRIEFING_PREDICATE = re.compile(r"\bbriefing IS NOT NULL\b")


def _literals(definition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", definition))


async def _scalar(session: AsyncSession, sql: str) -> str:
    value = (await session.execute(text(sql))).scalar_one()
    return str(value)


async def test_the_status_check_allows_exactly_the_known_statuses(
    db_session: AsyncSession,
) -> None:
    stored = await _scalar(
        db_session,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_article_analyses_status_known'",
    )
    model = next(
        c
        for c in TABLE.constraints
        if isinstance(c, CheckConstraint) and str(c.name).endswith("status_known")
    )

    assert _literals(stored) == STATUSES
    assert _literals(str(model.sqltext)) == STATUSES


async def test_the_view_and_the_index_select_the_same_briefings(db_session: AsyncSession) -> None:
    view = await _scalar(db_session, "SELECT pg_get_viewdef('analysis_briefings'::regclass)")
    index = await _scalar(
        db_session,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_article_analyses_briefings'",
    )
    model = next(i for i in TABLE.indexes if i.name == "ix_article_analyses_briefings")
    assert isinstance(model, Index)
    model_where = str(model.dialect_options["postgresql"]["where"])

    for definition in (view.split("WHERE", 1)[1], index.split("WHERE", 1)[1], model_where):
        assert _literals(definition) == BRIEFING_STATUSES, definition
        assert BRIEFING_PREDICATE.search(definition), definition
    assert BRIEFING_STATUSES < STATUSES
