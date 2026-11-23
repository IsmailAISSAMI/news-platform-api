"""Read-only catalog endpoints: sources, feeds and articles.

Articles are paginated by keyset on (published_at DESC, id DESC), backed by the
`ix_articles_published_at_id` index: stable under inserts and fast at any depth.
OFFSET is never used.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import Select, func, select, tuple_

from app.api.caching import cached_json
from app.api.deps import DbSession
from app.models import Article, Feed, Source
from app.schemas.catalog import (
    ArticleOut,
    FeedList,
    FeedOut,
    Page,
    SourceList,
    SourceOut,
    SourceRef,
)

router = APIRouter()

MAX_LIMIT = 100


@router.get("/sources", response_model=list[SourceOut], tags=["Sources"])
async def list_sources(request: Request, db: DbSession) -> Response:
    sources = (await db.execute(select(Source).order_by(Source.slug))).scalars()
    return cached_json(request, SourceList([SourceOut.model_validate(s) for s in sources]))


@router.get("/feeds", response_model=list[FeedOut], tags=["Feeds"])
async def list_feeds(request: Request, db: DbSession) -> Response:
    rows = await db.execute(
        select(Feed, Source.slug).join(Source).order_by(Source.slug, Feed.kind, Feed.id)
    )
    feeds = [
        FeedOut(
            id=feed.id,
            source=slug,
            url=feed.url,
            kind=feed.kind,
            state=feed.state,
            consecutive_failures=feed.consecutive_failures,
            last_success_at=feed.last_success_at,
            checkpoint_published_at=feed.checkpoint_published_at,
            uncovered_since=feed.uncovered_since,
            uncovered_until=feed.uncovered_until,
        )
        for feed, slug in rows.all()
    ]
    return cached_json(request, FeedList(feeds))


@router.get("/articles", response_model=Page[ArticleOut], tags=["Articles"])
async def list_articles(  # noqa: PLR0913  # each query parameter is a documented filter
    request: Request,
    db: DbSession,
    *,
    source: Annotated[str | None, Query(description="Source slug, e.g. hespress-en.")] = None,
    category: Annotated[str | None, Query(description="Exact category name.")] = None,
    language: Annotated[str | None, Query(description="Language code, e.g. en.")] = None,
    published_after: Annotated[datetime | None, Query()] = None,
    published_before: Annotated[datetime | None, Query()] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=2,
            max_length=200,
            description="Full-text search over titles (and excerpts when stored). "
            "Best effort, English stemming. Results stay in newest-first order.",
        ),
    ] = None,
    cursor: Annotated[str | None, Query(description="`next_cursor` from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
) -> Response:
    stmt = _article_query()
    if source:
        stmt = stmt.where(Source.slug == source)
    if category:
        stmt = stmt.where(Article.categories.contains([category]))
    if language:
        stmt = stmt.where(Article.language == language.lower())
    if published_after:
        stmt = stmt.where(Article.published_at >= published_after)
    if published_before:
        stmt = stmt.where(Article.published_at < published_before)
    if q:
        stmt = stmt.where(Article.search_tsv.bool_op("@@")(func.websearch_to_tsquery("english", q)))
    if cursor:
        after_published, after_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Article.published_at, Article.id) < tuple_(after_published, after_id)
        )

    rows = (await db.execute(stmt.limit(limit + 1))).all()
    page = [(article, src) for article, src in rows[:limit]]
    items = [_article_out(article, src) for article, src in page]
    last = page[-1][0] if page else None
    has_more = len(rows) > limit
    next_cursor = _encode_cursor(last.published_at, last.id) if has_more and last else None
    return cached_json(request, Page[ArticleOut](items=items, next_cursor=next_cursor))


@router.get(
    "/articles/{article_id}",
    response_model=ArticleOut,
    tags=["Articles"],
    responses={404: {"description": "No such article"}},
)
async def get_article(
    request: Request, db: DbSession, article_id: Annotated[int, Path(ge=1)]
) -> Response:
    row = (await db.execute(_article_query().where(Article.id == article_id))).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    article, source = row
    return cached_json(request, _article_out(article, source))


# --- helpers ---------------------------------------------------------------------------


def _article_query() -> Select[Article, Source]:
    return (
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .order_by(Article.published_at.desc(), Article.id.desc())
    )


def _article_out(article: Article, source: Source) -> ArticleOut:
    return ArticleOut(
        id=article.id,
        title=article.title,
        url=article.canonical_url,
        excerpt=article.excerpt,
        author=article.author,
        categories=list(article.categories),
        language=article.language,
        published_at=article.published_at,
        modified_at=article.modified_at,
        source=SourceRef.model_validate(source),
    )


def _encode_cursor(published_at: datetime, article_id: int) -> str:
    raw = json.dumps({"p": published_at.isoformat(), "i": article_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        published_at = datetime.fromisoformat(data["p"])
        article_id = int(data["i"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor"
        ) from exc
    if published_at.tzinfo is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor")
    return published_at, article_id
