"""Schema guarantees: seed data, constraints, dedup, generated search column, DB roles.

Requires the database at Alembic head (`uv run alembic upgrade head`; CI does this).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed, Source
from app.models.enums import DiscoveryChannel, FeedKind, RightsPolicy

pytestmark = pytest.mark.integration

ROLES_SQL = Path(__file__).resolve().parents[2] / "scripts" / "db_roles.sql"


async def hespress(session: AsyncSession) -> Source:
    return (await session.execute(select(Source).where(Source.slug == "hespress-en"))).scalar_one()


def make_article(source_id: int, external_id: str, **overrides: object) -> Article:
    fields: dict[str, object] = {
        "source_id": source_id,
        "external_id": external_id,
        "canonical_url": f"https://example.test/{external_id}-placeholder.html",
        "title": "Placeholder title about harbour logistics",
        "language": "en",
        "published_at": datetime(2026, 11, 23, 12, 0, tzinfo=UTC),
        "first_seen_via": DiscoveryChannel.RSS,
        "content_hash": "hash",
    }
    fields.update(overrides)
    return Article(**fields)


async def test_hespress_is_restricted_with_a_recorded_basis(db_session: AsyncSession) -> None:
    source = await hespress(db_session)

    assert source.rights_policy == RightsPolicy.RESTRICTED
    assert source.rights_basis is not None
    assert "educational" in source.rights_basis
    assert source.excerpt_max_chars == 0
    assert source.license_ref is None

    feeds = (await db_session.execute(select(Feed).where(Feed.source_id == source.id))).scalars()
    assert {feed.kind for feed in feeds} == {
        FeedKind.RSS,
        FeedKind.NEWS_SITEMAP,
        FeedKind.ARCHIVE_INDEX,
    }


async def test_article_dedup_per_source_and_external_id(db_session: AsyncSession) -> None:
    source = await hespress(db_session)
    db_session.add(make_article(source.id, "1001"))
    await db_session.flush()

    with pytest.raises(IntegrityError, match="uq_articles_source_id_external_id"):
        async with db_session.begin_nested():
            db_session.add(
                make_article(source.id, "1001", canonical_url="https://example.test/other.html")
            )
            await db_session.flush()


async def test_canonical_url_is_unique(db_session: AsyncSession) -> None:
    source = await hespress(db_session)
    db_session.add(make_article(source.id, "2001", canonical_url="https://example.test/same"))
    await db_session.flush()

    with pytest.raises(IntegrityError, match="uq_articles_canonical_url"):
        async with db_session.begin_nested():
            db_session.add(
                make_article(source.id, "2002", canonical_url="https://example.test/same")
            )
            await db_session.flush()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"rights_policy": "full"}, "ck_sources_rights_policy"),
        ({"excerpt_max_chars": -1}, "ck_sources_excerpt_max_chars_non_negative"),
        (
            {"rights_policy": RightsPolicy.LICENSED_FULL_TEXT, "license_ref": None},
            "ck_sources_license_required_for_full_text",
        ),
        (
            {"rights_policy": RightsPolicy.RESTRICTED, "rights_basis": None},
            "ck_sources_basis_required_for_restricted",
        ),
    ],
)
async def test_source_constraints(
    db_session: AsyncSession, overrides: dict[str, object], constraint: str
) -> None:
    fields: dict[str, object] = {
        "slug": "test-source",
        "name": "Test source",
        "site_url": "https://example.test",
        "language": "en",
        "rights_policy": RightsPolicy.METADATA_ONLY,
    }
    fields.update(overrides)

    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            db_session.add(Source(**fields))
            await db_session.flush()


async def test_licensed_full_text_is_accepted_with_a_license(db_session: AsyncSession) -> None:
    db_session.add(
        Source(
            slug="licensed-source",
            name="Licensed source",
            site_url="https://example.test",
            language="en",
            rights_policy=RightsPolicy.LICENSED_FULL_TEXT,
            license_ref="LIC-2026-001",
        )
    )
    await db_session.flush()


async def test_unknown_discovery_channel_is_rejected(db_session: AsyncSession) -> None:
    source = await hespress(db_session)

    with pytest.raises(IntegrityError, match="ck_articles_first_seen_via"):
        async with db_session.begin_nested():
            db_session.add(make_article(source.id, "3001", first_seen_via="scraper"))
            await db_session.flush()


async def test_search_vector_is_generated_and_weighted(db_session: AsyncSession) -> None:
    source = await hespress(db_session)
    db_session.add(make_article(source.id, "4001", title="Harbour expansion approved"))
    await db_session.flush()

    match = await db_session.scalar(
        select(Article.external_id).where(
            Article.search_tsv.bool_op("@@")(text("websearch_to_tsquery('english', 'harbours')"))
        )
    )
    assert match == "4001"  # stemming: "harbours" matches "Harbour"


async def test_timestamps_are_stored_in_utc(db_session: AsyncSession) -> None:
    source = await hespress(db_session)
    article = make_article(source.id, "5001")
    db_session.add(article)
    await db_session.flush()
    await db_session.refresh(article)

    assert article.published_at.utcoffset() is not None
    assert article.first_seen_at.tzinfo is not None


async def test_articles_table_cannot_hold_a_body(db_session: AsyncSession) -> None:
    """Bodies may only ever live in a separate, license-gated table (architecture §5.3)."""
    columns = set(
        (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'articles'"
                )
            )
        ).scalars()
    )

    assert not columns & {"body", "body_html", "body_text", "content", "content_encoded"}


async def apply_roles(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.exec_driver_sql(ROLES_SQL.read_text())


async def test_api_role_is_read_only(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    source = await hespress(db_session)

    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL ROLE news_api"))
        assert await db_session.scalar(text("SELECT count(*) FROM sources")) >= 1
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text("UPDATE sources SET rights_policy = 'licensed_full_text' WHERE id = :id"),
                    {"id": source.id},
                )


async def test_api_role_cannot_read_article_bodies(db_session: AsyncSession) -> None:
    await apply_roles(db_session)

    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL ROLE news_api"))
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text("SELECT body_text FROM article_bodies"))


async def test_ingest_role_can_write_but_not_change_schema(db_session: AsyncSession) -> None:
    await apply_roles(db_session)
    source = await hespress(db_session)

    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL ROLE news_ingest"))
        await db_session.execute(
            text(
                "INSERT INTO articles (source_id, external_id, canonical_url, title, language,"
                " published_at, first_seen_via, content_hash) VALUES (:s, '6001',"
                " 'https://example.test/6001', 'Placeholder', 'en', now(), 'rss', 'h')"
            ),
            {"s": source.id},
        )
        with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
            async with db_session.begin_nested():
                await db_session.execute(text("ALTER TABLE articles ADD COLUMN body text"))
        # Readable (the ingest checks the Alembic head), never writable.
        await db_session.execute(text("SELECT version_num FROM alembic_version"))
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text("DELETE FROM alembic_version"))
