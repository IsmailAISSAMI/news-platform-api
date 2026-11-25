"""Database-level security: which tables the `news_api` role can read.

The API role must never read article text or the internal analysis log. These checks run
against the test database (`news_test`), where the migrations have run. Roles are
cluster-wide, so `news_api` may or may not exist there; if it does not, it is created
NOLOGIN inside the rolled-back test transaction. Grants made by a test are rolled back too.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

RESTRICTED_TABLES = ("article_bodies", "article_analyses", "api_calls", "analysis_runs")
READABLE_TABLES = ("articles", "sources")


async def has_privilege(session: AsyncSession, table: str, privilege: str) -> bool:
    result = await session.execute(
        text("SELECT has_table_privilege('news_api', :table, :privilege)"),
        {"table": table, "privilege": privilege},
    )
    return bool(result.scalar_one())


async def ensure_api_role(session: AsyncSession) -> None:
    exists = await session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'news_api'"))
    if exists.scalar_one_or_none() is None:
        await session.execute(text("CREATE ROLE news_api NOLOGIN"))


async def test_api_role_cannot_read_restricted_tables(db_session: AsyncSession) -> None:
    await ensure_api_role(db_session)
    for table in RESTRICTED_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await has_privilege(db_session, table, privilege), (table, privilege)


async def test_api_role_can_read_public_tables_only_as_granted(db_session: AsyncSession) -> None:
    await ensure_api_role(db_session)
    # Mirrors scripts/db_roles.sql for the read-only API role (rolled back with the test).
    await db_session.execute(text("GRANT SELECT ON articles, sources TO news_api"))
    for table in READABLE_TABLES:
        assert await has_privilege(db_session, table, "SELECT"), table
        assert not await has_privilege(db_session, table, "INSERT"), table
        assert not await has_privilege(db_session, table, "UPDATE"), table


async def test_api_role_reads_briefings_only_through_the_view(db_session: AsyncSession) -> None:
    await ensure_api_role(db_session)
    # Mirrors scripts/db_roles.sql (rolled back with the test).
    await db_session.execute(text("GRANT SELECT ON analysis_briefings TO news_api"))
    assert await has_privilege(db_session, "analysis_briefings", "SELECT")
    assert not await has_privilege(db_session, "analysis_briefings", "INSERT")
    assert not await has_privilege(db_session, "article_analyses", "SELECT")

    # The view runs with its owner's privileges: news_api reads it, not the base table.
    await db_session.execute(text("SET LOCAL ROLE news_api"))
    await db_session.execute(text("SELECT briefing FROM analysis_briefings LIMIT 1"))
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db_session.begin_nested():
            await db_session.execute(text("SELECT raw_output FROM article_analyses LIMIT 1"))
    await db_session.execute(text("RESET ROLE"))


async def test_the_view_exposes_no_text_column(db_session: AsyncSession) -> None:
    columns = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'analysis_briefings' ORDER BY ordinal_position"
        )
    )
    assert list(columns.scalars()) == [
        "article_id",
        "analyzer_version",
        "status",
        "briefing",
        "updated_at",
    ]
