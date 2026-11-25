"""Read-only catalog API against the real database (synthetic data, rolled back per test)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog import _decode_cursor, _encode_cursor
from app.core.config import Settings
from app.db.session import create_engine
from app.models import Article, ArticleBody, Source

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def article(source: Source, n: int, published_at: datetime, **overrides: object) -> Article:
    fields: dict[str, object] = {
        "source_id": source.id,
        "external_id": str(n),
        "canonical_url": f"https://en.hespress.com/{n}-placeholder.html",
        "title": f"Placeholder headline number {n}",
        "language": "en",
        "categories": ["Economy"],
        "published_at": published_at,
        "first_seen_via": "rss",
        "content_hash": f"h{n}",
    }
    fields.update(overrides)
    return Article(**fields)


@pytest.fixture
async def client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def test_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = test_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def seeded(db_session: AsyncSession, source: Source) -> list[Article]:
    """25 articles, one per hour, newest first; two share a timestamp (tie on id)."""
    rows = [article(source, 100 + i, NOW - timedelta(hours=i)) for i in range(25)]
    rows.append(article(source, 999, NOW - timedelta(hours=3)))  # same time as #103
    db_session.add_all(rows)
    await db_session.flush()
    return rows


# --- Articles: pagination ---------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_articles_are_newest_first_with_a_bounded_page(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/articles", params={"limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 5
    published = [item["published_at"] for item in body["items"]]
    assert published == sorted(published, reverse=True)
    assert body["next_cursor"]


@pytest.mark.usefixtures("seeded")
async def test_cursor_pagination_visits_every_article_exactly_once(
    client: httpx.AsyncClient,
) -> None:
    seen: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/api/v1/articles", params=params)).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 26
    assert len(set(seen)) == 26


async def test_pagination_is_stable_when_new_articles_arrive(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source, seeded: list[Article]
) -> None:
    first = (await client.get("/api/v1/articles", params={"limit": 10})).json()
    db_session.add(article(source, 5000, NOW + timedelta(hours=1)))  # published meanwhile
    await db_session.flush()

    second = (
        await client.get("/api/v1/articles", params={"limit": 10, "cursor": first["next_cursor"]})
    ).json()

    first_ids = {i["id"] for i in first["items"]}
    second_ids = {i["id"] for i in second["items"]}
    assert not first_ids & second_ids  # no duplicates (OFFSET would shift by one)
    assert len(second_ids) == 10


@pytest.mark.usefixtures("seeded")
async def test_last_page_has_no_cursor(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/articles", params={"limit": 100})).json()

    assert len(body["items"]) == 26
    assert body["next_cursor"] is None


# --- Articles: filters and search --------------------------------------------------------


async def test_filters(client: httpx.AsyncClient, db_session: AsyncSession, source: Source) -> None:
    db_session.add_all(
        [
            article(source, 1, NOW, categories=["Sports"], title="Football league resumes"),
            article(source, 2, NOW - timedelta(days=2), categories=["Economy"]),
            article(source, 3, NOW - timedelta(days=5), language="fr"),
        ]
    )
    await db_session.flush()

    async def ids(**params: str) -> set[str]:
        body = (await client.get("/api/v1/articles", params=params)).json()
        return {item["url"].split("/")[-1].split("-")[0] for item in body["items"]}

    assert await ids(category="Sports") == {"1"}
    assert await ids(language="FR") == {"3"}
    assert await ids(source="hespress-en") == {"1", "2", "3"}
    assert await ids(source="unknown") == set()
    assert await ids(published_after=(NOW - timedelta(days=3)).isoformat()) == {"1", "2"}
    assert await ids(published_before=(NOW - timedelta(days=1)).isoformat()) == {"2", "3"}
    assert await ids(q="leagues") == {"1"}  # stemmed full-text search


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"limit": 0}, 422),
        ({"limit": 101}, 422),
        ({"q": "a"}, 422),
        ({"cursor": "not-a-cursor"}, 400),
        ({"cursor": _encode_cursor(NOW, 1)[:-3] + "!!!"}, 400),
        ({"published_after": "yesterday"}, 422),
    ],
)
async def test_invalid_parameters_are_rejected(
    client: httpx.AsyncClient, params: dict[str, object], status: int
) -> None:
    response = await client.get("/api/v1/articles", params=params)  # type: ignore[arg-type]

    assert response.status_code == status


# --- Article detail and rights ----------------------------------------------------------


async def test_article_detail_links_out_with_attribution(
    client: httpx.AsyncClient, seeded: list[Article]
) -> None:
    target = seeded[0]

    body = (await client.get(f"/api/v1/articles/{target.id}")).json()

    assert body["url"] == target.canonical_url
    assert body["source"] == {
        "slug": "hespress-en",
        "name": "Hespress English",
        "site_url": "https://en.hespress.com",
    }
    assert body["excerpt"] is None  # metadata_only, excerpt 0


@pytest.mark.usefixtures("seeded")
async def test_no_response_ever_carries_a_body(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/articles", params={"limit": 100})).json()

    for item in body["items"]:
        assert not set(item) & {"body", "body_html", "body_text", "content", "content_hash"}


async def test_stored_bodies_are_never_served(
    client: httpx.AsyncClient, db_session: AsyncSession, seeded: list[Article]
) -> None:
    private_text = "PRIVATE-BODY-TEXT-MUST-NOT-LEAK"
    db_session.add(ArticleBody(article_id=seeded[0].id, body_text=private_text, body_hash="h"))
    await db_session.flush()

    responses = [
        await client.get("/api/v1/articles", params={"limit": 100}),
        await client.get(f"/api/v1/articles/{seeded[0].id}"),
        await client.get("/api/v1/articles", params={"q": "private body text"}),
    ]

    for response in responses:
        assert private_text not in response.text


@pytest.mark.parametrize(("article_id", "status"), [(999_999_999, 404), (0, 422)])
async def test_missing_or_invalid_article(
    client: httpx.AsyncClient, article_id: int, status: int
) -> None:
    assert (await client.get(f"/api/v1/articles/{article_id}")).status_code == status


# --- Sources and feeds ------------------------------------------------------------------


async def test_sources(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/sources")).json()

    hespress = next(s for s in body if s["slug"] == "hespress-en")
    assert hespress["rights_policy"] == "restricted"
    assert "license_ref" not in hespress  # internal, never exposed
    assert "rights_basis" not in hespress


async def test_feeds(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/feeds")).json()

    kinds = {f["kind"] for f in body if f["source"] == "hespress-en"}
    assert kinds == {"rss", "news_sitemap", "archive_index"}
    assert all("etag" not in f for f in body)


# --- HTTP caching -----------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("path", ["/api/v1/articles", "/api/v1/sources", "/api/v1/feeds"])
async def test_etag_revalidation_returns_304(client: httpx.AsyncClient, path: str) -> None:
    first = await client.get(path)
    etag = first.headers["ETag"]
    assert first.headers["Cache-Control"] == "no-cache"

    again = await client.get(path, headers={"If-None-Match": etag})
    strong = await client.get(path, headers={"If-None-Match": etag.removeprefix("W/")})
    other = await client.get(path, headers={"If-None-Match": '"different"'})

    assert (again.status_code, again.content) == (304, b"")
    assert strong.status_code == 304  # weak comparison
    assert other.status_code == 200


async def test_etag_changes_when_data_changes(
    client: httpx.AsyncClient, db_session: AsyncSession, source: Source
) -> None:
    before = (await client.get("/api/v1/articles")).headers["ETag"]
    db_session.add(article(source, 1, NOW))
    await db_session.flush()

    assert (await client.get("/api/v1/articles")).headers["ETag"] != before


# --- Read-only enforcement --------------------------------------------------------------


async def test_the_api_engine_cannot_write(settings: Settings) -> None:
    engine = create_engine(settings, read_only=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW default_transaction_read_only")) == "on"
            await connection.scalar(select(text("count(*)")).select_from(Source))  # reads work
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await connection.execute(text("UPDATE sources SET name = name"))
    finally:
        await engine.dispose()


async def test_the_ingestion_engine_can_write(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW default_transaction_read_only")) == "off"
    finally:
        await engine.dispose()


def test_cursor_roundtrip_and_naive_timestamps_are_rejected() -> None:
    assert _decode_cursor(_encode_cursor(NOW, 42)) == (NOW, 42)

    naive = _encode_cursor(datetime(2026, 1, 1), 1)  # deliberately naive
    with pytest.raises(Exception, match="Invalid cursor"):
        _decode_cursor(naive)


async def test_article_page_size_is_bounded(client: httpx.AsyncClient) -> None:
    too_big = await client.get("/api/v1/articles", params={"limit": 1_000_000})
    at_cap = await client.get("/api/v1/articles", params={"limit": 100})

    assert too_big.status_code == 422
    assert at_cap.status_code == 200
