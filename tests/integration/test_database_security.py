"""Database-level security, checked after applying the real scripts/db_roles.sql.

Runs against the test database (`news_test`). Roles are cluster-wide, but CREATE ROLE,
GRANT and ALTER DEFAULT PRIVILEGES are transactional in PostgreSQL: everything the script
does here is rolled back with the test transaction.
"""

import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.session import PrivilegeError, assert_least_privilege

pytestmark = pytest.mark.integration

ROLES_SQL = Path(__file__).resolve().parents[2] / "scripts" / "db_roles.sql"

# The ONLY relations news_api may read. Adding one is a deliberate change here and in
# scripts/db_roles.sql.
API_SELECT_ALLOWLIST = {"analysis_briefings", "articles", "feeds", "sources"}


async def apply_roles(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.exec_driver_sql(ROLES_SQL.read_text())


async def readable_by(session: AsyncSession, role: str, privilege: str) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p', 'f')"
            " AND has_table_privilege(:role, c.oid, :privilege)"
        ),
        {"role": role, "privilege": privilege},
    )
    return set(rows.scalars())


async def test_api_role_selects_exactly_the_allowlist(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    for role in ("news_api", "news_api_login"):
        assert await readable_by(db_session, role, "SELECT") == API_SELECT_ALLOWLIST, role
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert await readable_by(db_session, role, privilege) == set(), (role, privilege)


async def test_new_tables_are_not_readable_by_the_api_role(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    await db_session.execute(text("CREATE TABLE zz_future_table (id int)"))
    assert "zz_future_table" not in await readable_by(db_session, "news_api", "SELECT")
    assert "zz_future_table" in await readable_by(db_session, "news_ingest", "INSERT")


async def test_ingest_role_cannot_write_through_the_briefings_view(
    db_session: AsyncSession,
) -> None:
    await apply_roles(db_session)
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert "analysis_briefings" not in await readable_by(db_session, "news_ingest", privilege)


async def test_login_roles_inherit_groups_and_are_unprivileged(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    rows = await db_session.execute(
        text(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls,"
            " rolinherit FROM pg_roles WHERE rolname IN ('news_api_login', 'news_ingest_login')"
        )
    )
    roles = {row[0]: row[1:] for row in rows}
    assert roles == {
        "news_api_login": (True, False, False, False, False, True),
        "news_ingest_login": (True, False, False, False, False, True),
    }
    assert await db_session.scalar(
        text("SELECT pg_has_role('news_api_login', 'news_api', 'USAGE')")
    )
    assert await db_session.scalar(
        text("SELECT pg_has_role('news_ingest_login', 'news_ingest', 'USAGE')")
    )
    assert not await db_session.scalar(
        text("SELECT pg_has_role('news_api_login', 'news_ingest', 'USAGE')")
    )


async def test_roles_script_is_idempotent_and_has_no_password(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    await apply_roles(db_session)
    assert not re.search(r"PASSWORD\s+'", ROLES_SQL.read_text(), re.IGNORECASE)


async def test_api_role_reads_briefings_only_through_the_view(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    await db_session.execute(text("SET LOCAL ROLE news_api_login"))
    await db_session.execute(text("SELECT briefing FROM analysis_briefings LIMIT 1"))
    for table in ("article_analyses", "article_bodies", "api_calls", "analysis_runs"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))  # noqa: S608  # fixed names
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


# --- startup guard --------------------------------------------------------------------------
# The test DB login is the schema owner (a superuser in compose and CI): exactly the
# misconfiguration the guard must catch.


async def test_guard_refuses_a_privileged_api_login_in_production(
    engine: AsyncEngine, settings: Settings
) -> None:
    production = settings.model_copy(update={"environment": "production"})
    with pytest.raises(PrivilegeError, match="API_DATABASE_URL"):
        await assert_least_privilege(engine, production)


async def test_guard_only_warns_outside_production(
    engine: AsyncEngine, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.db.session"):
        await assert_least_privilege(engine, settings)
    assert "Least privilege not met" in caplog.text
